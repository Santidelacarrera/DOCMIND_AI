"""Glue between the application lifecycle and the vector store (see app.ingestion).

* **Deletion is an outbox.** Whatever deletes, expires or purges a document calls
  :func:`queue_deletion` inside the same database transaction. ``process_deletions`` (Celery
  beat) then removes the vectors with tenant-scoped ``delete_document`` and retries with backoff
  until it works, so a Qdrant outage can delay a deletion but never lose it.
* **Indexing** (:func:`index_document`) runs after a document is processed and again whenever it
  is reprocessed (ingestion replaces the previous points). It re-checks the document after
  writing, so a delete that races with indexing cannot leave orphan vectors behind.
* Everything is inert unless ``QDRANT_URL`` is set.
"""

import logging
import uuid
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import settings
from app.db import SessionLocal
from app.ingestion.chunking import TenantError
from app.ingestion.embeddings import build_embedder
from app.ingestion.pipeline import IngestionPipeline, IngestionRefused
from app.ingestion.vectorstore import QdrantStore, VectorStore
from app.metrics import VECTOR_DELETIONS_TOTAL, VECTOR_INDEXING_TOTAL
from app.models import Document, VectorDeletion, utcnow
from app.storage import StorageAccessError, owned_key, storage

logger = logging.getLogger("docmind.vector_index")

MAX_BACKOFF = timedelta(hours=1)


class IndexTransientError(RuntimeError):
    """Indexing failed for a reason worth retrying (vector store or embedder unreachable)."""


def enabled() -> bool:
    return bool(settings().qdrant_url)


@lru_cache
def get_store() -> VectorStore:
    from qdrant_client import QdrantClient

    cfg = settings()
    key = cfg.qdrant_api_key.get_secret_value() if cfg.qdrant_api_key else None
    client = QdrantClient(url=cfg.qdrant_url, api_key=key, timeout=cfg.qdrant_timeout_seconds)
    return QdrantStore(client, cfg.qdrant_collection)


@lru_cache
def get_pipeline() -> IngestionPipeline:
    return IngestionPipeline.from_settings(get_store(), build_embedder(settings()))


def reset_caches() -> None:
    get_store.cache_clear()
    get_pipeline.cache_clear()


# --------------------------------------------------------------------------- deletion outbox
def queue_deletion(db: Session, organization_id: uuid.UUID, document_id: uuid.UUID) -> bool:
    """Record that this document's vectors must go. Call in the deleting transaction."""
    if not enabled():
        return False
    row = db.scalar(
        select(VectorDeletion).where(
            VectorDeletion.organization_id == organization_id, VectorDeletion.document_id == document_id
        )
    )
    if row is None:
        db.add(VectorDeletion(organization_id=organization_id, document_id=document_id))
    elif row.done_at is not None:  # deleted before, then re-indexed, now deleted again
        row.done_at, row.attempts, row.last_error, row.next_attempt_at = None, 0, None, utcnow()
    return True


