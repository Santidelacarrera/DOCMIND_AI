"""Upload, processing, download, extraction review and export."""

import csv
import hashlib
import io
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pypdf import PdfReader
from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import api_support, plans, vector_index
from app.antivirus import antivirus
from app.api_support import (
    MAX_FIELD_VALUE_BYTES,
    audit,
    content_disposition,
    live_document,
    neutralize_formula,
    sanitize_filename,
)
from app.core import settings
from app.db import get_db
from app.ingestion import formats
from app.ingestion.docx import DOCX_MIME
from app.models import (
    Document,
    DocumentPage,
    DocumentVersion,
    ExtractionField,
    ExtractionRun,
    ExtractionSchema,
    ExtractionSchemaVersion,
    JobStatus,
    ProcessingJob,
    Role,
    RunStatus,
    UsageRecord,
    User,
    utcnow,
)
from app.processing import ProcessingError, extract_any_pages, extract_docx_pages
from app.project_access import accessible_project_ids, require_document, require_project
from app.rate_limit import enforce
from app.retention import delete_objects, scrub_derived_text
from app.security import (
    ApiPrincipal,
    current_user,
    membership,
    require_user,
)
from app.storage import StorageAccessError, owned_key, storage
from app.worker import process_document, process_vector_deletions

logger = logging.getLogger("docmind")
_config = settings()
_production = _config.is_production

router = APIRouter()


def _resolve_requested_schema_version(
    db: Session, org_id: uuid.UUID, schema_id: uuid.UUID | None
) -> ExtractionSchemaVersion | None:
    """Pin the schema's *latest* version at the moment the user picked it, so a
    later edit to the schema doesn't silently retarget an in-flight job."""
    if schema_id is None:
        return None
    schema = db.scalar(
        select(ExtractionSchema).where(
            ExtractionSchema.id == schema_id, ExtractionSchema.organization_id == org_id
        )
    )
    if not schema:
        raise HTTPException(404, "SCHEMA_NOT_FOUND")
    version = db.scalar(
        select(ExtractionSchemaVersion)
        .where(ExtractionSchemaVersion.schema_id == schema.id)
        .order_by(ExtractionSchemaVersion.version.desc())
    )
    if not version:
        raise HTTPException(422, "SCHEMA_HAS_NO_VERSIONS")
    return version


def _duplicate_response(existing: Document) -> JSONResponse:
    """409 that tells a retrying client which document it already has (idempotent retry)."""
    return JSONResponse(
        status_code=409,
        content={"detail": "DUPLICATE_DOCUMENT", "document_id": str(existing.id)},
    )


def _enqueue(job_id: uuid.UUID) -> None:
    """Hand the job to the broker. The job row is already committed as QUEUED, so a broker
    outage must not fail the request: ``recover_stuck_jobs`` re-enqueues it."""
    try:
        process_document.delay(str(job_id))
    except Exception:
        logger.warning(
            "broker unavailable; job stays queued for recovery", extra={"job_id": str(job_id)}
        )


def _kick_vector_deletions() -> None:
    """Run the outbox soon instead of waiting for the next beat tick; the row is durable anyway."""
    if not vector_index.enabled():
        return
    try:
        process_vector_deletions.delay()
    except Exception:
        logger.warning("could not enqueue vector deletion run; beat will pick it up")


def _validated_upload(file: UploadFile) -> tuple[bytes, str, str, int]:
    """Read and check an uploaded file: size, type by name and magic bytes, page count, antivirus.
    Returns ``(content, mime_type, extension, page_count)``. Shared by new documents and new versions."""
    content = file.file.read(settings().max_upload_bytes + 1)
    if len(content) > settings().max_upload_bytes:
        raise HTTPException(413, "FILE_TOO_LARGE")
    try:
        spec = formats.detect_format(file.filename, content)
    except formats.UnsafeFormat as exc:
        code = 415 if exc.code == "UNSUPPORTED_FILE_TYPE" else 422
        raise HTTPException(code, exc.code) from None

    if spec.format_name == "docx":
        try:
            page_count = len(extract_docx_pages(content))
        except ProcessingError as exc:
            raise HTTPException(422, exc.code) from None
    elif spec.format_name == "pdf":
        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                raise HTTPException(422, "PDF_ENCRYPTED")
            page_count = len(reader.pages)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(422, "DOCUMENT_INVALID") from None
    else:
        try:
            extracted = extract_any_pages(content, spec.mime_type)
            page_count = len(extracted.ocr_images) if extracted.ocr_images else len(extracted.pages)
        except ProcessingError as exc:
            raise HTTPException(422, exc.code) from None

    if page_count < 1:
        raise HTTPException(422, "DOCUMENT_INVALID")
    if page_count > settings().max_pdf_pages:
        raise HTTPException(422, "PDF_PAGE_LIMIT_EXCEEDED")
    try:
        antivirus().scan(content)
    except RuntimeError as exc:
        raise HTTPException(503 if str(exc) == "ANTIVIRUS_UNAVAILABLE" else 422, str(exc)) from None
    return content, spec.mime_type, spec.extension, page_count


