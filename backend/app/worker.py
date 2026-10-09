import logging
import ssl
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import CursorResult, and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.core import settings
from app.db import SessionLocal
from app.logging_config import configure_logging
from app.metrics import (
    DOCUMENTS_PROCESSED_TOTAL,
    EXTRACTION_REQUIRES_REVIEW_TOTAL,
    JOBS_RECOVERED_TOTAL,
    PROCESSING_DURATION_SECONDS,
    WEBHOOK_DELIVERIES_TOTAL,
)
from app.models import (
    Document,
    DocumentPage,
    ExtractionField,
    ExtractionRun,
    ExtractionSchema,
    ExtractionSchemaVersion,
    JobStatus,
    ProcessingJob,
    ProcessingStep,
    RunStatus,
    UsageRecord,
    Webhook,
    WebhookDelivery,
    as_aware_utc,
    utcnow,
)
from app.ocr import TesseractProvider, needs_ocr
from app.processing import (
    ProcessingError,
    estimate_cost,
    estimate_tokens,
    extract_pdf_pages,
    extraction_envelope,
    llm,
)
from app.storage import StorageAccessError, owned_key, storage
from app.telemetry import configure_worker_tracing
from app.validation import (
    check_envelope,
    clamp_confidence,
    has_schema_violations,
    ocr_issues,
    validate_fields,
)
from app.webhooks import deliver as deliver_webhook_request

configure_logging()
configure_worker_tracing()
logger = logging.getLogger("docmind.worker")

celery_app = Celery(
    "docmind",
    broker=settings().effective_redis_url,
    backend=settings().effective_redis_url,
)

# TLS options only apply to rediss:// URLs; plain redis:// (local development) would
# otherwise fail the handshake.
_tls = (
    {
        "broker_use_ssl": {"ssl_cert_reqs": ssl.CERT_REQUIRED},
        "redis_backend_use_ssl": {"ssl_cert_reqs": ssl.CERT_REQUIRED},
    }
    if settings().effective_redis_url.startswith("rediss://")
    else {}
)

celery_app.conf.update(
    **_tls,
    task_serializer="json",
    accept_content=["json"],
    result_expires=3600,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # Redis re-delivers an unacked message after the visibility timeout; keep it well above the
    # hard time limit so a slow-but-alive task is never run twice by the broker itself.
    broker_transport_options={"visibility_timeout": max(3600, 2 * settings().processing_timeout_seconds)},
    task_time_limit=settings().processing_timeout_seconds,
    task_soft_time_limit=max(1, settings().processing_timeout_seconds - 15),
)

# `celery -A app.worker.celery_app beat` (the `scheduler` compose service) runs this.
celery_app.conf.beat_schedule = {
    "recover-stuck-jobs": {"task": "app.worker.recover_stuck_jobs", "schedule": 60.0},
    "purge-expired-data": {"task": "app.worker.purge_expired_data", "schedule": 3600.0},
}

MAX_RETRIES = 3
# Retry delay for transient failures: base * 2**attempt seconds, capped at one minute.
RETRY_BACKOFF_SECONDS = 1.0


def active_schema_version(
    db: Session, organization_id: uuid.UUID, project_id: uuid.UUID
) -> ExtractionSchemaVersion | None:
    """Latest version of the newest active schema for the project, else the org-wide one.

    Every lookup is scoped to the job's organization, so a tenant can never receive
    another tenant's schema.
    """
    schema = db.scalar(
        select(ExtractionSchema)
        .where(
            ExtractionSchema.organization_id == organization_id,
            ExtractionSchema.active.is_(True),
            or_(ExtractionSchema.project_id == project_id, ExtractionSchema.project_id.is_(None)),
        )
        # Project-specific schemas win over organization-wide ones.
        .order_by(ExtractionSchema.project_id.is_(None), ExtractionSchema.created_at.desc())
    )
    if schema is None:
        return None
    return db.scalar(
        select(ExtractionSchemaVersion)
        .where(ExtractionSchemaVersion.schema_id == schema.id)
        .order_by(ExtractionSchemaVersion.version.desc())
    )


