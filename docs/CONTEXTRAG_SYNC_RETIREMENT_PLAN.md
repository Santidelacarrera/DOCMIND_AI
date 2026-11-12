# Plan de retirada de la sincronización con ContextRAG externo

**Estado (2026-11-12):**
* Fases 1 y 2 **autorizadas** por el propietario (misión «Validación real, staging y producción», sección 11).
* **Fase 1 ejecutada**: `CONTEXTRAG_OUTBOX_ENABLED=false` por defecto (rama `claude/real-validation-prep`).
* **Fase 2 ejecutada** (rama `claude/contextrag-sync-phase2`): código, rutas, tareas, configuración y sus pruebas retirados; la tabla se conserva.
* **Fase 3 (borrar la tabla): no autorizada, no se ejecutará sin una autorización explícita.**

## Por qué existe y por qué sobra

Antes de ADR-005, DOCMIND enviaba el texto enmascarado de los documentos a un servicio ContextRAG aparte, que recuperaba y respondía. Desde las entregas 1–5, la recuperación (vectorial, BM25 y reranking), la verificación, los conflictos y las conversaciones se ejecutan dentro de DOCMIND (`backend/app/rag/`). La sincronización ya no aporta nada que DOCMIND necesite. Nunca se desplegó un ContextRAG accesible (`CONTEXTRAG_INTEGRATION.md`, «Estado real»).

## Inventario (verificado en el código el 2026-11-12)

| Componente | Qué hace | Dónde |
|---|---|---|
| Outbox `contextrag_events` | Una fila por cambio (upsert o retire) con identificadores y estado, **sin contenido**. Se escribe **aunque la sincronización esté desactivada**, para que activarla después no pierda nada | `app/models.py` (`ContextRagEvent`), migración `0007_contextrag_outbox`, `rls.TENANT_TABLES` |
| Encolado | Procesamiento completado → upsert; borrado, expiración y purga → retire. En la misma transacción que el cambio | `worker.py:513`, `routers/documents.py:572`, `retention.py:75` y `:105` |
| Entrega | Tarea de beat `deliver-contextrag-events` cada 30 s. **Sin hacer nada** si está desactivada (`process_due`: `if not enabled(): return`) | `worker.py:109`, `contextrag_sync.process_due` |
| Reconciliación | Tarea de beat `reconcile-contextrag` cada hora. Encola eventos que falten. **Se ejecuta aunque la sincronización esté desactivada** | `worker.py:110`, `contextrag_sync.reconcile` |
| Cliente HTTP | Contrato y cliente, con hosts permitidos y claves por tenant leídas de `CONTEXTRAG_KEYS_FILE` | `app/integrations/contextrag.py` |
| API de administración | `GET /api/v1/contextrag/events`, `GET …/events/list`, `POST …/events/{id}/cancel` (solo ADMIN) | `routers/admin.py:145-210` |
| Configuración | `CONTEXTRAG_SYNC_ENABLED`, `_URL`, `_KEYS_FILE`, `_ALLOWED_HOSTS`, `_CONTENT_MODE`, `_ALLOW_RAW_TEXT`, `_MAX_ATTEMPTS`, con validaciones de producción | `core.py:180-191` y `:219-290` |
| Pruebas | 62 pruebas (`test_contextrag_sync` 31, `test_contextrag_integration` 18, `test_outbox_admin` 13); ensayo en vivo `scripts/contextrag_live_check.py` | `backend/tests/`, `backend/scripts/` |
| Frontend | Ninguna referencia | — |
| Docker Compose | Ningún servicio ContextRAG | — |

## DOCMIND funciona sin ella (evidencia)

* Con la configuración por defecto (desactivada) pasan la suite completa (707 pruebas), el e2e de navegador y la prueba de humo del stack completo (28/28).
* Ninguna ruta de petición llama a ContextRAG: la única llamada de red está en `process_due`, que con la sincronización desactivada termina antes de leer la tabla.
* Ninguna respuesta, cita ni conversación lee `contextrag_events`.

## Coste de no retirarla

* `contextrag_events` crece con cada documento procesado, borrado o reprocesado (una fila de identificadores por cambio). No contiene texto, pero no tiene retención propia.
* Dos tareas de beat se ejecutan sin aportar nada: `reconcile` recorre documentos cada hora para encolar eventos que nadie entregará.
* Superficie de mantenimiento y de seguridad: 7 variables de configuración, un cliente HTTP con claves por tenant, 3 rutas de administración y 62 pruebas.
* Puede confundir sobre la arquitectura: el diseño actual es una sola plataforma.

