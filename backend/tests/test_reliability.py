"""Reliable processing: retries, timeouts, stuck-job recovery, idempotency, input limits.

These run the real task body (``process_document.run``) against the real database with
only the broker mocked; ``test_celery_redis.py`` covers the real broker.
"""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from celery.exceptions import Retry, SoftTimeLimitExceeded
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import main, worker
from app.core import settings
from app.db import SessionLocal
from app.models import (
    Document,
    ExtractionField,
    ExtractionRun,
    JobStatus,
    ProcessingJob,
    ProcessingStep,
    RunStatus,
    UsageRecord,
    utcnow,
)
from app.processing import ProcessingError
from app.storage import storage
from tests.conftest import make_pdf

# Called outside a worker, ``self.retry`` re-raises the original error; inside one it raises Retry.
RETRY_SIGNAL = (Retry, ProcessingError)


def _run(job_id: str) -> None:
    worker.process_document.run(job_id)  # type: ignore[attr-defined]


def _upload(register, content: bytes | None = None):
    acct = register()
    project = acct.project()
    body = acct.upload(project, content).json()
    return acct, project, body


def _job(job_id: str) -> ProcessingJob:
    with SessionLocal() as db:
        job = db.get(ProcessingJob, uuid.UUID(job_id))
        assert job is not None
        return job


def _count(model: Any) -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.count()).select_from(model)) or 0)


class Flaky:
    """Fails ``failures`` times with ``exc`` and then delegates to the real function."""

    def __init__(self, real: Any, failures: int, exc: Exception) -> None:
        self.real, self.failures, self.exc, self.calls = real, failures, exc, 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return self.real(*args, **kwargs)


# ------------------------------------------------------------------------- retries
def test_transient_storage_error_is_retried_then_succeeds(register, monkeypatch) -> None:
    _, _, body = _upload(register)
    flaky = Flaky(storage.get, 2, OSError("mount hiccup"))
    monkeypatch.setattr(storage, "get", flaky)
    for expected_retries in (1, 2):
        with pytest.raises(RETRY_SIGNAL):
            _run(body["job_id"])
        job = _job(body["job_id"])
        # State is durable between attempts: QUEUED again, retry counted, reason stored.
        assert job.status == JobStatus.queued
        assert job.retry_count == expected_retries
        assert job.failure_code == "PROCESSING_RETRY"
    _run(body["job_id"])
    job = _job(body["job_id"])
    assert job.status == JobStatus.completed and job.failure_code is None
    assert _count(ExtractionRun) == 1  # three attempts, one result


def test_retries_are_bounded_and_end_in_a_stable_failure_code(register, monkeypatch) -> None:
    _, _, body = _upload(register)
    monkeypatch.setattr(storage, "get", Flaky(storage.get, 99, OSError("down")))
    for _ in range(worker.MAX_RETRIES):
        with pytest.raises(RETRY_SIGNAL):
            _run(body["job_id"])
    # The attempt after the budget is spent fails for good instead of retrying again.
    _run(body["job_id"])
    job = _job(body["job_id"])
    assert job.status == JobStatus.failed
    assert job.failure_code == "STORAGE_UNAVAILABLE"
    assert "down" not in (job.failure_message or "")
    assert _count(ExtractionRun) == 0


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ("missing_object", "STORAGE_OBJECT_MISSING"),
        ("garbage", "DOCUMENT_INVALID"),
    ],
)
def test_permanent_errors_fail_immediately_without_retry(register, setup: str, code: str) -> None:
    _, _, body = _upload(register)
    with SessionLocal() as db:
        key = db.get(Document, uuid.UUID(body["document_id"])).storage_key  # type: ignore[union-attr]
    if setup == "missing_object":
        storage.delete(key)
    else:
        storage.put(key, b"%PDF-1.4 definitely not a pdf")
    _run(body["job_id"])  # would raise Retry if it were treated as transient
    job = _job(body["job_id"])
    assert (job.status, job.failure_code, job.retry_count) == (JobStatus.failed, code, 0)
    with SessionLocal() as db:
        steps = db.scalars(select(ProcessingStep).where(ProcessingStep.job_id == job.id)).all()
    assert steps and all(s.status == "FAILED" and s.error == code for s in steps)
    assert worker.dispatched_webhooks[-1][1] == "document.failed"  # type: ignore[attr-defined]


