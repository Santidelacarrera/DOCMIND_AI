# Threat model

Assets: documents, extracted values, organization membership, API keys and usage records.

Primary mitigations: authenticated tenant-scoped queries; opaque UUIDs; stored file keys never derive from filenames; MIME signature validation and upload caps; hashed API keys; secrets only in environment; CORS allow-list; job idempotency; server-side quota enforcement; no public storage paths. Residual risks include malicious PDF parser exploits and model prompt injection. Production deploys should sandbox parsing, malware-scan uploads, use object storage encryption, rate limiting, audit monitoring, backups and signed URLs.
