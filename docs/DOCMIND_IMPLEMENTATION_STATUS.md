# DocMind AI — Estado de implementación (2026-11-10)

**VALIDADA** = ejecutada y comprobada en esta sesión. **IMPLEMENTADA** = el código y sus pruebas existen, pero no se ejecutó de extremo a extremo hoy, o solo con dobles de prueba. **PENDIENTE** / **BLOQUEADA** = no existe, o depende de algo externo. Una prueba unitaria con dobles no cuenta como validación del servicio real.

| Funcionalidad | Estado | Evidencia | Problema | Prioridad |
|---|---|---|---|---|
| Arranque reproducible (Compose) | VALIDADA | `docker compose up --build --wait`: 7 servicios en marcha (db, redis, qdrant, api, worker, scheduler, web), `storage-init` terminado con 0; los 4 con healthcheck (db, redis, qdrant, api) sanos | Antes faltaban Qdrant y el cliente en la imagen | P0 cerrado |
| Migraciones Alembic | VALIDADA | Aplicadas por `api` al arrancar sobre PostgreSQL 17 vacío | Producción las ejecuta como paso aparte (documentado) | — |
| Health / readiness | VALIDADA | `/health`, `/ready` en `staging_smoke.py` | — | — |
| Registro, login, sesión | VALIDADA | `staging_smoke.py` (registro y Bearer); suite: `test_api.py`, `test_cross_org_negative.py` | — | — |
| Carga PDF | VALIDADA | Smoke: 202 → COMPLETED | — | — |
| Carga DOCX | IMPLEMENTADA | `test_word_and_citations.py`, `rag.py` (en proceso) | No subida por API en el stack hoy | P1 |
| Validación de formato, tamaño, magic bytes, contenido activo | IMPLEMENTADA | `test_ingestion.py`, `test_hardening_audit.py` | — | — |
| Antivirus ClamAV | IMPLEMENTADA | `test_antivirus.py`; servicio no levantado | Requiere daemon | P1 |
| Detección de escaneados y OCR Tesseract | IMPLEMENTADA | `ocr.py` (cobertura 22 % en Windows sin Tesseract); CI instala Tesseract | Sin prueba no omitible | P1 |
| Chunking por página | VALIDADA | `chunking.py`; la cita del smoke apunta a la página 2 correcta | — | — |
| Enmascarado PII | IMPLEMENTADA | `test_ingestion.py`, demo | RUT chileno no evaluado | P1 |
| Embeddings | VALIDADA (hashing) | Stack con `EMBEDDING_PROVIDER=hashing` | Semántico (`local`/`openai`) no medido | P1 |
| Persistencia vectorial Qdrant | VALIDADA | Qdrant v1.20 en servidor; `/ask` responde desde él | Snapshot no probado | P2 |
| Relación documento–página–chunk | VALIDADA | Cita del smoke: `politica_smoke.pdf, p.2`, `chunk_index` 1, extracto literal | — | — |
| Duplicados (SHA-256) | IMPLEMENTADA | `test_api.py`, `test_reliability.py` | — | — |
| Estados PENDING/PROCESSING/COMPLETED/FAILED, reintentos idempotentes | IMPLEMENTADA | `test_worker.py`, `test_reliability.py`, `test_celery_redis.py` (Redis real, se omite sin `redis-server`) | — | — |
| Extracción estructurada (LLM mock/OpenAI/Gemini) | VALIDADA (mock) | Smoke: 4 campos | LLM real: informe Gemini de 2026-10-31 (F1 96 % en sintético), no repetido hoy | Bloqueada (datos reales) |
| Evaluación de extracción (P/R/F1 por campo) | IMPLEMENTADA | `python -m app.evaluation` en CI (`--min-f1 0.89`) | Corpus sin campos chilenos (RUT, folio, CLP) | P1 |
| Consulta en lenguaje natural `/ask` | VALIDADA | `staging_smoke.py --ask` contra el stack | — | P0 cerrado |
| Citas verificables (documento, página, fragmento) | VALIDADA | `chunk_index` + `excerpt`; `test_ask_citation_locates_the_supporting_fragment` | — | P1 cerrado |
| Abstención sin evidencia | VALIDADA | `abstention_accuracy` 1,0 (antes 0,5); smoke | 15 % de falsas abstenciones (paráfrasis) | P0 cerrado |
| Evaluación RAG (Recall@K, P@K, MRR, citas, fidelidad, abstención) | VALIDADA | `docs/evaluation/rag-report.md`, `tests/test_rag_evaluation.py`, paso de CI | Corpus pequeño y ficticio | P1 |
| Separar evidencia de inferencia | VALIDADA (por diseño) | `answer()` es extractivo: solo cita frases literales; `faithfulness` 1,0 | No hay inferencia que etiquetar | — |
| Organizaciones, roles, miembros, invitaciones | IMPLEMENTADA | `test_members.py`, `test_collaboration.py` | — | — |
| Autorización por proyecto (expediente) | IMPLEMENTADA | `test_project_authorization.py`, `test_document_authorization.py` | — | — |
| Aislamiento entre organizaciones (API, SQL, Qdrant, archivos, exportaciones, webhooks) | VALIDADA | Suite completa + smoke (403/404 para B) + `cross_tenant_leaks` 0 | Sin RLS como segunda barrera | P1 |
| API keys con scopes | IMPLEMENTADA | `test_tenant_isolation.py::test_api_key_scopes_gate_each_capability_independently` | — | — |
| SSRF en webhooks | IMPLEMENTADA | `test_webhook_ssrf.py` | — | — |
| Exportación JSON/CSV/XLSX | VALIDADA (JSON) | Smoke: 200 | CSV/XLSX solo en suite | — |
| Revisión humana | IMPLEMENTADA | `test_review_workflow.py`, `test_mandatory_review.py` | — | — |
| Observabilidad (logs JSON, X-Request-Id, Prometheus, OTel) | IMPLEMENTADA | `test_observability_surface.py`, `test_metrics_labels.py` | Perfil `observability` no levantado hoy | P1 |
| Copia y restauración (base + archivos) | VALIDADA (local) | `docs/DEPLOYMENT.md` §Backups | S3/KMS no verificados | P1 |
| RLS PostgreSQL | PENDIENTE | `docs/RLS_DESIGN.md` | Decidida, sin migración | P1 |
| Frontend: lint + typecheck | VALIDADA | `npm run lint`, `npm run typecheck` | — | — |
| Frontend e2e (Playwright) | IMPLEMENTADA | `frontend/tests/e2e/` (7 specs) | No ejecutado hoy | P1 |
| Despliegue en staging/producción | BLOQUEADA | — | Sin entorno ni secretos | — |
| Kubernetes | No aplica | — | Compose es suficiente para el alcance actual | — |

