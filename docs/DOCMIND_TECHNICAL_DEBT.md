# DocMind AI — Deuda técnica (2026-11-10)

Solo deuda con evidencia en el código. Prioridad según el brief: P0 bloquea el flujo principal o la seguridad; P1 afecta a confiabilidad o mantenibilidad; P2 es mejora.

| # | Deuda | Evidencia | Impacto | Prioridad | Propuesta |
|---|---|---|---|---|---|
| D1 | RLS solo en fases 1–2 | `0009_rls_policies` + `app/rls.py`, probado en PostgreSQL 17 (47 pruebas) | Hasta la fase 3 no protege en producción | P1 | Fase 3: `SECURITY DEFINER` para login, clave API y reclamo de trabajos; políticas para `organizations`/`users`; conexión `docmind_system` para tareas de mantenimiento; staging con `FORCE` |
| D2 | `main.py` monolítico | 2 211 líneas, 52 rutas, serializadores en línea (había uno duplicado en `/ask`, ya unificado en `_answer_body`) | Cambios con mucho radio de impacto | P1 | Dividir en `APIRouter` por dominio (auth, documentos, revisión, ask), sin cambiar rutas; la prueba de la tabla de rutas protege la operación |
| D3 | ~~Selección de frases léxica en `/ask`~~ | Saldado: selección semántica con umbral calibrado por modelo (ADR-003); multilingüe 28/28 | — | — | Calibrar cada modelo nuevo antes de usarlo |
| D4 | Respuestas solo extractivas | `answering.py`: no hay generación | Sin síntesis entre fragmentos; respuestas como citas literales | P2 | Generación con LLM solo si se mantiene el contrato "cada afirmación con cita" y la abstención; añadir métrica de fidelidad con juez |
| D5 | ~~Cobertura baja en módulos operativos~~ | Saldado: `tests/test_ops_modules.py` cubre SMTP (TLS, login, sin host, selección de proveedor) y OpenTelemetry (no-op sin endpoint, una sola instrumentación, Celery, fallos registrados sin romper el arranque); `notifications.py` 58 → 94 %, `telemetry.py` 26 → ≥ 74 %. Las pruebas de OCR y Redis ya no se omiten en CI | — | — | — |
| D6 | Pasos temporales en CI | `ci.yml`: "TODO(temporary)" que publican salidas de auditoría en el PR | Ruido y permisos `pull-requests: write` extra | P2 | Retirar cuando se lean los logs de Actions directamente |
| D7 | ~~Advertencias de deprecación~~ | Saldado: `add_open_action(JavaScript(...))` | — | — | — |
| D8 | ~~Documentos de auditoría dispersos~~ | Saldado: `docs/INDEX.md` separa lo vigente de lo histórico | — | — | — |
| D9 | Qdrant sin copia ni restauración probada | Vectores derivados, reconstruibles por reproceso | RTO desconocido en un índice grande | P2 | Medir el tiempo de reindexado o probar snapshots |
| D10 | Corpus de evaluación sintético | `eval_corpus/` (ahora con 5 documentos tributarios chilenos ficticios) y `rag_questions.json` | Las métricas no representan documentos reales | P1 | Corpus anonimizado y consentido (decisión del propietario) |
| D11 | ~~Confianza OCR solo por página~~ | Saldado: `OCR_WORD_REVIEW_THRESHOLD`; el caso "Eiemplo" se captura por la palabra (confianza 0,08), carga de revisión 0,331 → 0,310 | — | — | — |

## Deuda saldada hoy

* Stack Docker sin vector index ni cliente Qdrant (P0).
* `/ask` respondía con una cita real pero sin evidencia suficiente (P0).
* Sin métricas de recuperación ni de citas (P0).
* Imagen en un Python distinto del probado (P1).
* Serializador de citas duplicado en `main.py`.
