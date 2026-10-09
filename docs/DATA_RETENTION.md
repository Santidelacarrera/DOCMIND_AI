# Data retention and deletion

The application enforces the technical side of a retention policy; **the durations are an operator decision** (legal basis, contracts, DPA). Defaults are conservative and configurable.

## What is stored, and where

| Data | Location | Removed |
|---|---|---|
| Original PDF (and every stored version) | private object storage, key `<organization_id>/<uuid>.pdf` | on user delete (immediately) · on automatic expiry · hard-deleted again by the purge |
| Extracted / OCR page text (`document_pages`) | database | on user delete (immediately) · expiry · purge |
| Extraction runs, fields, **manual corrections**, jobs, steps | database | by the purge, `RETENTION_DELETED_DAYS` after the user deleted the document |
| Usage records (billing totals) | database | kept; the link to the document is removed by the purge |
| Audit log | database | kept (identifiers only, never content). Records `document.delete`, `document.expire`, `document.purge` |
| Webhook delivery log | database | cascades with the webhook |
| Exports | generated on demand, **not stored** | n/a |
| Text sent to the LLM provider | provider side | governed by your provider agreement; the request contains only the (size-bounded) document text |

## Clocks

* **User delete** → the stored objects and the page text are deleted at once; the document disappears from every API view; no further processing result is ever written for it (a job running at that moment is cancelled with `DOCUMENT_DELETED`).
* **`RETENTION_DELETED_DAYS`** (default `30`, `0` = next run) → the `purge-expired-data` Celery beat task (hourly) hard-deletes the document row and every record derived from it, including extraction values and manual corrections.
* **`RETENTION_DOCUMENT_DAYS`** (default `0` = disabled) → live documents older than this are soft-deleted automatically (`document.expire`) and then follow the clock above.

Storage failures during a purge are counted (`docmind_retention_purged_total`, task result `storage_failures`) and never block the database purge; reconcile orphaned objects with a bucket lifecycle rule as a backstop.

## Operator checklist

* Configure the bucket's own lifecycle/versioning so deleted objects do not survive in old versions or replicas.
* Backups and logs are outside this application: set their retention to be no longer than the longest promise you make to customers.
* Before enabling an LLM provider, document the provider, region and DPA.
* Right to erasure for a whole organization: delete its documents (API), then run the purge with `RETENTION_DELETED_DAYS=0`.
