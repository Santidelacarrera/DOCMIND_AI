# Consulta jurídica por expediente (fase 3A)

Base técnica para responder preguntas usando solo los documentos de un proyecto (expediente) de una organización. No hay interfaz de NexusFlow ni aprovisionamiento automático de claves en esta fase. No hay RLS.

Estado: implementado en el árbol de trabajo, **sin commit**. Ver "Premisas revisadas" antes de integrar nada.

## Premisas revisadas

El brief partía de un estado que no existe en este entorno. Se inspeccionó antes de modificar nada:

| Premisa del brief | Estado real (2026-10-28) |
|---|---|
| Rama `phase2/security` con cambios sin commit | No existe, ni local ni remota. El árbol de `DOCMIND_AI` estaba limpio en `feat/docmind-ai-autonomo-2026-10-12` (HEAD `13a0c5e` antes de las fusiones de hoy) |
| Claves limitadas por proyecto | No existe. `ApiKey` solo tiene una lista de `scopes`, sin vínculo con proyectos |
| Repositorio NexusFlow-AI con cambios locales | No está en este entorno |
| ContextRAG_Engine con cambios locales | Está en `/home/user/santidelacarrera/contextrag_engine`, rama `claude/tender-cray-4zua6u`, árbol limpio. No se ha tocado |

La fase 3B añade el control por proyecto (ver "Autorización por expediente"). Antes de ella, un miembro de la organización podía consultar cualquier proyecto de ella.

## Índice vectorial

* Cada fragmento lleva `project_id` en el payload (`app/ingestion/vectorstore.py`, `PROJECT_KEY`), indexado como keyword en Qdrant.
* La búsqueda por expediente incluye la condición `project_id = <proyecto>` **dentro de la consulta vectorial** (`QdrantStore.search(..., project_id=...)`). Qdrant no puntúa ni devuelve fragmentos de otro proyecto. No se filtra después.
* `answer()` vuelve a comprobar `project_id` en cada resultado, como defensa en profundidad. Y `is_live` exige que el documento siga vivo y pertenezca al proyecto.
* Un vector sin `project_id` (legado, o indexado sin proyecto) nunca coincide con una consulta por expediente.
* `index_document` toma el proyecto del documento en base de datos. `Document.project_id` es obligatorio en el modelo, así que todo documento tiene proyecto verificable.

## Migración y estrategia de recuperación

No hay migración de esquema: `Document.project_id` ya existe. Los vectores antiguos se corrigen con un procedimiento idempotente:

```bash
cd backend && QDRANT_URL=... DATABASE_URL=... python scripts/backfill_vector_projects.py
```

* `vector_index.backfill_project_ids()` recorre los documentos vivos e indexados y fija `project_id` en sus puntos con `set_payload` (sin re-embeber, sin tocar borrados).
* Es idempotente: ejecutarlo dos veces produce el mismo resultado y no cambia nada la segunda vez (test `test_backfill_stamps_legacy_vectors_is_idempotent_and_restores_answers`).
* Un documento que no se puede marcar cuenta en `failed` y queda sin proyecto, por lo que sigue fuera de las consultas por expediente. El script sale con código 1 solo si hay fallos.
* `unscoped_points_remaining` cuenta vectores sin proyecto de la organización. Incluye los de documentos borrados que aún esperan al outbox de borrado, así que es informativo.
* Reindexar un documento (`index_document`) conserva el proyecto y no duplica puntos (test `test_reindexing_keeps_the_project_and_does_not_duplicate`).

Pendiente de ejecutar: el backfill sobre un índice real de Qdrant con datos antiguos. Las pruebas usan Qdrant en memoria.

## Contrato del endpoint

`POST /api/v1/projects/{project_id}/ask?organization_id={org}`

Autenticación: JWT de usuario, o clave API. Las claves API necesitan el alcance `documents:read`, deben pertenecer a la misma organización y tener el proyecto en su lista `project_id` (las claves sin lista se rechazan aquí).

Cuerpo JSON:

| Campo | Regla |
|---|---|
| `question` | Texto, 3 a 500 caracteres |
| `top_k` | Entero, 1 a 8 (por defecto 4). Fuera de rango: `422` |

Respuesta `200` (misma forma que `POST /api/v1/ask`):

