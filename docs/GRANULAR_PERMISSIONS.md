# Permisos Granulares, Protección de Documentos y Webhooks por Proyecto

> **Estado:** **COMPLETADO Y PROBADO** en [`backend/app/security.py`](../backend/app/security.py), [`backend/app/routers/documents.py`](../backend/app/routers/documents.py), [`backend/app/routers/projects.py`](../backend/app/routers/projects.py), [`backend/app/routers/admin.py`](../backend/app/routers/admin.py), [`backend/app/worker.py`](../backend/app/worker.py) y [`backend/tests/test_billing_and_granular_permissions.py`](../backend/tests/test_billing_and_granular_permissions.py).

---

## 1. Permiso específico `documents:delete` y documentos protegidos

### Alcance de claves API (`documents:delete`)
- Se añadió `"documents:delete"` a `ALLOWED_SCOPES` en [`backend/app/security.py`](../backend/app/security.py).
- Una clave API creada únicamente con `["documents:delete"]` puede eliminar documentos dentro de sus proyectos autorizados, pero **no puede** subir archivos ni modificar campos extraídos (`403 INSUFFICIENT_SCOPE`).
- **Modo estricto opcional (`REQUIRE_EXPLICIT_DELETE_SCOPE=true`)**:
  - Por defecto (`false`), tanto `"documents:delete"` como `"documents:write"` autorizan `DELETE /api/v1/documents/{id}` para mantener compatibilidad con integraciones existentes (Fase 3C).
  - Cuando `REQUIRE_EXPLICIT_DELETE_SCOPE=true`, una clave con solo `"documents:write"` recibe `403 INSUFFICIENT_SCOPE` al intentar borrar un documento; se exige explícitamente `"documents:delete"`.

### Protección de documentos críticos (`Document.protected`) y confirmación
- Los administradores y propietarios (`Role.admin`, `Role.owner`) pueden marcar o desmarcar un documento como protegido mediante:
  ```http
  PATCH /api/v1/documents/{document_id}/metadata?organization_id={org_id}
  {"protected": true}
  ```
- Un miembro regular (`Role.member`) o una clave API no pueden alterar el atributo `protected` (`403 FORBIDDEN`) ni eliminar un documento protegido (`409 DOCUMENT_PROTECTED`).
- Incluso un administrador o propietario que intente borrar un documento con `protected=true` sin confirmación explícita recibe `409 DOCUMENT_PROTECTED_CONFIRM_REQUIRED`; debe invocar:
  ```http
  DELETE /api/v1/documents/{document_id}?organization_id={org_id}&confirm=true
  ```

### Recuperación dentro de la ventana de retención (`restore`)
- Mientras un documento eliminado lógicamente (`deleted_at IS NOT NULL`) permanezca dentro de la ventana `RETENTION_DELETED_DAYS` antes de la purga definitiva, un `Role.admin` o `Role.owner` puede restaurar su registro mediante:
  ```http
  POST /api/v1/documents/{document_id}/restore?organization_id={org_id}
  ```

---

## 2. Concesión automática al miembro creador de un proyecto (`PROJECT_CREATOR_AUTO_GRANT`)

### Política implementada
- Cuando un usuario con rol `Role.member` crea un proyecto (`POST /api/v1/projects`), si `PROJECT_CREATOR_AUTO_GRANT=true` (activo por defecto), el sistema crea en la misma transacción una concesión `ProjectMember(project_id=project.id, user_id=user.id, access="write")`.
- **Principio de mínimo privilegio**:
  - El creador obtiene lectura y escritura **exclusivamente** sobre el proyecto que acaba de crear.
  - **No** recibe privilegios administrativos globales ni puede administrar concesiones de otros miembros (`PUT /api/v1/projects/{id}/members` sigue restringido a `Role.admin` y `Role.owner`).
  - Los propietarios y administradores (`Role.owner`, `Role.admin`) ya poseen acceso completo a todos los proyectos de su organización, por lo que no se crean filas redundantes en `project_members`.

---

## 3. Filtrado de Webhooks por `project_id` (`Webhook.project_ids`)

- El modelo `Webhook` y el endpoint `POST /api/v1/webhooks` aceptan ahora una lista opcional `project_ids`:
  ```json
  {
    "url": "https://erp.cliente.example.com/webhooks/docmind",
    "description": "Solo contratos legales",
    "events": ["document.completed", "document.failed"],
    "project_ids": ["11111111-2222-3333-4444-555555555555"]
  }
  ```
- **Validación en creación**: cada `project_id` debe existir dentro de la organización del solicitante (en caso contrario devuelve `404 PROJECT_NOT_FOUND`).
- **Aislamiento en despacho (`worker._dispatch_webhooks`)**:
  - Si `webhook.project_ids` es `null`, el webhook recibe los eventos de toda la organización (comportamiento clásico).
  - Si `webhook.project_ids` contiene una lista de proyectos, el worker verifica el `project_id` del documento en base de datos y **solo** entrega el evento a los webhooks que incluyan dicho proyecto.
