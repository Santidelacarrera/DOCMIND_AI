"""Conversations with the documents (ADR-005 delivery 3): private to their user, encrypted per organization.

* Only a signed-in user (not an API key) has conversations; each is visible to its user only. Another
  member, an administrator, a user of another organization and an unknown id all get the same 404.
* A conversation may be bound to one case: every question is answered from that case, after the same
  project check as ``/projects/{id}/ask`` (re-checked on every question, so revoked access stops it).
* Turns are encrypted with the organization's key (``app.conversations``) and immutable. Earlier turns
  reach the answer only as non-citable memory in ``ASK_MODE=generative``; citations always point to
  documents. Deleting a conversation deletes its turns (erasure); retention purges old ones.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import api_support, conversations
from app.api_support import audit
from app.core import settings
from app.db import get_db
from app.models import Conversation, ConversationMessage, Document, User, utcnow
from app.project_access import accessible_project_ids, require_project
from app.rate_limit import enforce
from app.routers.ask import AskInput, _answer_body, answer_in_scope
from app.security import ApiPrincipal, current_user, membership

router = APIRouter()


class ConversationInput(BaseModel):
    project_id: uuid.UUID | None = None


# Order of checks in every route: user session, organization membership, ownership of the conversation,
# then whether conversations are enabled. An outsider is refused (403/404) before learning anything,
# including whether the feature is configured.
def _user(principal: User | ApiPrincipal) -> User:
    if isinstance(principal, ApiPrincipal):
        raise HTTPException(403, "USER_SESSION_REQUIRED")
    return principal


def _require_enabled() -> None:
    if not conversations.enabled():
        raise HTTPException(503, "CONVERSATIONS_DISABLED")


def _owned(db: Session, org_id: uuid.UUID, user: User, conversation_id: str) -> Conversation:
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(404, "CONVERSATION_NOT_FOUND") from None
    conv = db.get(Conversation, cid)
    if conv is None or conv.organization_id != org_id or conv.user_id != user.id:
        raise HTTPException(404, "CONVERSATION_NOT_FOUND")
    return conv


WITHDRAWN = "[Contenido retirado: la fuente fue eliminada o ya no tienes acceso a ella.]"


def _readable_documents(db: Session, conv: Conversation, user: User, ids: set[str]) -> set[str]:
    """Of ``ids``, the documents that still exist and that ``user`` may read now (same rules as /ask)."""
    uuids = set()
    for value in ids:
        try:
            uuids.add(uuid.UUID(value))
        except ValueError:
            continue
    if not uuids:
        return set()
    visible = accessible_project_ids(db, conv.organization_id, user)
    rows = db.scalars(
        select(Document.id).where(
            Document.id.in_(uuids), Document.organization_id == conv.organization_id,
            Document.deleted_at.is_(None), Document.project_id.in_(visible),
        )
    ).all()
    return {str(r) for r in rows}


def _turns(db: Session, conv: Conversation, user: User) -> list[dict[str, Any]]:
    """Decrypted turns, in order. An answer that quoted a document the user can no longer read (deleted,
    expired, or access revoked) is shown as withdrawn: its text and citations are removed on every read,
    and it is not used as memory either. The ciphertext itself goes with the conversation (deletion or
    CONVERSATION_RETENTION_DAYS)."""
    rows = db.scalars(
        select(ConversationMessage).where(ConversationMessage.conversation_id == conv.id).order_by(ConversationMessage.seq)
    ).all()
    out = []
    for row in rows:
        try:
            content = conversations.open_turn(conv.organization_id, conv.id, row.seq, row.role, row.payload)
        except conversations.ConversationIntegrityError:
            raise HTTPException(500, "CONVERSATION_INTEGRITY") from None
        out.append({"seq": row.seq, "role": row.role, "created_at": row.created_at.isoformat(), **content})
    cited = {str(c.get("document_id")) for t in out for c in t.get("citations") or []}
    if cited:
        readable = _readable_documents(db, conv, user, cited)
        for turn in out:
            sources = {str(c.get("document_id")) for c in turn.get("citations") or []}
            if sources - readable:
                turn.update(text=WITHDRAWN, citations=[], conflicts=[], withdrawn=True)
    return out


def _summary(conv: Conversation, messages: int) -> dict[str, Any]:
    return {
        "id": str(conv.id), "project_id": str(conv.project_id) if conv.project_id else None,
        "created_at": conv.created_at.isoformat(), "updated_at": conv.updated_at.isoformat(), "messages": messages,
    }


@router.post("/api/v1/conversations", status_code=201)
def create_conversation(
    organization_id: str, payload: ConversationInput, principal: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user = _user(principal)
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db)
    if payload.project_id is not None:
        require_project(db, org_id, user, payload.project_id)
    _require_enabled()
    conv = Conversation(organization_id=org_id, user_id=user.id, project_id=payload.project_id)
    db.add(conv)
    db.flush()
    audit(db, "conversation.create", "conversation", str(conv.id), user, org_id)
    db.commit()
    return _summary(conv, 0)


@router.get("/api/v1/conversations")
def list_conversations(
    organization_id: str, principal: User | ApiPrincipal = Depends(current_user), db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    user = _user(principal)
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db)
    counts = (
        select(ConversationMessage.conversation_id, func.count().label("n"))
        .group_by(ConversationMessage.conversation_id).subquery()
    )
    rows = db.execute(
        select(Conversation, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.conversation_id == Conversation.id)
        .where(Conversation.organization_id == org_id, Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc()).limit(200)
    ).all()
    return [_summary(conv, int(n)) for conv, n in rows]


@router.get("/api/v1/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str, organization_id: str, principal: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user = _user(principal)
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db)
    conv = _owned(db, org_id, user, conversation_id)
    _require_enabled()
    turns = _turns(db, conv, user)
    return {**_summary(conv, len(turns)), "turns": turns}


@router.post("/api/v1/conversations/{conversation_id}/ask")
def ask_in_conversation(
    conversation_id: str, organization_id: str, payload: AskInput, request: Request,
    principal: User | ApiPrincipal = Depends(current_user), db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce(request, "conversation_ask", settings().rate_limit_api)
    user = _user(principal)
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="documents:read")
    conv = _owned(db, org_id, user, conversation_id)
    _require_enabled()
    if conv.project_id is not None:
        require_project(db, org_id, user, conv.project_id)  # access may have been revoked since
    earlier = _turns(db, conv, user)
    keep = settings().conversation_memory_turns * 2
    memory = "\n".join(
        f"{t['role']}: {t.get('text', '')}" for t in (earlier[-keep:] if keep else [])
        if not t.get("abstained") and not t.get("withdrawn")
    ) or None
    result = answer_in_scope(db, org_id, user, payload.question, payload.top_k, conv.project_id, memory)
    body = _answer_body(result)
    seq = (earlier[-1]["seq"] if earlier else 0) + 1
    db.add_all([
        ConversationMessage(
            organization_id=org_id, conversation_id=conv.id, seq=seq, role="user",
            payload=conversations.seal(org_id, conv.id, seq, "user", {"text": payload.question}),
        ),
        ConversationMessage(
            organization_id=org_id, conversation_id=conv.id, seq=seq + 1, role="assistant",
            payload=conversations.seal(org_id, conv.id, seq + 1, "assistant", {
                "text": body["answer"], "abstained": body["abstained"], "citations": body["citations"],
                "conflicts": body["conflicts"], "mode": body["mode"],
            }),
        ),
    ])
    conv.updated_at = utcnow()
    audit(db, "conversation.ask", "conversation", str(conv.id), user, org_id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # another request appended first: the client retries on the new version
        raise HTTPException(409, "CONVERSATION_CONFLICT") from None
    return {**body, "seq": seq + 1}


@router.delete("/api/v1/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: str, organization_id: str, principal: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> None:
    user = _user(principal)
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db)
    conv = _owned(db, org_id, user, conversation_id)
    db.execute(delete(ConversationMessage).where(ConversationMessage.conversation_id == conv.id))
    db.delete(conv)
    audit(db, "conversation.delete", "conversation", str(conv.id), user, org_id)
    db.commit()