def test_provider_outage_is_retried_but_a_rejected_request_is_not(register, monkeypatch) -> None:
    _, _, body = _upload(register)

    class Down:
        def extract(self, *a: Any, **k: Any) -> dict[str, Any]:
            raise ProcessingError("LLM_UNAVAILABLE", retryable=True)

    monkeypatch.setattr(worker, "llm", lambda: Down())
    with pytest.raises(RETRY_SIGNAL):
        _run(body["job_id"])
    assert _job(body["job_id"]).status == JobStatus.queued

    class Refused:
        def extract(self, *a: Any, **k: Any) -> dict[str, Any]:
            raise ProcessingError("LLM_REQUEST_FAILED")

    monkeypatch.setattr(worker, "llm", lambda: Refused())
    _run(body["job_id"])
    job = _job(body["job_id"])
    assert (job.status, job.failure_code) == (JobStatus.failed, "LLM_REQUEST_FAILED")


def test_unexpected_exception_fails_with_generic_code_and_no_leak(register, monkeypatch) -> None:
    _, _, body = _upload(register)

    class Boom:
        def extract(self, *a: Any, **k: Any) -> dict[str, Any]:
            raise RuntimeError("secret sk-live-123 in provider traceback")

    monkeypatch.setattr(worker, "llm", lambda: Boom())
    _run(body["job_id"])
    job = _job(body["job_id"])
    assert job.status == JobStatus.failed and job.failure_code == "PROCESSING_FAILED"
    assert "sk-live" not in (job.failure_message or "")


# ------------------------------------------------------------------------- timeouts
def test_soft_time_limit_marks_job_timed_out(register, monkeypatch) -> None:
    _, _, body = _upload(register)

    class Slow:
        def extract(self, *a: Any, **k: Any) -> dict[str, Any]:
            raise SoftTimeLimitExceeded()

    monkeypatch.setattr(worker, "llm", lambda: Slow())
    _run(body["job_id"])
    job = _job(body["job_id"])
    assert (job.status, job.failure_code) == (JobStatus.failed, "PROCESSING_TIMEOUT")
    assert worker.celery_app.conf.task_time_limit > worker.celery_app.conf.task_soft_time_limit


# ------------------------------------------------------------------------- idempotency
def test_redelivery_while_another_worker_is_running_does_nothing(register) -> None:
    _, _, body = _upload(register)
    with SessionLocal.begin() as db:
        job = db.get(ProcessingJob, uuid.UUID(body["job_id"]))
        assert job is not None
        job.status, job.started_at = JobStatus.extracting, utcnow()  # a live worker owns it
    _run(body["job_id"])
    assert _job(body["job_id"]).status == JobStatus.extracting
    assert _count(ExtractionRun) == 0


def test_abandoned_job_is_taken_over_after_the_hard_time_limit(register) -> None:
    _, _, body = _upload(register)
    stale = utcnow() - timedelta(seconds=settings().processing_timeout_seconds + 60)
    with SessionLocal.begin() as db:
        job = db.get(ProcessingJob, uuid.UUID(body["job_id"]))
        assert job is not None
        job.status, job.started_at = JobStatus.extracting, stale  # worker died mid-run
    _run(body["job_id"])
    assert _job(body["job_id"]).status == JobStatus.completed


def test_terminal_jobs_ignore_late_deliveries(register) -> None:
    _, _, body = _upload(register)
    _run(body["job_id"])
    snapshot = (_count(ExtractionRun), _count(ExtractionField), _count(UsageRecord))
    for _ in range(3):
        _run(body["job_id"])
    assert (_count(ExtractionRun), _count(ExtractionField), _count(UsageRecord)) == snapshot
    # ...and a *failed* job is not silently resurrected by a stale message either.
    _, _, other = _upload(register)
    with SessionLocal.begin() as db:
        j = db.get(ProcessingJob, uuid.UUID(other["job_id"]))
        assert j is not None
        j.status = JobStatus.failed
    _run(other["job_id"])
    assert _job(other["job_id"]).status == JobStatus.failed


