"""Secure document ingestion for vector indexing.

    bytes ──▶ sanitize ──▶ extract text ──▶ normalise ──▶ mask PII ──▶ chunk(tenant) ──▶ verify ──▶ embed ──▶ Qdrant
              (pdf.py)     (pipeline.py)    (text.py)     (pii.py)     (chunking.py)    (pipeline.py)         (vectorstore.py)

Design rules, enforced in code and tests:

* **Fail closed.** A step that cannot prove its guarantee raises; nothing reaches the vector
  store on a doubt (active PDF content, residual PII after masking, a chunk without / with a
  different tenant).
* **Tenant is data, not convention.** ``tenant_id`` (the organization UUID) is a required,
  validated field of every chunk and of every point payload, indexed in Qdrant as the tenant
  key; reads and deletes cannot be expressed without it.
* **PII is masked before it is embedded or stored**, never after. Reports and logs carry
  counts and types, never values.
* **Retrieved text is untrusted.** Chunks that look like prompt injection are flagged in the
  payload so the retrieval layer can quarantine them.
"""