def _check_page_quota(db: Session, org_id: uuid.UUID, page_count: int, *, file_bytes: int = 0) -> None:
    """Backward-compatible wrapper around SaaS plan quota enforcement."""
    plans.check_upload_quota(db, org_id, file_bytes=file_bytes, page_count=page_count, is_new_document=True)


@router.post("/api/v1/documents", status_code=status.HTTP_202_ACCEPTED, response_model=None)
def upload_document(
    organization_id: str,
    project_id: uuid.UUID,
    request: Request,
    schema_id: uuid.UUID | None = None,
    file: UploadFile = File(...),
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object] | JSONResponse:
    enforce(request, "upload", settings().rate_limit_upload)
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.member, scope="documents:write")
    requested_schema_version = _resolve_requested_schema_version(db, org_id, schema_id)
    declared = request.headers.get("content-length")
    # Multipart framing adds a small overhead; reject clearly oversized bodies before reading.
    if declared and declared.isdigit() and int(declared) > settings().max_upload_bytes + 65_536:
        raise HTTPException(413, "FILE_TOO_LARGE")
    require_project(db, org_id, user, project_id, write=True)  # phase 3B: a grant with write access, or a key for it
    content, mime, ext, page_count = _validated_upload(file)
    plans.check_upload_quota(db, org_id, file_bytes=len(content), page_count=page_count, is_new_document=True)
    checksum = hashlib.sha256(content).hexdigest()
    existing = db.scalar(
        select(Document).where(
            Document.project_id == project_id,
            Document.checksum == checksum,
            Document.deleted_at.is_(None),
        )
    )
    if existing:
        return _duplicate_response(existing)
    key = owned_key(org_id, f"{org_id}/{uuid.uuid4()}{ext}")
    storage.put(key, content)
    try:
        document = Document(
            organization_id=org_id,
            project_id=project_id,
            filename=sanitize_filename(file.filename),
            storage_key=key,
            mime_type=mime,
            size_bytes=len(content),
            checksum=checksum,
            page_count=page_count,
        )
        db.add(document)
        db.flush()
        db.add(DocumentVersion(document_id=document.id, storage_key=key, checksum=checksum))
        job = ProcessingJob(
            organization_id=org_id,
            document_id=document.id,
            status=JobStatus.queued,
            requested_schema_version_id=requested_schema_version.id
            if requested_schema_version
            else None,
        )
        db.add(job)
        db.add(
            UsageRecord(
                organization_id=org_id,
                metric="pages_reserved",
                quantity=page_count,
                document_id=document.id,
            )
        )
        audit(db, "document.upload", "document", str(document.id), user, org_id)
        db.commit()
    except IntegrityError:
        # A concurrent upload of the same bytes won the race (unique live-checksum index).
        db.rollback()
        storage.delete(key)
        winner = db.scalar(
            select(Document).where(
                Document.project_id == project_id,
                Document.checksum == checksum,
                Document.deleted_at.is_(None),
            )
        )
        if winner is None:
            raise
        return _duplicate_response(winner)
    except Exception:
        db.rollback()
        storage.delete(key)
        raise
    _enqueue(job.id)
    return {"document_id": str(document.id), "job_id": str(job.id), "status": job.status.value}


