# Staging deployment

This guide prepares configuration only. It does not contain credentials and must not be used as a secret store.

## Variables

| Variable | Service | Required in staging | Secret | Safe example | Description |
|---|---|---:|---:|---|---|
| `ENVIRONMENT` | API/worker | Yes | No | `staging` | Enables fail-closed staging validation. |
| `DATABASE_URL` | API/worker/Alembic | Yes | Yes | `postgresql+psycopg://<user>:<password>@<host>:5432/<db>` | Managed PostgreSQL connection. |
| `REDIS_URL` | API/worker | Yes | Yes | `redis://:<password>@<host>:6379/0` | Redis broker, result backend and rate limiter. |
| `JWT_SECRET` | API/worker | Yes | Yes | `<secret-manager-reference>` | At least 32 random characters; signs sessions. |
| `COOKIE_SECURE` | API | Yes | No | `true` | Requires HTTPS-only session cookies. |
| `COOKIE_SAMESITE` / `CSRF_ENABLED` | API/web | Yes | No | `lax` / `true` | Browser session and CSRF policy. |
| `CORS_ORIGINS` | API | Yes | No | `https://staging.example.invalid` | Comma-separated explicit HTTPS web origins. |
| `NEXT_PUBLIC_API_URL` | Web build | Yes | No | `https://api.staging.example.invalid` | Public API origin; this value is intentionally browser-visible. |
| `STORAGE_PROVIDER` | API/worker | Yes | No | `s3` | `local` is rejected in staging. |
| `S3_BUCKET`, `S3_REGION`, `S3_KMS_KEY_ID` | Storage | Yes | No | `<private-bucket>`, `<region>`, `<kms-key-id>` | Private encrypted object-storage target. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Supabase S3 storage | Yes | Yes | `<secret-manager-reference>` | Supabase S3-compatible credentials; API and worker receive them at runtime. |
| `AWS_SESSION_TOKEN` | Supabase S3 storage | No | Yes | `<secret-manager-reference>` | Only for temporary credentials. |
| `S3_ENDPOINT_URL` | Storage | No | No | `https://object.example.invalid` | Only for S3-compatible providers. |
| `ANTIVIRUS_PROVIDER` | API/worker | Yes | No | `clamav` | Upload scanning provider. |
| `CLAMAV_HOST`, `CLAMAV_PORT` | Antivirus | Yes | No | `<clamav-service>`, `3310` | Clamd endpoint; unavailable scanning fails closed. |
| `LLM_PROVIDER` | Worker | Yes | No | `mock` or `openai` | Keep `mock` until the external provider is approved. |
| `OPENAI_API_KEY` | Worker | Only when OpenAI is enabled | Yes | `<secret-manager-reference>` | Required only with `LLM_PROVIDER=openai`. |
| `OPENAI_MODEL` and `OPENAI_*` limits | Worker | Yes | No | `gpt-4o-mini` | Model, timeout, retries, token/input and concurrency limits. |
| `MAX_UPLOAD_BYTES`, `MAX_PDF_*`, `MAX_OCR_PAGES`, `OCR_TIMEOUT_SECONDS`, `PROCESSING_TIMEOUT_SECONDS`, `STUCK_JOB_SECONDS` | API/worker | Yes | No | See `.env.example` | Document and job resource limits. |
| `RATE_LIMIT_*`, `ACCESS_TOKEN_MINUTES`, `FREE_PAGES_PER_MONTH`, `CONFIDENCE_REVIEW_THRESHOLD` | API | Yes | No | See `.env.example` | Security and operational limits. |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Local Compose DB only | No | Password only | `<local-only>` | Not used when staging points at managed PostgreSQL. |
| `DOCMIND_BASE_URL`, `DOCMIND_INTEGRATION`, `E2E_BASE_URL` | Test tooling only | No | No | `https://api.staging.example.invalid` | Never required by deployed services. |

No monitoring-specific runtime variable exists today; send container logs/metrics through the deployment platform rather than adding credentials to this repository.

## Configuration order

1. Provision managed PostgreSQL, Redis, private encrypted object storage, ClamAV, HTTPS ingress, DNS, Secret Manager and centralized monitoring.
2. Create least-privilege Supabase S3 credentials and place them in the Secret Manager. Inject database, Redis, JWT, Supabase S3 and optional OpenAI secrets at runtime.
3. Set `ENVIRONMENT=staging`, exact HTTPS CORS origin, secure cookies, S3 settings and ClamAV endpoint. Startup deliberately fails if required staging values are absent.
4. Build with `docker compose -f docker-compose.yml -f docker-compose.prod.yml build` and deploy with the same files. The API/worker receive `.env` through `env_file`; replace that mechanism with platform secret injection in real staging. Web receives `NEXT_PUBLIC_API_URL` only at build/deploy time.
5. Run `docker compose exec api alembic upgrade head` as the release migration step, before rolling worker replicas.

## Healthchecks and smoke tests

Verify `/health` and `/ready` through the ingress, then verify containers are healthy with `docker compose ps`. Confirm API, worker and web run as `appuser` using `docker compose exec <service> whoami`.

Run a smoke test with a staging test account: register/login, create a project, upload a harmless PDF, confirm processing reaches `COMPLETED`, review extraction, edit one field, export XLSX, then test a second tenant receives `403` or `404`. Do not use production documents or real OpenAI credentials for this check.

## Avoiding split configuration

Docker Compose injects the repository-root `.env` into API and worker. A shell launched from `backend/` instead loads `backend/.env` through Pydantic. Do not place staging S3 configuration in only one of those files: use the same Secret Manager/runtime injection source for `ENVIRONMENT`, `STORAGE_PROVIDER`, `S3_*` and `AWS_*`, then recreate both API and worker. Before uploading, verify only presence (never values): `docker compose exec api python -c "import os; print(bool(os.getenv('AWS_ACCESS_KEY_ID')))"`.
