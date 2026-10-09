"""Retention and deletion: what disappears, when, and what is deliberately kept."""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from app import worker
from app.core import settings
from app.db import SessionLocal
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
from app.retention import purge_expired
from app.storage import storage
from tests.conftest import make_pdf

DERIVED = (DocumentPage, DocumentVersion, ExtractionRun, ExtractionField, ProcessingJob, ProcessingStep)


def _count(model: Any, **where: Any) -> int:
    with SessionLocal() as db:
        stmt = select(func.count()).select_from(model)
        for column, value in where.items():
            stmt = stmt.where(getattr(model, column) == value)
        return int(db.scalar(stmt) or 0)


def _processed(register, pages: int = 2):
    acct = register()
    upload = acct.upload(acct.project(), make_pdf(pages)).json()
    worker.process_document.run(upload["job_id"])  # type: ignore[attr-defined]
    return acct, upload


def _key(document_id: str) -> str:
    with SessionLocal() as db:
        return db.get(Document, uuid.UUID(document_id)).storage_key  # type: ignore[union-attr]


def _delete(client, acct, document_id: str) -> None:
    assert client.delete(
        f"/api/v1/documents/{document_id}?organization_id={acct.org}", headers=acct.headers
    ).status_code == 204


def test_delete_removes_file_and_text_immediately_but_keeps_the_rest_for_the_grace_period(client, register) -> None:
    acct, upload = _processed(register)
    key = _key(upload["document_id"])
    assert _count(DocumentPage) == 2
    _delete(client, acct, upload["document_id"])
    # Immediately: the stored original and the extracted/OCR text are gone.
    assert _count(DocumentPage) == 0
    try:
        storage.get(key)
        raise AssertionError("object should be deleted")
    except FileNotFoundError:
        pass
    # Extraction values wait for the retention job (they back the audit trail until then).
    assert _count(ExtractionField) > 0 and _count(ExtractionRun) == 1


def test_purge_waits_for_the_retention_window_then_removes_everything_derived(client, register) -> None:
    acct, upload = _processed(register)
    _delete(client, acct, upload["document_id"])
    assert purge_expired() == {"expired": 0, "purged": 0, "storage_failures": 0}  # still inside the window

    later = utcnow() + timedelta(days=settings().retention_deleted_days, seconds=5)
    assert purge_expired(later)["purged"] == 1
    assert _count(Document) == 0
    for model in DERIVED:
        assert _count(model) == 0, model.__name__
    # Usage is billing data: totals survive, the link to the document does not.
    assert _count(UsageRecord) > 0 and _count(UsageRecord, document_id=uuid.UUID(upload["document_id"])) == 0
    # Audit rows survive and record the purge; they hold identifiers only.
    with SessionLocal() as db:
        actions = [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.created_at))]
    assert "document.delete" in actions and "document.purge" in actions
    assert purge_expired(later)["purged"] == 0  # idempotent


def test_purge_never_touches_live_documents_or_other_tenants(client, register) -> None:
    keep, kept = _processed(register)
    gone, removed = _processed(register)
    _delete(client, gone, removed["document_id"])
    purge_expired(utcnow() + timedelta(days=settings().retention_deleted_days + 1))
    assert _count(Document) == 1
    assert client.get(
        f"/api/v1/documents/{kept['document_id']}/extraction?organization_id={keep.org}", headers=keep.headers
    ).status_code == 200
    assert storage.get(_key(kept["document_id"])).startswith(b"%PDF")


def test_automatic_expiry_of_live_documents_is_opt_in(client, register, monkeypatch) -> None:
    acct, upload = _processed(register)
    far_future = utcnow() + timedelta(days=3650)
    assert purge_expired(far_future)["expired"] == 0  # disabled by default

    monkeypatch.setattr(settings(), "retention_document_days", 90)
    key = _key(upload["document_id"])
    assert purge_expired(utcnow() + timedelta(days=91))["expired"] == 1
    assert client.get(
        f"/api/v1/documents?organization_id={acct.org}", headers=acct.headers
    ).json() == []
    try:
        storage.get(key)
        raise AssertionError("expired object should be deleted")
    except FileNotFoundError:
        pass
    assert _count(DocumentPage) == 0


def test_a_storage_failure_is_reported_and_never_blocks_the_purge(client, register, monkeypatch) -> None:
    acct, upload = _processed(register)
    _delete(client, acct, upload["document_id"])

    def broken(key: str) -> None:
        raise OSError("bucket unreachable")

    monkeypatch.setattr(storage, "delete", broken)
    result = purge_expired(utcnow() + timedelta(days=settings().retention_deleted_days + 1))
    assert result["purged"] == 1 and result["storage_failures"] >= 1


def test_the_retention_task_is_scheduled_and_callable(client, register) -> None:
    assert "purge-expired-data" in worker.celery_app.conf.beat_schedule
    assert worker.purge_expired_data.run() == {"expired": 0, "purged": 0, "storage_failures": 0}  # type: ignore[attr-defined]