@router.get("/api/v1/documents")
def list_documents(
    organization_id: str,
    status: JobStatus | None = Query(
        default=None, description="Only documents whose latest job is in this state, e.g. FAILED"
    ),
    project_id: uuid.UUID | None = Query(default=None),
    category: str | None = Query(default=None, max_length=64),
    tag: str | None = Query(default=None, max_length=64),
    q: str | None = Query(default=None, max_length=200),
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="documents:read")
    # Phase 3C: filter in the query, so hidden documents are neither returned nor counted.
    visible = accessible_project_ids(db, org_id, user)
    if not visible:
        return []
    if project_id is not None:
        if project_id not in visible:
            return []
        visible = [project_id]
    # Latest job per document in one query (avoids N+1).
    latest = (
        select(ProcessingJob.document_id, func.max(ProcessingJob.attempt).label("attempt"))
        .where(ProcessingJob.organization_id == org_id)
        .group_by(ProcessingJob.document_id)
        .subquery()
    )
    query = (
        select(Document, ProcessingJob.status, ProcessingJob.failure_code)
        .outerjoin(latest, latest.c.document_id == Document.id)
        .outerjoin(
            ProcessingJob,
            (ProcessingJob.document_id == latest.c.document_id)
            & (ProcessingJob.attempt == latest.c.attempt),
        )
        .where(
            Document.organization_id == org_id,
            Document.deleted_at.is_(None),
            Document.project_id.in_(visible),
        )
    )
    if status is not None:
        query = query.where(ProcessingJob.status == status)
    if category is not None and category.strip():
        query = query.where(Document.category == category.strip().lower())
    if q is not None and q.strip():
        term = f"%{q.strip()}%"
        page_match = exists(
            select(DocumentPage.id).where(
                DocumentPage.document_id == Document.id,
                DocumentPage.text.ilike(term),
            )
        )
        query = query.where(or_(Document.filename.ilike(term), Document.category.ilike(term), page_match))
    rows = db.execute(query.order_by(Document.created_at.desc()).limit(100)).all()
    clean_tag = tag.strip().lower() if tag and tag.strip() else None
    out: list[dict[str, object]] = []
    for d, job_status, failure_code in rows:
        doc_tags = [str(t) for t in (d.tags or [])]
        if clean_tag and clean_tag not in [t.lower() for t in doc_tags]:
            continue
        out.append(
            {
                "id": str(d.id),
                "filename": d.filename,
                "mime_type": d.mime_type,
                "size_bytes": d.size_bytes,
                "pages": d.page_count,
                "category": d.category,
                "tags": doc_tags,
                "tables_detected": d.tables_detected,
                "status": job_status.value if job_status else None,
                "failure_code": failure_code,
                "created_at": d.created_at,
                "project_id": str(d.project_id),
            }
        )
    return out