def process_deletions(*, limit: int = 100, now: datetime | None = None) -> dict[str, int]:
    """Run pending deletions; failures are kept with exponential backoff (1 min .. 1 h)."""
    now = now or utcnow()
    result = {"deleted": 0, "failed": 0}
    if not enabled():
        return result
    store = get_store()
    with SessionLocal.begin() as db:
        rows = db.scalars(
            select(VectorDeletion)
            .where(VectorDeletion.done_at.is_(None), VectorDeletion.next_attempt_at <= now)
            .order_by(VectorDeletion.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            try:
                # Tenant-scoped by construction: the call cannot be made without the organization.
                store.delete_document(str(row.organization_id), str(row.document_id))
            except Exception as exc:
                row.attempts += 1
                row.last_error = type(exc).__name__[:80]
                row.next_attempt_at = now + min(MAX_BACKOFF, timedelta(minutes=2 ** min(row.attempts, 6)))
                result["failed"] += 1
                logger.warning("vector deletion failed", extra={"document_id": str(row.document_id), "attempts": row.attempts})
            else:
                row.done_at, row.last_error = now, None
                result["deleted"] += 1
    VECTOR_DELETIONS_TOTAL.labels("deleted").inc(result["deleted"])
    VECTOR_DELETIONS_TOTAL.labels("failed").inc(result["failed"])
    return result


# --------------------------------------------------------------------------- indexing
def _mark(document_id: uuid.UUID, *, indexed: bool, error: str | None) -> bool:
    """Record the outcome; returns False if the document was deleted in the meantime."""
    with SessionLocal.begin() as db:
        doc = db.get(Document, document_id)
        if doc is None or doc.deleted_at is not None:
            return False
        doc.indexed_at = utcnow() if indexed else None
        doc.index_error = error
        return True


def index_document(organization_id: str, document_id: str) -> str:
    """Ingest one document into the vector store. Returns ``indexed``, ``skipped`` or ``refused:<code>``."""
    if not enabled():
        return "skipped"
    org, doc_id = uuid.UUID(organization_id), uuid.UUID(document_id)
    with SessionLocal() as db:
        doc = db.get(Document, doc_id)
        if doc is None or doc.deleted_at is not None or doc.organization_id != org:
            return "skipped"
        key, filename, project = doc.storage_key, doc.filename, str(doc.project_id)
    try:
        content = storage.get(owned_key(org, key))
    except (StorageAccessError, FileNotFoundError):
        _mark(doc_id, indexed=False, error="STORAGE_OBJECT_MISSING")
        return "refused:STORAGE_OBJECT_MISSING"
    try:
        get_pipeline().ingest(
            tenant_id=str(org), document_id=str(doc_id), content=content, source_name=filename, project_id=project,
        )
    except (IngestionRefused, TenantError) as exc:
        code = getattr(exc, "code", None) or str(exc)
        _mark(doc_id, indexed=False, error=code[:80])
        VECTOR_INDEXING_TOTAL.labels("refused").inc()
        return f"refused:{code}"
    except Exception as exc:
        VECTOR_INDEXING_TOTAL.labels("failed").inc()
        raise IndexTransientError(type(exc).__name__) from None
    if not _mark(doc_id, indexed=True, error=None):
        # Deleted while we were writing: the outbox row may already have run, so remove again.
        get_store().delete_document(str(org), str(doc_id))
        VECTOR_INDEXING_TOTAL.labels("raced_with_delete").inc()
        return "skipped"
    VECTOR_INDEXING_TOTAL.labels("indexed").inc()
    return "indexed"


def backfill_project_ids(*, store: VectorStore | None = None) -> dict[str, int]:
    """Stamp ``project_id`` on the vectors of documents indexed before the field existed.

    Idempotent and read-only for the database: it only sets a payload value on points that belong to
    a live, indexed document, using the project recorded on that document. Running it twice changes
    nothing the second time. It does not re-embed, and it does not touch deleted documents (their
    vectors are removed by the deletion outbox).

    A document whose vectors cannot be stamped is counted in ``failed`` and left unscoped, so project
    queries keep excluding it. Use :func:`unscoped_points` to verify the result.
    """
    result = {"documents": 0, "failed": 0}
    if not enabled():
        return result
    target = store or get_store()
    with SessionLocal() as db:
        rows = db.execute(
            select(Document.organization_id, Document.id, Document.project_id).where(
                Document.deleted_at.is_(None), Document.indexed_at.is_not(None)
            )
        ).all()
    for org_id, doc_id, project_id in rows:
        try:
            target.set_document_project(str(org_id), str(doc_id), str(project_id))
        except Exception as exc:
            result["failed"] += 1
            logger.warning("project backfill failed", extra={"document_id": str(doc_id), "error": type(exc).__name__})
        else:
            result["documents"] += 1
    return result


def unscoped_points(organization_id: str, *, store: VectorStore | None = None) -> int:
    """Vectors of this organization that carry no project: they are invisible to project queries."""
    return (store or get_store()).count_without_project(organization_id)


def snapshot(store: VectorStore | None = None) -> Any:  # pragma: no cover - debugging aid
    return (store or get_store())
