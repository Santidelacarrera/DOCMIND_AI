# Unificación DOCMIND + ContextRAG — registro de progreso

Registro principal de la unificación. Decisión de arquitectura: [ADR-005](decisions/ADR-005-contextrag-unification.md).

**Fase actual:** D (integración progresiva). Entrega 1 implementada y validada en local; pendiente de CI en la rama `claude/contextrag-unification-1`.
**Último punto verificado:** 2026-11-12. Suite backend completa en SQLite y evaluación RAG en los dos modos y con dos embedders (resultados abajo).

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
| Respuesta generativa con verificación por afirmación | Exclusiva de ContextRAG | **Pendiente** (entrega 2), opcional y desactivada por defecto |
| Términos nombrados ausentes → abstención | Exclusiva de ContextRAG | **Pendiente** (entrega 2) |
| Reranker con timeout | Exclusiva de ContextRAG | **Pendiente**; el léxico es redundante con la selección de frases de DOCMIND |
| Memoria conversacional | Exclusiva de ContextRAG; incompatible (SQLite, otra autorización) | **Pendiente** (entrega 3): rediseño en PostgreSQL con RLS |
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

## Pendiente (en orden)

1. **Entrega 2 — respuestas generativas verificadas** (opcional, `ASK_MODE=extractive` por defecto):
   * portar `LexicalEntailment` y `NLIVerifier` (cada afirmación con `[n]` y verificada contra la fuente citada) y `context/budget.py`;
   * cercar las fuentes como contenido no confiable (`processing.fence_document`);
   * descartar las afirmaciones no verificadas y abstenerse si no queda ninguna;
   * términos nombrados ausentes → abstención.

   Las pruebas usan un LLM simulado. La validación con un LLM real está **BLOQUEADA**: falta el proyecto Vertex o una clave.
2. **Entrega 3 — conversaciones:**
   * tablas `conversations` y `messages` con `organization_id`, políticas RLS y cifrado por organización;
   * retención y borrado, endpoints y página en el frontend;
   * pruebas de aislamiento.

   Diseño a partir de `memory/store.py`: versión optimista, compactación que solo avanza y turnos inmutables.
3. **Corpus de evaluación ampliado:** identificadores, tablas, documentos largos, contradicciones, inyección y escaneados. Juez LLM opcional (Ragas) sin hacerlo obligatorio.
4. Reranker cross-encoder opcional con timeout (`BoundedReranker`), solo si la medición lo justifica.
5. Retirar el outbox hacia ContextRAG externo (`contextrag_sync.py`), con aprobación del propietario.

## Bloqueos que dependen de Santiago

* Documentos reales anonimizados (30–50) para medir `hybrid`, la generación y los conflictos.
* Credenciales LLM (Vertex AI) para validar la respuesta generativa.
* Aprobación para retirar la integración externa con ContextRAG cuando las entregas 2 y 3 estén terminadas.

## Cómo continuar

```bash
cd backend
pytest tests/test_hybrid_retrieval.py tests/test_source_conflicts.py -q
python -m app.evaluation.rag --retrieval hybrid
```

Siguiente paso: la entrega 2, empezando por `app/rag/verification.py` (port de `nli_verifier.py`) y sus pruebas.