def test_one_extraction_run_per_job_is_enforced_by_the_database(register) -> None:
    _, _, body = _upload(register)
    _run(body["job_id"])
    with SessionLocal() as db:
        run = db.scalar(select(ExtractionRun))
        assert run is not None
        duplicate = ExtractionRun(
            job_id=run.job_id, organization_id=run.organization_id, document_id=run.document_id,
            provider="mock", status=RunStatus.completed,
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.commit()


def test_deleted_document_gets_no_derived_data(client, register, monkeypatch) -> None:
    acct, _, body = _upload(register)

    real = worker._compute

    def compute_then_user_deletes(claim, log):  # type: ignore[no-untyped-def]
        computed = real(claim, log)
        client.delete(
            f"/api/v1/documents/{body['document_id']}?organization_id={acct.org}", headers=acct.headers
        )
        return computed

    monkeypatch.setattr(worker, "_compute", compute_then_user_deletes)
    _run(body["job_id"])
    job = _job(body["job_id"])
    assert (job.status, job.failure_code) == (JobStatus.cancelled, "DOCUMENT_DELETED")
    assert _count(ExtractionRun) == 0 and _count(ExtractionField) == 0


# ------------------------------------------------------------------------- stuck-job recovery
def _age(job_id: str, **fields: Any) -> None:
    with SessionLocal.begin() as db:
        job = db.get(ProcessingJob, uuid.UUID(job_id))
        assert job is not None
        for name, value in fields.items():
            setattr(job, name, value)


def test_lost_broker_message_is_recovered_without_duplicates(register, monkeypatch) -> None:
    _, _, body = _upload(register)
    sent: list[str] = []
    monkeypatch.setattr(worker.process_document, "delay", lambda job_id: sent.append(job_id))

    assert worker.recover_stuck_jobs.run() == 0  # a fresh QUEUED job is left alone
    _age(body["job_id"], enqueued_at=utcnow() - timedelta(seconds=settings().queued_stuck_seconds + 5))
    assert worker.recover_stuck_jobs.run() == 1
    assert sent == [body["job_id"]]
    assert _job(body["job_id"]).retry_count == 1
    assert worker.recover_stuck_jobs.run() == 0  # just re-enqueued: not recovered twice

    # The re-enqueued message AND a late original both arrive: still a single result.
    _run(sent[0])
    _run(body["job_id"])
    assert _job(body["job_id"]).status == JobStatus.completed
    # usage: the upload reservation + one documents_processed + one pages_processed
    assert (_count(ExtractionRun), _count(UsageRecord)) == (1, 3)


def test_job_orphaned_by_a_worker_crash_is_requeued_then_completed(register, monkeypatch) -> None:
    _, _, body = _upload(register)
    sent: list[str] = []
    monkeypatch.setattr(worker.process_document, "delay", lambda job_id: sent.append(job_id))
    _age(
        body["job_id"], status=JobStatus.ocr_processing,
        started_at=utcnow() - timedelta(seconds=settings().stuck_job_seconds + 5),
    )
    assert worker.recover_stuck_jobs.run() == 1
    job = _job(body["job_id"])
    assert job.status == JobStatus.queued and job.started_at is None
    _run(sent[0])
    assert _job(body["job_id"]).status == JobStatus.completed


def test_recovery_gives_up_after_the_budget_and_reports_failure(register, monkeypatch) -> None:
    _, _, body = _upload(register)
    monkeypatch.setattr(worker.process_document, "delay", lambda job_id: None)
    old = utcnow() - timedelta(seconds=settings().queued_stuck_seconds + 5)
    for _ in range(settings().max_job_recoveries):
        _age(body["job_id"], enqueued_at=old)
        assert worker.recover_stuck_jobs.run() == 1
        assert _job(body["job_id"]).status == JobStatus.queued
    _age(body["job_id"], enqueued_at=old)
    assert worker.recover_stuck_jobs.run() == 1
    job = _job(body["job_id"])
    assert (job.status, job.failure_code) == (JobStatus.failed, "PROCESSING_TIMEOUT")
    assert worker.dispatched_webhooks[-1][1] == "document.failed"  # type: ignore[attr-defined]


def test_failed_job_can_be_reprocessed_without_duplicating_results(client, register) -> None:
    acct, _, body = _upload(register)
    _age(body["job_id"], status=JobStatus.failed, failure_code="PROCESSING_TIMEOUT")
    url = f"/api/v1/documents/{body['document_id']}/process?organization_id={acct.org}"
    first = client.post(url, headers=acct.headers).json()
    again = client.post(url, headers=acct.headers).json()
    assert first["reused"] is False and again["reused"] is True and again["job_id"] == first["job_id"]
    _run(first["job_id"])
    assert _count(ExtractionRun) == 1
    status = client.get(
        f"/api/v1/documents/{body['document_id']}/status?organization_id={acct.org}", headers=acct.headers
    ).json()
    assert status["status"] == "COMPLETED" and status["attempt"] == 2


def test_broker_outage_at_upload_keeps_the_job_for_recovery(client, register, monkeypatch) -> None:
    def unavailable(job_id: str) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(main.process_document, "delay", unavailable)
    acct = register()
    response = acct.upload(acct.project())
    assert response.status_code == 202 and response.json()["status"] == "QUEUED"
    assert _job(response.json()["job_id"]).status == JobStatus.queued


# ------------------------------------------------------------------------- LLM answer validation
def test_structurally_invalid_llm_answer_is_rejected_and_never_stored(register, monkeypatch) -> None:
    _, _, body = _upload(register)
    calls: list[int] = []

    class Garbage:
        def extract(self, *a: Any, **k: Any) -> dict[str, Any]:
            calls.append(1)
            return {"fields": "not an object", "confidence": []}

    monkeypatch.setattr(worker, "llm", lambda: Garbage())
    _run(body["job_id"])
    job = _job(body["job_id"])
    assert (job.status, job.failure_code) == (JobStatus.failed, "LLM_SCHEMA_INVALID")
    assert len(calls) == 1 + settings().llm_invalid_output_retries  # re-asked, then rejected
    with SessionLocal() as db:
        run = db.scalar(select(ExtractionRun))
        assert run is not None and run.status == RunStatus.failed and run.result == {}
        assert db.scalar(select(func.count()).select_from(ExtractionField)) == 0


def test_invalid_answer_is_retried_once_and_a_valid_one_is_accepted(register, monkeypatch) -> None:
    _, _, body = _upload(register)

    class OnceBad:
        n = 0

        def extract(self, *a: Any, **k: Any) -> dict[str, Any]:
            OnceBad.n += 1
            if OnceBad.n == 1:
                return {"fields": None, "confidence": None}
            return {"fields": {"a": "x"}, "confidence": {"a": 0.99}}

    monkeypatch.setattr(worker, "llm", lambda: OnceBad())
    _run(body["job_id"])
    assert _job(body["job_id"]).status == JobStatus.completed


def test_schema_violation_policy_reject_fails_the_job(register, client, monkeypatch) -> None:
    acct, project, _ = _upload(register)
    client.post(
        f"/api/v1/schemas?organization_id={acct.org}&name=strict",
        json={"type": "object", "properties": {"customer_name": {"type": "integer"}}},
        headers=acct.headers,
    )
    upload = acct.upload(project).json()
    monkeypatch.setattr(settings(), "schema_violation_policy", "reject")
    _run(upload["job_id"])
    job = _job(upload["job_id"])
    assert (job.status, job.failure_code) == (JobStatus.failed, "LLM_SCHEMA_INVALID")


def test_missing_confidence_and_unexpected_fields_force_review(client, register, monkeypatch) -> None:
    acct, project, _ = _upload(register)
    client.post(
        f"/api/v1/schemas?organization_id={acct.org}&name=s",
        json={"type": "object", "properties": {"a": {"type": "string"}}},
        headers=acct.headers,
    )
    upload = acct.upload(project).json()

    class Sloppy:
        def extract(self, *a: Any, **k: Any) -> dict[str, Any]:
            return {"fields": {"a": "ok", "admin_override": True}, "confidence": {"admin_override": 0.99}}

    monkeypatch.setattr(worker, "llm", lambda: Sloppy())
    _run(upload["job_id"])
    data = client.get(
        f"/api/v1/documents/{upload['document_id']}/extraction?organization_id={acct.org}", headers=acct.headers
    ).json()
    rules = {(i["field"], i["rule"]) for i in data["validation_issues"]}
    assert ("a", "no_confidence") in rules and ("admin_override", "unexpected_field") in rules
    assert data["requires_review"] is True


def test_low_ocr_confidence_sends_every_field_to_review(client, register, monkeypatch) -> None:
    class ShakyOCR:
        def __init__(self) -> None:
            self.last_confidences: list[float | None] = []

        def extract_pages(self, content: bytes) -> list[str]:
            self.last_confidences = [0.41]
            return ["smudged text"]

    monkeypatch.setattr(worker, "TesseractProvider", ShakyOCR)
    acct, _, body = _upload(register)
    _run(body["job_id"])
    data = client.get(
        f"/api/v1/documents/{body['document_id']}/extraction?organization_id={acct.org}", headers=acct.headers
    ).json()
    assert any(i["rule"] == "low_ocr_confidence" for i in data["validation_issues"])
    assert all(f["needs_review"] for f in data["fields"])


# ------------------------------------------------------------------------- input limits
def test_upload_limits_and_invalid_files(client, register, monkeypatch) -> None:
    from pypdf import PdfWriter

    acct = register()
    project = acct.project()
    assert acct.upload(project, b"", "empty.pdf").status_code == 415
    assert acct.upload(project, b"MZ\x90\x00 executable", "tool.pdf").status_code == 415
    assert acct.upload(project, make_pdf(), "invoice.docx").status_code == 415
    assert acct.upload(project, b"%PDF-1.7\n%%EOF", "x.pdf").status_code == 422

    import io

    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.encrypt("pw")
    buf = io.BytesIO()
    writer.write(buf)
    response = acct.upload(project, buf.getvalue(), "locked.pdf")
    assert (response.status_code, response.json()["detail"]) == (422, "PDF_ENCRYPTED")

    monkeypatch.setattr(settings(), "max_pdf_pages", 2)
    assert acct.upload(project, make_pdf(3), "long.pdf").json()["detail"] == "PDF_PAGE_LIMIT_EXCEEDED"
    assert _count(Document) == 0  # nothing from the rejected files was kept
    assert list((settings().local_storage_path / acct.org).rglob("*.pdf")) == []


def test_duplicate_upload_reports_the_existing_document(client, register) -> None:
    acct = register()
    project = acct.project()
    pdf = make_pdf()
    first = acct.upload(project, pdf).json()
    again = acct.upload(project, pdf)
    assert again.status_code == 409
    assert again.json() == {"detail": "DUPLICATE_DOCUMENT", "document_id": first["document_id"]}
    assert _count(Document) == 1 and len(main.queued_jobs) == 1  # type: ignore[attr-defined]


def test_database_rejects_two_live_documents_with_the_same_checksum(register) -> None:
    acct = register()
    project = acct.project()
    first = acct.upload(project, make_pdf()).json()
    with SessionLocal() as db:
        original = db.get(Document, uuid.UUID(first["document_id"]))
        assert original is not None
        db.add(
            Document(
                organization_id=original.organization_id, project_id=original.project_id, filename="x.pdf",
                storage_key=f"{original.organization_id}/{uuid.uuid4()}.pdf", mime_type="application/pdf",
                checksum=original.checksum,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_concurrent_upload_race_resolves_to_a_single_document(client, register, monkeypatch) -> None:
    """Both requests pass the application-level duplicate check; the unique index decides."""
    acct = register()
    project = acct.project()
    pdf = make_pdf()
    first = acct.upload(project, pdf).json()

    real_scalar = main.Session.scalar
    state = {"hide_once": True}

    def blind_first_check(self, statement, *a, **k):  # type: ignore[no-untyped-def]
        sql = str(statement)
        if state["hide_once"] and "documents.checksum" in sql and "projects" not in sql:
            state["hide_once"] = False
            return None  # the racing request did not see the winner yet
        return real_scalar(self, statement, *a, **k)

    monkeypatch.setattr(main.Session, "scalar", blind_first_check)
    loser = acct.upload(project, pdf)
    assert loser.status_code == 409
    assert loser.json()["document_id"] == first["document_id"]
    assert _count(Document) == 1
    # the loser's freshly written object was cleaned up; only the winner's remains
    assert len(list((settings().local_storage_path / acct.org).rglob("*.pdf"))) == 1


def test_failure_message_endpoint_exposes_job_trace(client, register) -> None:
    acct, _, body = _upload(register)
    _run(body["job_id"])
    status = client.get(
        f"/api/v1/documents/{body['document_id']}/status?organization_id={acct.org}", headers=acct.headers
    ).json()
    for key in ("job_id", "attempt", "retry_count", "started_at", "completed_at", "failure_code"):
        assert key in status
    assert status["started_at"] is not None and status["completed_at"] is not None


def test_failed_documents_can_be_listed_with_their_reason(client, register) -> None:
    acct = register()
    project = acct.project()
    good = acct.upload(project, make_pdf(1)).json()
    bad = acct.upload(project, make_pdf(2)).json()
    _run(good["job_id"])
    _age(bad["job_id"], status=JobStatus.failed, failure_code="PROCESSING_TIMEOUT")
    base = f"/api/v1/documents?organization_id={acct.org}"
    everything = client.get(base, headers=acct.headers).json()
    assert {d["status"] for d in everything} == {"COMPLETED", "FAILED"}
    failed = client.get(base + "&status=FAILED", headers=acct.headers).json()
    assert [(d["id"], d["failure_code"]) for d in failed] == [(bad["document_id"], "PROCESSING_TIMEOUT")]
    assert client.get(base + "&status=NOPE", headers=acct.headers).status_code == 422
    # Reprocessing clears it from the failed list once the new attempt succeeds.
    client.post(f"/api/v1/documents/{bad['document_id']}/process?organization_id={acct.org}", headers=acct.headers)
    again = client.get(base + "&status=FAILED", headers=acct.headers).json()
    assert again == []
