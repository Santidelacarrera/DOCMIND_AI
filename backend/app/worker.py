import logging
import ssl
import time
import uuid
from datetime import timedelta

from celery import Celery
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.core import settings
from app.db import SessionLocal
from app.logging_config import configure_logging
from app.metrics import (
    DOCUMENTS_PROCESSED_TOTAL,
    EXTRACTION_REQUIRES_REVIEW_TOTAL,
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
    utcnow,
)
from app.ocr import TesseractProvider, needs_ocr
from app.processing import extract_pdf_text, extraction_envelope, llm
from app.storage import storage
from app.telemetry import configure_worker_tracing
from app.validation import clamp_confidence, validate_fields
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
    task_time_limit=settings().processing_timeout_seconds,
    task_soft_time_limit=max(1, settings().processing_timeout_seconds - 15),
)

# `celery -A app.worker.celery_app beat` (the `scheduler` compose service) runs this.
celery_app.conf.beat_schedule = {
    "recover-stuck-jobs": {"task": "app.worker.recover_stuck_jobs", "schedule": 60.0},
}

MAX_RETRIES = 3


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


@celery_app.task
def recover_stuck_jobs() -> int:
    """Mark jobs exceeding the configured processing SLA as terminal failures.

    Schedule this task (for example via Celery Beat) at least once per minute in
    staging/production. It intentionally does not requeue blindly: operators can
    inspect/reprocess through the existing idempotent endpoint.
    """
    cutoff = utcnow() - timedelta(seconds=settings().stuck_job_seconds)
    active = [
        JobStatus.processing,
        JobStatus.ocr_processing,
        JobStatus.extracting,
        JobStatus.validating,
    ]

    with SessionLocal.begin() as db:
        jobs = db.scalars(
            select(ProcessingJob).where(
                ProcessingJob.status.in_(active),
                ProcessingJob.created_at < cutoff,
            )
        ).all()

        for job in jobs:
            job.status = JobStatus.failed
            job.failure_code = "PROCESSING_TIMEOUT"
            job.failure_message = "Processing exceeded the configured time limit."
            job.completed_at = utcnow()

        return len(jobs)


