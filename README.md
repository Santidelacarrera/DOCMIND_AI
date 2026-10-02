<div align="center">

# DocMind AI

**Secure, multi-tenant document intelligence: upload PDFs, extract structured data with an LLM, review it, export it.**

[![CI](https://github.com/Santidelacarrera/DOCMIND_AI/actions/workflows/ci.yml/badge.svg)](https://github.com/Santidelacarrera/DOCMIND_AI/actions/workflows/ci.yml)
[![CodeQL](https://github.com/Santidelacarrera/DOCMIND_AI/actions/workflows/codeql.yml/badge.svg)](https://github.com/Santidelacarrera/DOCMIND_AI/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Next.js](https://img.shields.io/badge/Next.js-16-000000?logo=next.js&logoColor=white)](https://nextjs.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-17-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white)](https://redis.io/)

[Quick start](#quick-start) · [Architecture](#architecture) · [Security](#security) · [API](#api) · [Configuration](#configuration) · [Testing](#testing) · [Deployment](#deployment) · [Roadmap](#roadmap)

</div>

---

## Table of contents

1. [What it does](#what-it-does)
2. [Quick start](#quick-start)
3. [Architecture](#architecture)
4. [Processing pipeline](#processing-pipeline)
5. [Security](#security)
6. [API](#api)
7. [Configuration](#configuration)
8. [Development](#development)
9. [Testing](#testing)
10. [Deployment](#deployment)
11. [Operations](#operations)
12. [Design decisions](#design-decisions)
13. [Troubleshooting](#troubleshooting)
14. [Roadmap](#roadmap)
15. [Contributing and license](#contributing-and-license)

## What it does

DocMind AI turns PDFs (invoices, contracts, forms…) into reviewable, exportable data.

- **Multi-tenant by construction** — organizations, roles (owner/admin/member/viewer), projects, scoped API keys, usage quotas and audit logs.
- **Safe ingestion** — magic-byte, size, page-count and parse validation, optional ClamAV scanning, checksum de-duplication, opaque storage keys.
- **Asynchronous pipeline** — Celery workers with idempotent jobs, retries, time limits and automatic stuck-job recovery.
- **Text first, OCR when needed** — `pypdf` extraction with Tesseract fallback for scans.
- **Pluggable LLMs** — deterministic mock for development/tests, OpenAI Responses API with structured output for real extraction.
- **Your schema, your fields** — define JSON Schemas per organization or project; the newest active one drives extraction.
- **Human in the loop** — edit values in the browser, side by side with the original PDF; edits are flagged as manually verified.
- **Exports** — JSON, CSV and XLSX, hardened against spreadsheet formula injection.

## Quick start

Requirements: Docker Engine with Compose v2.

```bash
git clone https://github.com/Santidelacarrera/DOCMIND_AI.git
cd DOCMIND_AI
cp .env.example .env          # safe local defaults, mock LLM, local storage
docker compose up --build     # applies migrations automatically
```

| Service | URL |
|---|---|
| Web app | <http://localhost:3000> |
| API + interactive docs (development only) | <http://localhost:8000/docs> |
| Liveness / readiness | <http://localhost:8000/health> · <http://localhost:8000/ready> |

1. Open the web app, **create an account** (password ≥ 12 characters).
2. In **Documents**, create a project and upload a PDF.
3. The worker processes it; open the document to review, edit and export the result.

> The default `.env.example` uses `LLM_PROVIDER=mock`, which returns a fixed fixture so the whole flow works offline. Set `LLM_PROVIDER=openai` and `OPENAI_API_KEY` for real extraction.

## Architecture

```mermaid
flowchart LR
    U[Browser] --> W[Next.js web]
    W -->|cookie + CSRF / Bearer| A[FastAPI /api/v1]
    K[API client] -->|API key| A
    A --> P[(PostgreSQL)]
    A --> S[StorageProvider<br/>local or S3-compatible]
    A --> R[(Redis<br/>rate limits + broker)]
    A -.->|optional| V[ClamAV]
    R --> C[Celery worker]
    B[Celery beat] --> R
    C --> S
    C --> X[pypdf text]
    X --> O{needs OCR?}
    O -->|yes| T[Tesseract]
    O -->|no| L[LLM provider]
    T --> L
    L --> P
```

| Component | Responsibility |
|---|---|
| **web** (`frontend/`) | Next.js 16 / React 19 UI: auth, projects, upload, review, schemas. Strict CSP. |
| **api** (`backend/app/main.py`) | The *only* authorization boundary: authentication, tenancy, validation, quotas, exports. |
| **worker** (`backend/app/worker.py`) | Idempotent document processing with row-level job locks, retries and timeouts. |
| **scheduler** | Celery beat; runs `recover_stuck_jobs` every minute. Run exactly one. |
| **PostgreSQL** | System of record, schema owned by Alembic. |
| **Redis** | Celery broker/result backend and fixed-window rate limiter (fails closed). |
| **Storage** | `StorageProvider` abstraction shared by API and worker. |

```text
.
├── backend/
│   ├── app/            # FastAPI app, models, security, worker, providers
│   ├── alembic/        # database migrations
│   └── tests/          # hermetic API + worker tests, opt-in Docker integration
├── frontend/
│   ├── app/            # Next.js routes
│   ├── components/     # shared UI
│   ├── lib/            # browser API client (CSRF aware)
│   └── tests/e2e/      # Playwright, against the real stack
├── docs/               # API, architecture, security, operations, staging, ADRs
├── docker-compose.yml          # development stack
├── docker-compose.prod.yml     # hardened overlay
└── .github/                    # CI, CodeQL, Dependabot, PR template
```

## Processing pipeline

1. **Upload** — authenticate, authorize (`member`+ or `documents:write`), validate (extension, `%PDF-` magic bytes, declared and actual size, parsable, page limit), optional antivirus, quota check under a row lock, de-duplicate by SHA-256, store under `{org}/{uuid}.pdf`, create document, version, usage reservation and job.
2. **Claim** — the worker locks the job row (`SELECT … FOR UPDATE`); completed jobs are no-ops, so at-least-once delivery is safe.
3. **Extract text** — `pypdf`; if text density is below the threshold, Tesseract OCR runs (bounded pages and time).
4. **LLM** — the newest active schema for the project (else organization) is wrapped in a `{"fields": …}` envelope. OpenAI *strict* structured output is requested only when the schema is strict-compatible. Document text is sent as untrusted data.
5. **Persist** — pages, steps, extraction run (with the schema version used), fields and usage records are committed atomically with the `COMPLETED` status. Failures store a stable code, never raw provider errors.
6. **Review & export** — edit fields, then export JSON / CSV / XLSX.

## Security

Security is the primary design constraint. Highlights (details in [SECURITY.md](SECURITY.md), [docs/SECURITY.md](docs/SECURITY.md) and the [threat model](docs/security/threat-model.md)):

**Identity and sessions**
- Argon2id password hashing; login does constant work for unknown users (no timing oracle) and is rate-limited per IP **and** per account.
- JWTs require `exp`, `sub` and a session version; malformed or forged tokens return `401`, never `500`. Logout bumps the version, revoking all tokens.
- Cookie sessions are `HttpOnly`, `Secure` outside development, `SameSite`, with double-submit CSRF on **every** state-changing request (including logout).
- API keys (`dm_live_…`) are random 256-bit secrets, shown once, stored as SHA-256, revocable, bound to one organization and an allow-list of scopes. They can never reach user-only endpoints (`/auth/*`, organizations, key management, audit logs).

**Tenancy**
- Every query is filtered by an organization id that is checked against membership server-side; cross-tenant ids return `403`/`404` (covered by tests for every resource).
- Worker schema lookup is organization-scoped, so a tenant can never receive another tenant's schema.

**Input and output**
- Bounded everything: request body, file size, pages, strings, scopes, JSON field values, schema size.
- Filenames are sanitized on ingest; `Content-Disposition` is RFC 6266 encoded (no header injection).
- CSV/XLSX exports neutralize `= + - @` formula prefixes (CSV injection).
- Responses carry `nosniff`, `Referrer-Policy`, `Permissions-Policy`, `Cache-Control: no-store`, CSP and `frame-ancestors`; HSTS when cookies are secure. The PDF viewer may only be framed by the configured web origins. OpenAPI/Swagger are disabled in staging/production.
- CORS allow-list with explicit methods/headers; production requires HTTPS origins.

**Fail-safe configuration**
- With `ENVIRONMENT=staging|production` the API refuses to start unless a ≥32-char `JWT_SECRET`, secure cookies, HTTPS CORS origins, private object storage, an antivirus provider and database/Redis URLs are configured.
- Containers run as a non-root user; the production overlay adds read-only filesystems, resource limits and internal networks.

**Pipeline**
- Quotas are enforced under an organization row lock to prevent concurrent over-spend.
- LLM prompts keep document text separate from instructions; input, output, timeout and concurrency are bounded.
- Supply chain: Dependabot, CodeQL, `bandit`, `pip-audit`, `npm audit`, lockfile installs.

To report a vulnerability, follow [SECURITY.md](SECURITY.md).

## API

Base path `/api/v1`. Authenticate with a session cookie (+ `X-CSRF-Token`), `Authorization: Bearer <JWT>`, or `Authorization: Bearer <api key>`.

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/register` · `POST /auth/login` · `POST /auth/logout` · `GET /auth/me` |
| Organizations | `GET, POST /organizations` · `GET, POST /members` · `PATCH, DELETE /members/{user_id}` |
| Projects | `GET, POST /projects` |
| Documents | `POST, GET /documents` · `GET, DELETE /documents/{id}` · `POST /documents/{id}/process` · `GET /documents/{id}/status` · `GET /documents/{id}/download[?inline=true]` |
| Extraction | `GET /documents/{id}/extraction` · `PATCH /extraction-fields/{id}` · `GET /documents/{id}/export?format=json\|csv\|xlsx` |
| Schemas | `GET, POST /schemas` |
| API keys | `GET, POST /api-keys` · `DELETE /api-keys/{id}` |
| Ops | `GET /usage` · `GET /audit-logs` · `GET /health` · `GET /ready` |

API-key scopes: `documents:read`, `documents:write`, `schemas:read`, `exports:read`, `usage:read`.

```bash
# register, create a project, upload, fetch the result
TOKEN=$(curl -s localhost:8000/api/v1/auth/register -H 'content-type: application/json' \
  -d '{"email":"me@example.com","password":"correct-horse-battery","organization_name":"Acme"}')
# ... see docs/API.md for the complete walkthrough
```

Full reference: [docs/API.md](docs/API.md) (and `/docs` in development).

## Configuration

All settings are environment variables (see [.env.example](.env.example)).

| Group | Variables |
|---|---|
| Application | `ENVIRONMENT`, `MAX_UPLOAD_BYTES`, `MAX_PDF_PAGES`, `MAX_PDF_TEXT_CHARS`, `PROCESSING_TIMEOUT_SECONDS`, `STUCK_JOB_SECONDS`, `FREE_PAGES_PER_MONTH` |
| Data | `DATABASE_URL`, `REDIS_URL`, `POSTGRES_*` |
| Storage | `STORAGE_PROVIDER` (`local`\|`s3`), `LOCAL_STORAGE_PATH`, `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_REGION`, `S3_KMS_KEY_ID`, `AWS_*` |
| LLM / OCR | `LLM_PROVIDER` (`mock`\|`openai`), `OPENAI_*`, `MAX_OCR_PAGES`, `OCR_TIMEOUT_SECONDS` |
| Security | `JWT_SECRET`, `JWT_ALGORITHM`, `ACCESS_TOKEN_MINUTES`, `COOKIE_SECURE`, `COOKIE_SAMESITE`, `CSRF_ENABLED`, `CORS_ORIGINS`, `RATE_LIMIT_*`, `ANTIVIRUS_PROVIDER`, `CLAMAV_*` |
| Web | `NEXT_PUBLIC_API_URL` (build-time, public, never a secret) |

## Development

```bash
docker compose up --build        # full stack with reload

# backend, without Docker (needs no services for the test-suite)
cd backend
pip install -e '.[dev]'
ruff check . && mypy app && pytest --cov=app

# frontend
cd frontend
npm ci
npm run dev
```

Create migrations with `docker compose exec api alembic revision --autogenerate -m "…"` and review them before committing. More in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## Testing

| Layer | Command | Notes |
|---|---|---|
| Static analysis | `ruff check .` · `mypy app` · `bandit -r app -ll` | Strict typing, security lint |
| Backend unit/API | `pytest --cov=app` | Hermetic: SQLite, in-memory Redis double, no broker. Covers auth, tenancy, API keys, uploads, exports, worker pipeline |
| Docker integration | `DOCMIND_INTEGRATION=1 pytest tests/test_docker_integration.py` | Real Postgres, Redis and worker |
| Frontend | `npm run lint` · `npm run typecheck` · `npm run build` | |
| Browser E2E | `npm run test:e2e` | Playwright against the real stack (`E2E_BASE_URL`, `E2E_API_URL`) |

CI runs all of the above plus CodeQL, dependency audits and Compose validation on every pull request.

## Deployment

`docker-compose.prod.yml` is a hardened overlay (non-reload workers, read-only filesystems, healthchecks, resource limits, internal networks):

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm api alembic upgrade head   # release step
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Before going live, provide (see [docs/STAGING.md](docs/STAGING.md) and [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)):

- TLS termination and a reverse proxy that sets `X-Forwarded-*` (run uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy>` so rate limiting sees real client addresses),
- managed PostgreSQL and Redis (TLS, auth, persistence, backups),
- private S3-compatible storage with encryption and lifecycle rules,
- a ClamAV service, secrets from a secret manager, and the web and API on the same registrable domain so `SameSite` cookies work.

## Operations

- **Health** `GET /health` (liveness) · `GET /ready` (database).
- **Stuck jobs** — beat runs `recover_stuck_jobs` every 60 s; jobs beyond `STUCK_JOB_SECONDS` become `FAILED/PROCESSING_TIMEOUT` and can be re-queued via `POST /documents/{id}/process`.
- **Audit** — `GET /audit-logs` (admin) records auth, uploads, processing, edits and key lifecycle, including the API key used.
- **Monitoring and retention** — see [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Design decisions

| Decision | Rationale |
|---|---|
| API is the single authorization boundary | One place to audit tenancy; the web app is untrusted |
| Fail-closed rate limiting | A broken Redis must not silently disable brute-force protection |
| Row locks for quotas and job claiming | Correctness under concurrency without distributed locks |
| Storage and LLM behind interfaces | Swap local/S3 and mock/OpenAI without touching business logic |
| Stable failure codes, no raw errors | Provider and parser errors can leak data |
| Strict mode only when the schema allows it | User schemas should not turn into opaque provider errors |

See [docs/decisions/](docs/decisions/) for ADRs.

## Troubleshooting

| Symptom | Check |
|---|---|
| `/ready` fails | Database connectivity and `alembic upgrade head` |
| `503 RATE_LIMIT_UNAVAILABLE` | Redis reachable at `REDIS_URL` (limiter fails closed) |
| Job stays `QUEUED` | Worker running and connected to the same Redis |
| PDF viewer is blank | `CORS_ORIGINS` must contain the exact web origin (it also drives `frame-ancestors`) |
| Browser calls the wrong API | `NEXT_PUBLIC_API_URL` is baked at build time; rebuild the web image |
| `403 CSRF_FAILED` | Send `X-CSRF-Token` equal to the `csrf_token` cookie, or use a Bearer token |
| API refuses to start in staging | Read the validation message; see [docs/STAGING.md](docs/STAGING.md) |

## Roadmap

- Per-document schema selection at upload time and schema versioning UI
- Confidence scores and business-rule validation stage
- Email invitations for people without an account yet
- Webhooks for job completion
- Structured logging, Prometheus metrics and OpenTelemetry traces

## Contributing and license

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Released under the [MIT License](LICENSE).
