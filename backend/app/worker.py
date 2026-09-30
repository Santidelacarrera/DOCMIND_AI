from datetime import timedelta
import ssl

from celery import Celery
from sqlalchemy import delete, select

from app.core import settings
from app.db import SessionLocal
from app.models import (
    Document,
    DocumentPage,
    ExtractionField,
    ExtractionRun,
    JobStatus,
    ProcessingJob,
    ProcessingStep,
    RunStatus,
    UsageRecord,
    utcnow,
)
from app.ocr import TesseractProvider, needs_ocr
from app.processing import extract_pdf_text, llm
from app.storage import storage


celery_app = Celery(
    "docmind",
    broker=settings().effective_redis_url,
    backend=settings().effective_redis_url,
)

celery_app.conf.update(
    broker_use_ssl={
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    },
    redis_backend_use_ssl={
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    },
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=settings().processing_timeout_seconds,
    task_soft_time_limit=max(1, settings().processing_timeout_seconds - 15),
)

MAX_RETRIES = 3


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
    import uuid

    retry_error: OSError | None = None

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

            result = llm().extract(
                text,
                {
                    "type": "object",
                    "properties": {
                        "fields": {
                            "type": "object",
                        }
                    },
                    "required": ["fields"],
                    "additionalProperties": False,
                },
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
                status=RunStatus.completed,
            )

            db.add(run)
            db.flush()

            for name, value in result.get("fields", {}).items():
                db.add(
                    ExtractionField(
                        extraction_run_id=run.id,
                        name=name,
                        original_value=value,
                        value=value,
                        confidence=None,
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

    if retry_error:
        raise self.retry(
            exc=retry_error,
            countdown=2 ** self.request.retries,
            max_retries=MAX_RETRIES,
        )

