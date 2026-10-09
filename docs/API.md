# API reference

Base path: `/api/v1`. Interactive OpenAPI is served at `/docs` **in development only**; it is disabled when `ENVIRONMENT` is `staging` or `production`.

## Authentication

| Method | How | Notes |
|---|---|---|
| Session cookie | `POST /auth/login` sets `access_token` (HttpOnly) and `csrf_token` | State-changing requests must send `X-CSRF-Token` equal to the `csrf_token` cookie |
| Bearer JWT | `Authorization: Bearer <access_token>` | Returned by `register` and `login`; no CSRF header needed |
| API key | `Authorization: Bearer dm_live_…` | Bound to one organization and a scope list; cannot call user-only endpoints |

Errors use `{"detail": "<STABLE_CODE>"}` (validation errors return the standard FastAPI list). Common codes: `AUTH_REQUIRED` 401, `FORBIDDEN` / `INSUFFICIENT_SCOPE` 403, `CSRF_FAILED` 403, `RATE_LIMITED` 429, `RATE_LIMIT_UNAVAILABLE` 503.

## Endpoints

User-only endpoints reject API keys with `403`.

| Endpoint | Auth | Description |
|---|---|---|
| `POST /auth/register` | public, rate-limited | Body `{email, password (12–256), organization_name}` → `201 {access_token, organization_id}` |
| `POST /auth/login` | public, rate-limited per IP and per account | Body `{email, password}` |
| `POST /auth/logout` | user | Revokes every token of the user (`204`) |
| `GET /auth/me` | user | Current user |
| `GET /organizations`, `POST /organizations?name=` | user | List / create |
| `GET /members?organization_id=` · `POST /members?organization_id=&email=&role=` · `PATCH /members/{user_id}?organization_id=&role=` · `DELETE /members/{user_id}?organization_id=` | admin, user-only | Add an *existing* account, change a role or remove a member. Only owners create/modify/remove owners; the last owner is protected; anyone may leave |
| `GET /projects?organization_id=`, `POST /projects?organization_id=&name=[&description=]` | viewer / member | Key scope `documents:read` for listing |
| `POST /documents?organization_id=&project_id=[&schema_id=]` | member (scope `documents:write`) | `multipart/form-data` field `file` (PDF). `schema_id` pins that schema's *latest* version at upload time, overriding the project/org default. `202 {document_id, job_id, status}`. Rejections use stable codes: `413 FILE_TOO_LARGE`, `415 UNSUPPORTED_FILE_TYPE`, `422 DOCUMENT_INVALID` / `PDF_ENCRYPTED` / `PDF_PAGE_LIMIT_EXCEEDED`, `402 QUOTA_EXCEEDED`. Re-sending identical bytes gives `409 {detail: "DUPLICATE_DOCUMENT", document_id}` |
| `GET /documents?organization_id=[&status=FAILED]` | scope `documents:read` | Latest 100, each with the latest job `status` and `failure_code`; `status` filters on it (e.g. `FAILED`, `QUEUED`) |
| `GET /documents/{id}?organization_id=` | scope `documents:read` | Metadata |
| `DELETE /documents/{id}?organization_id=` | member (scope `documents:write`) | Removes the stored PDF and extracted text now, hides the record, and schedules the rest for the retention purge (`204`; see DATA_RETENTION.md) |
| `GET /documents/{id}/status?organization_id=` | scope `documents:read` | Latest job: `status`, `failure_code`, `failure_message`, `attempt`, `retry_count`, `created_at`, `started_at`, `completed_at` |
| `POST /documents/{id}/process?organization_id=[&schema_id=]` | member (scope `documents:write`) | Idempotent re-queue; returns the active job if one exists |
| `GET /documents/{id}/download?organization_id=[&inline=true]` | scope `documents:read` | Original PDF |
| `GET /documents/{id}/extraction?organization_id=` | scope `documents:read` | Latest completed extraction, its fields (each with a `confidence` score), `{requires_review, validation_issues}` from the validation layer, `review {status: not_required\|pending\|approved, pending_fields}`, per-field `original_value`, `needs_review`, `correction_reason`, `corrected_at`, and `model {provider, name, prompt_tokens, completion_tokens, estimated_cost_usd}` |
| `PATCH /extraction-fields/{id}?organization_id=[&reason=]` | member (scope `documents:write`) | Body: any JSON value (≤ 64 KiB; `null` clears the value; an empty body is `422 VALUE_REQUIRED`). Marks the field verified, keeps `original_value`, records `reason` (≤ 200 chars; `confirmed` when unchanged, `unspecified` if omitted), who and when |
| `POST /documents/{id}/review/approve?organization_id=` | member (scope `documents:write`) | Human sign-off; `409 {detail: REVIEW_INCOMPLETE, pending_fields}` while flagged fields are unverified |
| `GET /review/queue?organization_id=` | scope `documents:read` | Documents whose latest extraction needs review and has not been approved |
| `GET /documents/{id}/export?organization_id=&format=json\|csv\|xlsx` | scope `exports:read` | Columns: `name, value, original_value, confidence, manually_verified, needs_review, correction_reason, review_status` (JSON carries the full extraction). Spreadsheet formulas are neutralized |
| `GET /schemas?organization_id=`, `POST /schemas?organization_id=&name=[&prompt_instructions=]` | scope `schemas:read` / member | `POST` body is an object JSON Schema (≤ 64 KiB); optional `project_id` |
| `GET /schemas/{id}/versions?organization_id=`, `POST /schemas/{id}/versions?organization_id=[&prompt_instructions=]` | scope `schemas:read` / member | List every version of a schema, or publish a new one (body: object JSON Schema). Prior versions are kept — a job that pinned one keeps extracting against it |
| `GET, POST /api-keys?organization_id=`, `DELETE /api-keys/{id}` | admin, user-only | `POST` body: JSON array of scopes. The secret is returned once |
| `GET /usage?organization_id=` | scope `usage:read` | Plan and metric totals |
| `GET /audit-logs?organization_id=` | admin, user-only | Latest 100 events |
| `POST /invitations?organization_id=`, `GET /invitations?organization_id=`, `DELETE /invitations/{id}?organization_id=` | admin, user-only | Email an invite (body `{email, role}`); the raw token is only ever sent by email |
| `POST /invitations/accept` | user-only | Body `{token}`; the signed-in user's email must match the invitation |
| `POST /webhooks?organization_id=`, `GET /webhooks?organization_id=`, `DELETE /webhooks/{id}?organization_id=` | admin, user-only | Body `{url, description?, events?}`; `url` must be a public HTTPS endpoint (SSRF-checked at creation *and* delivery time). `POST` returns the HMAC `signing_secret` once |
| `GET /health`, `GET /ready`, `GET /metrics` | public | Liveness / database readiness / Prometheus metrics |