```json
{
  "answer": "texto extraído verbatim del documento",
  "abstained": false,
  "citations": [
    {"document_id": "…", "document": "nombre.pdf", "page": 2, "page_basis": "explicit", "score": 0.73, "label": "nombre.pdf, p.2"}
  ]
}
```

Una respuesta siempre tiene al menos una cita, o es `abstained: true` sin citas. No hay paginación: el endpoint devuelve una respuesta, y `top_k` acota las citas.

### Orden de comprobaciones

1. Límite de peticiones (`enforce`, bucket `project_ask`).
2. Membresía de la organización con alcance `documents:read`. Una persona sin membresía recibe `403 FORBIDDEN`; una clave de otra organización, `403 FORBIDDEN`; una clave sin el alcance, `403 INSUFFICIENT_SCOPE`.
3. Autorización por proyecto (`require_project`, `app/project_access.py`): el proyecto debe existir en la organización y el llamador debe poder leerlo. Si no, `404 PROJECT_NOT_FOUND`. Un proyecto ajeno, uno sin permiso y uno inexistente o malformado dan **la misma respuesta**.
4. Disponibilidad del índice: `503 VECTOR_INDEX_DISABLED` si no hay `QDRANT_URL`.
5. Consulta filtrada por proyecto y registro de auditoría en la misma transacción.

### Errores

| Código | Cuándo |
|---|---|
| `401 AUTH_REQUIRED` | Token ausente, caducado o de una sesión revocada |
| `403 FORBIDDEN` | Sin membresía, u organización distinta de la de la clave |
| `403 INSUFFICIENT_SCOPE` | Clave sin `documents:read` |
| `404 PROJECT_NOT_FOUND` | Proyecto inexistente, malformado o de otra organización |
| `422` | Validación del cuerpo |
| `429 RATE_LIMITED` | Límite de peticiones (incluye `Retry-After`) |
| `503 RATE_LIMIT_UNAVAILABLE` | El contador de límites no responde (falla cerrada) |
| `503 VECTOR_INDEX_DISABLED` | Índice vectorial desactivado (solo tras las comprobaciones 1 a 3) |

## Trazabilidad y logs

* `X-Request-Id`: si el cliente lo envía y tiene hasta 64 caracteres de `[A-Za-z0-9._-]`, se usa; si no, se genera uno. Siempre se devuelve en la respuesta (`app/request_context.py`, `RequestIdMiddleware`). Un valor malformado nunca llega a los logs.
* La auditoría `document.project_ask` guarda `{request_id, citations, abstained}` en `metadata`. No guarda la pregunta ni el texto de los fragmentos.
* El log `project query answered` lleva `request_id`, `organization_id`, `project_id`, `citations` y `abstained` como campos estructurados. **No se registra la pregunta, el texto del documento ni fragmentos.** Lo comprueba `test_logs_do_not_contain_the_question_or_document_text`.

## Pruebas

`backend/tests/test_case_query.py` (19 tests, datos sintéticos, Qdrant en memoria):

* Proyecto correcto: cita solo su documento y su respuesta (no la de otro proyecto con la misma redacción).
* Filtro en la consulta vectorial: se inspecciona la condición que recibe Qdrant, y un punto de otro proyecto con mayor puntuación nunca se devuelve.
* Proyecto sin documentos: abstención.
* Organización ajena: 404 idéntico al de un identificador inexistente o malformado; la organización ajena se rechaza con 403 antes de cualquier consulta.
* Claves: alcance `documents:read` necesario; clave revocada deja de funcionar.
* Revocación de membresía: efecto inmediato (403).
* Vectores sin proyecto: nunca coinciden; migración con `backfill_project_ids`: estampa, es idempotente y restaura las respuestas; documentos borrados no se migran.
* Reindexación: conserva el proyecto y no duplica.
* Documento borrado: nunca se cita.
* Límites: `top_k` 9 rechazado, 8 aceptado; índice desactivado: 503 solo para proyecto propio.
* `X-Request-Id`: se devuelve, queda en la auditoría, y un valor inseguro se sustituye.
* Logs: sin pregunta ni contenido.

Además se ejecutó la mutación de control: quitar el filtro por proyecto de la consulta vectorial hace fallar 4 tests; el fichero se restauró y se comprobó byte a byte.

