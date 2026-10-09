# Operations and retention

## Required scheduling and monitoring

The `scheduler` compose service runs Celery beat and triggers `app.worker.recover_stuck_jobs` every 60 seconds (run exactly one instance, or use the platform scheduler instead). Alert on API 5xx, worker availability, queue depth, retries, failed jobs, stuck-job recoveries, database/Redis connection errors, antivirus unavailability and LLM latency/errors. Export container logs to the platform's structured logging system; never include request bodies, documents, prompts, tokens, passwords, JWTs or API keys.

## Job recovery

See [RELIABILITY.md](RELIABILITY.md) for the job state machine, failure codes and recovery rules. Watch `docmind_jobs_recovered_total` and the share of `FAILED` jobs by `failure_code` (`GET /documents/{id}/status`).

## Retention

The application purges deleted documents and (optionally) expires old ones: see [DATA_RETENTION.md](DATA_RETENTION.md). Retention is an operator-supplied policy, not a legal assertion. Configure lifecycle/retention on private object storage for documents, OCR artifacts, exports and versions; set log and backup retention at the platform. Before enabling an LLM provider, document the provider, region, DPA and the fact that extracted document text is transmitted for extraction. Database records and storage objects must be deleted/reconciled together by an operator-approved retention job.

## Redis/Celery guarantees

The worker uses late acknowledgements, rejects tasks lost with a worker, one-task prefetch, bounded retries and task time limits. This is at-least-once delivery: idempotency is enforced by an atomic database claim, ownership fencing and a unique result per job (see RELIABILITY.md). For restart resilience, use managed Redis with persistence, HA and monitoring; Redis alone is not a durable workflow system or dead-letter queue.