def resolve_schema_version(db: Session, job: ProcessingJob, document: Document) -> ExtractionSchemaVersion | None:
    """The version pinned at upload/reprocess time wins; otherwise fall back to
    whatever schema is active for the project/organization right now."""
    if job.requested_schema_version_id is not None:
        version = db.scalar(
            select(ExtractionSchemaVersion)
            .join(ExtractionSchema)
            .where(
                ExtractionSchemaVersion.id == job.requested_schema_version_id,
                ExtractionSchema.organization_id == job.organization_id,
            )
        )
        if version is not None:
            return version
    return active_schema_version(db, job.organization_id, document.project_id)


ACTIVE_STATUSES = (
    JobStatus.processing,
    JobStatus.ocr_processing,
    JobStatus.extracting,
    JobStatus.validating,
)
TERMINAL_STATUSES = (JobStatus.completed, JobStatus.failed, JobStatus.cancelled)

FAILURE_MESSAGES = {
    "PROCESSING_RETRY": "Processing temporarily unavailable; it will be retried automatically.",
    "PROCESSING_TIMEOUT": "Processing exceeded the configured time limit.",
    "DOCUMENT_INVALID": "The PDF could not be read.",
    "PDF_ENCRYPTED": "Encrypted PDFs are not supported.",
    "PDF_PAGE_LIMIT_EXCEEDED": "The PDF has more pages than the allowed maximum.",
    "STORAGE_OBJECT_MISSING": "The stored file is missing.",
    "STORAGE_UNAVAILABLE": "File storage is temporarily unavailable.",
    "OCR_FAILED": "Text recognition (OCR) failed.",
    "LLM_UNAVAILABLE": "The extraction service is temporarily unavailable.",
    "LLM_REQUEST_FAILED": "The extraction service rejected the request.",
    "LLM_SCHEMA_INVALID": "The extraction answer did not match the required structure and was rejected.",
    "DOCUMENT_DELETED": "The document was deleted before processing finished.",
    "DOCUMENT_NOT_FOUND": "The document record is missing.",
}
GENERIC_FAILURE = ("PROCESSING_FAILED", "Processing failed. Check operational logs.")


def _failure_message(code: str) -> str:
    return FAILURE_MESSAGES.get(code, GENERIC_FAILURE[1])


@dataclass
class _StepLog:
    """In-memory trace of pipeline steps, persisted together with the job outcome."""

    steps: list[dict[str, Any]] = field(default_factory=list)
    current: str | None = None

    def start(self, name: str) -> None:
        self.current = name
        self.steps.append({"name": name, "status": "STARTED", "started_at": utcnow()})

    def finish(self, error: str | None = None) -> None:
        for step in reversed(self.steps):
            if step["name"] == self.current and step["status"] == "STARTED":
                step["status"] = "FAILED" if error else "COMPLETED"
                step["error"] = error
                step["completed_at"] = utcnow()
                break
        self.current = None


def _persist_steps(db: Session, job_id: uuid.UUID, log: _StepLog, failure: str | None) -> None:
    for step in log.steps:
        if step["status"] == "STARTED":  # interrupted mid-step
            step["status"], step["error"], step["completed_at"] = "FAILED", failure, utcnow()
        db.add(
            ProcessingStep(
                job_id=job_id,
                name=step["name"],
                status=step["status"],
                started_at=step["started_at"],
                completed_at=step.get("completed_at"),
                error=step.get("error"),
            )
        )


@dataclass
class _Claim:
    job_id: uuid.UUID
    organization_id: uuid.UUID
    document_id: uuid.UUID
    storage_key: str
    # Ownership token: the ``started_at`` this worker wrote when it claimed the job.
    # Every later write is fenced on it, so a worker that was taken over (or recovered)
    # can never overwrite the state of whoever owns the job now.
    started_at: datetime


