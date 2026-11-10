# DocMind AI — Deuda técnica (2026-11-10)

Solo deuda con evidencia en el código. Prioridad según el brief: P0 bloquea el flujo principal o la seguridad; P1 afecta a confiabilidad o mantenibilidad; P2 es mejora.

| # | Deuda | Evidencia | Impacto | Prioridad | Propuesta |
|---|---|---|---|---|---|
| D1 | Sin Row-Level Security | `docs/RLS_DESIGN.md` (aprobado, sin migración) | Un fallo en un filtro SQL de la aplicación no tiene segunda barrera | P1 | Migración por fases ya decidida; necesita PostgreSQL 17 con rol `docmind_app` |
| D2 | `main.py` monolítico | 2 211 líneas, 52 rutas, serializadores en línea (había uno duplicado en `/ask`, ya unificado en `_answer_body`) | Cambios con mucho radio de impacto | P1 | Dividir en `APIRouter` por dominio (auth, documentos, revisión, ask), sin cambiar rutas; la prueba de la tabla de rutas protege la operación |
| D3 | Embedder de desarrollo léxico | `HashingEmbedder` en `.env.example`; paráfrasis 1/4 en `rag-report.md` | La calidad RAG en demo no representa producción | P1 | Medir `--embedder local` (fastembed + bge-small) y decidir el modelo de producción |
| D4 | Respuestas solo extractivas | `answering.py`: no hay generación | Sin síntesis entre fragmentos; respuestas como citas literales | P2 | Generación con LLM solo si se mantiene el contrato "cada afirmación con cita" y la abstención; añadir métrica de fidelidad con juez |
| D5 | Cobertura baja en módulos operativos | `ocr.py` 22 %, `telemetry.py` 26 %, `notifications.py` 58 % (la evaluación no corre en pytest) | Regresiones de OCR y SMTP no detectadas en CI local sin Tesseract | P1 | CI ya instala Tesseract; añadir pruebas de OCR con PDF escaneado sintético que no se omitan |
| D6 | Pasos temporales en CI | `ci.yml`: "TODO(temporary)" que publican salidas de auditoría en el PR | Ruido y permisos `pull-requests: write` extra | P2 | Retirar cuando se lean los logs de Actions directamente |
| D7 | Advertencias de deprecación | `writer.add_js` en pruebas (pypdf 7 lo elimina) | Las pruebas romperán con pypdf 7 | P2 | `add_open_action` |
| D8 | Documentos de auditoría dispersos | 5 `AUDIT_*.md` + `DECISIONS_*.md` | Difícil saber qué está vigente | P2 | Índice de vigencia en `docs/` (este conjunto `DOCMIND_*.md` es el estado actual) |
| D9 | Qdrant sin copia ni restauración probada | Vectores derivados, reconstruibles por reproceso | RTO desconocido en un índice grande | P2 | Medir el tiempo de reindexado o probar snapshots |
| D10 | Corpus de evaluación sintético | `eval_corpus/` y `rag_questions.json` son ficticios | Las métricas no representan documentos reales (RUT, nombres, montos chilenos no cubiertos) | P1 | Corpus anonimizado y consentido (decisión del propietario) |

## Deuda saldada hoy

* Stack Docker sin vector index ni cliente Qdrant (P0).
* `/ask` respondía con una cita real pero sin evidencia suficiente (P0).
* Sin métricas de recuperación ni de citas (P0).
* Imagen en un Python distinto del probado (P1).
* Serializador de citas duplicado en `main.py`.
