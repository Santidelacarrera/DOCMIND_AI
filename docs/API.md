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
| `POST /documents?organization_id=&project_id=` | member (scope `documents:write`) | `multipart/form-data` field `file` (PDF). `202 {document_id, job_id, status}` |
| `GET /documents?organization_id=` | scope `documents:read` | Latest 100 |
| `GET /documents/{id}?organization_id=` | scope `documents:read` | Metadata |
| `DELETE /documents/{id}?organization_id=` | member (scope `documents:write`) | Soft-deletes the record and removes the stored PDF (`204`) |
| `GET /documents/{id}/status?organization_id=` | scope `documents:read` | Latest job status and failure code |
| `POST /documents/{id}/process?organization_id=` | member (scope `documents:write`) | Idempotent re-queue; returns the active job if one exists |
| `GET /documents/{id}/download?organization_id=[&inline=true]` | scope `documents:read` | Original PDF |
| `GET /documents/{id}/extraction?organization_id=` | scope `documents:read` | Latest completed extraction and fields |
| `PATCH /extraction-fields/{id}?organization_id=` | member (scope `documents:write`) | Body: any JSON value (≤ 64 KiB); marks the field manually verified |
| `GET /documents/{id}/export?organization_id=&format=json\|csv\|xlsx` | scope `exports:read` | Spreadsheet formulas are neutralized |
| `GET /schemas?organization_id=`, `POST /schemas?organization_id=&name=` | scope `schemas:read` / member | `POST` body is an object JSON Schema (≤ 64 KiB); optional `project_id` |
| `GET, POST /api-keys?organization_id=`, `DELETE /api-keys/{id}` | admin, user-only | `POST` body: JSON array of scopes. The secret is returned once |
| `GET /usage?organization_id=` | scope `usage:read` | Plan and metric totals |
| `GET /audit-logs?organization_id=` | admin, user-only | Latest 100 events |
| `GET /health`, `GET /ready` | public | Liveness / database readiness |

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

A schema is an object JSON Schema describing the `fields` the model should return. For each document the worker uses the newest **active** schema of its project, falling back to the newest organization-wide schema, and records the version in the extraction run. OpenAI *strict* structured output is requested only when every object in the schema sets `additionalProperties: false` and lists all of its properties in `required`; otherwise the model is called in non-strict mode.
