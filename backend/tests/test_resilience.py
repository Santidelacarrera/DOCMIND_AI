"""Operational failure modes with synthetic data: broker down at upload, storage failing at upload.

The guarantees checked here: an accepted upload is never silently lost, a failed upload leaves no row,
and error responses never carry the internal exception text (paths, hosts, document content).
"""

import uuid

from fastapi.testclient import TestClient

from app import main, worker
from app.db import SessionLocal
from app.models import Document, JobStatus, ProcessingJob
from app.storage import storage
from tests.conftest import make_pdf


def test_broker_down_at_upload_keeps_the_job_queued_and_recovery_finishes_it(
    register, client, monkeypatch
) -> None:
    def broker_down(job_id: str) -> None:
        raise ConnectionError("redis://internal-broker:6379 refused")

    monkeypatch.setattr(worker.process_document, "delay", broker_down)
    acct = register()
    response = acct.upload(acct.project(), make_pdf(1))
    assert response.status_code == 202, response.text  # accepted: the document is stored and queued
    body = response.json()
    assert "internal-broker" not in response.text  # the broker address is not echoed back
    with SessionLocal() as db:
        job = db.get(ProcessingJob, uuid.UUID(body["job_id"]))
        assert job is not None and job.status == JobStatus.queued
        job.enqueued_at = job.created_at  # make the queued job look stale to the recovery task
    monkeypatch.setattr(
        worker.process_document, "delay", lambda job_id: worker.process_document.run(job_id)
    )  # broker back
    from app.core import settings

    monkeypatch.setattr(settings(), "queued_stuck_seconds", 0)
    worker.recover_stuck_jobs()
    with SessionLocal() as db:
        job = db.get(ProcessingJob, uuid.UUID(body["job_id"]))
        assert job is not None and job.status == JobStatus.completed


def test_storage_failure_at_upload_leaves_no_document_row(register, client, monkeypatch) -> None:
    def disk_error(key: str, content: bytes) -> None:
        raise OSError("/var/lib/docmind/secret-mount/ write failed")

    monkeypatch.setattr(storage, "put", disk_error)
    acct = register()
    # What a real client sees: a server error without the exception, not a re-raised Python exception.
    acct.client = TestClient(main.app, raise_server_exceptions=False)
    response = acct.upload(acct.project(), make_pdf(1))
    assert response.status_code >= 500, response.text
    assert (
        "secret-mount" not in response.text and "OSError" not in response.text
    )  # no internals echoed
    with SessionLocal() as db:
        assert (
            db.query(Document).filter(Document.organization_id == uuid.UUID(acct.org)).count() == 0
        )


def test_storage_read_failure_on_download_is_a_generic_error(register, client, monkeypatch) -> None:
    from app.storage import storage as store

    class OCR:
        def extract_pages(self, content: bytes) -> list[str]:
            return ["Readable once."]

    monkeypatch.setattr(worker, "TesseractProvider", OCR)
    acct = register()
    upload = acct.upload(acct.project(), make_pdf(1)).json()
    worker.process_document.run(upload["job_id"])  # type: ignore[attr-defined]

    def unreadable(key: str) -> bytes:
        raise OSError("/var/lib/docmind/secret-mount/ read failed")

    monkeypatch.setattr(store, "get", unreadable)
    tc = TestClient(main.app, raise_server_exceptions=False)
    acct.client = tc
    response = acct.client.get(
        f"/api/v1/documents/{upload['document_id']}/download?organization_id={acct.org}",
        headers=acct.headers,
    )
    assert response.status_code >= 500
    assert "secret-mount" not in response.text and "OSError" not in response.text
