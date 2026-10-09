"""The job queue against a real Redis broker and a real Celery worker (in-process thread).

Skipped when no ``redis-server`` binary is available. Everything else is real: messages go
through Redis, the worker consumes them with acks-late semantics, state lands in the database.
"""

import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from celery.contrib.testing.worker import start_worker
from redis import Redis
from sqlalchemy import func, select

from app import worker
from app.core import settings
from app.db import SessionLocal
from app.models import ExtractionRun, JobStatus, ProcessingJob, UsageRecord, utcnow
from app.storage import storage

pytestmark = pytest.mark.skipif(shutil.which("redis-server") is None, reason="redis-server not installed")


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [shutil.which("redis-server") or "", "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"redis://127.0.0.1:{port}/0"
    for _ in range(50):
        try:
            if Redis.from_url(url).ping():
                break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip("redis-server did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def live_worker(redis_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    app = worker.celery_app
    original = (app.conf.broker_url, app.conf.result_backend)
    app.conf.update(broker_url=redis_url, result_backend=redis_url)
    Redis.from_url(redis_url).flushall()
    # conftest stubs `.delay` so API tests never need a broker; here the real one is wanted.
    monkeypatch.delattr(worker.process_document, "delay", raising=False)
    monkeypatch.setattr(worker, "RETRY_BACKOFF_SECONDS", 0.05)
    with start_worker(app, perform_ping_check=False, pool="threads", concurrency=3, loglevel="WARNING"):
        yield
    app.conf.update(broker_url=original[0], result_backend=original[1])


def _status(job_id: str) -> JobStatus:
    with SessionLocal() as db:
        job = db.get(ProcessingJob, uuid.UUID(job_id))
        assert job is not None
        return job.status


def _wait(job_id: str, wanted: set[JobStatus], timeout: float = 30) -> ProcessingJob:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with SessionLocal() as db:
            job = db.get(ProcessingJob, uuid.UUID(job_id))
            assert job is not None
            if job.status in wanted:
                return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} stuck in {_status(job_id)}")


def _count(model: Any) -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.count()).select_from(model)) or 0)


def _upload(register) -> tuple[Any, dict[str, Any]]:
    acct = register()
    return acct, acct.upload(acct.project()).json()


def test_job_goes_through_redis_and_completes(live_worker, register) -> None:
    _acct, body = _upload(register)
    worker.process_document.apply_async(args=[body["job_id"]])
    job = _wait(body["job_id"], {JobStatus.completed, JobStatus.failed})
    assert job.status == JobStatus.completed and job.started_at and job.completed_at
    assert _count(ExtractionRun) == 1


def test_duplicate_messages_for_one_job_produce_one_result(live_worker, register) -> None:
    _acct, body = _upload(register)
    for _ in range(6):  # at-least-once delivery, worst case
        worker.process_document.apply_async(args=[body["job_id"]])
    _wait(body["job_id"], {JobStatus.completed})
    time.sleep(1.0)  # let the remaining duplicates drain
    assert _count(ExtractionRun) == 1
    with SessionLocal() as db:
        processed = db.scalar(
            select(func.count()).select_from(UsageRecord).where(UsageRecord.metric == "documents_processed")
        )
    assert processed == 1


def test_transient_failure_is_retried_through_the_broker(live_worker, register, monkeypatch) -> None:
    _acct, body = _upload(register)
    real = storage.get
    calls = {"n": 0}

    def flaky(key: str) -> bytes:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OSError("storage blip")
        return real(key)

    monkeypatch.setattr(storage, "get", flaky)
    worker.process_document.apply_async(args=[body["job_id"]])
    job = _wait(body["job_id"], {JobStatus.completed, JobStatus.failed})
    assert job.status == JobStatus.completed
    assert job.retry_count == 2 and calls["n"] == 3
    assert _count(ExtractionRun) == 1


def test_message_lost_with_the_broker_is_recovered_from_the_database(
    live_worker, register, monkeypatch
) -> None:
    """Upload committed the job but the enqueue never happened (or Redis was flushed)."""
    monkeypatch.setattr(worker.process_document, "delay", lambda job_id: None)  # message dropped
    _acct, body = _upload(register)
    assert _status(body["job_id"]) == JobStatus.queued
    time.sleep(0.5)
    assert _status(body["job_id"]) == JobStatus.queued  # truly lost
    with SessionLocal.begin() as db:
        job = db.get(ProcessingJob, uuid.UUID(body["job_id"]))
        assert job is not None
        job.enqueued_at = utcnow() - timedelta(seconds=settings().queued_stuck_seconds + 10)
    monkeypatch.delattr(worker.process_document, "delay")  # recovery re-enqueues for real
    worker.recover_stuck_jobs.apply_async()
    job = _wait(body["job_id"], {JobStatus.completed, JobStatus.failed})
    assert job.status == JobStatus.completed and job.retry_count == 1
    assert _count(ExtractionRun) == 1
