# Matriz de autorización (generada)

Generada por `backend/scripts/authorization_matrix.py` a partir de las rutas de la aplicación. No editar a mano.

Lectura de `scope=` para personas: en la función `membership()`, un usuario (no una API key) solo está
limitado por su rol mínimo; el `scope` se aplica únicamente a las claves API. Por eso `scope="usage:read"`
permite a **cualquier miembro**, incluido un viewer, leer los totales de uso. Ver la sección de uso.

| Método | Ruta | Comprobación en el handler |
|---|---|---|
| GET | `/api/v1/api-keys` | membership(org_id, user, db, Role.admin) |
| POST | `/api/v1/api-keys` | membership(org_id, user, db, Role.admin) |
| DELETE | `/api/v1/api-keys/{key_id}` | membership(org_id, user, db, Role.admin) |
| POST | `/api/v1/ask` | accessible_project_ids(db, org_id, user); membership(org_id, user, db, scope="documents:read") |
| GET | `/api/v1/audit-logs` | membership(org_id, user, db, Role.admin) |
| POST | `/api/v1/auth/login` | **ninguna** |
| POST | `/api/v1/auth/logout` | **ninguna** |
| GET | `/api/v1/auth/me` | **ninguna** |
| POST | `/api/v1/auth/register` | **ninguna** |
| GET | `/api/v1/contextrag/events` | membership(org_id, user, db, Role.admin) |
| GET | `/api/v1/contextrag/events/list` | membership(org_id, user, db, Role.admin) |
| POST | `/api/v1/contextrag/events/{event_id}/cancel` | membership(org_id, user, db, Role.admin) |
| GET | `/api/v1/documents` | accessible_project_ids(db, org_id, user); membership(org_id, user, db, scope="documents:read") |
| POST | `/api/v1/documents` | membership(org_id, user, db, Role.member, scope="documents:write"); require_project(db, org_id, user, project_id, write=True) |
| DELETE | `/api/v1/documents/{document_id}` | membership(org_id, user, db, Role.member, scope="documents:write"); require_document(db, org_id, user, document_id, write=True) |
| GET | `/api/v1/documents/{document_id}` | membership(org_id, user, db, scope="documents:read"); require_document(db, org_id, user, document_id) |
| GET | `/api/v1/documents/{document_id}/download` | membership(org_id, user, db, scope="documents:read"); require_document(db, org_id, user, document_id) |
| GET | `/api/v1/documents/{document_id}/export` | membership(org_id, user, db, scope="exports:read"); require_document(db, org_id, user, document_id) |
| GET | `/api/v1/documents/{document_id}/extraction` | membership(org_id, user, db, scope="documents:read"); require_document(db, org_id, user, document_id) |
| POST | `/api/v1/documents/{document_id}/process` | membership(org_id, user, db, Role.member, scope="documents:write"); require_document(db, org_id, user, document_id, write=True) |
| POST | `/api/v1/documents/{document_id}/review/approve` | membership(org_id, user, db, Role.member, scope="documents:write"); require_document(db, org_id, user, document_id, write=True) |
| GET | `/api/v1/documents/{document_id}/status` | membership(org_id, user, db, scope="documents:read"); require_document(db, org_id, user, document_id) |
| PATCH | `/api/v1/extraction-fields/{field_id}` | membership(org_id, user, db, Role.member, scope="documents:write"); require_document(db, org_id, user, row[1].document_id, write=True, not_found="FIELD_NOT_FOUND") |
| GET | `/api/v1/invitations` | membership(org_id, user, db, Role.admin) |
| POST | `/api/v1/invitations` | membership(org_id, user, db, Role.admin) |
| POST | `/api/v1/invitations/accept` | **ninguna** |
| DELETE | `/api/v1/invitations/{invitation_id}` | membership(org_id, user, db, Role.admin) |
| GET | `/api/v1/members` | membership(org_id, user, db, Role.admin) |
| POST | `/api/v1/members` | membership(org_id, user, db, Role.admin) |
| DELETE | `/api/v1/members/{user_id}` | membership(org_id, user, db, Role.viewer if user_id == user.id else Role.admin) |
| PATCH | `/api/v1/members/{user_id}` | membership(org_id, user, db, Role.admin) |
| GET | `/api/v1/organizations` | **ninguna** |
| POST | `/api/v1/organizations` | **ninguna** |
| GET | `/api/v1/projects` | accessible_project_ids(db, org_id, user); membership(org_id, user, db, scope="documents:read") |
| POST | `/api/v1/projects` | membership(org_id, user, db, Role.member) |
| POST | `/api/v1/projects/{project_id}/ask` | membership(org_id, user, db, scope="documents:read"); require_project(db, org_id, user, project_uuid) |
| GET | `/api/v1/projects/{project_id}/members` | membership(org_id, user, db, Role.admin) |
| PUT | `/api/v1/projects/{project_id}/members` | membership(org_id, user, db, Role.admin) |
| DELETE | `/api/v1/projects/{project_id}/members/{member_user_id}` | membership(org_id, user, db, Role.admin) |
| GET | `/api/v1/review/queue` | accessible_project_ids(db, org_id, user); membership(org_id, user, db, scope="documents:read") |
| GET | `/api/v1/schemas` | membership(org_id, user, db, scope="schemas:read") |
| POST | `/api/v1/schemas` | membership(org_id, user, db, Role.member) |
| GET | `/api/v1/schemas/{schema_id}/versions` | membership(org_id, user, db, scope="schemas:read") |
| POST | `/api/v1/schemas/{schema_id}/versions` | membership(org_id, user, db, Role.member) |
| GET | `/api/v1/usage` | membership(org_id, user, db, scope="usage:read") |
| GET | `/api/v1/webhooks` | membership(org_id, user, db, Role.admin) |
| POST | `/api/v1/webhooks` | membership(org_id, user, db, Role.admin) |
| DELETE | `/api/v1/webhooks/{webhook_id}` | membership(org_id, user, db, Role.admin) |
| GET | `/health` | **ninguna** |
| GET | `/metrics` | **ninguna** |
| GET | `/ready` | **ninguna** |
