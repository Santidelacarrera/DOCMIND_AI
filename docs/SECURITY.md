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

## Multi-tenant isolation and untrusted documents (tested)

* **Isolation is enumerated, not sampled.** `tests/test_tenant_isolation.py` walks FastAPI's route table: every endpoint must be tenant-scoped (or on a short allow-list of public/user-level ones), every scoped endpoint must answer `403` to a member of another organization and `401` without a session, and naming a *foreign object id through your own organization* must give `403/404` and leak none of the victim's ids, filenames or values. A new endpoint without `organization_id` fails the suite.
* **Storage layer.** Object keys are `<organization_id>/<uuid>.pdf`; every read, delete and the worker pass them through `owned_key()`, which refuses keys outside the caller's prefix (tested with a tampered database row pointing at another tenant's object). Local storage refuses path traversal. S3 objects are written without ACLs (bucket stays private) and with KMS when configured. No presigned URLs are ever generated and nothing is served statically; downloads require authorization and are `private, no-store`.
* **API-key scopes** are independent (`documents:read` ≠ `exports:read` ≠ `documents:write`) and bound to one organization.
* **Documents are untrusted LLM input** (`tests/test_prompt_injection.py`). Text reaches the model only inside a `<document>` fence whose terminator cannot be forged from inside; the instructions are fixed; the model is given **no tools**; its answer is a JSON object validated against the envelope and the tenant schema; unexpected keys, schema violations and missing confidences force human review. Processing performs no network I/O of its own (asserted by blocking sockets during a hijacked-answer test), so an obeyed injection can at worst produce wrong *values* — which are flagged — never actions or leaks.
* **Retention and deletion**: see [DATA_RETENTION.md](DATA_RETENTION.md).