@router.post("/api/v1/documents/{document_id}/process", status_code=status.HTTP_202_ACCEPTED)
def process_existing_document(
    document_id: uuid.UUID,
    organization_id: str,
    request: Request,
    schema_id: uuid.UUID | None = None,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Queue one logical process at a time, including across concurrent requests."""
    enforce(request, "process", settings().rate_limit_api)
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.member, scope="documents:write")
    require_document(db, org_id, user, document_id, write=True)  # phase 3C: project write access
    requested_schema_version = _resolve_requested_schema_version(db, org_id, schema_id)
    # The document row lock is the database-level serialization point. It is
    # deliberately not an application-only existence check.
    item = db.scalar(
        select(Document)
        .where(
            Document.id == document_id,
            Document.organization_id == org_id,
            Document.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if not item:
        raise HTTPException(404, "DOCUMENT_NOT_FOUND")
    active_states = [
        JobStatus.queued,
        JobStatus.processing,
        JobStatus.ocr_processing,
        JobStatus.extracting,
        JobStatus.validating,
    ]
    active = db.scalar(
        select(ProcessingJob)
        .where(
            ProcessingJob.document_id == item.id,
            ProcessingJob.organization_id == org_id,
            ProcessingJob.status.in_(active_states),
        )
        .order_by(ProcessingJob.created_at.desc())
    )
    if active:
        return {
            "document_id": str(item.id),
            "job_id": str(active.id),
            "status": active.status.value,
            "reused": True,
        }
    prior_attempt = db.scalar(
        select(func.coalesce(func.max(ProcessingJob.attempt), 0)).where(
            ProcessingJob.document_id == item.id, ProcessingJob.organization_id == org_id
        )
    )
    job = ProcessingJob(
        organization_id=org_id,
        document_id=item.id,
        status=JobStatus.queued,
        attempt=int(prior_attempt or 0) + 1,
        requested_schema_version_id=requested_schema_version.id
        if requested_schema_version
        else None,
    )
    db.add(job)
    db.flush()
    audit(db, "document.process", "document", str(item.id), user, org_id)
    db.commit()
    _enqueue(job.id)
    return {
        "document_id": str(item.id),
        "job_id": str(job.id),
        "status": job.status.value,
        "reused": False,
    }


@router.post("/api/v1/documents/{document_id}/versions", status_code=status.HTTP_202_ACCEPTED)
def upload_document_version(
    document_id: uuid.UUID,
    organization_id: str,
    request: Request,
    file: UploadFile = File(...),
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Replace a document's content with a new version and reprocess it (ADR-005, document update).

    The previous file stays in ``document_versions`` (history; removed with the document). Processing
    replaces the pages and extractions; indexing removes the previous version's vectors before adding the
    new ones (``vector_index._index_document``), so no answer cites text that is no longer in the document.
    Refused with 409 while a processing job is active. The same bytes as the current version: no-op.
    """
    enforce(request, "upload", settings().rate_limit_upload)
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.member, scope="documents:write")
    require_document(db, org_id, user, document_id, write=True)
    content, mime, ext, page_count = _validated_upload(file)
    plans.check_upload_quota(db, org_id, file_bytes=len(content), page_count=page_count, is_new_document=False)
    item = db.scalar(
        select(Document)
        .where(Document.id == document_id, Document.organization_id == org_id, Document.deleted_at.is_(None))
        .with_for_update()
    )
    if item is None:
        raise HTTPException(404, "DOCUMENT_NOT_FOUND")
    busy = db.scalar(
        select(ProcessingJob.id).where(
            ProcessingJob.document_id == item.id,
            ProcessingJob.status.in_([JobStatus.queued, JobStatus.processing, JobStatus.ocr_processing,
                                      JobStatus.extracting, JobStatus.validating]),
        )
    )
    if busy is not None:
        raise HTTPException(409, "DOCUMENT_BUSY")
    checksum = hashlib.sha256(content).hexdigest()
    if checksum == item.checksum:
        return {"document_id": str(item.id), "status": "unchanged"}
    key = owned_key(org_id, f"{org_id}/{uuid.uuid4()}{ext}")
    storage.put(key, content)
    try:
        prior_attempt = db.scalar(
            select(func.coalesce(func.max(ProcessingJob.attempt), 0)).where(ProcessingJob.document_id == item.id)
        )
        item.storage_key, item.checksum, item.page_count = key, checksum, page_count
        item.mime_type = mime
        item.size_bytes = len(content)
        item.filename = sanitize_filename(file.filename)
        item.indexed_at = None
        last_version = db.scalar(
            select(func.coalesce(func.max(DocumentVersion.version), 0)).where(DocumentVersion.document_id == item.id)
        )
        db.add(DocumentVersion(document_id=item.id, version=int(last_version or 0) + 1, storage_key=key,
                               checksum=checksum))
        job = ProcessingJob(organization_id=org_id, document_id=item.id, status=JobStatus.queued,
                            attempt=int(prior_attempt or 0) + 1)
        db.add(job)
        db.add(UsageRecord(organization_id=org_id, metric="pages_reserved", quantity=page_count, document_id=item.id))
        audit(db, "document.version", "document", str(item.id), user, org_id)
        db.commit()
    except IntegrityError:
        db.rollback()
        storage.delete(key)
        raise HTTPException(409, "DUPLICATE_DOCUMENT") from None
    except Exception:
        db.rollback()
        storage.delete(key)
        raise
    _enqueue(job.id)
    return {"document_id": str(item.id), "job_id": str(job.id), "status": job.status.value}


@router.get("/api/v1/documents/{document_id}/status")
def document_status(
    document_id: uuid.UUID,
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="documents:read")
    require_document(db, org_id, user, document_id)
    job = db.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.document_id == document_id, ProcessingJob.organization_id == org_id)
        .order_by(ProcessingJob.created_at.desc())
    )
    if not job:
        raise HTTPException(404, "DOCUMENT_NOT_FOUND")
    return {
        "document_id": str(document_id),
        "job_id": str(job.id),
        "status": job.status.value,
        "failure_code": job.failure_code,
        "failure_message": job.failure_message,
        "attempt": job.attempt,
        "retry_count": job.retry_count,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    }


