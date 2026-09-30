# Security operations

Use a unique 32-byte `JWT_SECRET`, HTTPS at the ingress, private storage, and a managed PostgreSQL/Redis deployment. Do not use `LLM_PROVIDER=mock` outside development. `OPENAI_API_KEY` is server-only.

The worker treats document text as untrusted data and keeps it distinct from extraction instructions. It does not execute document content. PDFs are signature-checked, size-limited, page-limited, malware-scanned in staging/production, and stored by generated keys.

Cookie sessions are HttpOnly and use Secure cookies outside development. State-changing cookie requests require the `X-CSRF-Token` double-submit value. JWTs carry a session version; logout increments it, invalidating all prior JWTs for that user. Bearer/API-key clients do not use cookie CSRF protection.
