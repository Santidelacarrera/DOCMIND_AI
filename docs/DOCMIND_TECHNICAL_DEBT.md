# DocMind AI — Deuda técnica (2026-11-10)

Solo deuda con evidencia en el código. Prioridad según el brief: P0 bloquea el flujo principal o la seguridad; P1 afecta a confiabilidad o mantenibilidad; P2 es mejora.

| # | Deuda | Evidencia | Impacto | Prioridad | Propuesta |
|---|---|---|---|---|---|
| D1 | RLS solo en fases 1–2 | `0009_rls_policies` + `app/rls.py`, probado en PostgreSQL 17 (47 pruebas) | Hasta la fase 3 no protege en producción | P1 | Fase 3: `SECURITY DEFINER` para login, clave API y reclamo de trabajos; políticas para `organizations`/`users`; conexión `docmind_system` para tareas de mantenimiento; staging con `FORCE` |
| D2 | ~~`main.py` monolítico~~ | Saldado: 2 211 → 150 líneas; rutas en `app/routers/` (7 módulos) y ayudas en `app/api_support.py`. Contrato verificado: mismas 55 rutas en el mismo orden y OpenAPI idéntico byte a byte | — | — | Las rutas se añaden a la lista plana de la app, no con `include_router` (que en FastAPI 0.162 las ocultaría de `app.routes`) |
| D3 | ~~Selección de frases léxica en `/ask`~~ | Saldado: selección semántica con umbral calibrado por modelo (ADR-003); multilingüe 28/28 | — | — | Calibrar cada modelo nuevo antes de usarlo |
| D4 | Respuestas solo extractivas | `answering.py`: no hay generación | Sin síntesis entre fragmentos; respuestas como citas literales | P2 | Generación con LLM solo si se mantiene el contrato "cada afirmación con cita" y la abstención; añadir métrica de fidelidad con juez |
| D5 | ~~Cobertura baja en módulos operativos~~ | Saldado: `tests/test_ops_modules.py` cubre SMTP (TLS, login, sin host, selección de proveedor) y OpenTelemetry (no-op sin endpoint, una sola instrumentación, Celery, fallos registrados sin romper el arranque); `notifications.py` 58 → 94 %, `telemetry.py` 26 → ≥ 74 %. Las pruebas de OCR y Redis ya no se omiten en CI | — | — | — |
| D6 | ~~Pasos temporales en CI~~ | Saldado: sin comentarios en el PR ni `pull-requests: write`; e2e sube informe, trazas y logs como artefacto al fallar | — | — | — |
| D7 | ~~Advertencias de deprecación~~ | Saldado: `add_open_action(JavaScript(...))` | — | — | — |
| D8 | ~~Documentos de auditoría dispersos~~ | Saldado: `docs/INDEX.md` separa lo vigente de lo histórico | — | — | — |
| D9 | ~~Qdrant sin procedimiento de recuperación~~ | Saldado: `scripts/reindex_vectors.py` (sin LLM, sin tocar extracciones); medido 5,3 docs/s con el modelo multilingüe | — | — | Snapshots de Qdrant solo si el reindexado (≈ 31 min por 10 000 documentos y proceso) no basta |
| D10 | Corpus de evaluación sintético | `eval_corpus/` (ahora con 5 documentos tributarios chilenos ficticios) y `rag_questions.json` | Las métricas no representan documentos reales | P1 | Corpus anonimizado y consentido (decisión del propietario) |
| D11 | ~~Confianza OCR solo por página~~ | Saldado: `OCR_WORD_REVIEW_THRESHOLD`; el caso "Eiemplo" se captura por la palabra (confianza 0,08), carga de revisión 0,331 → 0,310 | — | — | — |

## Deuda saldada hoy

* Stack Docker sin vector index ni cliente Qdrant (P0).
* `/ask` respondía con una cita real pero sin evidencia suficiente (P0).
* Sin métricas de recuperación ni de citas (P0).
* Imagen en un Python distinto del probado (P1).
* Serializador de citas duplicado en `main.py`.
* División de `main.py`: ruff eliminó como "no usado" el import que las rutas llamaban por `globals()["organization_id"]`. Sustituido por llamadas explícitas `api_support.organization_id(...)`, visibles para ruff y mypy.
