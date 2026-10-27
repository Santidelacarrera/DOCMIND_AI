"""Periodic tasks: schedule shape, registration, and idempotency of a repeated run.

Deterministic and local. Whether beat actually fires these on time is checked by the beat smoke run
(docs/evaluation/beat-smoke-2026-10-27.txt), which needs a real broker and is not part of pytest.
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import contextrag_sync, worker
from app.core import settings
from app.db import SessionLocal
from app.models import ContextRagEvent, JobStatus, ProcessingJob, utcnow
from tests.conftest import make_pdf

EXPECTED = {
    # name in beat -> (task, maximum interval in seconds)
    "recover-stuck-jobs": ("app.worker.recover_stuck_jobs", 60),
    "purge-expired-data": ("app.worker.purge_expired_data", 3600),
    "process-vector-deletions": ("app.worker.process_vector_deletions", 60),
    "reconcile-storage": ("app.worker.reconcile_storage", 86400),
    "deliver-contextrag-events": ("app.worker.deliver_contextrag_events", 60),
    "reconcile-contextrag": ("app.worker.reconcile_contextrag", 3600),
}


def test_every_expected_periodic_task_is_scheduled_with_its_interval() -> None:
    schedule = worker.celery_app.conf.beat_schedule
    assert set(schedule) == set(EXPECTED), "an unexpected or missing periodic task"
    for name, (task, max_interval) in EXPECTED.items():
        assert schedule[name]["task"] == task
        assert 0 < float(schedule[name]["schedule"]) <= max_interval, name


def test_every_scheduled_task_is_registered_with_celery() -> None:
    for entry in worker.celery_app.conf.beat_schedule.values():
        assert entry["task"] in worker.celery_app.tasks, entry["task"]


def test_outbox_delivery_is_deliberately_more_frequent_than_reconciliation() -> None:
    schedule = worker.celery_app.conf.beat_schedule
    assert (
        schedule["deliver-contextrag-events"]["schedule"]
        < schedule["reconcile-contextrag"]["schedule"]
    )


def test_the_delivery_pass_budget_fits_inside_the_hard_time_limit() -> None:
    """Celery kills a task at processing_timeout_seconds; a pass must finish (or defer) before that."""
    import inspect

    budget = inspect.signature(contextrag_sync.process_due).parameters["budget_seconds"].default
    assert (
        budget + 2 * 15 < settings().processing_timeout_seconds
    )  # two HTTP calls of 15 s at the edge
    assert worker.celery_app.conf.task_time_limit == settings().processing_timeout_seconds


def test_a_task_that_runs_twice_on_the_same_state_changes_nothing_the_second_time(
    register, monkeypatch
) -> None:
    class OCR:
        def extract_pages(self, content: bytes) -> list[str]:
            return ["Idempotent periodic work."]

    monkeypatch.setattr(worker, "TesseractProvider", OCR)
    acct = register("idem")
    upload = acct.upload(acct.project(), make_pdf(1)).json()
    job_id = upload["job_id"]
    with SessionLocal() as db:  # a queued job whose message was lost long ago
        job = db.get(ProcessingJob, uuid.UUID(job_id))
        assert job is not None and job.status == JobStatus.queued
        job.enqueued_at = utcnow() - timedelta(seconds=settings().queued_stuck_seconds + 60)
        db.commit()
    first = worker.recover_stuck_jobs()
    second = worker.recover_stuck_jobs()
    assert first == 1
    assert second == 0  # the first run refreshed enqueued_at; the second finds nothing to do


def test_repeated_delivery_passes_are_idempotent_on_an_empty_queue(monkeypatch) -> None:
    monkeypatch.setattr(settings(), "contextrag_sync_enabled", True)
    monkeypatch.setattr(settings(), "contextrag_url", "http://localhost:8000")
    monkeypatch.setattr(settings(), "contextrag_keys_file", "unused")
    results = [contextrag_sync.process_due() for _ in range(3)]
    assert all(r["done"] == 0 and r["failed"] == 0 for r in results)


def test_repeated_reconciliation_creates_no_duplicate_events(register, monkeypatch) -> None:
    class OCR:
        def extract_pages(self, content: bytes) -> list[str]:
            return ["Reconciled repeatedly."]

    monkeypatch.setattr(worker, "TesseractProvider", OCR)
    acct = register("recon")
    upload = acct.upload(acct.project(), make_pdf(1)).json()
    worker.process_document.run(upload["job_id"])  # type: ignore[attr-defined]
    contextrag_sync.reconcile()
    contextrag_sync.reconcile()
    with SessionLocal() as db:
        count = len(
            db.scalars(
                select(ContextRagEvent).where(
                    ContextRagEvent.document_id == uuid.UUID(upload["document_id"])
                )
            ).all()
        )
    assert count == 1


@pytest.mark.parametrize("task_name", ["purge_expired_data", "reconcile_contextrag"])
def test_periodic_entry_points_are_callable_without_arguments(task_name: str) -> None:
    """Beat calls them with no arguments; a signature change would break them silently in production."""
    function = getattr(worker, task_name)
    function.run()  # Celery's task entry point, no arguments
