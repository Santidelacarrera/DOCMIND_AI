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
| ContextRAG outbox (`contextrag_events`) | database | identifiers and status only, no text. Kept after delivery as the audit of what was sent or retired |
| Documents in ContextRAG_Engine | ContextRAG (separate service) | retired when the document is deleted, expired or purged, and verified by listing; see `docs/CONTEXTRAG_INTEGRATION.md` |
| Exports | generated on demand, **not stored** | n/a |
| Text sent to the LLM provider | provider side | governed by your provider agreement; the request contains only the (size-bounded) document text |

## Clocks

* **User delete** → the stored objects and the page text are deleted at once; the document disappears from every API view; no further processing result is ever written for it (a job running at that moment is cancelled with `DOCUMENT_DELETED`).
* **`RETENTION_DELETED_DAYS`** (default `30`, `0` = next run) → the `purge-expired-data` Celery beat task (hourly) hard-deletes the document row and every record derived from it, including extraction values and manual corrections.
* **`RETENTION_DOCUMENT_DAYS`** (default `0` = disabled) → live documents older than this are soft-deleted automatically (`document.expire`) and then follow the clock above.

Storage failures during a purge are counted (`docmind_retention_purged_total`, task result `storage_failures`) and never block the database purge.

### Objetos huérfanos (reconciliación)

Un objeto puede quedar sin fila de base de datos si un fallo cae entre escribirlo y confirmar la fila, o si su borrado falla durante una purga. La tarea diaria `reconcile-storage` (y `python -m app.reconcile [--delete]`) los detecta:

* **Por defecto solo informa**: cuenta objetos, huérfanos, claves no reconocidas y filas sin objeto.
* Solo considera huérfanos las claves con forma `<org-uuid>/<uuid>.(pdf|docx)`. Cualquier otra clave se informa y **nunca** se borra.
* Ignora objetos más recientes que `STORAGE_RECONCILE_GRACE_HOURS` (24 h por defecto): una subida puede estar confirmándose.
* Borra solo si `STORAGE_RECONCILE_DELETE=true`, y se niega si el número de huérfanos supera `STORAGE_RECONCILE_MAX_DELETE_FRACTION` (50 %): ese patrón indica una base de datos equivocada, no huérfanos reales.

Prueba: `backend/tests/test_reconcile.py`. Recomendación: activar el borrado solo después de revisar un informe de reconciliación real.

## Storage reconciliation

Objects of a deleted document are removed when it is deleted, so `rows_without_object` counts only live documents: a deleted document whose object is gone is correct, not an inconsistency (verified: `tests/test_reconcile.py::test_a_deleted_document_is_not_reported_as_a_missing_object`). A live document whose object is missing is reported.


`reconcile-storage` runs daily and reports objects without a database row. `python -m app.reconcile --dry-run --decisions file.json` lists the decision for every object (fingerprint, action, reason) and deletes nothing. Deletion requires `STORAGE_RECONCILE_DELETE=true` and stays below `STORAGE_RECONCILE_MAX_DELETE_FRACTION`.

## Operator checklist

* Configure the bucket's own lifecycle/versioning so deleted objects do not survive in old versions or replicas.
* Backups and logs are outside this application: set their retention to be no longer than the longest promise you make to customers.
* Before enabling an LLM provider, document the provider, region and DPA.
* Right to erasure for a whole organization: delete its documents (API), then run the purge with `RETENTION_DELETED_DAYS=0`.

---
Relacionado: [[empresa]]
