"""Data retention and deletion (policy: docs/DATA_RETENTION.md).

Two independent clocks, both driven by the ``purge_expired_data`` Celery task:

* ``retention_deleted_days`` -- a document a user deleted keeps its (already blanked)
  database shell for this long, then everything derived from it is hard-deleted.
* ``retention_document_days`` -- optional: live documents older than this are
  soft-deleted automatically (0 disables it).

"Derived data" means the stored PDF and every version of it, extracted page text, OCR
output, extraction runs and fields (including manual corrections), processing jobs and
steps, and per-document usage rows (kept as anonymous totals). Audit-log rows are kept:
they hold only identifiers, never document content.
"""

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core import settings
from app.db import SessionLocal
from app.metrics import RETENTION_PURGED_TOTAL
from app.models import (
    AuditLog,
    Document,
    DocumentPage,
    DocumentVersion,
    ExtractionField,
    ExtractionRun,
    ProcessingJob,
    ProcessingStep,
    UsageRecord,
    utcnow,
)
from app.storage import StorageAccessError, owned_key, storage

logger = logging.getLogger("docmind.retention")


def scrub_derived_text(db: Session, document_id: uuid.UUID) -> None:
    """Immediately drop the extracted page text of a deleted document."""
    db.execute(delete(DocumentPage).where(DocumentPage.document_id == document_id))


def delete_objects(organization_id: uuid.UUID, keys: set[str]) -> int:
    """Delete stored objects; returns how many could not be removed (reconcile later)."""
    failures = 0
    for key in keys:
        try:
            storage.delete(owned_key(organization_id, key))
        except StorageAccessError:
            failures += 1
            logger.error("refusing to delete object outside organization prefix")
        except Exception:
            failures += 1
            logger.warning("storage cleanup failed", extra={"organization_id": str(organization_id)})
    return failures


def hard_delete_document(db: Session, document: Document) -> int:
    """Remove a document and everything derived from it. Returns storage failures."""
    keys = {document.storage_key} | set(
        db.scalars(select(DocumentVersion.storage_key).where(DocumentVersion.document_id == document.id))
    )
    failures = delete_objects(document.organization_id, keys)
    run_ids = select(ExtractionRun.id).where(ExtractionRun.document_id == document.id)
    job_ids = select(ProcessingJob.id).where(ProcessingJob.document_id == document.id)
    db.execute(delete(ExtractionField).where(ExtractionField.extraction_run_id.in_(run_ids)))
    db.execute(delete(ExtractionRun).where(ExtractionRun.document_id == document.id))
    db.execute(delete(ProcessingStep).where(ProcessingStep.job_id.in_(job_ids)))
    db.execute(delete(ProcessingJob).where(ProcessingJob.document_id == document.id))
    db.execute(delete(DocumentPage).where(DocumentPage.document_id == document.id))
    db.execute(delete(DocumentVersion).where(DocumentVersion.document_id == document.id))
    # Usage is billing data: keep the totals, drop the link to the document.
    db.execute(update(UsageRecord).where(UsageRecord.document_id == document.id).values(document_id=None))
    db.execute(delete(Document).where(Document.id == document.id))
    return failures


def purge_expired(now: datetime | None = None) -> dict[str, int]:
    now = now or utcnow()
    cfg = settings()
    summary = {"expired": 0, "purged": 0, "storage_failures": 0}

    if cfg.retention_document_days > 0:
        cutoff = now - timedelta(days=cfg.retention_document_days)
        with SessionLocal.begin() as db:
            expired = db.scalars(
                select(Document).where(Document.deleted_at.is_(None), Document.created_at < cutoff)
            ).all()
            for document in expired:
                document.deleted_at = now
                scrub_derived_text(db, document.id)
                db.add(
                    AuditLog(
                        organization_id=document.organization_id,
                        actor_id=None,
                        action="document.expire",
                        target_type="document",
                        target_id=str(document.id),
                        metadata_={"reason": "retention_document_days"},
                    )
                )
                # Keep the original out of storage as soon as it expires.
                delete_objects(document.organization_id, {document.storage_key})
            summary["expired"] = len(expired)

    cutoff = now - timedelta(days=cfg.retention_deleted_days)
    with SessionLocal.begin() as db:
        due = db.scalars(
            select(Document).where(Document.deleted_at.is_not(None), Document.deleted_at <= cutoff)
        ).all()
        for document in due:
            summary["storage_failures"] += hard_delete_document(db, document)
            db.add(
                AuditLog(
                    organization_id=document.organization_id,
                    actor_id=None,
                    action="document.purge",
                    target_type="document",
                    target_id=str(document.id),
                    metadata_={"reason": "retention_deleted_days"},
                )
            )
        summary["purged"] = len(due)

    RETENTION_PURGED_TOTAL.labels("documents").inc(summary["purged"])
    RETENTION_PURGED_TOTAL.labels("expired").inc(summary["expired"])
    return summary