def _claim(job_id: str) -> _Claim | None:
    """Phase 1 (own short transaction): atomically take ownership and commit RUNNING state.

    The claim is a single compare-and-set ``UPDATE ... WHERE`` so exactly one of any
    number of concurrent deliveries wins, on every database (``SELECT ... FOR UPDATE`` is a
    no-op on SQLite). A job is claimable when it is QUEUED, or in-flight but abandoned: a
    worker is hard-killed at ``processing_timeout_seconds``, so anything claimed longer ago
    than that belongs to a dead worker. Committing immediately makes the state observable to
    the API, the recovery task and operators.
    """
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        return None
    now = utcnow()
    abandoned = now - timedelta(seconds=settings().processing_timeout_seconds)
    with SessionLocal.begin() as db:
        claimed = cast(CursorResult[Any], db.execute(
            update(ProcessingJob)
            .where(
                ProcessingJob.id == job_uuid,
                or_(
                    ProcessingJob.status.in_((JobStatus.queued, JobStatus.uploaded)),
                    and_(
                        ProcessingJob.status.in_(ACTIVE_STATUSES),
                        or_(ProcessingJob.started_at.is_(None), ProcessingJob.started_at < abandoned),
                    ),
                ),
            )
            .values(status=JobStatus.processing, started_at=now, failure_code=None, failure_message=None)
        )).rowcount
        if claimed != 1:
            # Unknown, already finished, or being worked on by a live worker: nothing to do.
            return None
        job = db.get(ProcessingJob, job_uuid)
        assert job is not None
        document = db.get(Document, job.document_id)
        if document is None or document.deleted_at is not None:
            job.status = JobStatus.cancelled if document else JobStatus.failed
            job.failure_code = "DOCUMENT_DELETED" if document else "DOCUMENT_NOT_FOUND"
            job.failure_message = _failure_message(job.failure_code)
            job.completed_at = now
            return None
        return _Claim(job.id, job.organization_id, document.id, document.storage_key, now)


def _owns(job: ProcessingJob | None, claim: _Claim) -> bool:
    return (
        job is not None
        and job.status in ACTIVE_STATUSES
        and job.started_at is not None
        and as_aware_utc(job.started_at) == as_aware_utc(claim.started_at)
    )


def _set_status(job_id: uuid.UUID, status: JobStatus) -> None:
    with SessionLocal.begin() as db:
        job = db.get(ProcessingJob, job_id)
        if job is not None and job.status in ACTIVE_STATUSES:
            job.status = status


def _read_object(claim: _Claim) -> bytes:
    """Fetch the stored PDF, classifying storage errors as permanent or transient."""
    try:
        return storage.get(owned_key(claim.organization_id, claim.storage_key))
    except StorageAccessError:
        raise ProcessingError("STORAGE_OBJECT_MISSING") from None
    except FileNotFoundError:
        raise ProcessingError("STORAGE_OBJECT_MISSING") from None
    except OSError:
        raise ProcessingError("STORAGE_UNAVAILABLE", retryable=True) from None
    except Exception as exc:
        # boto3 raises its own hierarchy (not OSError): a missing key is permanent,
        # everything else (throttling, 5xx, connection) is worth retrying.
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
        if code in {"NoSuchKey", "404", "NotFound"}:
            raise ProcessingError("STORAGE_OBJECT_MISSING") from None
        if type(exc).__module__.startswith(("botocore", "boto3")):
            raise ProcessingError("STORAGE_UNAVAILABLE", retryable=True) from None
        raise


@dataclass
class _Computed:
    pages_text: list[str]
    ocr_used: bool
    ocr_confidences: list[float | None]
    result: dict[str, Any]
    fields: dict[str, Any]
    confidence_by_field: dict[str, float | None]
    issues: list[dict[str, Any]]
    schema_version_id: uuid.UUID | None
    usage: dict[str, Any]


