# Architecture

```mermaid
flowchart LR
  U[User] --> W[Next.js]
  W --> A[FastAPI /api/v1]
  A --> P[(PostgreSQL)]
  A --> R[(Redis)]
  A --> S[StorageProvider]
  R --> C[Celery worker]
  C --> D[Document processing]
  D --> X[PDF parser / OCRProvider / LLMProvider]
  D --> P
```

The API is the authorization boundary. Every organization-owned query receives an organization id derived from the authenticated principal; client-supplied organization ids never grant access. Workers reload the document version from the database and atomically claim jobs, making retries safe.
