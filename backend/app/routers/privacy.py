"""Data-subject privacy rights (ARCOP) and technical transparency under Chile's Ley N° 21.719."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import api_support, conversations
from app.api_support import audit
from app.core import settings
from app.db import get_db
from app.models import (
    AuditLog,
    Conversation,
    ConversationMessage,
    Document,
    QueryHistory,
    User,
    utcnow,
)
from app.project_access import accessible_project_ids
from app.rate_limit import enforce
from app.security import ApiPrincipal, current_user, membership

router = APIRouter()


def _require_session_user(principal: User | ApiPrincipal) -> User:
    if isinstance(principal, ApiPrincipal):
        raise HTTPException(403, "USER_SESSION_REQUIRED")
    return principal


@router.get("/api/v1/privacy/policy-summary")
def privacy_policy_summary(
    organization_id: str,
    request: Request,
    principal: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Technical privacy posture of this workspace (transparency duty, Ley N° 21.719)."""
    enforce(request, "privacy_policy", settings().rate_limit_api)
    org_id = api_support.organization_id(organization_id)
    membership(org_id, principal, db)
    cfg = settings()
    return {
        "framework": "Ley N° 21.719 (Chile — vigencia 1 de diciembre de 2026)",
        "organization_id": str(org_id),
        "llm_provider": cfg.llm_provider,
        "embedding_provider": cfg.embedding_provider,
        "storage_provider": cfg.storage_provider,
        "antivirus_provider": cfg.antivirus_provider,
        "rls_enforced": cfg.rls_enforced,
        "pii_detectors": [d.strip() for d in cfg.pii_detectors.split(",") if d.strip()],
        "conversations_encrypted_at_rest": conversations.enabled(),
        "retention": {
            "deleted_documents_days": cfg.retention_deleted_days,
            "live_documents_days": cfg.retention_document_days,
            "conversations_days": cfg.conversation_retention_days,
        },
        "third_party_ai_transfer_warning": cfg.llm_provider == "gemini",
        "third_party_ai_transfer_note": (
            "LLM_PROVIDER=gemini (Google AI Studio) is suitable for synthetic/staging data; "
            "switch to LLM_PROVIDER=vertex with a signed Google Cloud DPA before processing "
            "confidential client personal data under Ley N° 21.719."
            if cfg.llm_provider == "gemini"
            else "Configured provider does not use consumer AI Studio tier."
        ),
        "arcop_endpoints": {
            "access_and_portability": "GET /api/v1/privacy/me/export",
            "erasure_history_and_conversations": "DELETE /api/v1/privacy/me/history",
            "erasure_documents": "DELETE /api/v1/documents/{document_id}",
        },
    }


@router.get("/api/v1/privacy/me/export")
def export_my_data(
    organization_id: str,
    request: Request,
    principal: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Right of Access and Data Portability (Ley N° 21.719, Arts. 5 & 9).

    Returns a structured, machine-readable JSON bundle of the authenticated user's personal and
    operational records inside this organization. Never includes password hashes or signing secrets.
    """
    enforce(request, "privacy_export", settings().rate_limit_api)
    user = _require_session_user(principal)
    org_id = api_support.organization_id(organization_id)
    member = membership(org_id, user, db)

    visible_projects = accessible_project_ids(db, org_id, user)
    docs = (
        db.scalars(
            select(Document)
            .where(
                Document.organization_id == org_id,
                Document.project_id.in_(visible_projects),
                Document.deleted_at.is_(None),
            )
            .order_by(Document.created_at.desc())
            .limit(500)
        ).all()
        if visible_projects
        else []
    )

    queries = db.scalars(
        select(QueryHistory)
        .where(QueryHistory.organization_id == org_id, QueryHistory.user_id == user.id)
        .order_by(QueryHistory.created_at.desc())
        .limit(500)
    ).all()

    convs = db.scalars(
        select(Conversation)
        .where(Conversation.organization_id == org_id, Conversation.user_id == user.id)
        .order_by(Conversation.created_at.desc())
        .limit(200)
    ).all()
    exported_convs: list[dict[str, Any]] = []
    for conv in convs:
        turns_out: list[dict[str, Any]] = []
        if conversations.enabled():
            rows = db.scalars(
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id == conv.id)
                .order_by(ConversationMessage.seq)
            ).all()
            for row in rows:
                try:
                    content = conversations.open_turn(
                        conv.organization_id, conv.id, row.seq, row.role, row.payload
                    )
                    turns_out.append(
                        {
                            "seq": row.seq,
                            "role": row.role,
                            "created_at": row.created_at.isoformat(),
                            "text": content.get("text", ""),
                        }
                    )
                except conversations.ConversationIntegrityError:
                    turns_out.append({"seq": row.seq, "role": row.role, "text": "[unreadable]"})
        exported_convs.append(
            {
                "id": str(conv.id),
                "project_id": str(conv.project_id) if conv.project_id else None,
                "created_at": conv.created_at.isoformat(),
                "turns": turns_out,
            }
        )

    audits = db.scalars(
        select(AuditLog)
        .where(AuditLog.organization_id == org_id, AuditLog.actor_id == user.id)
        .order_by(AuditLog.created_at.desc())
        .limit(500)
    ).all()

    audit(db, "privacy.data_export", "user", str(user.id), user, org_id)
    db.commit()

    return {
        "schema_version": "cl-21719-v1",
        "exported_at": utcnow().isoformat(),
        "user": {
            "id": str(user.id),
            "email": user.email,
            "created_at": user.created_at.isoformat(),
        },
        "organization_id": str(org_id),
        "role": member.role.value,
        "documents_accessible": [
            {
                "id": str(d.id),
                "filename": d.filename,
                "mime_type": d.mime_type,
                "size_bytes": d.size_bytes,
                "created_at": d.created_at.isoformat(),
            }
            for d in docs
        ],
        "query_history": [
            {
                "id": str(q.id),
                "question": q.question,
                "answer_preview": q.answer_preview,
                "abstained": q.abstained,
                "created_at": q.created_at.isoformat(),
            }
            for q in queries
        ],
        "conversations": exported_convs,
        "audit_events": [
            {
                "id": str(a.id),
                "action": a.action,
                "target_type": a.target_type,
                "target_id": a.target_id,
                "created_at": a.created_at.isoformat(),
            }
            for a in audits
        ],
    }


@router.delete("/api/v1/privacy/me/history")
def erase_my_history(
    organization_id: str,
    request: Request,
    principal: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Right of Erasure for query history and private conversations (Ley N° 21.719, Art. 7)."""
    enforce(request, "privacy_erase", settings().rate_limit_api)
    user = _require_session_user(principal)
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db)

    q_deleted = (
        db.execute(
            delete(QueryHistory).where(
                QueryHistory.organization_id == org_id,
                QueryHistory.user_id == user.id,
            )
        ).rowcount
        or 0
    )

    conv_ids = db.scalars(
        select(Conversation.id).where(
            Conversation.organization_id == org_id,
            Conversation.user_id == user.id,
        )
    ).all()
    if conv_ids:
        db.execute(
            delete(ConversationMessage).where(ConversationMessage.conversation_id.in_(conv_ids))
        )
        db.execute(delete(Conversation).where(Conversation.id.in_(conv_ids)))

    audit(
        db,
        "privacy.history_erased",
        "user",
        str(user.id),
        user,
        org_id,
    )
    db.commit()
    return {"queries_deleted": int(q_deleted), "conversations_deleted": len(conv_ids)}
