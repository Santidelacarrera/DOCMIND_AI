# DOCMIND AI — preparación final para producción

**Fecha:** 2026-11-12. **Rama:** `claude/final-readiness`, desde `main` en `45670be` (PR #76 fusionado).
**Conclusión:** **IMPLEMENTADO, CON VALIDACIONES EXTERNAS PENDIENTES.** Detalle al final.

Este documento es el punto de continuidad: otra sesión puede retomar desde «Próximas acciones». El historial completo de la unificación está en `docs/UNIFICATION_PROGRESS.md`.

## Niveles de validación

Se distinguen cinco niveles, y ninguno implica el siguiente:

1. **Implementado:** el código existe.
2. **Probado automáticamente:** pruebas unitarias y de integración en SQLite y PostgreSQL, y CI.
3. **Validado en un stack real local:** Docker Compose completo (PostgreSQL 17, Redis, Qdrant 1.20, API, worker, beat y web), con datos ficticios.
4. **Validado en staging.**
5. **Desplegado y validado en producción.**

**Ningún criterio ha llegado a los niveles 4 o 5:** no hay un entorno de staging autorizado.

## Estado por etapa

| # | Etapa | Estado | Evidencia |
|---|---|---|---|
| 1 | Auditoría de los 27 criterios | Hecha | `UNIFICATION_PROGRESS.md`, «Criterios de aceptación», revisada contra el código y las pruebas |
| 2 | Vertex AI y modelos reales | **BLOQUEADA** | No hay credenciales en esta máquina: ni `application_default_credentials.json`, ni `GOOGLE_APPLICATION_CREDENTIALS`, ni claves de LLM en `.env`. `gcloud` está instalado, pero no se usó una cuenta personal sin autorización. Integración preparada y probada sin red (`test_vertex_provider`, `test_generative_answers`). Pasos: `OWNER_SETUP.md` §1 |
| 3 | Evaluación con documentos reales | **BLOQUEADA** | No hay documentos (`backend/eval_private/` no existe). Procedimiento listo y probado con un PDF real generado (`test_a_corpus_of_real_files_can_be_evaluated`). Pasos: `OWNER_SETUP.md` §3 |
| 4 | Seguridad y aislamiento | Hecha (nivel 3) | Intentos de saltarse los controles: se encontraron y corrigieron 2 fallos (abajo). bandit sin hallazgos; npm audit con 0 vulnerabilidades; pip-audit sin vulnerabilidades en las dependencias de la aplicación (solo en el `pip` del entorno local; la imagen ahora instala `pip>=26.2`). Registros del stack sin contenido ni secretos |
| 5 | Ciclo de vida documental | Hecha (nivel 3) | Humo `--lifecycle` 28/28; `test_document_lifecycle_e2e`; cuota de versiones corregida; redacción de fuentes retiradas |
| 6 | RAG y calidad | Hecha con corpus ficticios; con documentos reales, **BLOQUEADA** | `docs/evaluation/*.md`: 0 fugas y 0 usos del documento con inyección en todas las configuraciones |
| 7 | Memoria conversacional | Hecha (nivel 3) | Persistencia tras cerrar sesión y volver a entrar, redacción de fuentes retiradas, cifrado comprobado en PostgreSQL real, aislamiento |
| 8 | Frontend | Hecha (nivel 3) | Playwright 11/11, tres veces seguidas; nueva prueba para móvil (375 px) que encontró y corrigió desbordamientos |
| 9 | Rendimiento y resiliencia | Hecha (nivel 3) | Mediciones abajo; PostgreSQL caído → 503 (antes, 500); Qdrant caído → 503; LLM caído → 503; reranker caído → se conserva el orden |
| 10 | Retirada de ContextRAG Sync | **Plan listo, sin ejecutar** | `docs/CONTEXTRAG_SYNC_RETIREMENT_PLAN.md`. Necesita tu autorización |
| 11 | Staging y producción | Preparada, **sin staging** | Hueco crítico corregido: la imagen no traía `fastembed`. Modelos provisionados y verificados sin red en la imagen de producción |
| 12 | Observabilidad | Hecha (nivel 2–3) | 12 alertas con `promtool check` y `test rules`. Registros revisados: 0 apariciones de texto de documentos, preguntas, tokens, contraseñas o claves |
| 13 | Testing final | Hecho | Abajo |
| 14 | Documentación | Hecha | Archivos listados abajo |
| 15 | Git y entregas | PR desde `claude/final-readiness` | Se fusiona solo con todas las comprobaciones en verde |

## Problemas encontrados y corregidos en esta misión

| # | Problema | Gravedad | Corrección | Prueba |
|---|---|---|---|---|
| 1 | La imagen Docker no instalaba `fastembed`. En staging y producción el embedder `hashing` está prohibido y el valor por defecto es `local`, así que **un despliegue siguiendo la documentación no podía indexar ni responder** | Crítica | Dockerfile con el extra `embeddings`, `EMBEDDING_CACHE_DIR=/opt/models`, volumen `models` (solo lectura) en `docker-compose.prod.yml` y `scripts/fetch_models.py` (descarga y verifica sin red) | Imagen construida; los dos modelos se cargan con `--network none` |
| 2 | Las versiones nuevas de un documento **no pasaban la cuota de páginas del plan FREE** | Media | `_check_page_quota`, compartida entre subidas y versiones | `test_new_versions_count_against_the_free_page_quota` |
| 3 | Una conversación seguía mostrando el texto citado de documentos **borrados, o de expedientes a los que el usuario ya no tiene acceso**, y lo pasaba como memoria | Media (privacidad y autorización) | Al leer cada turno se comprueba el acceso a sus fuentes: si alguna ya no es legible, el texto y las citas se sustituyen por un aviso de contenido retirado y el turno no se usa como memoria | `test_answers_quoting_a_deleted_document_are_withdrawn`, `test_answers_from_a_case_the_user_lost_access_to_are_withdrawn` |
| 4 | Con PostgreSQL caído, las peticiones devolvían 500 | Media | Manejador global: 503 `DATABASE_UNAVAILABLE` con `Retry-After: 5`; `/ready` también falla | `test_a_database_outage_is_a_retryable_503_not_a_500` |
| 5 | En un teléfono, el panel y la página de miembros se salían de la pantalla (348 px y 42 px) | Baja (usabilidad) | La navegación pasa a varias líneas; estadísticas, paneles y listas adaptadas a pantallas estrechas | `frontend/tests/e2e/responsive.spec.ts` |
| 6 | La imagen traía un `pip` con avisos de seguridad publicados | Baja | `pip>=26.2` en la imagen (comprobado: 26.3) | Imagen construida |

## Mediciones (stack Docker Compose local, Windows con Docker Desktop, sin GPU; no son afirmaciones de capacidad)

| Prueba | Resultado |
|---|---|
| `/ask`: 200 preguntas, concurrencia 8 (API con `--workers 2`, como en producción) | 0 respuestas incorrectas, 0 errores HTTP, 11,3 preguntas/s, p50 48 ms, p95 188 ms, máximo 2,05 s |
| Conversaciones: 100 preguntas, concurrencia 4 | 0 respuestas incorrectas, 0 errores, 8,6 preguntas/s, p50 57 ms, p95 109 ms |
| Subidas: 40 documentos, concurrencia 8 (desde la red de Docker) | 20 aceptadas y procesadas, 0 fallidas, 0 pendientes; 20 rechazadas con 402 por la cuota FREE (20 páginas al mes), aplicada exactamente bajo concurrencia; p50 0,32 s, p95 0,98 s; 273 documentos/min de principio a fin |
| Disponibilidad: desde la subida hasta que se puede responder | 1,1–5,5 s |
| Reranking (cross-encoder, CPU) | Entre 1 y 2 s más por pregunta (`docs/evaluation/rerank-2026-11-12.md`) |

**Hallazgo de entorno (no es un fallo del producto):**
* Las subidas concurrentes desde Windows hacia el puerto publicado por Docker Desktop provocan reinicios de conexión. El servidor registró todas las peticiones como aceptadas, y dentro de la red de Docker no se reproduce.
* El servidor de desarrollo con `--reload` se cayó por falta de memoria de su vigilante de ficheros. Producción no usa `--reload`.

## Pruebas ejecutadas (resultados reales)

| Batería | Resultado |
|---|---|
| Suite backend completa (SQLite) | **712 passed, 91 skipped, 0 failed** (10 min 36 s) |
| PostgreSQL 17 con RLS aplicada y sin ella (pruebas nuevas y de aislamiento) | ver `UNIFICATION_PROGRESS.md` (44/2 omitidas y 64 en la entrega 5); esta entrega: **35 passed, 2 skipped** con RLS aplicada (seguridad de esta misión, conversaciones, ciclo de vida, aislamiento, RLS fase 3) y **63 passed** sin ella |
| Seguridad y resiliencia de esta misión (`test_readiness_security.py`) | 5 passed |
| Playwright contra el stack completo | 11/11, tres veces seguidas |
| Humo del stack completo (`staging_smoke.py --lifecycle`) | 28/28 (entrega 5; misma imagen y migraciones) |
| Carga (`ask_load_smoke.py`, `load_smoke.py`) | tabla anterior |
| Imagen de producción + `fetch_models.py` sin red | correcto: los 2 modelos cargan |
| `promtool check rules` / `test rules` | 12 reglas, SUCCESS |
| bandit, npm audit, pip-audit, ruff, mypy, tsc, eslint | sin hallazgos en el código de la aplicación |
| CI de la PR | ver la PR |

## Respuestas del informe

* **A. ¿Está DOCMIND AI completamente implementado?** Sí, todo lo que pedían las misiones, salvo lo descartado con su motivo (DeBERTa, Ragas y el orden «edges»; ver `UNIFICATION_PROGRESS.md`).
* **B. ¿Están cumplidos los 27 criterios?** Están implementados y auditados, y en el nivel 2 o 3. Ninguno en el nivel 4 o 5.
* **C. ¿Cuáles están validados con evidencia real?** Con servicios reales locales (PostgreSQL, Qdrant y Redis en contenedores, imagen real, navegador real): 1–4, 7, 8, 10–13, 15–22, 24–27. Con **modelos y documentos reales**: ninguno (bloqueado).
* **D. Pruebas:** tabla anterior.
* **E. Problemas encontrados y corregidos:** tabla «Problemas encontrados».
* **F. Riesgos que permanecen:**
  * calidad con documentos reales sin medir;
  * modo generativo sin probar con un LLM real;
  * no hay rotación de `CONVERSATION_ENCRYPTION_KEY` (cambiarla deja ilegibles las conversaciones anteriores);
  * la tabla `contextrag_events` crece mientras no se retire la sincronización;
  * el reranker añade entre 1 y 2 s en CPU si se activa;
  * Alertmanager no está conectado.
* **G. Qué falta de fuera:**
  * credenciales de Vertex AI;
  * 30–50 documentos reales anonimizados con sus preguntas;
  * `CONVERSATION_ENCRYPTION_KEY` de staging y producción;
  * un entorno de staging con dominio y HTTPS;
  * destinatarios de las alertas.
* **H. ¿Está preparado para staging?** Sí, en cuanto a código, imagen y procedimiento (`DEPLOYMENT.md`, `STAGING.md`, `OWNER_SETUP.md`). Falta el entorno y sus secretos.
* **I. ¿Está preparado para producción?** **No todavía.** Antes hay que validarlo en staging, con RLS activada y con documentos reales.
* **J. Qué requiere autorización del propietario:**
  * la retirada de ContextRAG Sync (por fases);
  * el uso de cualquier credencial o recurso de pago;
  * activar RLS en staging;
  * cualquier despliegue.

## Archivos modificados en esta misión

`backend/Dockerfile`, `docker-compose.prod.yml`, `backend/app/main.py`, `backend/app/routers/{conversations,documents}.py`, `frontend/app/styles.css`, `backend/scripts/{fetch_models,ask_load_smoke}.py` (nuevos), `backend/tests/test_readiness_security.py` (nuevo), `frontend/tests/e2e/responsive.spec.ts` (nuevo), `docs/{FINAL_PRODUCTION_READINESS,CONTEXTRAG_SYNC_RETIREMENT_PLAN}.md` (nuevos), `docs/{DEPLOYMENT,OWNER_SETUP,UNIFICATION_PROGRESS}.md`.

## Próximas acciones (en orden)

1. **Propietario:** documentos reales (`OWNER_SETUP.md` §3). Con ellos se ejecuta la evaluación y se deciden `RETRIEVAL_MODE`, `RERANK_MODEL` y `ASK_MODE`.
2. **Propietario:** credenciales de Vertex (§1). Con ellas se valida `ASK_MODE=generative` y la extracción real.
3. **Propietario:** entorno de staging y secretos (§2 y `STAGING.md`). Después:
   * `fetch_models.py`;
   * `staging_smoke.py --lifecycle`;
   * activar RLS (`RLS_DESIGN.md`);
   * `ask_load_smoke.py`.
4. **Propietario:** decidir las fases de `CONTEXTRAG_SYNC_RETIREMENT_PLAN.md`.

Para retomar desde otra sesión: leer este documento y `UNIFICATION_PROGRESS.md`, ejecutar `cd backend && pytest -q` y continuar por el primer punto desbloqueado.