def _compute(claim: _Claim, log: _StepLog) -> _Computed:
    """Phase 2 (no open transaction): the slow, failure-prone work."""
    log.start("text_extraction")
    content = _read_object(claim)
    pages_text = extract_pdf_pages(content)
    pages = len(pages_text)
    text = "\n".join(pages_text)[: settings().max_pdf_text_chars]
    log.finish()

    ocr_used = False
    ocr_confidences: list[float | None] = []
    if needs_ocr(text, pages):
        _set_status(claim.job_id, JobStatus.ocr_processing)
        log.start("ocr")
        try:
            ocr = TesseractProvider()
            ocr_pages = ocr.extract_pages(content)
            ocr_confidences = list(getattr(ocr, "last_confidences", []))
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            raise ProcessingError("OCR_FAILED") from None
        ocr_used = True
        pages_text = [ocr_pages[i] if i < len(ocr_pages) else "" for i in range(pages)]
        text = "\n".join(pages_text)
        log.finish()

    _set_status(claim.job_id, JobStatus.extracting)
    with SessionLocal() as db:
        job = db.get(ProcessingJob, claim.job_id)
        assert job is not None
        document = db.get(Document, claim.document_id)
        assert document is not None
        schema_version = resolve_schema_version(db, job, document)
        json_schema = schema_version.json_schema if schema_version else None
        instructions = schema_version.prompt_instructions if schema_version else None
        schema_version_id = schema_version.id if schema_version else None

    log.start("llm_extraction")
    result: dict[str, Any] = {}
    problems: list[str] = ["no attempt made"]
    for _ in range(1 + max(0, settings().llm_invalid_output_retries)):
        result = llm().extract(text, extraction_envelope(json_schema), prompt_instructions=instructions)
        problems = check_envelope(result)
        if not problems:
            break
        logger.warning("llm answer rejected", extra={"job_id": str(claim.job_id), "problems": problems})
    if problems:
        raise ProcessingError("LLM_SCHEMA_INVALID")
    log.finish()

    _set_status(claim.job_id, JobStatus.validating)
    log.start("validation")
    usage = result.pop("usage", None) or {}
    fields = result["fields"]
    confidence_by_field: dict[str, float | None] = {
        str(name): clamp_confidence(value) for name, value in (result.get("confidence") or {}).items()
    }
    known = {name: score for name, score in confidence_by_field.items() if score is not None}
    issues = validate_fields(json_schema, fields, known)
    if ocr_used and ocr_confidences:
        issues.extend(ocr_issues(ocr_confidences))
    if settings().schema_violation_policy == "reject" and has_schema_violations(issues):
        raise ProcessingError("LLM_SCHEMA_INVALID")
    log.finish()
    if "prompt_tokens" not in usage:
        usage = {
            "prompt_tokens": estimate_tokens(text),
            "completion_tokens": estimate_tokens(str(result)),
            "estimated": True,
        }
    return _Computed(pages_text, ocr_used, ocr_confidences, result, fields, confidence_by_field, issues, schema_version_id, usage)


def _commit_success(claim: _Claim, computed: _Computed, log: _StepLog) -> bool:
    """Phase 3 (one atomic transaction): persist results and the COMPLETED state."""
    with SessionLocal.begin() as db:
        # Fence: only the current owner may finish the job. Doing it as one conditional
        # UPDATE (not read-then-write) keeps this safe without row locks.
        finished = cast(CursorResult[Any], db.execute(
            update(ProcessingJob)
            .where(
                ProcessingJob.id == claim.job_id,
                ProcessingJob.status.in_(ACTIVE_STATUSES),
                ProcessingJob.started_at == claim.started_at,
            )
            .values(status=JobStatus.completed, completed_at=utcnow(), failure_code=None, failure_message=None)
        )).rowcount
        if finished != 1:
            return False  # recovered, taken over or already finished: drop our result
        job = db.get(ProcessingJob, claim.job_id)
        assert job is not None
        document = db.get(Document, claim.document_id)
        if document is None or document.deleted_at is not None:
            # Never create derived data for a document the user already deleted.
            job.status = JobStatus.cancelled
            job.failure_code = "DOCUMENT_DELETED"
            job.failure_message = _failure_message("DOCUMENT_DELETED")
            job.completed_at = utcnow()
            return False
        pages = len(computed.pages_text)
        document.page_count = pages
        db.execute(delete(DocumentPage).where(DocumentPage.document_id == document.id))
        for number, page_text in enumerate(computed.pages_text, start=1):
            db.add(
                DocumentPage(
                    document_id=document.id,
                    page_number=number,
                    text=page_text,
                    ocr_used=computed.ocr_used,
                    ocr_confidence=(
                        computed.ocr_confidences[number - 1]
                        if number - 1 < len(computed.ocr_confidences)
                        else None
                    ),
                )
            )
        provider = settings().llm_provider
        prompt_tokens = computed.usage.get("prompt_tokens")
        completion_tokens = computed.usage.get("completion_tokens")
        run = ExtractionRun(
            job_id=job.id,
            organization_id=job.organization_id,
            document_id=document.id,
            provider=provider,
            model=settings().openai_model if provider == "openai" else None,
            result=computed.result,
            schema_version_id=computed.schema_version_id,
            status=RunStatus.completed,
            validation_issues=computed.issues,
            requires_review=bool(computed.issues),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost=estimate_cost(prompt_tokens, completion_tokens) if provider == "openai" else 0.0,
        )
        db.add(run)
        db.flush()
        if computed.issues:
            EXTRACTION_REQUIRES_REVIEW_TOTAL.inc()
            logger.info(
                "extraction requires review",
                extra={"document_id": str(document.id), "run_id": str(run.id), "issue_count": len(computed.issues)},
            )
        for name, value in computed.fields.items():
            db.add(
                ExtractionField(
                    extraction_run_id=run.id,
                    name=name,
                    original_value=value,
                    value=value,
                    confidence=computed.confidence_by_field.get(name),
                )
            )
        db.add(UsageRecord(organization_id=job.organization_id, metric="documents_processed", quantity=1, document_id=document.id))
        db.add(UsageRecord(organization_id=job.organization_id, metric="pages_processed", quantity=pages, document_id=document.id))
        _persist_steps(db, job.id, log, None)
        return True


