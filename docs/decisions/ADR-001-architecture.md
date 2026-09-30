# ADR-001: Async, provider-based processing

**Decision:** FastAPI accepts and authorizes uploads; Celery performs processing. Storage, OCR, and LLM integrations use interfaces.

**Why:** PDF/OCR/model latency must not hold HTTP workers, and providers need replacement without domain changes. PostgreSQL is the durable source of truth; Redis is only queue infrastructure.
