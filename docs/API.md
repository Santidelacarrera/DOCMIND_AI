# API

Interactive OpenAPI is served at `/docs`. Register with JSON `POST /api/v1/auth/register` using `email`, `password` (12+ characters), and `organization_name`; send the returned JWT as `Authorization: Bearer <token>` for private endpoints.

All organization-scoped endpoints require `organization_id` and verify its membership server-side. Uploads additionally require `project_id`. Public credentials are never accepted via query parameters.

The documented API-key model stores only SHA-256 hashes and returns an issued key once. JWT remains the active auth path for the current MVP endpoints; API-key scope enforcement is the next security increment.
