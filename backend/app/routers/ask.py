"""Questions answered from documents, always with citations (ADR-002, ADR-003)."""

import logging
import uuid
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import api_support, request_context, vector_index
from app.api_support import audit
from app.core import settings
from app.db import get_db
from app.models import (
    AuditLog,
    Document,
    User,
)
from app.project_access import accessible_project_ids, as_strings, require_project
from app.rate_limit import enforce
from app.security import (
    ApiPrincipal,
    current_user,
    membership,
)

logger = logging.getLogger("docmind")
_config = settings()
_production = _config.is_production

router = APIRouter()


class AskInput(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    top_k: int = Field(default=4, ge=1, le=8)


def _answer(pipeline: Any, **kwargs: Any) -> Any:
    """``answering.answer`` for the ask endpoints: a collection indexed with another embedding
    model is a configuration error the operator must fix (503), never a 500 or a wrong answer.
    In ``ASK_MODE=generative`` an unavailable model is a 503 ``LLM_UNAVAILABLE``, not a guess."""
    from app.ingestion.answering import answer
    from app.ingestion.vectorstore import EmbeddingModelMismatch
    from app.rag.generation import GenerationError, build_generator

    cfg = settings()
    if cfg.ask_mode == "generative":
        kwargs.update(
            answer_mode="generative", generator=build_generator(cfg.llm_provider),
            generation_threshold=cfg.ask_generation_threshold, context_chars=cfg.ask_context_chars,
        )
    try:
        return answer(pipeline, **kwargs)
    except EmbeddingModelMismatch:
        raise HTTPException(503, "EMBEDDING_MODEL_MISMATCH") from None
    except GenerationError:
        raise HTTPException(503, "LLM_UNAVAILABLE") from None
    except Exception as exc:
        if _vector_store_outage(exc):
            logger.warning("vector store unavailable for a question", extra={"error": type(exc).__name__})
            raise HTTPException(503, "VECTOR_INDEX_UNAVAILABLE") from None
        raise


def _vector_store_outage(exc: Exception) -> bool:
    """Qdrant unreachable or failing (network, timeout, 5xx): a 503 the client may retry, not a 500."""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    try:
        from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
    except ImportError:  # pragma: no cover - qdrant extra not installed
        return False
    return isinstance(exc, (ResponseHandlingException, UnexpectedResponse))


def _answer_body(result: Any) -> dict[str, object]:
    """Response of both ask endpoints. Each citation names document, page and the supporting
    fragment (``chunk_index`` and the quoted ``excerpt``), so a reader can check the source."""
    return {
        "answer": result.text,
        "abstained": result.abstained,
        "mode": result.mode,
        "dropped_claims": result.dropped_claims,
        "citations": [
            {
                "document_id": c.document_id,
                "document": c.document,
                "page": c.page,
                "page_basis": c.page_basis,
                "score": c.score,
                "label": c.label(),
                "chunk_index": c.chunk_index,
                "excerpt": c.excerpt,
            }
            for c in result.citations
        ],
        # Sources that may disagree with a cited one (dates, figures, negation). Heuristic, not
        # semantic: shown so a reader checks both, never used to pick a side.
        "conflicts": [c.to_dict() for c in result.conflicts],
    }


@router.post("/api/v1/ask")
def ask(
    organization_id: str,
    payload: AskInput,
    request: Request,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Answer a question from this organization's indexed documents, always with its sources.

    Extractive and local (no language model, no network call). The tenant is the authenticated
    membership, never a request field; deleted documents are never cited.
    """

    enforce(request, "ask", settings().rate_limit_api)
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="documents:read")
    if not vector_index.enabled():
        raise HTTPException(503, "VECTOR_INDEX_DISABLED")

    def is_live(document_id: str) -> bool:
        try:
            doc_uuid = uuid.UUID(document_id)
        except ValueError:
            return False
        return (
            db.scalar(
                select(Document.id).where(
                    Document.id == doc_uuid,
                    Document.organization_id == org_id,
                    Document.deleted_at.is_(None),
                )
            )
            is not None
        )

    # Phase 3B: only the projects the caller may read. Vectors without a project (legacy, not yet
    # backfilled) match no list, so they are excluded instead of being answered without an owner.
    result = _answer(
        vector_index.get_pipeline(),
        tenant_id=str(org_id),
        question=payload.question,
        top_k=payload.top_k,
        is_live=is_live,
        project_ids=as_strings(accessible_project_ids(db, org_id, user)),
        min_coverage=settings().ask_min_evidence_coverage,
        min_similarity=settings().ask_min_semantic_similarity,
    )
    audit(db, "document.ask", "organization", str(org_id), user, org_id)
    db.commit()
    return _answer_body(result)


@router.post("/api/v1/projects/{project_id}/ask")
def project_ask(
    project_id: str,
    organization_id: str,
    payload: AskInput,
    request: Request,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Answer a question from one case (project) of the organization. Same response as ``/api/v1/ask``.

    Order of checks: organization membership (or a key with ``documents:read`` for this organization),
    then the project must belong to that organization. A project of another organization and a
    project that does not exist get the same 404, so the response reveals nothing about identifiers.
    Retrieval is filtered by project inside the vector query. Question and document text are not logged.
    """

    enforce(request, "project_ask", settings().rate_limit_api)
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="documents:read")
    try:
        project_uuid = uuid.UUID(project_id)
    except ValueError:
        raise HTTPException(404, "PROJECT_NOT_FOUND") from None
    # A legacy key names no project, so it cannot use a case endpoint (phase 3B).
    if isinstance(user, ApiPrincipal) and user.key.project_ids is None:
        raise HTTPException(404, "PROJECT_NOT_FOUND")
    # Phase 3B: the caller must be allowed to read this project (grant, org role, or key list).
    # Refusals are 404 like a missing project, and come before the feature check.
    require_project(db, org_id, user, project_uuid)
    if not vector_index.enabled():
        raise HTTPException(503, "VECTOR_INDEX_DISABLED")

    def is_live(document_id: str) -> bool:
        try:
            doc_uuid = uuid.UUID(document_id)
        except ValueError:
            return False
        return (
            db.scalar(
                select(Document.id).where(
                    Document.id == doc_uuid,
                    Document.organization_id == org_id,
                    Document.project_id == project_uuid,
                    Document.deleted_at.is_(None),
                )
            )
            is not None
        )

    request_id = request_context.get()
    result = _answer(
        vector_index.get_pipeline(),
        tenant_id=str(org_id),
        question=payload.question,
        top_k=payload.top_k,
        is_live=is_live,
        project_id=str(project_uuid),
        min_coverage=settings().ask_min_evidence_coverage,
        min_similarity=settings().ask_min_semantic_similarity,
    )
    # Same transaction as the response: the audit record exists if and only if the answer was given.
    db.add(
        AuditLog(
            organization_id=org_id,
            actor_id=user.id if isinstance(user, User) else None,
            action="document.project_ask",
            target_type="project",
            target_id=str(project_uuid),
            metadata_={"request_id": request_id, "citations": len(result.citations), "abstained": result.abstained},
        )
    )
    db.commit()
    logger.info(
        "project query answered",
        extra={
            "request_id": request_id,
            "organization_id": str(org_id),
            "project_id": str(project_uuid),
            "citations": len(result.citations),
            "abstained": result.abstained,
        },
    )
    return _answer_body(result)


def answer_in_scope(
    db: Session, org_id: uuid.UUID, user: User | ApiPrincipal, question: str, top_k: int,
    project_uuid: uuid.UUID | None, memory: str | None = None,
) -> Any:
    """The answer ``/ask`` (no project) or ``/projects/{id}/ask`` (project) would give, for callers that
    already checked membership and project access (conversations). Same scope and liveness rules."""
    if not vector_index.enabled():
        raise HTTPException(503, "VECTOR_INDEX_DISABLED")

    def is_live(document_id: str) -> bool:
        try:
            doc_uuid = uuid.UUID(document_id)
        except ValueError:
            return False
        query = select(Document.id).where(
            Document.id == doc_uuid, Document.organization_id == org_id, Document.deleted_at.is_(None),
        )
        if project_uuid is not None:
            query = query.where(Document.project_id == project_uuid)
        return db.scalar(query) is not None

    scope: dict[str, Any] = (
        {"project_id": str(project_uuid)} if project_uuid is not None
        else {"project_ids": as_strings(accessible_project_ids(db, org_id, user))}
    )
    return _answer(
        vector_index.get_pipeline(), tenant_id=str(org_id), question=question, top_k=top_k, is_live=is_live,
        min_coverage=settings().ask_min_evidence_coverage, min_similarity=settings().ask_min_semantic_similarity,
        memory=memory, **scope,
    )