## Pruebas de esta sesión (2026-11-10, Windows 11, Python 3.12.10)

| Comando | Resultado |
|---|---|
| `pytest --cov=app` antes de los cambios | 577 passed, 35 skipped, cobertura 85 % |
| `pytest --cov=app` después | **585 passed, 35 skipped, 0 failed**, cobertura 84 % (5 415 líneas) |
| `ruff check .` · `mypy app` · `bandit -r app -ll` | limpios |
| `python -m app.evaluation.rag --min-recall-at-5 0.95 --min-citation-accuracy 0.85 --min-abstention-accuracy 1.0` | exit 0 |
| `python -m app.ingestion.demo` | Citas correctas 10/10 |
| `docker compose config -q` (dev y overlay prod) | válidos |
| `docker compose up --build --wait` + `scripts/staging_smoke.py --base-url http://localhost:8000 --ask` | `smoke test passed` (19 pasos) |
| Copia/restauración base + archivos | conteos iguales, SHA-256 idénticos, `reconcile` 0 huérfanos |
| `npm run lint` · `npm run typecheck` | limpios |

Omitidas (35): Docker integration, spaCy, fastembed, Redis real sin `redis-server` y bloqueo de filas solo en PostgreSQL; ninguna de aislamiento. **No ejecutado hoy:** suite sobre PostgreSQL (CI la ejecuta), Playwright, `npm run build`, perfil de observabilidad, ClamAV, OCR real.
