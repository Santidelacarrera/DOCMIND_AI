# Integración con ContextRAG_Engine

DOCMIND_AI extrae y protege el documento; [ContextRAG_Engine](https://github.com/Santidelacarrera/ContextRAG_Engine) recupera y responde. La sincronización es **unidireccional**: DOCMIND envía; ContextRAG nunca escribe en DOCMIND. Ninguna prueba de DOCMIND necesita ContextRAG para pasar.

Código: `backend/app/contextrag_sync.py` (outbox y entrega), `backend/app/integrations/contextrag.py` (contrato y cliente HTTP).
Pruebas: `backend/tests/test_contextrag_sync.py` (33), `backend/tests/test_contextrag_integration.py` (22). Ensayo en vivo: `backend/scripts/contextrag_live_check.py`, resultado en `docs/evaluation/contextrag-live-check-2026-10-26.json`.

## Estado real

| Pieza | Estado | Cómo se comprobó |
|---|---|---|
| Outbox transaccional | **Implementado y conectado**: procesamiento completado, borrado, expiración y purga escriben un evento en la misma transacción | pruebas con ContextRAG real en proceso y en PostgreSQL 16 |
| Entrega asíncrona | **Implementada**: tarea Celery `deliver-contextrag-events` (cada 30 s) | pruebas de reintento, recuperación y fencing |
| Reconciliación | **Implementada**: tarea horaria `reconcile-contextrag`; encola lo que falte | `test_reconcile_recreates_missing_events_*` |
| Activación | **Desactivada por defecto** (`CONTEXTRAG_SYNC_ENABLED=false`). Los eventos se registran igual, para que una activación posterior no pierda nada | configuración y pruebas |
| ContextRAG real, en proceso | Probado contra el código del commit `6a5c7c1` | pruebas automáticas |
| ContextRAG como proceso HTTP aparte (local, puerto 8100) | **Probado** con el ensayo en vivo: creación, borrado, caída, reinicio y recuperación | no es un despliegue; claves de prueba |
| Despliegue conjunto | **No probado**: no hay un ContextRAG desplegado en un entorno accesible | pendiente, ver abajo |

## Garantías y cómo se hacen cumplir

| Garantía | Mecanismo | Prueba |
|---|---|---|
| Nada se pierde si el proceso cae entre el cambio y la entrega | Fila en `contextrag_events` escrita en la misma transacción | `test_processed_document_is_delivered_*` |
| Un reintento no duplica | `dedupe_key` único; `ON CONFLICT DO NOTHING`; ContextRAG reemplaza por `doc_id` | `test_retries_never_duplicate_documents_or_pages`, `test_enqueueing_twice_creates_one_event` |
| Un reprocesado con contenido distinto sí se sincroniza | La clave del upsert incluye un resumen del contenido de las páginas | `test_changed_content_is_resynchronised_*` |
| Un reprocesado sin cambios no genera trabajo | Misma clave, misma fila | `test_unchanged_reprocessing_creates_no_new_event` |
| Un documento eliminado no vuelve a publicarse | El upsert se reconstruye desde la base al entregarse; si el documento está borrado o su contenido cambió, se cancela (`DOCUMENT_DELETED`, `SUPERSEDED`) y no se envía nada | `test_deleted_document_cannot_be_resurrected_by_a_pending_upsert` |
| Un HTTP 200 no basta para dar por borrado | Tras el `DELETE`, se pide a ContextRAG la lista de páginas del documento; si queda alguna, el evento vuelve a pendiente (`VERIFY_FAILED`) | `test_a_success_status_alone_does_not_count_as_deleted` |
| Un documento no se procesa en paralelo consigo mismo | Un solo evento activo por documento | `test_retire_and_upsert_for_one_document_never_run_concurrently` |
| Dos workers no reclaman el mismo evento | Bloqueo de fila con `SKIP LOCKED` (PostgreSQL) | `test_two_workers_never_claim_the_same_event` (solo PostgreSQL) |
| Un worker que se reinicia no deja eventos bloqueados | Reclamaciones con más de 15 min se liberan | `test_claims_left_by_a_restarted_worker_are_released` |
| Un worker obsoleto no termina una reclamación ajena | Token de reclamación (UUID), no marca temporal | `test_a_stale_worker_cannot_finish_a_claim_it_no_longer_owns` |
| Sin clave para la organización, no sale nada | `NO_KEY_FOR_TENANT`, estado `failed` visible | `test_pending_delivery_for_an_organization_without_a_key_fails_visibly` |
| Una organización nunca escribe en el tenant de otra | Cada evento lleva su organización; la clave se elige por ella | `test_events_and_deliveries_never_cross_organizations` |

**Lección registrada:** la primera versión comparaba marcas temporales en Python para el *fencing*. En SQLite las fechas vuelven sin zona horaria, la comparación fallaba en silencio y los eventos quedaban en `processing` para siempre. Se sustituyó por un token. La prueba que lo detectó es `test_two_workers…` y los tests de fencing.

## Política de minimización de datos

| Ajuste | Valor por defecto | Efecto |
|---|---|---|
| `CONTEXTRAG_CONTENT_MODE` | `masked` | Se envía el texto de las páginas **con PII enmascarada** y los metadatos indican `pii_masked: true` |
| `CONTEXTRAG_ALLOW_RAW_TEXT` | `false` | Sin esta opción explícita, `raw` no se acepta. En `staging` y `production` **nunca** se permite texto sin enmascarar |

**Consecuencias del texto enmascarado.** El enmascarado sustituye nombres, cuentas, teléfonos y similares por marcadores (`[EMAIL_1]`). Eso protege datos personales, pero:

* empeora la recuperación cuando la pregunta contiene el dato enmascarado («¿cuánto cobra Juan Pérez?» no encuentra «[PERSON_1]»);
* el enmascarado de nombres y direcciones es heurístico: no garantiza anonimización (ver `docs/INGESTION.md`);
* la respuesta citará un texto que **no es el original**. Por eso el contrato lo marca explícitamente.

Enviar texto sin enmascarar necesita una decisión documentada por la organización cliente, y en producción no está disponible.

## Permisos y revocación

* **Autorización de ContextRAG = clave por tenant**, no por usuario. ContextRAG liga cada clave API a un tenant y a unos grupos ACL; DOCMIND nunca envía con una clave de otra organización.
* **Revocar un miembro en DOCMIND** corta su acceso a DOCMIND de inmediato (probado en `test_cross_org_negative.py`), pero **no cambia nada en ContextRAG**: un usuario no tiene permisos propios allí. La revocación de conocimiento en ContextRAG se hace revocando o rotando la clave del tenant.
* **Cambiar permisos a un documento** no existe hoy en DOCMIND: no hay ACL por documento. El campo `acl` del contrato viaja vacío por defecto. Si se añaden ACL por documento, habrá que emitir un evento de upsert al cambiarlas; la clave del evento no cambia con el ACL, así que habría que incluirlo en el resumen. **Pendiente de diseño.**
* **Eventos pendientes al cambiar permisos:** un upsert que ya está en cola se entrega con el contenido actual de la base de datos en ese momento, no con el que tenía al encolarse.

## Operación

* **Activar:** `CONTEXTRAG_SYNC_ENABLED=true`, `CONTEXTRAG_URL=https://…`, `CONTEXTRAG_KEYS_FILE=/ruta/fuera/del/repositorio.json` (`{"<organización>": "<clave admin>"}`), `CONTEXTRAG_ALLOWED_HOSTS=<host>`.
* **Observar:** `GET /api/v1/contextrag/events?organization_id=…` (solo administradores de la organización, nunca claves API): recuentos por estado y últimos problemas con su código de error. No incluye contenido.
* **Reintentos:** error transitorio → backoff de 2 min a 1 h, hasta `CONTEXTRAG_MAX_ATTEMPTS` (25); después `failed` con reintento lento cada 6 h. Un 4xx o una clave ausente pasan a `failed` de inmediato.
* **Recuperarse:** un evento `failed` se reabre al volver a pedir el mismo cambio, o al corregir la configuración y esperar su reintento.

## Procedimiento para validar con ContextRAG real

```bash
# 1. Instalar ContextRAG en el mismo entorno (desde un clon local)
pip install -e /ruta/a/contextrag_engine
# 2. Pruebas de contrato en proceso (no requieren red)
cd backend && pytest tests/test_contextrag_sync.py tests/test_contextrag_integration.py -v
# 3. Validación contra un servicio ContextRAG desplegado (pendiente: requiere credenciales de prueba)
#    Arrancar ContextRAG con CONTEXTRAG_KEYS_FILE de prueba, luego en DOCMIND:
#    CONTEXTRAG_SYNC_ENABLED=true CONTEXTRAG_URL=http://localhost:8000 CONTEXTRAG_KEYS_FILE=… \
#    CONTEXTRAG_ALLOWED_HOSTS=localhost  python -m app.worker   (y el beat de Celery)
```

## Limitaciones operativas conocidas

* Un evento `failed` (por ejemplo `NO_KEY_FOR_TENANT` o un 401) se reintenta cada 6 horas **indefinidamente** hasta que alguien lo cancele o se corrija la configuración. Verificado en el ensayo en vivo: 175 eventos de otras organizaciones permanecieron en `failed` con `attempts` creciendo.
* **Cancelación de upserts (administradores):** `POST /api/v1/contextrag/events/{event_id}/cancel` cancela un upsert en estado `failed` o `pending`. Requiere `confirm` igual al identificador del evento, un `reason` de 10 a 300 caracteres y rol ADMIN; deja un registro de auditoría en la misma transacción. Los borrados (`retire`) no se cancelan nunca, porque dejarían un documento borrado recuperable en ContextRAG; los eventos en `processing` y los finales se rechazan con 409. La actualización es condicional, así que una entrega que tome el evento a la vez gana y la cancelación se rechaza. Cancelar no borra nada en ContextRAG: un documento ya publicado sigue allí hasta que se retire por otro cambio. Ver `docs/API.md`.
* Un 200 con cuerpo que no es JSON se registra como `failed` con el código `BAD_RESPONSE` (verificado en pruebas).

## Qué no se ha probado todavía

* Un despliegue de ContextRAG en un entorno accesible, con claves de prueba gestionadas fuera del repositorio.
* La caída de un proceso worker en mitad de una entrega real (el reinicio se simula con un claim obsoleto).
* El timeout del cliente es fijo (15 s) en código; no hay variable de entorno para cambiarlo.
* La entrega de eventos en un entorno con Redis caído (las tareas periódicas necesitan el broker).

## Pendiente para Santiago

* Decidir si la sincronización se activa en algún entorno real y con qué organizaciones.
* Crear claves de administración de ContextRAG **de prueba**; ninguna clave real debe usarse en esta fase.
* Confirmar la política de texto enmascarado con el cliente.
* Diseñar las ACL por documento si se necesitan (ver «Permisos»).
* Desplegar ContextRAG en un entorno de pruebas y ejecutar el procedimiento anterior.

---
Relacionado: [[empresa]]
