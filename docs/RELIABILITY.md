# Reliable processing

## Job lifecycle (all state is in the database)

```
QUEUED ──claim──▶ PROCESSING ─▶ OCR_PROCESSING ─▶ EXTRACTING ─▶ VALIDATING ─▶ COMPLETED
   ▲                  │                                                 
   └── transient ─────┤── permanent error ─▶ FAILED (stable failure_code)
   └── recovery ──────┘── document deleted ─▶ CANCELLED
```

* **Claim** is one atomic `UPDATE … WHERE status=QUEUED OR (in-flight AND started_at older than the hard time limit)`; exactly one of N concurrent deliveries wins, on every database. The winner's `started_at` is its ownership token: results are committed only if it still matches (fencing), so a worker that was taken over can never overwrite the new owner.
* **Commit** of a result is one transaction: pages, extraction run, fields, usage, steps, `COMPLETED`. `extraction_runs.job_id` is unique, so even a bug cannot store two results for one job.
* Status is committed at every stage (`/documents/{id}/status` shows `PROCESSING → OCR_PROCESSING → …`, `attempt`, `retry_count`, `started_at`, `completed_at`, `failure_code`).

## Retries, timeouts, stuck jobs

| Situation | Behaviour | Code |
|---|---|---|
| Storage / LLM transient (I/O error, 5xx, rate limit, connection) | job back to `QUEUED`, `retry_count+1`, exponential backoff, up to 3 retries | `PROCESSING_RETRY` → then `STORAGE_UNAVAILABLE` / `LLM_UNAVAILABLE` |
| Permanent (corrupt/encrypted/too long PDF, missing object, request refused, OCR failure, invalid LLM answer) | `FAILED` immediately, no retry | `DOCUMENT_INVALID`, `PDF_ENCRYPTED`, `PDF_PAGE_LIMIT_EXCEEDED`, `STORAGE_OBJECT_MISSING`, `OCR_FAILED`, `LLM_REQUEST_FAILED`, `LLM_SCHEMA_INVALID` |
| Soft time limit (`PROCESSING_TIMEOUT_SECONDS − 15 s`) | `FAILED` | `PROCESSING_TIMEOUT` |
| Worker dies mid-job | after `STUCK_JOB_SECONDS` the recovery task re-enqueues it | – |
| Broker message lost (Redis flushed, crash between commit and enqueue, broker down at upload) | after `QUEUED_STUCK_SECONDS` the recovery task re-enqueues it | – |
| Recovery budget (`MAX_JOB_RECOVERIES`) spent | `FAILED`, `document.failed` webhook | `PROCESSING_TIMEOUT` |
| Unexpected exception | `FAILED`; raw error only in logs | `PROCESSING_FAILED` |

`recover_stuck_jobs` runs every 60 s from Celery beat (exactly one instance). Failed jobs are listed by status/`failure_code` and re-run with `POST /documents/{id}/process`, which is idempotent (an active job is returned instead of a new one). Metric: `docmind_jobs_recovered_total{outcome}`.

## Duplicates

* **Re-delivered task**: terminal jobs are ignored; a job owned by a live worker is skipped; the unique `job_id` on runs is the last line of defence.
* **Re-sent upload**: same bytes in the same project → `409 {"detail":"DUPLICATE_DOCUMENT","document_id": …}` so a retrying client learns the existing id. The check is also enforced atomically by a unique partial index on live `(project_id, checksum)`; a concurrent race resolves to one document and the loser's stored object is removed.
* **Re-sent process request**: returns the active job (`reused: true`).

## Input limits

Size (`MAX_UPLOAD_BYTES`, enforced while streaming, not only via `Content-Length`), type (`.pdf` + `%PDF-` magic), structure (must parse, ≥ 1 page, ≤ `MAX_PDF_PAGES`, not encrypted), optional ClamAV scan, per-plan page quota. Rejections happen before anything is stored and use stable codes.

## Tests

`tests/test_reliability.py` (state machine, recovery, idempotency, limits) and `tests/test_celery_redis.py` (a real `redis-server` and a real Celery worker: completion, six duplicate messages → one result, retry through the broker, lost message recovered from the database). The whole suite also passes on PostgreSQL: `TEST_DATABASE_URL=postgresql+psycopg://… pytest`.

Not covered: a hard `kill -9` of a prefork worker mid-task. Its recovery path (job stuck in-flight → re-enqueued after `STUCK_JOB_SECONDS`) is tested from the database state it leaves behind.