Resultados (2026-10-28, backend SQLite): suite completa **560 passed, 18 skipped**. `ruff check` limpio; `mypy app` limpio (47 ficheros). La matriz de autorización se regeneró (52 rutas).

No ejecutado: PostgreSQL 16/17 con esta funcionalidad; Qdrant real (solo en memoria); el backfill contra un índice con datos antiguos reales; embeddings reales (se usa `HashingEmbedder`).

## Row-level security: no implementado

Este cambio no implementa RLS. Lo que haría falta para integrarla con el esquema actual:

* El contexto de seguridad sería `app.organization_id` y `app.project_id`, fijado con `SET LOCAL` dentro de la transacción, nunca `SET` de sesión (en un pool, `SET` de sesión se filtra a la siguiente petición).
* Tablas afectadas: `documents`, `document_pages`, `extraction_*`, `processing_jobs`, `usage_records`, `audit_logs`, `api_keys`, `contextrag_events`. Los vectores no están en PostgreSQL, así que su aislamiento sigue en la capa de aplicación y en el filtro de Qdrant.
* Políticas con `NULLIF(current_setting(...), '')` para que una variable ausente no devuelva filas.
* Riesgos: el rol de la aplicación no debe ser propietario de las tablas ni tener `BYPASSRLS`; un superusuario o un rol de mantenimiento salta RLS; cualquier conexión directa a la base de datos (psql, herramientas de BI, backups restaurados) ve todo; un `SET` de sesión no revertido contamina el pool. Ver `docs/RLS_DESIGN.md`.

## Riesgos pendientes

1. **Resuelto en la fase 3C** (ver `docs/DOCUMENT_AUTHORIZATION.md`): todas las rutas de documentos comprueban el proyecto del documento en el servidor. El riesgo que queda de esta sección es el borrado, que sigue bajo `documents:write` (ver ese documento).
2. **Resuelto en la fase 3C:** `GET /api/v1/projects` muestra solo los proyectos legibles por el llamador. Pendiente de decisión: un miembro que crea un proyecto no recibe concesión automática (ver `docs/DOCUMENT_AUTHORIZATION.md`).
3. **Acceso directo a la base de datos** y roles con privilegios elevados: ver la sección RLS.
4. **Qdrant con una sola colección compartida** y filtro por tenant y proyecto: un error de configuración del filtro expondría datos. El filtro es obligatorio en el adaptador y tiene test de mutación, pero no hay un segundo control independiente en Qdrant.
5. **Backfill no ejecutado contra datos reales** ni contra un Qdrant con volumen.
6. **Claves legacy (sin `project_ids`) siguen siendo de organización completa** para los endpoints de organización. Es el comportamiento anterior, conservado a propósito; las claves nuevas deben crearse con `project_id`.

## Autorización por expediente (fase 3B)

### Modelo de permisos

Personas:

| Rol de organización | Acceso a los proyectos de la organización |
|---|---|
| `OWNER`, `ADMIN` | Todos, sin concesión |
| `MEMBER` | Solo los proyectos con concesión (`read` o `write`) |
| `VIEWER` | Solo lectura, con concesión; no puede subir (rol mínimo `MEMBER` para subir) |

* Concesión: tabla `project_members` (`project_id`, `user_id`, `access` ∈ {`read`, `write`}, `granted_by`). Única por proyecto y persona.
* Gestión: `PUT /api/v1/projects/{id}/members` (crear o cambiar acceso), `GET …/members` y `DELETE …/members/{user_id}` (revocar). Solo `OWNER`/`ADMIN` y solo con sesión de persona; una clave API no puede darse permisos.
* Revocación: efectiva en la siguiente petición, porque la comprobación se hace en cada llamada y no hay caché.
* `write` permite subir documentos al proyecto. Leer lo permiten las dos.

Claves API:

| Clave | Proyectos | Uso |
|---|---|---|
| Con `project_id` (uno o varios) | Solo esos | Consultas por expediente, organización `/ask` restringido, subida si tiene `documents:write` |
| Sin `project_id` (legacy) | Toda la organización | Endpoints de organización como antes; **rechazada** por los endpoints por expediente (`404`) |

