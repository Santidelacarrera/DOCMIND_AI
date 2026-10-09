# Deployment

`docker-compose.yml` is development-only. Staging uses `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d` with secrets injected by the platform; it must set `ENVIRONMENT=staging`, `COOKIE_SECURE=true`, `JWT_SECRET`, `DATABASE_URL`, `REDIS_URL`, exact `CORS_ORIGINS`, and `ANTIVIRUS_PROVIDER=clamav`. Run `alembic upgrade head` before rolling API and worker replicas.

Production requires managed PostgreSQL and Redis, private encrypted object storage, ClamAV (or an equivalent provider implementation), TLS termination/reverse proxy, secret manager, centralized structured logs/metrics and alerting. The application sets browser security headers; the ingress owns certificates, TLS redirects and request-size limits.

Backups are an infrastructure responsibility: take encrypted PostgreSQL point-in-time backups and storage versioned backups. Define RPO/RTO with the operator and rehearse restores in staging: restore a timestamped PostgreSQL backup into an isolated database, restore matching object-storage versions, run migrations, then verify `/ready` and a tenant-scoped document download. Do not claim a backup exists merely because this procedure exists.

## Compose overlay networks

`docker-compose.prod.yml` puts the bundled `db`/`redis` on an internal `backend` network without egress, while `api` and `worker` also join `edge` for outbound traffic to object storage, the LLM provider and ClamAV. Set `REDIS_PASSWORD` and use it in `REDIS_URL`. Prefer managed PostgreSQL/Redis in production; the bundled services are for single-host staging.