### Scopes

`documents:read`, `documents:write`, `schemas:read`, `exports:read`, `usage:read`.

### Roles

`OWNER > ADMIN > MEMBER > VIEWER`. Viewers are read-only; members upload and edit; admins manage keys and read audit logs.

## Example

```bash
API=http://localhost:8000

# Register (returns a JWT and the organization id)
curl -s $API/api/v1/auth/register -H 'content-type: application/json' \
  -d '{"email":"me@example.com","password":"correct-horse-battery","organization_name":"Acme"}'

# Create a project, upload a PDF, wait for processing, export
curl -s -X POST "$API/api/v1/projects?organization_id=$ORG&name=Invoices" -H "Authorization: Bearer $TOKEN"
curl -s -X POST "$API/api/v1/documents?organization_id=$ORG&project_id=$PROJECT" \
  -H "Authorization: Bearer $TOKEN" -F file=@invoice.pdf
curl -s "$API/api/v1/documents/$DOC/status?organization_id=$ORG" -H "Authorization: Bearer $TOKEN"
curl -s "$API/api/v1/documents/$DOC/export?organization_id=$ORG&format=csv" -H "Authorization: Bearer $TOKEN"

# Machine access with a scoped key
curl -s -X POST "$API/api/v1/api-keys?organization_id=$ORG&name=ci" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '["documents:read","exports:read"]'
```

## Extraction schemas

A schema is an object JSON Schema describing the `fields` the model should return. Picking one explicitly (`schema_id` on upload or reprocess) pins its *latest version at that moment* to the job, so editing the schema later never retargets a job already in flight. Without an explicit choice, the worker uses the newest **active** schema of the document's project, falling back to the newest organization-wide schema. Either way the resolved version is recorded on the extraction run. OpenAI *strict* structured output is requested only when every object in the schema sets `additionalProperties: false` and lists all of its properties in `required`; otherwise the model is called in non-strict mode.

A schema can have multiple versions (`POST /schemas/{id}/versions`); `GET /schemas/{id}/versions` lists them newest-first.

## Confidence scores and validation

Every extraction asks the model for a 0–1 confidence score per field alongside the value itself (`ExtractionField.confidence`, also in the `GET .../extraction` response). Right after extraction, two independent checks run before the result is considered final:

1. **JSON Schema conformance** — the extracted `fields` object is validated against the resolved schema version (`app.validation.validate_fields`).
2. **Confidence thresholding** — any field below `CONFIDENCE_REVIEW_THRESHOLD` (default `0.70`) is flagged, regardless of schema validity.

Neither check blocks persistence — the run is still saved with whatever the model returned — but a run with any issue is marked `requires_review: true`, and `GET /documents/{id}/extraction` returns both that flag and the `validation_issues` list (`{field, rule, message}`) so a reviewer or an automated pipeline can route it before trusting it.

## Webhooks

Subscribe to `document.completed` / `document.failed` and every matching event fires a signed `POST`:

```json
{"event": "document.completed", "data": {"document_id": "..."}}
```

Headers: `X-DocMind-Event: document.completed` and `X-DocMind-Signature: sha256=<hex HMAC-SHA256 of the raw body, keyed with the webhook's signing_secret>`. Verify it before trusting the payload. The signing secret is derived (never stored) from `JWT_SECRET` and the webhook id, and is shown to you exactly once, in the `POST /webhooks` response.

The target URL must be a public, routable HTTPS endpoint — anything resolving to a private, loopback, link-local or reserved address is rejected both when the webhook is created and again on every delivery attempt (DNS can change in between).