## Fases propuestas (cada una reversible hasta la 3)

### Fase 1 — dejar de producir trabajo (sin borrar nada) — EJECUTADA
* Un ajuste nuevo, `CONTEXTRAG_OUTBOX_ENABLED=false`, que deja de encolar eventos; y sacar del beat `deliver-contextrag-events` y `reconcile-contextrag` mientras esté en `false`.
* **Reversión:** volver a `true`. `reconcile()` vuelve a crear los eventos que falten (ya probado: `test_reconcile_recreates_missing_events_*`).
* **Pruebas previas:** suite completa con el ajuste en `false`; comprobar que no se crean filas nuevas en la tabla.
* **Resultado:**
  * `test_with_the_outbox_retired_documents_are_processed_and_no_event_is_written`: procesar, borrar y reconciliar no escriben eventos.
  * `test_retired_contextrag_tasks_are_not_scheduled_but_remain_registered`: las tareas siguen registradas, sin programar.
  * Reversibilidad comprobada: con el ajuste en `true` y ContextRAG en `PYTHONPATH` (sin instalarlo ni escribir en su repositorio), 57 pruebas de la sincronización pasan; 1 omitida (solo PostgreSQL); 1 fallida porque necesita `fork`, que no existe en Windows, y que en CI se omite.
  * `CONTEXTRAG_SYNC_ENABLED=true` exige ahora `CONTEXTRAG_OUTBOX_ENABLED=true`.

### Fase 2 — retirar el código (la tabla se conserva) — EJECUTADA
* Quitar `contextrag_sync.py`, `integrations/contextrag.py`, las 3 rutas de administración, las tareas de beat, las 7 variables y sus pruebas y documentación. `ContextRagEvent` se conserva para que la base de datos no cambie.
* **Cambio de contrato:** las 3 rutas de administración pasan a 404. Comprobar antes que ningún cliente las usa (registros de acceso).
* **Reversión:** revertir el commit (no se ha tocado la base de datos).
* **Resultado:**
  * Eliminados `app/contextrag_sync.py`, `app/integrations/contextrag.py`, las 3 rutas de administración, las tareas `deliver_contextrag_events` y `reconcile_contextrag`, las 8 variables `CONTEXTRAG_*` (con `extra="ignore"`, un `.env` antiguo sigue cargando), `scripts/contextrag_live_check.py` y las pruebas del código retirado (`test_contextrag_sync`, `test_contextrag_integration`, `test_outbox_admin`, `test_outbox_process_kill` y la prueba de registros de la entrega en `test_observability_surface`).
  * Se conservan `ContextRagEvent`, la migración 0007 y la política RLS de la tabla.
  * Nuevas pruebas: `test_retired_contextrag_tasks_are_gone`, y la prueba de procesamiento y borrado sin eventos.
  * `docs/AUTHORIZATION_MATRIX.md` regenerada.

### Fase 3 — borrar la tabla (**destructiva**)
* **Copia previa:** `pg_dump -Fc -t contextrag_events` verificado con `pg_restore --list`, guardado con las copias de la base de datos.
* Migración `0012_drop_contextrag_events`: `DROP TABLE contextrag_events` y retirarla de `rls.TENANT_TABLES` (la política desaparece con la tabla). `downgrade()` crea la tabla vacía. Los datos se restauran desde la copia con `pg_restore -t contextrag_events`.
* **Pruebas previas:** subir, bajar y volver a subir la migración en una base nueva y en una copia de staging; suite completa; job `backend-postgres-rls`.

## Riesgos

| Riesgo | Probabilidad | Mitigación |
|---|---|---|
| Existe un ContextRAG desplegado que alguien espera mantener sincronizado | Baja: nunca se desplegó uno accesible | Confirmarlo con el propietario antes de la fase 1 |
| Un script o integración externa usa las rutas de administración | Baja | Revisar los registros de acceso antes de la fase 2 |
| Pérdida del historial de eventos | Solo en la fase 3 | Copia `pg_dump` previa; los eventos contienen solo identificadores y estados |

## Decisión requerida

Autorizar (o no) cada fase por separado. Recomendación técnica: fases 1 y 2 juntas, y la fase 3 después de un ciclo de copias de seguridad en staging.
