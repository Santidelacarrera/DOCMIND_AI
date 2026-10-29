# Autorización por expediente en las rutas de documentos (fase 3C)

Inventario de las rutas que leen, cambian o borran documentos, sus archivos o sus datos derivados, con el control aplicado en cada una. La fuente de verdad de los decoradores es `app/main.py`; la matriz generada en `docs/AUTHORIZATION_MATRIX.md` se regenera con `backend/scripts/authorization_matrix.py`.

## Reglas comunes

* **Organización:** `membership(...)` sigue siendo la primera comprobación. Una persona o clave de otra organización recibe 403 antes de ninguna búsqueda.
* **Proyecto del documento:** siempre se resuelve en el servidor desde el documento (`Document.project_id`). Nunca se toma un `project_id` de la petición para decidir sobre un documento.
* **Permiso:** `require_document(..., write=False|True)` (`app/project_access.py`) reutiliza `project_level`, la misma lógica que `require_project`. No hay una segunda implementación.
* **Lectura:** `read` (concesión de lectura, de escritura, o acceso de owner/admin, o clave con el proyecto en su lista).
* **Escritura:** `write`. Un lector que intenta cambiar algo recibe `403 PROJECT_WRITE_DENIED`, porque ya puede ver el documento.
* **Sin acceso:** exactamente la respuesta de un documento inexistente (`404`, mismo código y mismo cuerpo). Así no se revela que el documento existe.
* **Listados:** el filtro va en la consulta SQL, antes del límite, así que los documentos ocultos no se devuelven ni se cuentan.
* **Lotes:** la API no tiene endpoints por lote. La única operación sobre varios documentos es un listado o una cola de revisión, y ambos filtran antes de devolver nada. No hay cambios parciales posibles.

## Inventario

| Método y ruta | Cómo se obtiene el proyecto | Organización | Permiso de proyecto | Permiso de alcance (claves) | Sin autorización |
|---|---|---|---|---|---|
| `POST /api/v1/documents` | Parámetro `project_id` de la petición, validado con `require_project` | `membership` (rol mínimo `MEMBER`) | **write** | `documents:write` | `404 PROJECT_NOT_FOUND`; lector: `403 PROJECT_WRITE_DENIED` |
| `GET /api/v1/documents` | Por documento, en la consulta | `membership` (`documents:read`) | **read** (filtro SQL) | `documents:read` | Documento omitido; sin contar los ocultos |
| `GET /api/v1/documents/{id}` | Documento | `membership` | **read** | `documents:read` | `404 DOCUMENT_NOT_FOUND` |
| `GET /api/v1/documents/{id}/status` | Documento | `membership` | **read** | `documents:read` | `404 DOCUMENT_NOT_FOUND` |
| `GET /api/v1/documents/{id}/download` | Documento; la clave de almacenamiento se valida con `owned_key` además | `membership` | **read** | `documents:read` | `404 DOCUMENT_NOT_FOUND` (sin bytes) |
| `GET /api/v1/documents/{id}/extraction` | Documento | `membership` | **read** | `documents:read` | `404 DOCUMENT_NOT_FOUND` |
| `GET /api/v1/documents/{id}/export` | Documento | `membership` | **read** | `exports:read` | `404 DOCUMENT_NOT_FOUND` |
| `POST /api/v1/documents/{id}/process` | Documento | `membership` (rol `MEMBER`) | **write** | `documents:write` | `404 DOCUMENT_NOT_FOUND`; lector: `403 PROJECT_WRITE_DENIED` |
| `POST /api/v1/documents/{id}/review/approve` | Documento | `membership` (rol `MEMBER`) | **write** | `documents:write` | `404 DOCUMENT_NOT_FOUND`; lector: `403` |
| `PATCH /api/v1/extraction-fields/{field}` | Campo → ejecución → documento, en el servidor | `membership` (rol `MEMBER`) | **write** | `documents:write` | `404 FIELD_NOT_FOUND` (igual que un campo inexistente); lector: `403` |
| `DELETE /api/v1/documents/{id}` | Documento | `membership` (rol `MEMBER`) | **write** | `documents:write` | `404 DOCUMENT_NOT_FOUND`; lector: `403` |
| `GET /api/v1/review/queue` | Por documento, en la consulta | `membership` (`documents:read`) | **read** (filtro SQL) | `documents:read` | Documento omitido |
| `POST /api/v1/ask` | Proyectos legibles del llamador | `membership` | **read** (lista de proyectos) | `documents:read` | Fragmentos de proyectos no legibles nunca se puntúan |
| `POST /api/v1/projects/{id}/ask` | Ruta, validada con `require_project` | `membership` | **read** | `documents:read` | `404 PROJECT_NOT_FOUND` |
| `GET /api/v1/projects` | Lista de proyectos legibles | `membership` | **read** (filtro SQL) | `documents:read` | Proyecto omitido, sin contar |

