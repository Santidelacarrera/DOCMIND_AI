# Operations and retention

## Required scheduling and monitoring

The `scheduler` compose service runs Celery beat and triggers `app.worker.recover_stuck_jobs` every 60 seconds (run exactly one instance, or use the platform scheduler instead). Alert on API 5xx, worker availability, queue depth, retries, failed jobs, stuck-job recoveries, database/Redis connection errors, antivirus unavailability and LLM latency/errors. Export container logs to the platform's structured logging system; never include request bodies, documents, prompts, tokens, passwords, JWTs or API keys.

## Retention

Retention is an operator-supplied policy, not a legal assertion. Configure lifecycle/retention on private object storage for documents, OCR artifacts, exports and versions; set log and backup retention at the platform. Before enabling an LLM provider, document the provider, region, DPA and the fact that extracted document text is transmitted for extraction. Database records and storage objects must be deleted/reconciled together by an operator-approved retention job.

## Redis/Celery guarantees

The worker uses late acknowledgements, rejects tasks lost with a worker, one-task prefetch, bounded retries and task time limits. This is at-least-once delivery: idempotency is enforced by database job/document locking. For restart resilience, use managed Redis with persistence, HA and monitoring; Redis alone is not a durable workflow system or dead-letter queue.
