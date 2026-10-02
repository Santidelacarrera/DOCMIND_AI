# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them privately through
GitHub's *Security → Report a vulnerability* flow on this repository, including:

- the affected endpoint, component or commit,
- reproduction steps or a proof of concept,
- the impact you expect (data exposure, privilege escalation, denial of service…).

You will receive an acknowledgement within 3 business days and a remediation plan within
10. Fixes are released first, then disclosed; reporters are credited unless they prefer
otherwise.

## Supported versions

Only the `main` branch receives security fixes.

## Security model in brief

| Concern | Control |
|---|---|
| Authentication | Argon2id passwords; signed JWTs with required `exp`/`sub`/`sv` claims and a server-side session version, so logout revokes every earlier token |
| Brute force | Per-IP and per-account Redis rate limits; fail-closed when Redis is down; constant-work login so unknown accounts are not distinguishable by timing |
| Tenant isolation | Every query is scoped by an `organization_id` that is re-validated against server-side membership; API keys are bound to one organization and an explicit scope list |
| CSRF | Double-submit token for cookie sessions on every state-changing request (including logout); Bearer/API-key calls are not cookie-authenticated |
| Uploads | Extension, magic bytes, size (declared and actual), page count, parse check, optional ClamAV scan, opaque storage keys, sanitized display filenames |
| Output | `Content-Disposition` built per RFC 6266; CSV/XLSX exports neutralize spreadsheet formulas; `nosniff`, CSP, `frame-ancestors`, `no-store` headers |
| LLM | Document text is passed as untrusted data, never as instructions; bounded input/output, timeouts and concurrency |
| Secrets | API keys are shown once and stored as SHA-256 hashes; staging/production refuse to start without strong secrets, secure cookies, HTTPS CORS, S3 and antivirus |
| Supply chain | Dependabot, CodeQL, `pip-audit`, `npm audit` and `bandit` run in CI; lockfile-based installs |

The full analysis lives in [docs/security/threat-model.md](docs/security/threat-model.md)
and operational guidance in [docs/SECURITY.md](docs/SECURITY.md).

## Out of scope

Findings that require a compromised host or database, social engineering, volumetric DoS,
or deployments that ignore the documented staging/production requirements (for example
running with `ENVIRONMENT=development` on the internet).
