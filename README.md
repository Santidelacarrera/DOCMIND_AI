# DocMind AI

DocMind AI is a multi-tenant document intelligence platform. You upload documents, extract structured fields against your own JSON schemas, review and export the results, and ask questions with answers tied to exact document names, pages, and passages.

Built by **Nexus Security & AI Lab**. Everything runs locally with zero external API costs by default.

[![CI](https://github.com/Santidelacarrera/DOCMIND_AI/actions/workflows/ci.yml/badge.svg)](https://github.com/Santidelacarrera/DOCMIND_AI/actions/workflows/ci.yml)
[![CodeQL](https://github.com/Santidelacarrera/DOCMIND_AI/actions/workflows/codeql.yml/badge.svg)](https://github.com/Santidelacarrera/DOCMIND_AI/actions/workflows/codeql.yml)
[![License: Proprietary](https://img.shields.io/badge/License-Proprietary-red.svg)](LICENSE)

---

## Core capabilities

- **Multi-format ingestion & OCR**: Accepts PDF, Word (`.docx`), TXT, Markdown, CSV, Excel (`.xlsx`), PowerPoint (`.pptx`), and images. Text is extracted directly when available, with Tesseract OCR as a fallback for scans. Uploads are validated by magic bytes, size, page limits, and optional ClamAV scanning, then deduplicated by SHA-256.
- **Schema-driven extraction & human review**: Define JSON Schemas per organization or project (including built-in templates such as Chilean DTE `dte_cl`), pin versions on upload, validate fields against confidence thresholds, edit values side by side with the document in the browser, and export to JSON, CSV, or XLSX (protected against spreadsheet formula injection).
- **Hybrid RAG & source-cited answers**: Combines Qdrant vector search with BM25 lexical search (`RETRIEVAL_MODE=hybrid`). Supports verbatim extractive answers and verified generative answers (`ASK_MODE=generative`), abstains when evidence is missing, flags conflicting numbers or dates across documents, and keeps encrypted per-organization conversation history (`/api/v1/conversations`).
- **Document intelligence**: Automatic classification and tagging, cited entity extraction, document and project summaries, and multi-document comparison and contradiction reports exportable to JSON, CSV, or XLSX.
- **Multi-tenant SaaS & billing**: Workspaces, projects, RBAC (`owner`, `admin`, `member`, `viewer`), PostgreSQL Row-Level Security (RLS), scoped API keys (`dm_live_…`), configurable 5-tier SaaS plans (`PLANS_CONFIG_JSON`), payment gateway adapters (Stripe, Mercado Pago, Transbank, and Sandbox), usage metering, email invitations, and project-scoped signed webhooks.

---

## Quick start (Docker Compose)

Requires Docker Engine with Compose v2.

```bash
git clone https://github.com/Santidelacarrera/DOCMIND_AI.git
cd DOCMIND_AI
cp .env.example .env          # safe local defaults (mock LLM, local storage)
docker compose up --build     # runs Alembic migrations automatically
```

| Service | URL |
|---|---|
| Web app | <http://localhost:3000> |
| API & Swagger UI (development only) | <http://localhost:8000/docs> |
| Health / readiness | <http://localhost:8000/health> · <http://localhost:8000/ready> |

1. Open the web app and create an account (password of at least 12 characters).
2. Go to **Documents**, create a project, and upload a file.
3. Once the worker finishes processing, open the document to inspect or edit extracted fields and export them.
4. Go to **Ask** to query your documents with page-level citations, or **Intelligence** to compare documents.

> The default `.env.example` uses `LLM_PROVIDER=mock` and the offline `hashing` embedder so everything works with no API keys or model downloads. For real extraction, set `LLM_PROVIDER=openai` (`OPENAI_API_KEY`) or `LLM_PROVIDER=gemini` (`GEMINI_API_KEY`). For semantic retrieval in staging or production, use `EMBEDDING_PROVIDER=local` (`pip install '.[embeddings]'`) or `openai`.

To smoke-test a running stack end to end:

```bash
python backend/scripts/staging_smoke.py --base-url http://localhost:8000 --ask
```

---

## Local setup without Docker

Requires Python 3.12+ (3.13 works). `tesseract-ocr` and `poppler-utils` are only needed for scanned PDFs.

```bash
git clone https://github.com/Santidelacarrera/DOCMIND_AI.git
cd DOCMIND_AI/backend
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

# Unit & API tests (SQLite + in-memory doubles, no external services required)
pytest --cov=app --cov-fail-under=75

# Local CLI demo (fictitious PDF + Word docs, 10/10 cited answers)
python -m app.ingestion.demo --output-dir demo_output

# Extraction quality benchmark over the reference corpus
python -m app.evaluation
```

Frontend local dev:

```bash
cd frontend
npm ci
npm run dev
```

---

## Architecture

```text
DOCMIND_AI/
├── backend/
│   ├── app/            # FastAPI routers, Celery worker, RAG & ingestion pipeline, billing, security
│   ├── alembic/        # PostgreSQL migrations (including RLS policies)
│   ├── scripts/        # Smoke tests, load benchmarks, model manifest & DB tools
│   └── tests/          # Unit, API, tenant-isolation, RLS, and opt-in live service tests
├── frontend/
│   ├── app/            # Next.js 16 / React 19 App Router pages
│   ├── components/     # Shared UI components
│   ├── lib/            # CSRF-aware API client
│   └── tests/e2e/      # Playwright end-to-end suite
├── deploy/             # Backup scripts and observability templates
├── docs/               # Architecture, security, API, operations, commercial & staging docs
├── docker-compose.yml
└── docker-compose.prod.yml
```

The FastAPI service (`backend/app/main.py`) is the single authorization boundary. All queries are scoped by `organization_id` with membership verified server-side and backed by PostgreSQL RLS. Background ingestion runs in Celery (`backend/app/worker.py`) with row-level job locking and stuck-job recovery (`recover_stuck_jobs`).

---

## Security highlights

- **Auth & sessions**: Argon2id password hashing, constant-time login handling, IP + account rate limiting via Redis (fails closed), `HttpOnly`/`SameSite` cookies with CSRF double-submit protection, JWT session-version revocation on logout, and SHA-256 hashed API keys with explicit scope allow-lists.
- **Data & exports**: AES-GCM encrypted conversations (with key rotation support via `CONVERSATION_PREVIOUS_ENCRYPTION_KEY`), RFC 6266 `Content-Disposition` headers, formula-neutralized CSV/XLSX exports, SSRF-validated webhook URLs, and strict CSP/CORS headers.
- **Fail-closed production config**: When `ENVIRONMENT` is `staging` or `production`, startup is blocked unless `JWT_SECRET` (≥ 32 chars), secure cookies, HTTPS CORS origins, private object storage, antivirus, and Postgres/Redis URLs are properly configured.

Full details in [SECURITY.md](SECURITY.md), [docs/SECURITY.md](docs/SECURITY.md), and [docs/security/threat-model.md](docs/security/threat-model.md).

---

## API overview

All endpoints live under `/api/v1`. Authenticate with a session cookie (`+ X-CSRF-Token`), `Authorization: Bearer <JWT>`, or `Authorization: Bearer <api_key>`.

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/register` · `POST /auth/login` · `POST /auth/logout` · `GET /auth/me` |
| Workspaces & members | `GET, POST /organizations` · `GET, POST /members` · `PATCH, DELETE /members/{user_id}` · `/invitations` |
| Projects & schemas | `GET, POST /projects` · `GET, POST /schemas` · `GET /schemas/templates` |
| Documents | `POST, GET /documents` · `GET, DELETE /documents/{id}` · `POST /documents/{id}/process` · `POST /documents/{id}/restore` · `GET /documents/{id}/download` |
| Extraction & export | `GET /documents/{id}/extraction` · `PATCH /extraction-fields/{id}` · `GET /documents/{id}/export?format=json\|csv\|xlsx` |
| Intelligence | `POST /documents/compare` · `POST /projects/{id}/summarize` |
| Q&A & conversations | `POST /ask` · `GET, POST, DELETE /conversations` |
| Billing & API keys | `GET /billing/plans` · `GET /billing/usage` · `POST /billing/checkout` · `PATCH /billing/subscription` · `GET, POST, DELETE /api-keys` |
| Ops & webhooks | `GET, POST, DELETE /webhooks` · `GET /audit-logs` · `GET /health` · `GET /ready` · `GET /metrics` |

Complete API reference: [docs/API.md](docs/API.md).

---

## Testing

| Check | Command | What it covers |
|---|---|---|
| Backend lint & security | `ruff check app tests` · `mypy app` · `bandit -r app -ll` | Style, typing, static security checks |
| Backend unit & API | `pytest --cov=app --cov-fail-under=75` | Auth, tenancy, ingestion, RAG, billing, exports |
| PostgreSQL + RLS | `TEST_DATABASE_URL=postgresql+psycopg://… pytest tests/test_rls_policies.py` | Row-Level Security isolation across tenant tables |
| RAG evaluation | `python -m app.evaluation.rag` | Recall@k, MRR, citation accuracy, faithfulness, abstention, tenant leaks |
| Frontend | `npm run lint` · `npm run typecheck` · `npm run build` | ESLint, TypeScript, Next.js production build |
| Playwright E2E | `npm run test:e2e` | Full browser flows against the Docker Compose stack |

---

## Production deployment

Use `docker-compose.prod.yml` as the hardened overlay (non-reload workers, read-only root filesystems, dropped capabilities, internal data network):

```bash
export REDIS_PASSWORD=...
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm api alembic upgrade head
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

See [docs/STAGING.md](docs/STAGING.md), [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md), and [docs/OWNER_SETUP.md](docs/OWNER_SETUP.md) for TLS, managed Postgres/Redis, S3/KMS, ClamAV, and payment gateway setup.

---

## Further documentation

- [Installation & environment](docs/ENVIRONMENT.md)
- [Local processing & models](docs/LOCAL_PROCESSING.md)
- [Ingestion & RAG architecture](docs/INGESTION.md) · [ADR-005](docs/decisions/ADR-005-contextrag-unification.md)
- [Commercial model](docs/COMMERCIAL_MODEL.md) · [Billing integration](docs/BILLING_INTEGRATION.md) · [Granular permissions](docs/GRANULAR_PERMISSIONS.md)
- [Operations & reliability](docs/OPERATIONS.md) · [Troubleshooting](docs/TROUBLESHOOTING.md)

---

## Contributing & license

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Proprietary software, all rights reserved — see [LICENSE](LICENSE) and [docs/LICENSING.md](docs/LICENSING.md).