Las concesiones (`PUT`, `GET`, `DELETE /api/v1/projects/{id}/members`) son solo para owner/admin y no tocan documentos.

## Rutas auxiliares revisadas

* **Almacenamiento directo:** no hay rutas que lean archivos fuera de `download`. `worker.py` y `vector_index.py` leen el almacenamiento solo en tareas de fondo, que disparan las rutas ya autorizadas o beat.
* **Routers:** no hay routers montados fuera de `app/main.py`.
* **Auditoría (`GET /api/v1/audit-logs`):** solo owner/admin. Contiene identificadores de documento y acciones, no contenido. No se filtra por proyecto: un admin ya administra toda la organización.
* **Uso (`GET /api/v1/usage`):** totales de la organización, sin contenido de documentos.
* **Sincronización ContextRAG (`GET /api/v1/contextrag/events…`):** solo admin, sin contenido. Sigue desactivada por defecto.
* **Webhooks:** la entrega envía solo `document_id` y el evento a una URL que configura un admin. No hay control por proyecto en la entrega; es un riesgo residual, sin contenido.

## Decisiones y límites

* **Borrado:** sigue requiriendo `documents:write` para claves y rol `MEMBER` para personas, más **write** en el proyecto. No hay un permiso de borrado independiente. Un permiso propio exigiría un alcance nuevo (`documents:delete`) que no existe en `ALLOWED_SCOPES`: cambia la forma de las claves ya emitidas, así que no se hace sin decisión del responsable. **Riesgo abierto:** quien tenga escritura en un proyecto puede borrar sus documentos.
* **Claves legacy** (sin `project_ids`): siguen siendo de organización completa en estos endpoints, como antes. No se convierten en claves limitadas ni se amplían. Los endpoints por expediente las rechazan.
* **Creación de proyectos:** un miembro que crea un proyecto no recibe una concesión automática, para no ampliar permisos sin decisión. Consecuencia: no ve ni puede subir al proyecto que creó hasta que un admin le conceda acceso. **Decisión pendiente del responsable.**
* **Visibilidad de proyectos:** `GET /api/v1/projects` muestra a cada persona o clave solo sus proyectos; owner y admin ven todos.

## Pruebas

* `backend/tests/test_document_authorization.py` (18 tests, datos sintéticos): lector, escritor, sin concesión frente a un documento inexistente (misma respuesta), listados ocultos, owner y admin, otra organización, revocación, claves con alcance y proyecto, manipulación de IDs, errores sin fugas, descarga, y visibilidad de proyectos.
* Mutación: quitar la comprobación por documento (`require_document` sin efecto) hace fallar 8 de los 16 tests que existían entonces; quitar el filtro de los listados hace fallar 3. Ambos ficheros se restauraron y se comprobaron byte a byte.
* Ajustes de tests existentes, sin cambiar sus aserciones: `test_cross_org_negative.py` y `test_contextrag_sync.py` necesitaban una concesión de lectura para el miembro que el test espera que lea. Es la consecuencia del modelo nuevo, no una relajación.
* Test intermitente de Celery: ver el apartado de resultados.

## Resultados (2026-10-29, código final de la fase 3C)

| Comprobación | Resultado |
|---|---|
| `ruff check app tests scripts alembic` | Limpio |
| `mypy app` | Limpio (48 ficheros) |
| SQLite, suite completa | **608 pasan, 22 omitidos, 0 fallan** |
| PostgreSQL 16.15, suite completa, servicios vivos configurados | **613 pasan, 17 omitidos, 0 fallan** |
| PostgreSQL 17.11, suite completa, servicios vivos configurados | **613 pasan, 17 omitidos, 0 fallan** |
| `bandit -ll -r app` | Limpio |
| `pip-audit` | Sin vulnerabilidades conocidas (`contextrag` y `docmind-api` no están en PyPI y se omiten) |
| `npm audit --omit=dev` | 0 vulnerabilidades |
| Frontend `typecheck`, `lint` | Pasan |
| Playwright e2e (7 tests) con el stack en la configuración de CI | **7 de 7 pasan** |

Las 4 pruebas vivas de `test_live_services.py` se omiten en SQLite porque requieren su servicio configurado; con los servicios configurados se ejecutan y pasan en PostgreSQL 16 y 17. El resto de omisiones no se ha desglosado por motivo en este documento.

Las pruebas vivas usan Qdrant, ClamAV, SeaweedFS (S3) y Mailpit (SMTP) reales en contenedores locales. KMS no está validado (ver `EXTERNAL_DEPENDENCIES.md`).
