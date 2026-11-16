"""Document intelligence endpoints: templates, entities, summaries and multi-document comparison."""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import api_support, intelligence
from app.api_support import audit
from app.core import settings
from app.db import get_db
from app.models import Document, DocumentPage, ExtractionField, ExtractionRun, RunStatus, User
from app.project_access import require_document, require_project
from app.rate_limit import enforce
from app.security import ApiPrincipal, current_user, membership

router = APIRouter()


def _document_pages(db: Session, document_id: uuid.UUID) -> list[str]:
    rows = db.scalars(
        select(DocumentPage.text)
        .where(DocumentPage.document_id == document_id)
        .order_by(DocumentPage.page_number.asc())
    ).all()
    return [t or "" for t in rows]


def _latest_fields(db: Session, org_id: uuid.UUID, document_id: uuid.UUID) -> list[dict[str, Any]]:
    run = db.scalar(
        select(ExtractionRun)
        .where(
            ExtractionRun.document_id == document_id,
            ExtractionRun.organization_id == org_id,
            ExtractionRun.status == RunStatus.completed,
        )
        .order_by(ExtractionRun.created_at.desc())
    )
    if run is None:
        return []
    fields = db.scalars(select(ExtractionField).where(ExtractionField.extraction_run_id == run.id)).all()
    return [
        {
            "name": f.name,
            "value": f.value,
            "page": f.page_number,
            "snippet": f.source_text,
        }
        for f in fields
    ]


@router.get("/api/v1/schemas/templates")
def schema_templates(
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Built-in configurable extraction schema templates (invoices, DTE Chile, contracts, policies)."""
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="schemas:read")
    return intelligence.list_schema_templates()


@router.get("/api/v1/documents/{document_id}/entities")
def document_entities(
    document_id: uuid.UUID,
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Deterministic, source-cited entities extracted from the document's processed pages."""
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="documents:read")
    doc = require_document(db, org_id, user, document_id)
    pages = _document_pages(db, doc.id)
    return {
        "document_id": str(doc.id),
        "filename": doc.filename,
        "category": doc.category,
        "tags": list(doc.tags or []),
        "entities": intelligence.extract_entities(pages),
    }


class SummarizeInput(BaseModel):
    max_sentences: int = Field(default=5, ge=1, le=12)


@router.post("/api/v1/documents/{document_id}/summarize")
def summarize_document(
    document_id: uuid.UUID,
    organization_id: str,
    request: Request,
    payload: SummarizeInput = SummarizeInput(),
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce(request, "summarize", settings().rate_limit_api)
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="documents:read")
    doc = require_document(db, org_id, user, document_id)
    pages = _document_pages(db, doc.id)
    result = intelligence.summarize_documents(
        [(str(doc.id), doc.filename, pages)], max_sentences=payload.max_sentences
    )
    audit(db, "document.summarize", "document", str(doc.id), user, org_id)
    db.commit()
    return {"document_id": str(doc.id), "filename": doc.filename, **result}


@router.post("/api/v1/projects/{project_id}/summarize")
def summarize_project(
    project_id: uuid.UUID,
    organization_id: str,
    request: Request,
    payload: SummarizeInput = SummarizeInput(),
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce(request, "summarize", settings().rate_limit_api)
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="documents:read")
    require_project(db, org_id, user, project_id)
    docs = db.scalars(
        select(Document)
        .where(
            Document.organization_id == org_id,
            Document.project_id == project_id,
            Document.deleted_at.is_(None),
        )
        .order_by(Document.created_at.desc())
        .limit(25)
    ).all()
    bundle = [(str(d.id), d.filename, _document_pages(db, d.id)) for d in docs]
    result = intelligence.summarize_documents(bundle, max_sentences=payload.max_sentences)
    audit(db, "project.summarize", "project", str(project_id), user, org_id)
    db.commit()
    return {"project_id": str(project_id), "documents_included": len(docs), **result}


class CompareInput(BaseModel):
    document_ids: list[uuid.UUID] = Field(min_length=2, max_length=5)
    topic: str = Field(default="", max_length=200)


@router.post("/api/v1/documents/compare", response_model=None)
def compare_documents_endpoint(
    organization_id: str,
    payload: CompareInput,
    request: Request,
    format: Literal["json", "csv", "xlsx"] = Query(default="json"),
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any] | Response:
    """Compare 2..5 documents for field divergences, textual contradictions and entity deltas."""
    enforce(request, "compare", settings().rate_limit_api)
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="documents:read")
    unique_ids = list(dict.fromkeys(payload.document_ids))
    if len(unique_ids) < 2:
        raise HTTPException(422, "COMPARE_NEEDS_TWO_DOCUMENTS")

    docs_data: list[dict[str, Any]] = []
    for doc_id in unique_ids:
        doc = require_document(db, org_id, user, doc_id)
        docs_data.append(
            {
                "document_id": str(doc.id),
                "filename": doc.filename,
                "pages": _document_pages(db, doc.id),
                "fields": _latest_fields(db, org_id, doc.id),
            }
        )

    report = intelligence.compare_documents(docs_data, topic=payload.topic)
    audit(db, "document.compare", "organization", str(org_id), user, org_id)
    db.commit()
    if format == "json":
        return report
    content, media_type, filename = intelligence.export_comparison(report, format)
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