* Lectura y escritura se separan con los alcances: `documents:read` para consultar, `documents:write` para subir. No hay alcance de borrado propio; el borrado de documentos sigue en el control de organización (riesgo 1).
* Un secreto de clave solo se muestra al crearla. Se guarda su hash, y no aparece en logs ni auditoría (test `test_the_key_secret_is_shown_once_and_never_stored_in_plain_text`).

### Organización `/api/v1/ask`

* Antes: buscaba en toda la organización.
* Ahora: solo en los proyectos que el llamador puede leer (`accessible_project_ids`). Una persona sin concesiones recibe una abstención sin consultar el índice.
* Los vectores **sin proyecto** (legados, sin backfill) no coinciden con ninguna lista de proyectos: quedan excluidos. Decisión: excluir, no rechazar. Antes de activar consultas en una organización con datos antiguos, ejecutar el backfill; `unscoped_points` indica cuántos quedan.

### Migración 0008 (`backend/alembic/versions/0008_project_authorization.py`)

* Añade `api_keys.project_ids` (JSON, nullable) y crea `project_members`.
* Idempotente: comprueba el esquema antes de cada paso. Verificado en PostgreSQL 16.15 real: subida a `head`, segunda subida sin efecto, bajada a `0007` (la tabla y la columna desaparecen) y subida de nuevo.
* Rollback: `alembic downgrade 0007_contextrag_outbox`. **Atención:** al bajar, las claves que estaban limitadas a proyectos vuelven a ser de organización completa, porque el código anterior no lee `project_ids`. Antes de bajar, revocar esas claves o aceptar esa ampliación. Las concesiones se pierden; quedan registradas en la auditoría (`project.member.grant`).

### Pruebas

`backend/tests/test_project_authorization.py` (22 tests, datos sintéticos): dos organizaciones, tres proyectos, owner, admin, lector y escritor con concesiones, miembro sin concesiones, viewer, outsider y claves. Cubren:

* Acceso autorizado (owner, admin, concesión de lectura y de escritura) y denegado (sin concesión, viewer al subir, lector al subir).
* Revocación de concesión y de clave; clave sin `documents:read`; clave limitada a un proyecto (lectura, subida, y `/ask` organizacional restringido).
* Clave legacy: rechazada por el endpoint por expediente, igual que ve el dueño en el endpoint organizacional.
* Falsificación: `project_id` en query o en cuerpo no amplía el acceso; el de la ruta manda; una clave no se amplía nombrando otro proyecto.
* Organización ajena: 403 para personas y claves de otra organización, 404 uniforme para proyectos ajenos.
* Gestión de concesiones: solo owner/admin, nunca clave; el usuario debe pertenecer a la organización.
* Vectores legados excluidos del endpoint organizacional, y restaurados tras el backfill.
* Divulgación: respuestas denegadas y logs sin nombres de documento, sin la pregunta, sin el secreto de la clave.
* Esquema y migración idempotente.

**Mutación:** quitar la comprobación (`require_project` sin efecto y `accessible_project_ids` devolviendo todos los proyectos) hace fallar **12 de 22** tests de la fase; el fichero se restauró y se comprobó byte a byte. Los 22 tests se ejecutan contra SQLite y PostgreSQL.

Resultados:
* SQLite, suite completa: 581 pasan, 18 omitidos, 1 fallo en `test_celery_redis` (test intermitente ya registrado; en aislamiento falló 1 de 3 ejecuciones).
* PostgreSQL 16.15 real, suite completa: **583 pasan, 17 omitidos, 0 fallos**.
* `ruff check` y `mypy app` limpios.

No ejecutado: Qdrant real (no hay servicio en `127.0.0.1:6333` y el daemon de Docker no responde); todas las pruebas vectoriales usan Qdrant en memoria.

## Integración pendiente

**ContextRAG** (`/home/user/santidelacarrera/contextrag_engine`): el índice que consume la sincronización no conoce todavía `project_id`. Antes de sincronizar documentos por expediente hay que añadir el campo al contrato de upsert (`app/integrations/contextrag.py`), reflejarlo en el outbox y verificar con el servicio real. La sincronización sigue desactivada.

**NexusFlow-AI** (no disponible en este entorno): el cliente debe enviar solo la clave del servicio y el identificador del proyecto, nunca un identificador de usuario que el servicio confíe sin verificar. El servidor valida la membresía o la clave, y responde con el contrato anterior. La interfaz no forma parte de esta fase.
