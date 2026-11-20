"""Explicit user feedback (Dynamic Few-Shot Prompting) and personal semantic memory management."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import api_support, conversations, vector_index
from app.api_support import audit
from app.core import settings
from app.db import get_db
from app.models import AnswerFeedback, Role, User, UserSemanticMemory
from app.project_access import require_project
from app.rag.memory import open_semantic_memories
from app.rag.safety import contains_secret_leak, has_active_content
from app.rate_limit import enforce
from app.security import ApiPrincipal, current_user, membership

router = APIRouter()


class FeedbackInput(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    answer: str = Field(min_length=1, max_length=4000)
    rating: Literal["positive", "negative"]
    corrected_answer: str | None = Field(default=None, max_length=4000)
    project_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    turn_seq: int | None = Field(default=None, ge=1)
    cited_document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=25)


@router.post("/api/v1/feedback", status_code=201)
def submit_feedback(
    organization_id: str,
    payload: FeedbackInput,
    request: Request,
    principal: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Record explicit positive/negative feedback on a Q&A answer to drive Dynamic Few-Shot Prompting."""
    enforce(request, "feedback", settings().rate_limit_api)
    org_id = api_support.organization_id(organization_id)
    membership(org_id, principal, db, scope="documents:read")
    if payload.project_id is not None:
        require_project(db, org_id, principal, payload.project_id)

    if has_active_content(payload.answer) or contains_secret_leak(payload.answer):
        raise HTTPException(400, "UNSAFE_FEEDBACK_CONTENT")
    if payload.corrected_answer and (
        has_active_content(payload.corrected_answer) or contains_secret_leak(payload.corrected_answer)
    ):
        raise HTTPException(400, "UNSAFE_FEEDBACK_CONTENT")

    masked_q = payload.question
    if vector_index.enabled():
        try:
            masked_q = vector_index.get_pipeline().mask_query(tenant_id=str(org_id), question=payload.question)
        except Exception:
            masked_q = payload.question

    row = AnswerFeedback(
        organization_id=org_id,
        user_id=principal.id if isinstance(principal, User) else None,
        project_id=payload.project_id,
        conversation_id=payload.conversation_id,
        turn_seq=payload.turn_seq,
        question=masked_q[:500],
        answer=payload.answer[:4000],
        corrected_answer=payload.corrected_answer[:4000] if payload.corrected_answer else None,
        rating=payload.rating,
        cited_document_ids=[str(d) for d in payload.cited_document_ids],
    )
    db.add(row)
    db.flush()
    audit(db, "answer.feedback", "answer_feedback", str(row.id), principal, org_id)
    db.commit()
    return {
        "id": str(row.id),
        "rating": row.rating,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/api/v1/feedback")
def list_feedback(
    organization_id: str,
    limit: int = 50,
    principal: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """List workspace feedback (admins/owners see all; regular members see their own)."""
    org_id = api_support.organization_id(organization_id)
    member = membership(org_id, principal, db, scope="documents:read")
    stmt = select(AnswerFeedback).where(AnswerFeedback.organization_id == org_id)
    if isinstance(principal, User) and member.role not in (Role.owner, Role.admin):
        stmt = stmt.where(AnswerFeedback.user_id == principal.id)
    rows = db.scalars(stmt.order_by(AnswerFeedback.created_at.desc()).limit(max(1, min(limit, 100)))).all()
    return [
        {
            "id": str(r.id),
            "project_id": str(r.project_id) if r.project_id else None,
            "conversation_id": str(r.conversation_id) if r.conversation_id else None,
            "turn_seq": r.turn_seq,
            "question": r.question,
            "answer": r.answer,
            "corrected_answer": r.corrected_answer,
            "rating": r.rating,
            "cited_document_ids": list(r.cited_document_ids or []),
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/api/v1/memory/me")
def list_my_memories(
    organization_id: str,
    project_id: uuid.UUID | None = None,
    principal: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """List decrypted long-term semantic memories stored for the current user (Ley N° 21.719 transparency)."""
    if isinstance(principal, ApiPrincipal):
        raise HTTPException(403, "USER_SESSION_REQUIRED")
    org_id = api_support.organization_id(organization_id)
    membership(org_id, principal, db)
    if not conversations.enabled():
        return []
    return open_semantic_memories(db, org_id, principal.id, project_id=project_id)


@router.delete("/api/v1/memory/me/{memory_id}", status_code=204)
def delete_my_memory(
    memory_id: str,
    organization_id: str,
    principal: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> None:
    """Erase a single semantic memory entry owned by the authenticated user (Ley N° 21.719 Art. 7)."""
    if isinstance(principal, ApiPrincipal):
        raise HTTPException(403, "USER_SESSION_REQUIRED")
    org_id = api_support.organization_id(organization_id)
    membership(org_id, principal, db)
    try:
        mid = uuid.UUID(memory_id)
    except ValueError:
        raise HTTPException(404, "MEMORY_NOT_FOUND") from None
    row = db.get(UserSemanticMemory, mid)
    if row is None or row.organization_id != org_id or row.user_id != principal.id:
        raise HTTPException(404, "MEMORY_NOT_FOUND")
    db.delete(row)
    audit(db, "memory.deleted", "user_semantic_memory", str(mid), principal, org_id)
    db.commit()
