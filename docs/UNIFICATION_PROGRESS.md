# Unificación DOCMIND + ContextRAG — registro de progreso

Registro principal de la unificación. Decisión de arquitectura: [ADR-005](decisions/ADR-005-contextrag-unification.md).

**Fase actual:** G (documentación y cierre). Entregas 1–5 implementadas. La 1 está fusionada (PR #74) y las 2–4 también (PR #75, 9/9 comprobaciones en verde); la 5 está en la rama `claude/contextrag-unification-3`.
**Último punto verificado:** 2026-11-12. Suite backend completa en SQLite. Pruebas nuevas en PostgreSQL 17, con RLS aplicada y sin ella. Migración 0011 probada en ambos sentidos. Evaluación RAG en dos corpus, dos embedders, dos modos de respuesta y con o sin reranker. Stack Docker Compose completo: prueba de humo 28/28 y Playwright 11/11. Detalle en «Criterios de aceptación».

## Fase A — Auditoría

### DOCMIND_AI (producto principal) — estado real

| Área | Implementación | Pruebas |
|---|---|---|
| API | FastAPI, routers `auth`, `documents`, `projects`, `members`, `admin`, `ask`, `schemas` | ~650 pruebas backend |
| Datos | SQLAlchemy + Alembic (10 migraciones), PostgreSQL 17 | jobs `backend-postgres` y `backend-postgres-rls` |
| Multi-tenancy | Membresía por organización, autorización por expediente (proyecto), claves API con alcances, RLS fase 3 (`docmind_app` sin `BYPASSRLS`, ADR-004) | `test_cross_org_negative`, `test_rls_phase3`, suite completa como `docmind_app` |
| Ingesta | Saneado de PDF (contenido activo), texto por página, OCR, DOCX, normalización, enmascarado de PII antes de trocear, fragmentos que no cruzan páginas, marca de inyección | `test_ingestion`, `test_word_and_citations` |
| Extracción | LLM (mock, OpenAI, Gemini, Vertex) con esquema JSON, validación, procedencia por campo, revisión manual | `test_vertex_provider`, `test_api` |
| Recuperación | Qdrant, una colección, clave de tenant `is_tenant`, filtro de proyecto dentro de la consulta, guardia de modelo de embeddings | `test_semantic_answering`, `test_rag_evaluation` |
| Respuestas | Extractivas: citan frases literales con documento, página y fragmento; abstención si la evidencia no basta (ADR-002/003) | `test_case_query`, evaluación RAG |
| Ciclo de vida | Outbox de borrado de vectores, reconciliación, retención, reindexado | `test_ops_modules`, `test_retention` |
| Operación | Celery + Redis, Prometheus (11 alertas al inicio; 12 tras la entrega 5), OpenTelemetry opcional, copias pg_dump, S3/KMS, ClamAV, webhooks | promtool, moto, CI `docker` |
| Frontend | Next.js: login, organizaciones, documentos, expedientes, `/ask` con citas | `frontend`, `e2e` (Playwright) |

### ContextRAG_Engine (referencia, `C:\Proyectos\ContextRAG_Engine`, rama `chore/production-readiness`, commit `3ce473c`, sin cambios locales; no se modificó)

| Capacidad | Código | Estado real (según el código) |
|---|---|---|
| BM25 (Plus, incremental) | `retrieval/bm25.py` | Implementado |
| Híbrida dense + BM25 + RRF | `retrieval/hybrid_search.py` | Implementado |
| Parent/child | `core/chunking.py`, `HybridRetriever.retrieve_detailed` | Implementado sobre su propio chunker |
| Reranking | `retrieval/reranker.py`: léxico, cross-encoder, Cohere, envoltura con timeout | El cross-encoder y Cohere necesitan modelo o API |
| Verificación NLI | `verification/nli_verifier.py`: `LexicalEntailment` (heurística, falla cerrada) y `DebertaNLI` | El modelo DeBERTa necesita `torch` |
| Conflictos | `verification/conflicts.py`: estructurado y NLI | El estructurado es heurístico y sin calibrar |
| Términos nombrados ausentes | `verification/entities.py` | Implementado |
| Inyección | `verification/injection.py` (lista de patrones) | Implementado |
| Presupuesto de contexto | `context/budget.py`: deduplicación Jaccard, presupuesto, orden «edges» | Implementado; el beneficio de «edges» es una hipótesis sin medir |
| Memoria conversacional | `memory/store.py`: SQLite, cifrado por tenant, compactación, borrado | Implementado sobre SQLite, no PostgreSQL |
| Autorización | Clave API por tenant + grupos ACL | Modelo distinto del de DOCMIND |
| Evaluación | Ragas (opcional), benchmarks de recuperación, inyección, conflictos y entidades | Corpus propios |

Sus pruebas (60 ficheros en `tests/`) **no se ejecutaron en esta sesión**: el código portado se prueba aquí, con los datos y contratos de DOCMIND.

## Fase B — Matriz de integración y diseño

| Funcionalidad | Clasificación | Decisión |
|---|---|---|
| Ingesta, OCR, DOCX, PII, fragmentos por página | Exclusiva de DOCMIND, más completa | **Se mantiene** como pipeline único |
| Chunker de ContextRAG (parent/child) | Duplicada | **No se porta.** Parent/child, si se mide útil, se hará agrupando fragmentos canónicos |
| Búsqueda vectorial con filtro de tenant y proyecto | Duplicada; la de DOCMIND tiene filtro de proyecto y guardia de modelo | **Se mantiene la de DOCMIND** |
| BM25 + RRF | Exclusiva de ContextRAG | **Portado** (entrega 1), opcional |
| Conflictos estructurados | Exclusiva de ContextRAG | **Portado** (entrega 1) |
| Patrones de inyección | Duplicada parcialmente | **Fusionados** sin los patrones que dan falsos positivos (entrega 1) |
| Respuesta extractiva con abstención | Exclusiva de DOCMIND | **Se mantiene**; no puede inventar texto |
| Respuesta generativa con verificación por afirmación | Exclusiva de ContextRAG | **Portado** (entrega 2), opcional: `ASK_MODE=generative` |
| Términos nombrados ausentes → abstención | Exclusiva de ContextRAG | **Portado** (entrega 2), en los dos modos de respuesta |
| Reranker | Exclusiva de ContextRAG | **Portado en la entrega 5** tras reevaluarlo: cross-encoder ONNX (fastembed, sin `torch`), opcional. El descarte anterior («el corpus ya está saturado») no era un motivo suficiente |
| Memoria conversacional | Exclusiva de ContextRAG; incompatible (SQLite, otra autorización) | **Rediseñada** (entrega 3) en PostgreSQL con RLS y AES-GCM por organización |
| Presupuesto de contexto | Exclusiva de ContextRAG | **Portado en parte** (entrega 2): deduplicación y límite de caracteres. El orden «edges» no se porta: el propio ContextRAG lo declara una hipótesis sin medir |
| DeBERTa NLI | Exclusiva de ContextRAG | **No portado**: necesita `torch`. El verificador acepta cualquier objeto con `entailment(pairs)` para conectarlo más adelante |
| Ragas (juez LLM) | Exclusiva de ContextRAG | **No portado**: necesita una clave de LLM y es opcional en el propio ContextRAG. Las métricas de DOCMIND son deterministas y no dependen de una API externa |
| Autorización por clave de tenant + ACL | Incompatible | **Descartada**; manda la de DOCMIND |
| Outbox hacia ContextRAG externo | Exclusiva de DOCMIND | Se mantiene desactivado; se retira al terminar la unificación |

## Fase D — Entrega 1 (2026-11-12)

### Trabajo realizado

* `backend/app/rag/`: `bm25.py`, `fusion.py`, `lexical.py`, `conflicts.py`.
* `answering.retrieve/answer(mode=...)`: modo `hybrid` (vectorial + BM25 → RRF). Un fragmento encontrado solo por vía léxica recibe como `score` el coseno de su vector almacenado, para que `min_score` se aplique igual a todos. El filtrado por tenant, proyecto y vigencia se aplica después de fusionar, como antes.
* `QdrantStore.scroll_chunks` y `QdrantStore.vectors`, ambos con filtro de tenant. La escritura y el borrado invalidan el corpus léxico del proceso.
* `Answer.conflicts` y campo `conflicts` en `/api/v1/ask` y `/api/v1/projects/{id}/ask`. La interfaz los muestra.
* `looks_like_injection`: patrones en español, revelación del prompt y ocultación.
* Métricas: `docmind_retrieval_lexical_total{outcome}` y `docmind_retrieval_duration_seconds{kind}` (`kind`, no `mode`: `test_observability_surface` solo admite etiquetas de una lista cerrada).
* `python -m app.evaluation.rag --retrieval dense|hybrid`.

### Archivos modificados

`backend/app/{core,metrics}.py`, `backend/app/ingestion/{answering,pipeline,text,vectorstore}.py`, `backend/app/routers/ask.py`, `backend/app/evaluation/rag.py`, `backend/app/rag/*` (nuevo), `backend/tests/{test_hybrid_retrieval,test_source_conflicts}.py` (nuevos), `backend/tests/{test_ingestion,test_case_query}.py`, `frontend/app/ask/page.tsx`, `.env.example`, `docs/ENVIRONMENT.md`, `docs/decisions/ADR-005-contextrag-unification.md`, `docs/evaluation/hybrid-retrieval-2026-11-12.md`, este documento.

### Pruebas y resultados (ejecutados)

| Comando | Resultado |
|---|---|
| `pytest tests/test_hybrid_retrieval.py` | 9 passed |
| `pytest tests/test_source_conflicts.py` | 6 passed |
| `pytest tests/test_hybrid_retrieval.py tests/test_semantic_answering.py tests/test_rag_evaluation.py tests/test_word_and_citations.py tests/test_case_query.py` | 88 passed (antes de añadir `conflicts`) |
| `pytest` (suite completa, SQLite) | 676 passed, 87 skipped, 0 failed (17 min 48 s) |
| `python -m app.evaluation.rag --retrieval dense/hybrid` (hashing y MiniLM multilingüe) | tabla en `docs/evaluation/hybrid-retrieval-2026-11-12.md`; 0 fugas entre organizaciones en los cuatro casos |
| `ruff check`, `mypy app`, `tsc --noEmit`, `eslint` | sin errores |

### Decisiones

* `RETRIEVAL_MODE=dense` por defecto. Con el modelo de producción, `hybrid` no mejora este corpus.
* De los patrones de inyección de ContextRAG se excluyen los de rol («Sistema:») y exfiltración («envía … historial»): un fragmento marcado queda fuera de todas las respuestas, así que un falso positivo oculta contenido legítimo. Las pruebas cubren seis frases de empresa que no deben marcarse.
* Los conflictos se devuelven como aviso y no cambian la respuesta citada.

### No validado

* `hybrid` con documentos reales y preguntas por identificadores (**BLOQUEADO**: faltan documentos reales anonimizados).
* `hybrid` contra un Qdrant servidor (en local se usó `QdrantClient(":memory:")`; el job `backend-postgres` de CI usa el mismo modo en memoria).
* Rendimiento del corpus léxico con organizaciones grandes. El límite `RETRIEVAL_LEXICAL_MAX_CHUNKS` evita la carga, pero el coste de memoria no se midió.

## Entregas 2–4 (2026-11-12, rama `claude/contextrag-unification-2`)

### Entrega 2 — respuestas generativas verificadas
* `app/rag/verification.py`: `LexicalEntailment` y `parse_claims` portados de ContextRAG, más `verify()`. Cada frase debe llevar `[n]` y estar implicada por el pasaje que cita. Rechaza cifras distintas, calificadores omitidos («puede»), negaciones, citas inexistentes y frases sin cita.
* `app/rag/generation.py`:
  * pasajes deduplicados y con presupuesto, cercados como datos no confiables (se neutralizan las etiquetas `<passage>` y `<memory>` dentro de los documentos);
  * se pide al modelo `[n]` o `NO_ANSWER`;
  * las frases no verificadas se eliminan, y si no queda ninguna, se abstiene.

  Generadores: `ChatGenerator` (OpenAI, Gemini, Vertex) y `ExtractiveGenerator` (sin conexión).
* `app/rag/entities.py`: si la pregunta nombra algo (un nombre propio o un identificador) que no está en ningún pasaje, se abstiene sin llamar al modelo. Se aplica también al modo extractivo, porque la evaluación ampliada encontró respuestas sobre expedientes inexistentes.
* `ASK_MODE`, `ASK_GENERATION_THRESHOLD`, `ASK_CONTEXT_CHARS` y `ASK_GENERATION_MAX_TOKENS`. La respuesta incluye `mode` y `dropped_claims`. 503 `LLM_UNAVAILABLE`.

### Entrega 3 — conversaciones
* Tablas `conversations` y `conversation_messages` (migración `0011_conversations`), con políticas RLS por `organization_id` (`rls.TENANT_TABLES_0011`) y permisos para `docmind_app` y `docmind_system`.
* `app/conversations.py`: AES-256-GCM con clave HKDF por organización, a partir de `CONVERSATION_ENCRYPTION_KEY`. Los datos asociados (conversación, secuencia, rol) impiden mover una fila. No hay respaldo en claro: sin clave, 503.
* `app/routers/conversations.py`:
  * conversaciones privadas del usuario (404 para cualquier otro, administradores incluidos) y sin claves API;
  * opcionalmente ligadas a un expediente, con el permiso vuelto a comprobar en cada pregunta;
  * turnos inmutables numerados, con conflicto 409 si dos peticiones escriben a la vez;
  * memoria no citable en modo generativo;
  * borrado completo y purga por retención (`CONVERSATION_RETENTION_DAYS`).
* Frontend: página `/conversations`.

### Entrega 4 — ciclo de vida, resiliencia, evaluación ampliada
* `POST /api/v1/documents/{id}/versions`: versión nueva del documento, guardada en `document_versions` y procesada de nuevo. 409 mientras hay un trabajo activo; `unchanged` si es el mismo archivo. Frontend: subida en la página del documento.
* `vector_index._index_document` borra los vectores anteriores antes de indexar, porque el id del fragmento incluye el hash de su contenido. Antes, una versión cambiada habría conservado fragmentos obsoletos. `QdrantStore.delete_document` sin colección no hace nada.
* `/ask` con Qdrant caído → 503 `VECTOR_INDEX_UNAVAILABLE` (antes, 500).
* `conflicts.strip_heading`: encabezados en mayúsculas unidos a la frase por la extracción de PDF.
* Corpus `eval_corpus/rag_questions_extended.json` y opciones `--corpus extended` y `--answer-mode generative`; métricas `conflict_detection` e `injection_documents_used`. Dos pasos nuevos en CI. Informe: `docs/evaluation/rag-extended-2026-11-12.md`.
* `pyproject.toml`: faltaba el paquete `app.rag` (la imagen funcionaba porque ejecuta desde el código copiado).

### Pruebas ejecutadas (entregas 2–4)

| Comando | Resultado |
|---|---|
| `pytest tests/test_generative_answers.py` | 11 passed |
| `pytest tests/test_conversations.py` | 7 passed |
| `pytest tests/test_document_lifecycle_e2e.py` | 3 passed: subir → procesar → indexar → preguntar → versión nueva → reindexar (el texto anterior desaparece) → borrar → vectores eliminados → abstención; otra organización no puede subir versiones; Qdrant caído → 503 |
| `pytest tests/test_source_conflicts.py` | 7 passed |
| PostgreSQL 17 con `TEST_RLS_ENFORCED=1`: conversaciones, ciclo de vida, generación, `test_rls_phase3` | 27 passed, 2 skipped |
| PostgreSQL 17 sin RLS aplicada: conversaciones, `test_rls_policies` (incluye las tablas nuevas) | 58 passed |
| `alembic upgrade head` → `downgrade -1` → `upgrade head` en una base nueva | 2 políticas y las tablas → 0 → 2 |
| Suite backend completa (SQLite) | 695 passed, 91 skipped, 3 failed. Los 3 eran pruebas que recorren todas las rutas o el resumen de retención: necesitaban los cuerpos válidos de las rutas nuevas y la clave `conversations`. Corregidos; esos tres ficheros, 27 passed. La suite completa se vuelve a ejecutar en CI |
| Evaluación ampliada: 2 embedders × 2 recuperaciones × 2 modos | `docs/evaluation/rag-extended-2026-11-12.md`: 0 fugas y 0 usos del documento con inyección en todos los casos |
| `ruff`, `mypy`, `tsc`, `eslint` | sin errores |

## Entrega 5 — reranking, validación de extremo a extremo, preparación para el propietario (2026-11-12, rama `claude/contextrag-unification-3`)

* **Reranking reevaluado y portado** (`app/rag/rerank.py`). Antes se había descartado con un motivo insuficiente: que el corpus ya estaba saturado. Ahora:
  * cross-encoder ONNX con fastembed, sin `torch`;
  * tiempo máximo, y si falla se conserva el orden de la primera etapa;
  * recibe la pregunta enmascarada y nunca amplía el alcance;
  * medido con `bge-reranker-v2-m3-int8`.

  Decisión: opcional (`RERANK_MODEL`), apagado por defecto. Resultados, ventajas, limitaciones y motivo en `docs/evaluation/rerank-2026-11-12.md`.
* **Alerta** `DocMindRerankDegraded` (12 reglas; `promtool check` y `test rules`: SUCCESS).
* **Evaluación con documentos reales preparada**: el corpus admite ficheros reales (`path`) y `--corpus <ruta>`; `backend/eval_private/` está ignorado por git. Prueba: `test_a_corpus_of_real_files_can_be_evaluated`.
* **Instrucciones para el propietario**: `docs/OWNER_SETUP.md` (Vertex, `CONVERSATION_ENCRYPTION_KEY`, evaluación con documentos reales, lo que requiere su autorización). No contiene credenciales.
* **Prueba de humo de extremo a extremo ampliada**: `scripts/staging_smoke.py --lifecycle` (versión nueva y conversaciones).
* **e2e de navegador nuevos**: `frontend/tests/e2e/conversations.spec.ts`:
  * conversación con cita, turnos guardados y borrado;
  * versión nueva desde la página del documento, primero sin cambios y después con cambios.

  En CI, el job e2e genera una clave aleatoria por ejecución, que nunca sale del runner.

### Validación de extremo a extremo (ejecutada el 2026-11-12, stack Docker Compose completo en local)

| Comprobación | Resultado |
|---|---|
| `docker compose up --build --wait` (db, redis, qdrant, api, worker, scheduler, web), con una clave de prueba aleatoria en un *override* temporal | todos los servicios sanos; `alembic upgrade head` llegó a `0011_conversations`; 2 políticas RLS en las tablas de conversaciones |
| `python scripts/staging_smoke.py --base-url http://localhost:8000 --lifecycle` | **28/28 ok**: health, ready, registro de dos organizaciones, subida, procesamiento, extracción, exportación, aislamiento, respuesta citada (documento, página, fragmento), abstención, aislamiento de `/ask`, versión nueva (la respuesta pasa de 23 a 26 días y el texto antiguo no se cita), conversación (creada, con cita, turnos guardados, otra organización 403, borrada) |
| Cifrado en reposo en PostgreSQL real | 2 turnos guardados; ninguno contiene en claro el texto de la pregunta (`position(... in payload) = 0`) |
| `npx playwright test` contra ese stack | **11/11 passed** (los 9 previos y los 2 nuevos) |
| Stack retirado con `down -v` (solo volúmenes de esta prueba, proyecto `docmind-e2e`) | — |
| Suite backend completa (SQLite) con la entrega 5 | **707 passed, 91 skipped, 0 failed** |
| PostgreSQL 17 con `TEST_RLS_ENFORCED=1`: reranking, evaluación (documentos reales), conversaciones, ciclo de vida, `test_rls_phase3`, `test_tenant_isolation` | **44 passed, 2 skipped** |
| PostgreSQL 17 sin RLS aplicada: reranking, conversaciones, `test_rls_policies` | **64 passed** |

## Pendiente

Todo lo que se puede hacer sin el propietario está hecho. Lo que falta necesita su intervención (ver «Bloqueos»). Lo único descartado tras reevaluarlo, con su motivo:

* **DeBERTa NLI:** necesita `torch` (unos 2 GB de dependencias) y un modelo de 1,7 GB. El verificador acepta cualquier modelo con `entailment(pairs)`, así que conectarlo más adelante no cambia el diseño. Se valorará cuando el modo generativo esté validado con un LLM real y se vea cuántas paráfrasis correctas rechaza el verificador léxico (`dropped_claims`).
* **Ragas:** necesita un LLM juez de pago. Las métricas de DOCMIND son deterministas y suficientes para las puertas de CI. Se puede añadir como evaluación opcional cuando haya credenciales.
* **Orden «edges» del contexto:** el propio ContextRAG lo declara una hipótesis sin medir.

## Bloqueos que dependen de Santiago (cómo resolverlos: `docs/OWNER_SETUP.md`)

| Bloqueo | Qué desbloquea | Cómo resolverlo |
|---|---|---|
| Credenciales de Vertex AI | Validar `ASK_MODE=generative` con un modelo real | `OWNER_SETUP.md` §1: proyecto, API, cuenta de servicio con `roles/aiplatform.user`, variables `VERTEX_*` |
| `CONVERSATION_ENCRYPTION_KEY` en staging y producción | Conversaciones en esos entornos | §2: generarla en una máquina de confianza, guardarla en el gestor de secretos y copiarla aparte |
| 30–50 documentos reales anonimizados y sus preguntas | Medir calidad real; decidir `RETRIEVAL_MODE`, `RERANK_MODEL` y `ASK_MODE` | §3: en `backend/eval_private/`, formato del corpus, comandos |
| Autorización para retirar el outbox hacia ContextRAG externo | Quitar código y la tabla `contextrag_events` (migración destructiva) | Decisión explícita. **No se ha tocado.** |
| Staging y activación de RLS | Validar el despliegue real | `docs/RLS_DESIGN.md` «Activación» y `docs/STAGING.md` |

## Criterios de aceptación (sección 23 del encargo) — revisión estricta del 2026-11-12

Estados: **VALIDADO** = implementado y con evidencia ejecutada y reproducible. **VALIDADO EN LOCAL / CI** = la evidencia es de pruebas automatizadas o del stack local, no de un entorno real. **PARCIAL** = falta una parte. **BLOQUEADO** = depende del propietario.

| # | Criterio | Estado | Evidencia verificable | Qué falta |
|---|---|---|---|---|
| 1 | Una sola plataforma DOCMIND | VALIDADO EN LOCAL / CI | Capacidades de ContextRAG en `backend/app/rag/`. Ninguna prueba ni flujo necesita ContextRAG. Stack Compose sin ContextRAG: humo 28/28 | El outbox antiguo hacia ContextRAG sigue en el código, **desactivado**, hasta tu autorización (criterio 6 de tu mensaje) |
| 2 | Una sola interfaz | VALIDADO EN LOCAL / CI | Next.js: `/ask`, `/conversations`, documentos y versiones. Playwright 11/11 en local y job `e2e` en CI | — |
| 3 | Arquitectura modular | VALIDADO | `docs/architecture.md` (tabla de módulos), ADR-001 y ADR-005. Un backend, un modelo documental | — |
| 4 | Pipeline documental unificado | VALIDADO EN LOCAL / CI | Un solo pipeline (`ingestion/`, `worker.py`); `test_document_lifecycle_e2e`; humo con el stack completo | — |
| 5 | Extracción documental | VALIDADO EN LOCAL / CI (sin LLM real) | Puerta F1 ≥ 0,89 en CI con el proveedor simulado; extracción en el humo | Extracción con Vertex real: BLOQUEADO (credenciales) |
| 6 | OCR | VALIDADO EN CI | Tesseract y Poppler en el job `backend`: documentos escaneados del corpus de extracción | Escaneos reales: BLOQUEADO (documentos) |
| 7 | Modelo documental canónico | VALIDADO | Un fragmento = tenant + proyecto + documento + página + `chunk_index`. BM25, reranking, conflictos y generación usan esos mismos fragmentos (`test_hybrid_retrieval` comprueba la página citada) | — |
| 8 | Indexación consistente | VALIDADO EN LOCAL / CI | El reindexado borra los vectores anteriores (`test_document_lifecycle_e2e`: «1800» desaparece); humo: 23 → 26 días | — |
| 9 | Búsqueda semántica | VALIDADO EN LOCAL | MiniLM multilingüe: recall@1 = 1,0 en los dos corpus (`docs/evaluation/*.md`). CI usa el embedder `hashing` | Con documentos reales: BLOQUEADO |
| 10 | Búsqueda híbrida | VALIDADO (opcional) | `RETRIEVAL_MODE=hybrid`; `test_hybrid_retrieval` (9); puerta del corpus ampliado en CI con `hybrid` | Decidir el valor por defecto con documentos reales |
| 11 | BM25 | VALIDADO | `app/rag/bm25.py`; pruebas unitarias e integración por tenant y alcance | — |
| 12 | Qdrant | VALIDADO EN LOCAL / CI | Cliente en memoria en CI; **servidor Qdrant real** (contenedor 1.20) en el humo del stack completo | — |
| 13 | Re-ranking | VALIDADO (opcional, apagado por defecto) | `app/rag/rerank.py`; `tests/test_rerank.py` (6); medición en `docs/evaluation/rerank-2026-11-12.md` | Activarlo depende de la medición con documentos reales y de la latencia aceptable |
| 14 | Verificación de evidencia | VALIDADO EN LOCAL / CI (sin LLM real) | Regla de evidencia suficiente (extractivo); verificación por frase y términos nombrados (generativo); `test_generative_answers` (13) y puerta generativa en CI | Generación con LLM real: BLOQUEADO |
| 15 | Consultas individuales | VALIDADO | `/projects/{id}/ask` con su suite de autorización (`test_case_query`, `test_project_authorization`) | — |
| 16 | Consultas sobre colecciones | VALIDADO | `/ask` sobre todos los expedientes legibles (`accessible_project_ids`) | — |
| 17 | Respuestas con citas | VALIDADO EN LOCAL / CI | `citation_accuracy` 1,0 con MiniLM; humo: documento, página, fragmento y extracto | Con documentos reales: BLOQUEADO |
| 18 | Abstención | VALIDADO EN LOCAL / CI | `abstention_accuracy` 1,0 en los dos corpus (puertas de CI); humo: abstención y otra organización vacía | — |
| 19 | Memoria conversacional segura | VALIDADO EN LOCAL / CI | `test_conversations` (7) en SQLite y PostgreSQL con RLS aplicada; humo; cifrado comprobado en PostgreSQL real; e2e de navegador | Clave real en staging/producción: BLOQUEADO (§2) |
| 20 | Aislamiento multi-tenant | VALIDADO EN LOCAL / CI | Suite completa como `docmind_app` (job `backend-postgres-rls`); recorrido de todas las rutas (`test_tenant_isolation`); 0 fugas en todas las evaluaciones; humo 403/404 | Activar RLS en staging: BLOQUEADO |
| 21 | Actualización documental consistente | VALIDADO EN LOCAL / CI | `/documents/{id}/versions` en API, humo y e2e de navegador | — |
| 22 | Eliminación verificable | VALIDADO EN LOCAL / CI | Outbox con verificación; vectores contados a 0 en `test_document_lifecycle_e2e`; conversaciones borradas (404 y 0 filas) | — |
| 23 | Evaluación RAG | VALIDADO (con un límite) | Dos corpus, recuperación densa o híbrida, respuesta extractiva o generativa, con o sin reranker; tres puertas en CI | Ragas (juez LLM) no portado; documentos reales: BLOQUEADO |
| 24 | Observabilidad | VALIDADO EN CI | Métricas de recuperación, reranking y coste; 12 alertas con `promtool check` y `test rules` | Alertmanager conectado: tuyo (`PRODUCTION_CHECKLIST.md`) |
| 25 | Pruebas automatizadas | VALIDADO | Suite backend completa, PostgreSQL con y sin RLS, e2e y CodeQL en CI | — |
| 26 | Infraestructura reproducible | VALIDADO EN LOCAL | `docker compose up --build --wait` del stack completo y humo 28/28 en esta sesión; Compose de producción y copias de seguridad (ADR-004) | Despliegue en staging: BLOQUEADO |
| 27 | Documentación completa | VALIDADO | README (diagrama de preguntas), `architecture.md`, `API.md`, `ENVIRONMENT.md`, ADR-005, `OWNER_SETUP.md` e informes de evaluación | — |

**Resumen honesto:** los 27 criterios están implementados. Ninguno queda parcial tras la reevaluación del reranking. Ocho de ellos (5, 6, 9, 14, 17, 19, 20 y 26) tienen una parte que solo se puede validar con tus credenciales, tus documentos o un entorno de staging. Hasta entonces, su validación es local o de CI, no de producción.

## Cómo continuar

```bash
cd backend
pytest -q                                   # suite completa
python -m app.evaluation.rag --corpus extended --retrieval hybrid --answer-mode generative
python scripts/staging_smoke.py --base-url http://localhost:8000 --lifecycle   # con el stack levantado
```

Siguiente paso: los bloqueos de la tabla anterior, en este orden: documentos reales (decide los valores por defecto), Vertex (modo generativo) y staging con RLS.
