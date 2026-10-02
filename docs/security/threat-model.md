# Threat model

**Assets:** documents, extracted values, organization membership, credentials (passwords, JWTs, API keys), usage and audit records.

**Trust boundaries:** browser ⇄ API (untrusted client), API ⇄ database/Redis/storage (trusted network), worker ⇄ LLM provider (third party), uploaded PDFs (hostile input).

| # | Threat | Mitigations | Residual risk |
|---|---|---|---|
| 1 | Credential stuffing / brute force | Argon2id, per-IP and per-account rate limits, fail-closed limiter, constant-work login | Distributed attacks below the limits; add CAPTCHA/lockout at the edge |
| 2 | Token theft / replay | Short-lived JWTs, `jti`, session-version revocation on logout, HttpOnly + Secure cookies | Token theft inside its lifetime |
| 3 | CSRF | Double-submit token on all cookie-authenticated writes, SameSite cookies, strict CORS allow-list | Subdomain takeover |
| 4 | Broken tenant isolation / IDOR | Org id validated against membership on every route, org-scoped queries, tests per resource; API keys bound to one org and scopes | New endpoints must follow the rule (PR checklist) |
| 5 | Privilege escalation via API key | Keys rejected on user-only endpoints, scope allow-list, hashed storage, revocation | Leaked key within its scopes |
| 6 | Malicious upload (malware, parser exploit, oversized PDFs) | Magic bytes, size/page/text limits, parse check, ClamAV, worker time limits, non-root containers | Zero-day in `pypdf`/Poppler/Tesseract — sandbox the worker |
| 7 | Injection via filenames or values | Sanitized names, RFC 6266 headers, `nosniff`, CSP, React escaping, JSON-only responses | — |
| 8 | CSV/Excel formula injection | `= + - @` and control-character prefixes neutralized in exports | Consumers that re-import data |
| 9 | Prompt injection | Document text isolated from instructions, strict output contract, no tool use, bounded I/O | Wrong values from misled models — human review step |
| 10 | Resource exhaustion | Body/field/schema caps, rate limits, quotas under row locks, task time limits, bounded OCR | Volumetric DoS — handle at the edge |
| 11 | Secret exposure | Env-only secrets, staging/production validation, hashed API keys, no secret logging, sanitized failure messages | Operator mistakes |
| 12 | Supply chain | Lockfiles, Dependabot, CodeQL, pip-audit, npm audit, bandit | Compromised upstream between scans |
| 13 | Data loss / orphaned objects | Storage write rolled back on DB failure, DB uniqueness constraints, backups (operator) | Objects without rows after crashes — reconcile periodically |

Production deployments additionally need TLS, managed data stores, encrypted object storage, centralized logging with alerts, backups and restore drills (see [OPERATIONS](../OPERATIONS.md)).
