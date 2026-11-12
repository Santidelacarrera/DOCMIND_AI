# Unificación DOCMIND + ContextRAG — registro de progreso

Registro principal de la unificación. Decisión de arquitectura: [ADR-005](decisions/ADR-005-contextrag-unification.md).

**Fase actual:** G (documentación y cierre). Entregas 1–4 implementadas. La entrega 1 está fusionada en `main` (PR #74); las entregas 2–4 están en la rama `claude/contextrag-unification-2`.
**Último punto verificado:** 2026-11-12. Suite backend completa en SQLite. Pruebas nuevas en PostgreSQL 17, con RLS aplicada y sin ella. Migración 0011 probada en ambos sentidos. Evaluación RAG en dos corpus, dos embedders y dos modos de respuesta. Detalle en «Criterios de aceptación».

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
| Operación | Celery + Redis, Prometheus (11 alertas), OpenTelemetry opcional, copias pg_dump, S3/KMS, ClamAV, webhooks | promtool, moto, CI `docker` |
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
| Reranker | Exclusiva de ContextRAG | **No portado.** El léxico es redundante con la selección de frases de DOCMIND. El cross-encoder necesita `torch` y un modelo de ~1 GB, y en los dos corpus el primer resultado ya es correcto en el 100 % con MiniLM o con `hybrid`: no hay margen medible que lo justifique |
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

## Pendiente

Todo lo que se puede hacer sin el propietario está hecho, salvo lo descartado con su motivo en la matriz (reranker, DeBERTa, Ragas, orden «edges»). Lo que falta depende de él:

1. Validar `ASK_MODE=generative` con un LLM real (Vertex). Las pruebas usan un modelo simulado y el generador sin conexión.
2. Medir `hybrid`, la generación y los conflictos con documentos reales anonimizados.
3. Retirar el outbox hacia ContextRAG externo (`contextrag_sync.py`, migración destructiva de `contextrag_events`). Hace falta su aprobación.
4. Definir `CONVERSATION_ENCRYPTION_KEY` en staging y producción (un secreto: no lo genero yo).

## Bloqueos que dependen de Santiago

* Documentos reales anonimizados (30–50) para medir `hybrid`, la generación y los conflictos.
* Credenciales LLM (Vertex AI) para validar la respuesta generativa.
* Aprobación para retirar la integración externa con ContextRAG.
* `CONVERSATION_ENCRYPTION_KEY` y el resto de secretos de staging.

## Criterios de aceptación (sección 23 del encargo)

| # | Criterio | Estado | Evidencia |
|---|---|---|---|
| 1 | Una sola plataforma | IMPLEMENTADO Y VALIDADO | ContextRAG portado a `app/rag`; ningún servicio aparte es necesario |
| 2 | Una sola interfaz | IMPLEMENTADO Y VALIDADO | Next.js: `/ask`, `/conversations`, documentos (e2e en CI) |
| 3 | Arquitectura modular | IMPLEMENTADO Y VALIDADO | `docs/architecture.md`, ADR-001 y ADR-005 |
| 4 | Pipeline documental unificado | IMPLEMENTADO Y VALIDADO | Un solo pipeline (`ingestion/`, `worker.py`) |
| 5 | Extracción documental | IMPLEMENTADO Y VALIDADO | Corpus de extracción, puerta F1 ≥ 0,89 en CI |
| 6 | OCR | IMPLEMENTADO Y VALIDADO | Tesseract en CI (documentos escaneados del corpus de extracción) |
| 7 | Modelo documental canónico | IMPLEMENTADO Y VALIDADO | Un fragmento = documento + página + `chunk_index` + tenant + proyecto; sin segundo chunker |
| 8 | Indexación consistente | IMPLEMENTADO Y VALIDADO | Reindexar borra lo anterior (`test_document_lifecycle_e2e`) |
| 9 | Búsqueda semántica | IMPLEMENTADO Y VALIDADO | MiniLM multilingüe, evaluación |
| 10 | Búsqueda híbrida | IMPLEMENTADO Y VALIDADO (opcional) | `RETRIEVAL_MODE=hybrid`, puerta en CI |
| 11 | BM25 | IMPLEMENTADO Y VALIDADO | `app/rag/bm25.py`, `test_hybrid_retrieval` |
| 12 | Qdrant | IMPLEMENTADO Y VALIDADO | Cliente en memoria en CI; servidor real en `test_live_services` (opcional) |
| 13 | Re-ranking | PARCIAL | Fusión RRF y selección léxica de frases. Cross-encoder no portado, con su motivo |
| 14 | Verificación de evidencia | IMPLEMENTADO Y VALIDADO | Verificación por frase (`app/rag/verification.py`) y regla de evidencia suficiente |
| 15 | Consultas individuales | IMPLEMENTADO Y VALIDADO | `/projects/{id}/ask` |
| 16 | Consultas sobre colecciones | IMPLEMENTADO Y VALIDADO | `/ask`, sobre todos los expedientes legibles |
| 17 | Respuestas con citas | IMPLEMENTADO Y VALIDADO | `citation_accuracy` 1,0 con MiniLM |
| 18 | Abstención | IMPLEMENTADO Y VALIDADO | `abstention_accuracy` 1,0 en los dos corpus |
| 19 | Memoria conversacional segura | IMPLEMENTADO Y VALIDADO | `test_conversations` en SQLite y PostgreSQL con RLS |
| 20 | Aislamiento multi-tenant | IMPLEMENTADO Y VALIDADO | RLS fase 3, 0 fugas en la evaluación, pruebas entre organizaciones |
| 21 | Actualización documental consistente | IMPLEMENTADO Y VALIDADO | `/documents/{id}/versions` |
| 22 | Eliminación verificable | IMPLEMENTADO Y VALIDADO | Outbox con verificación; vectores comprobados a 0 en la prueba de ciclo de vida |
| 23 | Evaluación RAG | IMPLEMENTADO Y VALIDADO | Dos corpus y tres puertas en CI. Ragas no portado |
| 24 | Observabilidad | IMPLEMENTADO Y VALIDADO | Métricas de recuperación y 11 alertas validadas con promtool |
| 25 | Pruebas automatizadas | IMPLEMENTADO Y VALIDADO | Suite completa y CI con 9 comprobaciones |
| 26 | Infraestructura reproducible | IMPLEMENTADO Y VALIDADO | Docker Compose para desarrollo y producción, copias de seguridad |
| 27 | Documentación | IMPLEMENTADO Y VALIDADO | README, arquitectura, API, entorno, ADR-005 e informes |
| — | Generación con un LLM real | BLOQUEADO | Faltan credenciales de Vertex |
| — | Calidad con documentos reales | BLOQUEADO | Faltan documentos reales anonimizados |

## Cómo continuar

```bash
cd backend
pytest tests/test_generative_answers.py tests/test_conversations.py tests/test_document_lifecycle_e2e.py -q
python -m app.evaluation.rag --corpus extended --retrieval hybrid --answer-mode generative
```

Siguiente paso: con credenciales de Vertex, `ASK_MODE=generative LLM_PROVIDER=vertex` sobre el corpus ampliado y sobre documentos reales. Hay que comparar `dropped_claims` y la tasa de abstención frente al modo extractivo.