@celery_app.task(bind=True)
def process_document(self, job_id: str) -> None:
    retry_error: OSError | None = None
    started_at = time.perf_counter()
    # Captured inside the transaction so the webhook/metrics dispatch below (which
    # must run after commit, outside the session) knows what happened.
    outcome: dict[str, object] = {"status": None, "organization_id": None, "document_id": None}

    with SessionLocal.begin() as db:
        job = db.scalar(
            select(ProcessingJob)
            .where(ProcessingJob.id == uuid.UUID(job_id))
            .with_for_update()
        )

        if not job or job.status == JobStatus.completed:
            return

        job.status = JobStatus.processing
        db.add(
            ProcessingStep(
                job_id=job.id,
                name="text_extraction",
                status="STARTED",
            )
        )

        document = db.get(Document, job.document_id)

        if not document:
            raise RuntimeError("document missing")

        try:
            content = storage.get(document.storage_key)
            text, pages = extract_pdf_text(content)
            document.page_count = pages

            ocr_pages: list[str] | None = None

            if needs_ocr(text, pages):
                job.status = JobStatus.ocr_processing

                db.add(
                    ProcessingStep(
                        job_id=job.id,
                        name="ocr",
                        status="STARTED",
                    )
                )

                ocr_pages = TesseractProvider().extract_pages(content)
                text = "\n".join(ocr_pages)

                db.add(
                    ProcessingStep(
                        job_id=job.id,
                        name="ocr",
                        status="COMPLETED",
                        completed_at=utcnow(),
                    )
                )

            job.status = JobStatus.extracting

            db.add(
                ProcessingStep(
                    job_id=job.id,
                    name="text_extraction",
                    status="COMPLETED",
                    completed_at=utcnow(),
                )
            )

            # pypdf exposes per-page text; persist source material separately
            # from model output.
            from io import BytesIO

            from pypdf import PdfReader

            reader = PdfReader(BytesIO(content))

            # A deliberate reprocess replaces the cached page text rather than
            # violating the per-document/page uniqueness invariant.
            db.execute(
                delete(DocumentPage).where(
                    DocumentPage.document_id == document.id
                )
            )

            for number, page in enumerate(reader.pages, start=1):
                page_text = (
                    ocr_pages[number - 1]
                    if ocr_pages
                    else page.extract_text() or ""
                )

                db.add(
                    DocumentPage(
                        document_id=document.id,
                        page_number=number,
                        text=page_text,
                        ocr_used=ocr_pages is not None,
                    )
                )

            db.add(
                ProcessingStep(
                    job_id=job.id,
                    name="llm_extraction",
                    status="STARTED",
                )
            )

            schema_version = resolve_schema_version(db, job, document)
            result = llm().extract(
                text,
                extraction_envelope(schema_version.json_schema if schema_version else None),
                prompt_instructions=schema_version.prompt_instructions if schema_version else None,
            )

            fields = result.get("fields", {})
            confidence_by_field = {
                name: clamp_confidence(value)
                for name, value in (result.get("confidence") or {}).items()
            }
            validation_issues = validate_fields(
                schema_version.json_schema if schema_version else None,
                fields,
                confidence_by_field,
            )

            run = ExtractionRun(
                organization_id=job.organization_id,
                document_id=document.id,
                provider=settings().llm_provider,
                model=(
                    settings().openai_model
                    if settings().llm_provider == "openai"
                    else None
                ),
                result=result,
                schema_version_id=schema_version.id if schema_version else None,
                status=RunStatus.completed,
                validation_issues=validation_issues,
                requires_review=bool(validation_issues),
            )

            db.add(run)
            db.flush()

            if validation_issues:
                EXTRACTION_REQUIRES_REVIEW_TOTAL.inc()
                logger.info(
                    "extraction requires review",
                    extra={
                        "document_id": str(document.id),
                        "run_id": str(run.id),
                        "issue_count": len(validation_issues),
                    },
                )

            for name, value in fields.items():
                db.add(
                    ExtractionField(
                        extraction_run_id=run.id,
                        name=name,
                        original_value=value,
                        value=value,
                        confidence=confidence_by_field.get(name),
                    )
                )

            db.add(
                UsageRecord(
                    organization_id=job.organization_id,
                    metric="documents_processed",
                    quantity=1,
                    document_id=document.id,
                )
            )

            db.add(
                UsageRecord(
                    organization_id=job.organization_id,
                    metric="pages_processed",
                    quantity=pages,
                    document_id=document.id,
                )
            )

            db.add(
                ProcessingStep(
                    job_id=job.id,
                    name="llm_extraction",
                    status="COMPLETED",
                    completed_at=utcnow(),
                )
            )

            job.status = JobStatus.completed
            job.completed_at = utcnow()
            outcome.update(
                status="completed", organization_id=str(job.organization_id), document_id=str(document.id)
            )

        except OSError as exc:
            # Missing storage objects are permanent. Other I/O failures can be
            # transient (network-mounted storage, provider outage) and are retried.
            retryable = (
                not isinstance(exc, FileNotFoundError)
                and self.request.retries < MAX_RETRIES
            )

            job.status = JobStatus.queued if retryable else JobStatus.failed

            job.failure_code = (
                "PROCESSING_RETRY"
                if retryable
                else "PROCESSING_FAILED"
            )

            job.failure_message = (
                "Processing temporarily unavailable."
                if retryable
                else "Processing failed. Check operational logs."
            )

            db.flush()

            for step in db.scalars(
                select(ProcessingStep).where(
                    ProcessingStep.job_id == job.id,
                    ProcessingStep.status == "STARTED",
                )
            ):
                step.status = "FAILED"
                step.error = job.failure_code
                step.completed_at = utcnow()

            if retryable:
                retry_error = exc
            elif document is not None:
                outcome.update(
                    status="failed", organization_id=str(job.organization_id), document_id=str(document.id)
                )

        except Exception:
            # The error state must commit with the job. Re-raising inside this
            # transaction would roll it back and leave a misleading PROCESSING state.
            job.status = JobStatus.failed
            job.failure_code = "PROCESSING_FAILED"

            # Provider and filesystem errors can include sensitive request context;
            # persist a stable operational code instead of raw exception details.
            job.failure_message = (
                "Processing failed. Check operational logs."
            )

            db.flush()

            for step in db.scalars(
                select(ProcessingStep).where(
                    ProcessingStep.job_id == job.id,
                    ProcessingStep.status == "STARTED",
                )
            ):
                step.status = "FAILED"
                step.error = "PROCESSING_FAILED"
                step.completed_at = utcnow()
            outcome.update(
                status="failed", organization_id=str(job.organization_id), document_id=str(document.id)
            )

    if outcome["status"] in ("completed", "failed"):
        PROCESSING_DURATION_SECONDS.observe(time.perf_counter() - started_at)
        DOCUMENTS_PROCESSED_TOTAL.labels(outcome["status"]).inc()
        event = "document.completed" if outcome["status"] == "completed" else "document.failed"
        try:
            # The document's own terminal state is already committed; a broker
            # hiccup here must never turn into a failed/retried processing job.
            dispatch_webhooks.delay(
                str(outcome["organization_id"]), event, {"document_id": outcome["document_id"]}
            )
        except Exception:
            logger.warning("could not enqueue webhook dispatch", extra={"event": event})

    if retry_error:
        raise self.retry(
            exc=retry_error,
            countdown=2 ** self.request.retries,
            max_retries=MAX_RETRIES,
        )


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

