# Security operations

For the project security policy and how to report vulnerabilities see the repository-level [SECURITY.md](../SECURITY.md). This page covers operating the controls.

## Secrets

- `JWT_SECRET` must be at least 32 random characters (`python -c "import secrets; print(secrets.token_urlsafe(48))"`). Rotating it invalidates every session.
- Never use `LLM_PROVIDER=mock` outside development. `OPENAI_API_KEY` is server-only.
- `NEXT_PUBLIC_*` values ship to browsers; never put secrets there.
- API keys are displayed once and stored as SHA-256 hashes; revoke compromised keys with `DELETE /api-keys/{id}`.

## Sessions and CSRF

Cookies are HttpOnly and `Secure` outside development. Cookie-authenticated `POST/PUT/PATCH/DELETE` requests (except `login` and `register`, which create the session) require `X-CSRF-Token` to equal the `csrf_token` cookie. Bearer/API-key requests are not cookie-authenticated and are exempt. Logout increments the user's session version, which invalidates all earlier JWTs.

Serve the web app and API from the same registrable domain (for example `app.example.com` and `api.example.com`) so `SameSite=Lax` cookies and the embedded PDF viewer work.

## Rate limiting

Fixed one-minute Redis windows: per client address (`RATE_LIMIT_*`) and, for login, per account. Behind a reverse proxy start uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy address>` so the client address is the real one. If Redis is unreachable, protected endpoints return `503` instead of skipping the check.

## Upload pipeline

Extension and `%PDF-` signature, declared and actual size (`MAX_UPLOAD_BYTES`), page limit, full parse, optional ClamAV scan (mandatory in staging/production), SHA-256 de-duplication, opaque storage keys (`{org}/{uuid}.pdf`) and sanitized display names. Parsing runs in the worker with time limits; for hostile-input environments, run the worker in a sandboxed, network-restricted container.

## LLM safety

Document text is untrusted: instructions and content are separate messages, input/output are bounded, and results must match the `{"fields": …}` contract. Review extracted data before acting on it — a model can still be misled by document content.

## Hardening checklist for production

- [ ] `ENVIRONMENT=production` (startup validation enforces secrets, HTTPS, S3, antivirus)
- [ ] TLS everywhere, including Redis (`rediss://`) and PostgreSQL (`sslmode=require`)
- [ ] Private bucket, encryption (KMS), lifecycle/retention, versioning
- [ ] Database user with least privilege; backups and a rehearsed restore
- [ ] WAF / request-size limits at the ingress
- [ ] Log shipping without request bodies, tokens or document content
- [ ] Alerts: 5xx, queue depth, failed/stuck jobs, antivirus unavailable