@router.get("/api/v1/documents/{document_id}")
def document(
    document_id: uuid.UUID,
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="documents:read")
    item = require_document(db, org_id, user, document_id)
    return {
        "id": str(item.id),
        "filename": item.filename,
        "mime_type": item.mime_type,
        "size_bytes": item.size_bytes,
        "pages": item.page_count,
        "category": item.category,
        "tags": list(item.tags or []),
        "tables_detected": item.tables_detected,
        "protected": bool(item.protected),
        "project_id": str(item.project_id),
        "created_at": item.created_at,
    }


@router.get("/api/v1/documents/{document_id}/preview")
def document_preview(
    document_id: uuid.UUID,
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Extracted page text and OCR confidence for safe in-app preview across all formats."""
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="documents:read")
    item = require_document(db, org_id, user, document_id)
    pages = db.scalars(
        select(DocumentPage)
        .where(DocumentPage.document_id == item.id)
        .order_by(DocumentPage.page_number.asc())
    ).all()
    return {
        "document_id": str(item.id),
        "filename": item.filename,
        "mime_type": item.mime_type,
        "category": item.category,
        "tags": list(item.tags or []),
        "tables_detected": item.tables_detected,
        "pages": [
            {
                "page_number": p.page_number,
                "text": p.text or "",
                "ocr_used": p.ocr_used,
                "ocr_confidence": p.ocr_confidence,
            }
            for p in pages
        ],
    }


class DocumentMetadataInput(BaseModel):
    category: str | None = Field(default=None, max_length=64)
    tags: list[str] | None = Field(default=None, max_length=20)
    protected: bool | None = None


@router.patch("/api/v1/documents/{document_id}/metadata")
def update_document_metadata(
    document_id: uuid.UUID,
    organization_id: str,
    payload: DocumentMetadataInput,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = api_support.organization_id(organization_id)
    member = membership(org_id, user, db, Role.member, scope="documents:write")
    item = require_document(db, org_id, user, document_id, write=True)
    if payload.protected is not None:
        if isinstance(user, ApiPrincipal) or member is None or member.role not in (Role.owner, Role.admin):
            raise HTTPException(403, "FORBIDDEN")
        item.protected = bool(payload.protected)
    if payload.category is not None:
        item.category = payload.category.strip().lower()[:64] or None
    if payload.tags is not None:
        cleaned = sorted({t.strip().lower()[:40] for t in payload.tags if t.strip()})[:20]
        item.tags = cleaned
    audit(db, "document.metadata", "document", str(item.id), user, org_id)
    db.commit()
    return {
        "id": str(item.id),
        "category": item.category,
        "tags": list(item.tags or []),
        "protected": bool(item.protected),
    }


@router.delete("/api/v1/documents/{document_id}", status_code=204)
def delete_document(
    document_id: uuid.UUID,
    organization_id: str,
    confirm: bool = Query(default=False),
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Delete a document: the stored objects and extracted text go now; the remaining
    derived records (extraction values, jobs) are hard-deleted by the retention job
    after ``retention_deleted_days`` (see docs/DATA_RETENTION.md)."""
    org_id = api_support.organization_id(organization_id)
    delete_scopes: str | tuple[str, ...] = (
        "documents:delete"
        if settings().require_explicit_delete_scope
        else ("documents:delete", "documents:write")
    )
    member = membership(org_id, user, db, Role.member, scope=delete_scopes)
    require_document(db, org_id, user, document_id, write=True)  # phase 3C: project write access
    item = db.scalar(
        select(Document)
        .where(
            Document.id == document_id,
            Document.organization_id == org_id,
            Document.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if not item:
        raise HTTPException(404, "DOCUMENT_NOT_FOUND")
    if item.protected:
        if isinstance(user, ApiPrincipal) or member is None or member.role not in (Role.owner, Role.admin):
            raise HTTPException(409, "DOCUMENT_PROTECTED")
        if not confirm:
            raise HTTPException(409, "DOCUMENT_PROTECTED_CONFIRM_REQUIRED")
    keys = {item.storage_key} | set(
        db.scalars(
            select(DocumentVersion.storage_key).where(DocumentVersion.document_id == item.id)
        )
    )
    item.deleted_at = datetime.now(UTC)
    scrub_derived_text(db, item.id)
    vector_index.queue_deletion(db, org_id, item.id)  # same transaction: cannot be lost
    audit(db, "document.delete", "document", str(item.id), user, org_id)
    db.commit()
    _kick_vector_deletions()
    if delete_objects(org_id, keys):
        # The record is already gone; the orphaned object is reconciled by operations.
        logger.warning("storage cleanup incomplete for deleted document %s", document_id)
    return Response(status_code=204)


@router.post("/api/v1/documents/{document_id}/restore")
def restore_document(
    document_id: uuid.UUID,
    organization_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Restore a soft-deleted document shell within the retention grace window (admin/owner only)."""
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin)
    item = db.scalar(
        select(Document)
        .where(
            Document.id == document_id,
            Document.organization_id == org_id,
            Document.deleted_at.is_not(None),
        )
        .with_for_update()
    )
    if item is None:
        raise HTTPException(404, "DOCUMENT_NOT_FOUND")
    item.deleted_at = None
    audit(db, "document.restore", "document", str(item.id), user, org_id)
    db.commit()
    return {"id": str(item.id), "restored": True, "filename": item.filename}


@router.get("/api/v1/documents/{document_id}/download")
def download(
    document_id: uuid.UUID,
    organization_id: str,
    inline: bool = False,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="documents:read")
    item = require_document(db, org_id, user, document_id)
    frame_ancestors = " ".join(o.strip() for o in settings().cors_origins.split(",") if o.strip())
    try:
        # The key must live under the caller's organization prefix, independently of the
        # database lookup above.
        content = storage.get(owned_key(org_id, item.storage_key))
    except (StorageAccessError, FileNotFoundError):
        raise HTTPException(404, "DOCUMENT_NOT_FOUND") from None
    return Response(
        content,
        media_type=item.mime_type,
        headers={
            "Content-Disposition": content_disposition(item.filename, inline=inline),
            # Only the configured web origins may embed the PDF viewer.
            "Content-Security-Policy": f"default-src 'none'; frame-ancestors {frame_ancestors}",
            "Cache-Control": "private, no-store",
        },
    )


def _latest_run(db: Session, org_id: uuid.UUID, document_id: uuid.UUID) -> ExtractionRun:
    run = db.scalar(
        select(ExtractionRun)
        .where(
            ExtractionRun.document_id == document_id,
            ExtractionRun.organization_id == org_id,
            ExtractionRun.status == RunStatus.completed,
        )
        .order_by(ExtractionRun.created_at.desc())
    )
    if not run:
        raise HTTPException(404, "EXTRACTION_NOT_FOUND")
    return run


def _flagged_fields(run: ExtractionRun) -> set[str]:
    issues = run.validation_issues or []
    flagged = {str(i["field"]) for i in issues if i.get("field")}
    if any(i.get("rule") == "low_ocr_confidence" for i in issues):
        # An unreliable scan taints every value read from it.
        flagged |= set((run.result or {}).get("fields", {}))
    return flagged


def _review_state(run: ExtractionRun, fields: list[ExtractionField]) -> dict[str, object]:
    """``not_required`` | ``pending`` | ``approved`` plus the fields still awaiting a human."""
    flagged = _flagged_fields(run)
    pending = sorted(f.name for f in fields if f.name in flagged and not f.manually_verified)
    if run.reviewed_at is not None:
        state = "approved"
    elif run.requires_review:
        state = "pending"
    else:
        state = "not_required"
    return {
        "status": state,
        "pending_fields": pending if state == "pending" else [],
        "approved_at": run.reviewed_at,
    }


def _field_trust(f: ExtractionField, flagged: set[str]) -> str:
    """What a consumer may assume about a value, strongest first.

    ``human_reviewed``      a person confirmed or corrected it;
    ``automatic_evidenced`` extracted by the model, found in the document, no open issue;
    ``automatic_unverified`` extracted by the model without that support (not located in the
                             text, flagged by validation, or from a run predating provenance).
    """
    if f.manually_verified:
        return "human_reviewed"
    if f.evidence_status in ("exact", "normalized") and f.name not in flagged:
        return "automatic_evidenced"
    return "automatic_unverified"


def _extraction_payload(db: Session, org_id: uuid.UUID, document_id: uuid.UUID) -> dict[str, Any]:
    """The extraction view shared by the read and export endpoints. Callers authorize first."""
    document = live_document(db, org_id, document_id)
    run = _latest_run(db, org_id, document_id)
    fields = list(
        db.scalars(select(ExtractionField).where(ExtractionField.extraction_run_id == run.id)).all()
    )
    flagged = _flagged_fields(run)
    ocr_used = bool(
        db.scalar(
            select(DocumentPage.id)
            .where(DocumentPage.document_id == document.id, DocumentPage.ocr_used.is_(True))
            .limit(1)
        )
    )
    text_source = (
        "docx" if document.mime_type == DOCX_MIME else ("ocr" if ocr_used else "embedded_text")
    )
    return {
        "run_id": str(run.id),
        "document_id": str(document.id),
        "result": run.result,
        "requires_review": run.requires_review,
        "validation_issues": run.validation_issues,
        "review": _review_state(run, fields),
        "method": {"kind": "llm", "text_source": text_source, "confidence_calibrated": False},
        "model": {
            "provider": run.provider,
            "name": run.model,
            "prompt_tokens": run.prompt_tokens,
            "completion_tokens": run.completion_tokens,
            "estimated_cost_usd": run.estimated_cost,
        },
        "fields": [
            {
                "id": str(f.id),
                "name": f.name,
                "value": f.value,
                "original_value": f.original_value,
                # Self-reported by the model and not calibrated: a ranking hint, not a probability.
                "confidence": f.confidence,
                "manually_verified": f.manually_verified,
                "needs_review": f.name in flagged and not f.manually_verified,
                "correction_reason": f.correction_reason,
                "corrected_at": f.corrected_at,
                "trust": _field_trust(f, flagged),
                "evidence": {
                    "status": f.evidence_status or "unavailable",
                    "document_id": str(document.id),
                    "page": f.page_number,
                    "snippet": f.source_text,
                },
            }
            for f in fields
        ],
    }


@router.get("/api/v1/documents/{document_id}/extraction")
def extraction(
    document_id: uuid.UUID,
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="documents:read")
    require_document(db, org_id, user, document_id)
    return _extraction_payload(db, org_id, document_id)


@router.patch("/api/v1/extraction-fields/{field_id}")
def edit_field(
    field_id: uuid.UUID,
    organization_id: str,
    request: Request,
    value: object = Body(None),
    reason: str | None = Query(default=None, max_length=200),
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Correct (or, with an unchanged value, confirm) one extracted field.

    ``original_value`` is never overwritten, so every correction stays comparable with
    what the model produced; ``reason`` records why a person changed it. A JSON ``null``
    body clears the value (e.g. a hallucinated field); an empty body is rejected.
    """
    if request.headers.get("content-length") in (None, "", "0"):
        raise HTTPException(422, "VALUE_REQUIRED")
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.member, scope="documents:write")
    if len(json.dumps(value, default=str)) > MAX_FIELD_VALUE_BYTES:
        raise HTTPException(413, "FIELD_VALUE_TOO_LARGE")
    row = db.execute(
        select(ExtractionField, ExtractionRun)
        .join(ExtractionRun, ExtractionRun.id == ExtractionField.extraction_run_id)
        .join(Document, Document.id == ExtractionRun.document_id)
        .where(
            ExtractionField.id == field_id,
            ExtractionRun.organization_id == org_id,
            Document.deleted_at.is_(None),
        )
    ).first()
    if not row:
        raise HTTPException(404, "FIELD_NOT_FOUND")
    # Phase 3C: the field's document decides, and changing it needs project write access.
    require_document(db, org_id, user, row[1].document_id, write=True, not_found="FIELD_NOT_FOUND")
    field = row[0]
    cleaned_reason = (reason or "").strip() or None
    changed = value != field.original_value
    field.value = value
    field.manually_verified = True
    field.correction_reason = cleaned_reason or ("unspecified" if changed else "confirmed")
    field.corrected_by = user.id if isinstance(user, User) else None
    field.corrected_at = utcnow()
    audit(db, "extraction.edit", "extraction_field", str(field.id), user, org_id)
    db.commit()
    return {
        "id": str(field.id),
        "value": field.value,
        "manually_verified": True,
        "changed": changed,
        "correction_reason": field.correction_reason,
    }


@router.post("/api/v1/documents/{document_id}/review/approve", response_model=None)
def approve_review(
    document_id: uuid.UUID,
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object] | JSONResponse:
    """Human sign-off. Refused while any flagged field is still unverified."""
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.member, scope="documents:write")
    require_document(db, org_id, user, document_id, write=True)
    run = _latest_run(db, org_id, document_id)
    fields = list(
        db.scalars(select(ExtractionField).where(ExtractionField.extraction_run_id == run.id)).all()
    )
    state = _review_state(run, fields)
    if state["pending_fields"]:
        return JSONResponse(
            status_code=409,
            content={"detail": "REVIEW_INCOMPLETE", "pending_fields": state["pending_fields"]},
        )
    run.reviewed_at = utcnow()
    run.reviewed_by = user.id if isinstance(user, User) else None
    audit(db, "extraction.approve", "extraction_run", str(run.id), user, org_id)
    db.commit()
    return {"run_id": str(run.id), "status": "approved", "approved_at": run.reviewed_at}


@router.get("/api/v1/review/queue")
def review_queue(
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    """Documents whose latest extraction needs a human and has not been signed off."""
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="documents:read")
    latest = (
        select(ExtractionRun.document_id, func.max(ExtractionRun.created_at).label("at"))
        .where(ExtractionRun.organization_id == org_id, ExtractionRun.status == RunStatus.completed)
        .group_by(ExtractionRun.document_id)
        .subquery()
    )
    rows = db.execute(
        select(ExtractionRun, Document)
        .join(Document, Document.id == ExtractionRun.document_id)
        .join(
            latest,
            (latest.c.document_id == ExtractionRun.document_id)
            & (latest.c.at == ExtractionRun.created_at),
        )
        .where(
            ExtractionRun.organization_id == org_id,
            ExtractionRun.requires_review.is_(True),
            ExtractionRun.reviewed_at.is_(None),
            Document.deleted_at.is_(None),
            Document.project_id.in_(accessible_project_ids(db, org_id, user)),
        )
        .order_by(ExtractionRun.created_at.asc())
        .limit(100)
    ).all()
    return [
        {
            "document_id": str(doc.id),
            "filename": doc.filename,
            "run_id": str(run.id),
            "issue_count": len(run.validation_issues or []),
            "flagged_fields": sorted(_flagged_fields(run)),
            "created_at": run.created_at,
        }
        for run, doc in rows
    ]


@router.get("/api/v1/documents/{document_id}/export")
def export(
    document_id: uuid.UUID,
    organization_id: str,
    format: str = Query(default="json", pattern="^(json|csv|xlsx)$"),
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="exports:read")
    require_document(db, org_id, user, document_id)
    data = _extraction_payload(db, org_id, document_id)
    fields: list[dict[str, Any]] = data["fields"]
    review_status = data["review"]["status"]
    columns = [
        "name",
        "value",
        "original_value",
        "confidence",
        "manually_verified",
        "needs_review",
        "correction_reason",
        "review_status",
        "trust",
        "evidence_status",
        "evidence_page",
        "evidence_snippet",
    ]

    def cell(value: object) -> object:
        return neutralize_formula(json.dumps(value) if isinstance(value, (dict, list)) else value)

    def row(f: dict[str, Any]) -> list[object]:
        return [
            neutralize_formula(f["name"]),
            cell(f["value"]),
            cell(f["original_value"]),
            f["confidence"],
            f["manually_verified"],
            f["needs_review"],
            neutralize_formula(f["correction_reason"]),
            review_status,
            f["trust"],
            f["evidence"]["status"],
            f["evidence"]["page"],
            neutralize_formula(f["evidence"]["snippet"]),  # document text: may start with = + - @
        ]

    if format == "json":
        return Response(
            json.dumps(data, default=str),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=extraction.json"},
        )
    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(columns)
        writer.writerows(row(f) for f in fields)
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=extraction.csv"},
        )
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Extraction"
    sheet.append(columns)
    for f in fields:
        sheet.append(row(f))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return Response(
        buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=extraction.xlsx"},
    )