def _commit_failure(claim: _Claim, error: ProcessingError, log: _StepLog) -> str:
    """Record a failed attempt. Returns ``retry``, ``failed`` or ``ignored``."""
    with SessionLocal.begin() as db:
        job = db.scalar(select(ProcessingJob).where(ProcessingJob.id == claim.job_id).with_for_update())
        if not _owns(job, claim):
            return "ignored"  # someone else owns (or already finished) the job
        assert job is not None
        retry = error.retryable and job.retry_count < MAX_RETRIES
        _persist_steps(db, job.id, log, error.code)
        if retry:
            job.status = JobStatus.queued
            job.retry_count += 1
            job.enqueued_at = utcnow()
            job.failure_code = "PROCESSING_RETRY"
            job.failure_message = _failure_message("PROCESSING_RETRY")
            return "retry"
        job.status = JobStatus.failed
        job.failure_code = error.code
        job.failure_message = _failure_message(error.code)
        job.completed_at = utcnow()
        if error.code == "LLM_SCHEMA_INVALID":
            db.add(
                ExtractionRun(
                    job_id=job.id,
                    organization_id=job.organization_id,
                    document_id=claim.document_id,
                    provider=settings().llm_provider,
                    status=RunStatus.failed,
                    error=error.code,
                    result={},
                    requires_review=True,
                )
            )
        return "failed"


def _notify(status: str, organization_id: uuid.UUID, document_id: uuid.UUID, started_at: float) -> None:
    """Metrics + webhook dispatch; runs after commit and must never raise."""
    PROCESSING_DURATION_SECONDS.observe(time.perf_counter() - started_at)
    DOCUMENTS_PROCESSED_TOTAL.labels(status).inc()
    event = "document.completed" if status == "completed" else "document.failed"
    try:
        dispatch_webhooks.delay(str(organization_id), event, {"document_id": str(document_id)})
    except Exception:
        logger.warning("could not enqueue webhook dispatch", extra={"event": event})


@celery_app.task(bind=True)
def process_document(self, job_id: str) -> None:
    """Idempotent pipeline: claim -> compute -> commit, with durable state at each edge.

    Safe to deliver more than once: a finished job is ignored, a job still being worked
    on by a live worker is skipped, and the ``extraction_runs.job_id`` unique constraint
    is the last line of defence against duplicated results.
    """
    started_at = time.perf_counter()
    claim = _claim(job_id)
    if claim is None:
        return
    log = _StepLog()
    error: ProcessingError | None = None
    try:
        computed = _compute(claim, log)
        if _commit_success(claim, computed, log):
            _notify("completed", claim.organization_id, claim.document_id, started_at)
        return
    except ProcessingError as exc:
        error = exc
    except SoftTimeLimitExceeded:
        error = ProcessingError("PROCESSING_TIMEOUT")
    except Exception:
        # Raw provider/filesystem errors can carry sensitive context: log them here,
        # persist only a stable code.
        logger.exception("unexpected processing failure", extra={"job_id": job_id})
        error = ProcessingError(GENERIC_FAILURE[0])
    outcome = _commit_failure(claim, error, log)
    if outcome == "retry":
        raise self.retry(
            exc=error,
            countdown=min(RETRY_BACKOFF_SECONDS * 2 ** (self.request.retries + 1), 60),
            max_retries=MAX_RETRIES + 1,
        )
    if outcome == "failed":
        _notify("failed", claim.organization_id, claim.document_id, started_at)


