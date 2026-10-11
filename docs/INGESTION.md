# Secure document ingestion for vector indexing (`app/ingestion`)

Turns an untrusted PDF into **masked, tenant-bound vectors** in Qdrant. It is a library: call `IngestionPipeline.ingest(tenant_id=…, document_id=…, content=…)` from a Celery task, an API handler or `ContextRAG_Engine`'s loader.

```
bytes ─▶ pdf.sanitize ─▶ extract (text layer / OCR) ─▶ text.normalise ─▶ pii.mask ─▶ chunk(tenant) ─▶ verify ─▶ embed ─▶ Qdrant
```

| Module | Responsibility |
|---|---|
| `pdf.py` | `inspect_pdf` / `sanitize_pdf`: detect and drop document JavaScript, launch/submit actions, embedded files and file-attachment annotations, XFA, annotations, XMP and the whole Info dictionary; the PDF is **rebuilt from page content only**. Active content is *rejected* by default (`INGEST_REJECT_ACTIVE_CONTENT`), otherwise stripped |
| `text.py` | NFKC normalisation (folds full-width digits), removal of zero-width / bidi-control / control characters, and a **prompt-injection heuristic** used to flag (never to trust) |
| `pii.py` | Detection by pattern **plus checksum** (Luhn cards, IBAN mod-97, Spanish DNI/NIE letter, SSN rules, IPv4 ranges), e-mail, phone, credentials/tokens. Masking is irreversible (no mapping kept): `[EMAIL_1]` per document, or keyed pseudonyms `[EMAIL:3fa9c1d2]` (HMAC, per tenant) when `PII_MASKING_KEY` is set. `EntityDetector` is the plug-in point for NER |
| `chunking.py` | Paragraph-aware windows (`INGEST_CHUNK_CHARS`, `INGEST_CHUNK_OVERLAP`) with page ranges. `Chunk` **cannot exist without a valid `tenant_id`** (organization UUID); its point id is `uuid5(tenant, document, index, content hash)` → re-ingestion is idempotent and ids never collide across tenants |
| `vectorstore.py` | Qdrant adapter. One shared collection, `tenant_id` payload indexed with `is_tenant=True` (Qdrant's multitenancy layout). `search`, `delete_document` and `upsert` **require** a tenant and always add the tenant condition; there is no unfiltered variant |
| `pipeline.py` | Orchestration and the fail-closed checks; `HashingEmbedder` (non-semantic, tests/dev only) and the `Embedder` protocol |

## Guarantees (each has a test in `tests/test_ingestion.py`)

1. **Nothing is written unless every step passed.** Active content, an invalid tenant, a malformed/encrypted PDF, empty text, or PII still detectable *after* masking all raise `IngestionRefused` / `TenantError` before the store is touched (the collection is not even created).
2. **PII is masked before chunking, embedding and storage**, so no chunk boundary can split an identifier and no vector is computed from raw PII. Obfuscation (zero-width characters inside an e-mail, full-width digits in a card number) does not evade detection. Checksums keep amounts, dates, invoice numbers and invalid IBAN/card/DNI lookalikes unmasked. Reports and logs contain counts and kinds, never values.
3. **Metadata cannot leak.** Author, producer, title, XMP, attachments and JavaScript are absent from the sanitised file and from every payload.
4. **Tenants are isolated by construction.** Same query, same topic, two tenants → each sees only its own points; an unknown tenant sees nothing; deleting with the wrong tenant removes nothing; a batch mixing tenants is refused.
5. **Retrieved text is treated as untrusted.** Chunks matching the injection heuristic get `suspicious: true` and are excluded from `search` unless `include_suspicious=True`.

## Payload contract for `ContextRAG_Engine`

```json
{ "tenant_id": "<org uuid>", "document_id": "<uuid>", "chunk_index": 0, "page_start": 1, "page_end": 2,
  "text": "<masked text>", "content_hash": "<sha256>", "pii": {"EMAIL": 1}, "suspicious": false, "schema_version": 1 }
```

Always query through `QdrantStore.search(tenant_id, …)` / `QdrantStore.tenant_filter(tenant_id, …)`. The tenant must come from the authenticated session, never from the request body.

## Use

```python
from qdrant_client import QdrantClient
from app.core import settings
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.vectorstore import QdrantStore

store = QdrantStore(QdrantClient(url="https://qdrant.internal:6333", api_key=…), settings().qdrant_collection)
pipeline = IngestionPipeline.from_settings(store, my_embedder)       # my_embedder: .dimension, .embed(list[str])
report = pipeline.ingest(tenant_id=str(org_id), document_id=str(doc_id), content=pdf_bytes)
```

Install the client with `pip install '.[qdrant]'`. Settings: `PII_MASKING_KEY`, `QDRANT_COLLECTION`, `INGEST_CHUNK_CHARS`, `INGEST_CHUNK_OVERLAP`, `INGEST_REJECT_ACTIVE_CONTENT`.

## What is *not* done (be explicit before relying on it)

* **Names, postal addresses and organisations are not detected** by the built-in rules (no reliable pattern exists). Add an NER detector via `masker_extra` (Presidio, spaCy, a hosted service) and measure its recall on your documents. Until then, treat the output as *structured-PII-masked*, not *anonymised*.
* **Pseudonymised text is still personal data** under GDPR when a key exists; with no key (`PII_MASKING_KEY` unset) placeholders are not reversible by anyone.
* **Not wired in automatically.** The pipeline is not called from `worker.process_document`, and deleting a document / running the retention purge does not yet delete its vectors. When you wire it: call `store.delete_document(tenant, document)` from `delete_document` and `retention.hard_delete_document`, and from the org-erasure procedure.
* **Embedding** uses a placeholder (`HashingEmbedder`); choose a model and decide whether masked text may be sent to a hosted embedding API.
* **The injection heuristic is a signal, not a defence**; the real controls are masking, tenant filters, no tools for the model, and validated outputs (see SECURITY.md).
* **Qdrant itself**: use a server with API key + TLS, and per-tenant payload filters are an application-level boundary — for hard isolation requirements use collection-per-tenant or a separate cluster. The in-memory client used in tests ignores payload indexes.
