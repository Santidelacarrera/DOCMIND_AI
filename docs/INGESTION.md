# Secure document ingestion for vector indexing (`app/ingestion`)

> **Componente reutilizable de Nexus Security & AI Lab.** No depende de ningún cliente ni proyecto: recibe `tenant_id`, `document_id`, bytes y un nombre de archivo, y devuelve un informe; el almacén vectorial, el embedder y los detectores se inyectan. Para reutilizarlo en otro producto copia `app/ingestion/` y sus tests, y cablea `VectorStore`/`Embedder`; no arrastra datos, credenciales ni código de este proyecto.

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

## Word (.docx) and PDF, and answers with citations

* **Formats:** PDF and Word `.docx` (`docx.py`). `.doc`, `.docm` and macro-enabled files are refused. Word is read with the standard library + `defusedxml` (no Word/LibreOffice): macros, embedded OLE objects, remote templates, zip bombs (entry count, size, ratio), path traversal and XML entity attacks are rejected with stable codes; hidden text (`w:vanish`), tracked deletions and metadata are **not** extracted. Pages follow explicit page breaks / section breaks / `lastRenderedPageBreak`; with none, pages are *estimated* (3 000 characters) and flagged `page_basis="estimated"`.
* **Page-exact chunks:** a chunk never crosses a page, so each point carries `source_name`, `page` and `page_basis`.
* **Answers always cite** (`answering.py`, `POST /api/v1/ask`): extractive and local (no language model). Each answer has at least one `{document, document_id, page}` citation or is an explicit abstention (`abstained: true`, no citations); enforced in the constructor and tested. Deleted documents are never cited (checked against the database even while their vectors await the deletion outbox), suspicious chunks are never used, and the tenant comes from the authenticated membership.
* Demo and numbers: [DEMO.md](DEMO.md). Local-only operation and how to verify it: [LOCAL_PROCESSING.md](LOCAL_PROCESSING.md).

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

## Lifecycle integration (`app/vector_index.py`)

Everything below is inert unless `QDRANT_URL` is set.

| Event | What happens |
|---|---|
| Document processed (`COMPLETED`) | the worker enqueues `index_document`; the PDF is read through the tenant-prefix storage check and ingested. Reprocessing re-ingests and **replaces** the points. Outcome is stored on the document: `indexed_at`, or `index_error` with a stable code (`ACTIVE_CONTENT_REJECTED`, `PII_RESIDUAL_AFTER_MASKING`, …) |
| User deletes a document | in the **same transaction** as the soft delete, a row is written to the `vector_deletions` outbox; the API then kicks `process_vector_deletions` |
| Retention: expiry or hard purge | both write the outbox row too (idempotent), so vectors die with the document even if it was deleted by another path |
| `process_vector_deletions` (beat, every 60 s) | calls the **tenant-scoped** `delete_document(organization, document)`; success → `done_at`; failure → `attempts+1`, `last_error` (exception class only), exponential backoff 2 min … 1 h. A Qdrant outage delays a deletion, never loses it |
| Delete racing with indexing | after writing, the indexer re-checks the document; if it was deleted meanwhile it removes what it just wrote |

Metrics: `docmind_vector_deletions_total{outcome}`, `docmind_vector_indexing_total{outcome}` (remember these live in the worker process, see RELIABILITY.md). Alert on rows with `done_at IS NULL AND attempts > 5`. Erasing a whole organization = delete its documents, then run the purge with `RETENTION_DELETED_DAYS=0`; for belt and braces also `QdrantStore.tenant_filter(org)`-delete at the end.

## Names and addresses (`entities.py`)

`PII_DETECTORS=rules,spacy` (comma list; `rules` is the default).

* **`rules`** – deterministic and configurable. A name is recognised after a label (`Name:`, `Nombre:`, `Bill to:`, `Cliente:` … extendable with `RuleConfig.name_labels`), a title (`Dr.`, `Sra.`, `Don`) or a signing phrase (`firmado por`, `signed by`); an address by street word + number (ES: `Calle/Avenida/Plaza…`, EN: `… Street/Road/Avenue…`), postal code + city lines, UK postcodes and P.O. boxes. Organisations are deliberately **not** masked (legal-form suffixes `S.A., Ltd, GmbH…` and words such as `Hotel`, `Hospital`). `PII_EXTRA_TERMS` / `PII_TERMS_FILE` add a **gazetteer** (customer names, employees, project code names) matched case-insensitively on word boundaries.
* **`spacy`** – statistical NER for running text (PERSON; places opt-in). Probabilistic: it can miss and over-mask, and its verdict on already-masked text proves nothing, so it takes no part in the residual check — the deterministic detectors do.
* A configured detector that cannot start (library or model missing, unknown name) is a **startup error**, never silently skipped.

Measured on the annotated set in `tests/test_entities.py` (19 sentences with names/addresses, 11 negatives such as organisations and order numbers): **0 of 19 leaked, 0 false positives** with the rule detector. That set is small, was written by the same people who wrote the rules, and covers ES/EN only: treat it as a regression guard, not as a recall estimate. Measure on your own documents before claiming anonymisation. Not covered: bare names in running text (use `spacy`), non-Latin scripts, addresses without a street word, free-text locations ("near the old mill").

## Embeddings and data egress (`embeddings.py`)

| `EMBEDDING_PROVIDER` | Where text goes | Rules |
|---|---|---|
| `local` (default) | nowhere – ONNX on this host (`pip install '.[embeddings]'`) | `EMBEDDING_LOCAL_FILES_ONLY=true` forbids runtime downloads: provision `EMBEDDING_MODEL` in the image or `EMBEDDING_CACHE_DIR`; a missing model is a startup error |
| `openai` (OpenAI-compatible) | **leaves the host** | refused unless `EMBEDDING_ALLOW_EXTERNAL=true`, the URL is `https` and its host is in `EMBEDDING_ALLOWED_HOSTS`; checked when the embedder is built and again when the pipeline is constructed, i.e. before any document is read. Look-alike hosts (`api.openai.com.evil.test`) and plain `http` are denied |
| `hashing` | nowhere | non-semantic, **development only** – rejected in staging/production |

Only masked, verified text reaches `embed()`; search questions are masked the same way (`IngestionPipeline.query_vector`), so a query naming a person cannot leak that name to a hosted API. Every point records `embedding_model`, and a collection refuses vectors of a different dimension (`EMBEDDING_DIMENSION_MISMATCH`): changing model means re-indexing into a new collection. Staging/production refuse to start with an insecure vector setup (`QDRANT_URL` not https, no API key, `hashing`, or `openai` without the egress switches). Whether masked text may go to a hosted embedder at all is a contract/DPA decision: the code only makes it explicit and opt-in.

## What is *not* done (be explicit before relying on it)

* **Anonymisation is not claimed.** Structured PII and context-triggered names/addresses are masked; anything else a person could be re-identified by (rare job titles, free-text locations, combinations of facts) is not. Pseudonymised text (with `PII_MASKING_KEY`) remains personal data under GDPR.
* **The local embedding model was not exercised end to end here**: the sandbox cannot download model weights. The adapter, its fail-closed behaviour with a missing model, dimension probing and the egress path are tested with injected models; run one smoke test with your real model before go-live.
* **Qdrant itself**: use a server with API key + TLS. Per-tenant payload filters are an application-level boundary — for hard isolation use a collection or cluster per tenant. The in-memory client used in tests ignores payload indexes.
* **The injection heuristic is a signal, not a defence**; the real controls are masking, tenant filters, no tools for the model, and validated outputs (see SECURITY.md).

---
Relacionado: [[empresa]]