@celery_app.task
def recover_stuck_jobs() -> int:
    """Re-enqueue or fail jobs that stopped making progress. Run at least once a minute.

    Two kinds of stuck work are handled, both detected purely from persisted state so
    it works after any restart of workers, broker or API:

    * in-flight jobs (PROCESSING/OCR/EXTRACTING/VALIDATING) claimed longer ago than
      ``stuck_job_seconds`` -- the worker died;
    * QUEUED jobs not (re-)enqueued for ``queued_stuck_seconds`` -- the broker message
      was lost.

    Each is re-enqueued up to ``max_job_recoveries`` times, then failed with
    ``PROCESSING_TIMEOUT`` so an operator or user can reprocess it. Re-enqueueing is safe
    because ``process_document`` is idempotent.
    """
    now = utcnow()
    in_flight_cutoff = now - timedelta(seconds=settings().stuck_job_seconds)
    queued_cutoff = now - timedelta(seconds=settings().queued_stuck_seconds)
    requeue: list[str] = []
    failed: list[tuple[uuid.UUID, uuid.UUID]] = []

    with SessionLocal.begin() as db:
        jobs = db.scalars(
            select(ProcessingJob)
            .where(
                or_(
                    and_(
                        ProcessingJob.status.in_(ACTIVE_STATUSES),
                        func.coalesce(ProcessingJob.started_at, ProcessingJob.created_at) < in_flight_cutoff,
                    ),
                    and_(
                        ProcessingJob.status == JobStatus.queued,
                        func.coalesce(ProcessingJob.enqueued_at, ProcessingJob.created_at) < queued_cutoff,
                    ),
                )
            )
            .with_for_update(skip_locked=True)
        ).all()
        for job in jobs:
            if job.retry_count >= settings().max_job_recoveries:
                job.status = JobStatus.failed
                job.failure_code = "PROCESSING_TIMEOUT"
                job.failure_message = _failure_message("PROCESSING_TIMEOUT")
                job.completed_at = now
                failed.append((job.organization_id, job.document_id))
            else:
                job.status = JobStatus.queued
                job.retry_count += 1
                job.enqueued_at = now
                job.started_at = None
                job.failure_code = "PROCESSING_RETRY"
                job.failure_message = _failure_message("PROCESSING_RETRY")
                requeue.append(str(job.id))

    for job_id in requeue:
        try:
            process_document.delay(job_id)
        except Exception:
            logger.warning("could not re-enqueue recovered job", extra={"job_id": job_id})
    for organization_id, document_id in failed:
        _notify("failed", organization_id, document_id, time.perf_counter())
    JOBS_RECOVERED_TOTAL.labels("requeued").inc(len(requeue))
    JOBS_RECOVERED_TOTAL.labels("failed").inc(len(failed))
    return len(requeue) + len(failed)


@celery_app.task
def purge_expired_data() -> dict[str, int]:
    """Apply the retention policy (see docs/DATA_RETENTION.md)."""
    from app.retention import purge_expired

    return purge_expired()


@celery_app.task(
    bind=True, max_retries=4, default_retry_delay=30, autoretry_for=(ConnectionError,)
)
def dispatch_webhooks(self, organization_id: str, event: str, payload: dict[str, object]) -> int:
    """Fan the event out to every active webhook in the organization subscribed
    to it. Each delivery attempt is logged to WebhookDelivery for operator
    visibility; failures do not raise (a tenant's broken endpoint must never
    poison the queue or affect other tenants)."""
    delivered = 0
    with SessionLocal.begin() as db:
        webhooks = db.scalars(
            select(Webhook).where(
                Webhook.organization_id == uuid.UUID(organization_id), Webhook.active.is_(True)
            )
        ).all()
        for webhook in webhooks:
            if event not in (webhook.events or []):
                continue
            success, status_code, error = deliver_webhook_request(webhook.id, webhook.url, event, payload)
            WEBHOOK_DELIVERIES_TOTAL.labels(str(success)).inc()
            db.add(
                WebhookDelivery(
                    webhook_id=webhook.id,
                    event=event,
                    status_code=status_code,
                    success=success,
                    error=error,
                    attempt=self.request.retries + 1,
                )
            )
            webhook.last_delivery_at = utcnow()
            webhook.last_delivery_status = status_code
            if success:
                delivered += 1
            else:
                logger.warning(
                    "webhook delivery failed",
                    extra={"webhook_id": str(webhook.id), "event": event, "error": error},
                )
    return delivered

