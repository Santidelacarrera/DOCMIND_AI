# DocMind AI — Deuda técnica (2026-11-10)

Solo deuda con evidencia en el código. Prioridad según el brief: P0 bloquea el flujo principal o la seguridad; P1 afecta a confiabilidad o mantenibilidad; P2 es mejora.

| # | Deuda | Evidencia | Impacto | Prioridad | Propuesta |
|---|---|---|---|---|---|
| D1 | Sin Row-Level Security | `docs/RLS_DESIGN.md` (aprobado, sin migración) | Un fallo en un filtro SQL de la aplicación no tiene segunda barrera | P1 | Migración por fases ya decidida; necesita PostgreSQL 17 con rol `docmind_app` |
| D2 | `main.py` monolítico | 2 211 líneas, 52 rutas, serializadores en línea (había uno duplicado en `/ask`, ya unificado en `_answer_body`) | Cambios con mucho radio de impacto | P1 | Dividir en `APIRouter` por dominio (auth, documentos, revisión, ask), sin cambiar rutas; la prueba de la tabla de rutas protege la operación |
| D3 | Selección de frases léxica en `/ask` | Medido: con `paraphrase-multilingual-MiniLM-L12-v2` la recuperación es perfecta (recall@1 = MRR = 1,0) pero las respuestas a paráfrasis siguen en 1/4, igual que con `hashing` | El límite de las paráfrasis está en elegir la frase, no en el embedder | P1 | Puntuar las frases con el mismo embedder (similitud semántica) manteniendo la regla de evidencia suficiente; y decidir el modelo de producción: el actual por defecto (`bge-small-en`) es inglés |
| D4 | Respuestas solo extractivas | `answering.py`: no hay generación | Sin síntesis entre fragmentos; respuestas como citas literales | P2 | Generación con LLM solo si se mantiene el contrato "cada afirmación con cita" y la abstención; añadir métrica de fidelidad con juez |
| D5 | Cobertura baja en módulos operativos | `telemetry.py` 26 %, `notifications.py` 58 % | Regresiones de SMTP y trazas no detectadas | P2 | **OCR saldado:** las pruebas con Tesseract y Redis ya no se omiten en CI (`tests/_tools.py`) |
| D6 | Pasos temporales en CI | `ci.yml`: "TODO(temporary)" que publican salidas de auditoría en el PR | Ruido y permisos `pull-requests: write` extra | P2 | Retirar cuando se lean los logs de Actions directamente |
| D7 | ~~Advertencias de deprecación~~ | Saldado: `add_open_action(JavaScript(...))` | — | — | — |
| D8 | Documentos de auditoría dispersos | 5 `AUDIT_*.md` + `DECISIONS_*.md` | Difícil saber qué está vigente | P2 | Índice de vigencia en `docs/` (este conjunto `DOCMIND_*.md` es el estado actual) |
| D9 | Qdrant sin copia ni restauración probada | Vectores derivados, reconstruibles por reproceso | RTO desconocido en un índice grande | P2 | Medir el tiempo de reindexado o probar snapshots |
| D10 | Corpus de evaluación sintético | `eval_corpus/` (ahora con 5 documentos tributarios chilenos ficticios) y `rag_questions.json` | Las métricas no representan documentos reales | P1 | Corpus anonimizado y consentido (decisión del propietario) |
| D11 | La confianza OCR es por página, no por palabra | Una factura escaneada con CER 0,5 % leyó "Eiemplo" en el emisor y el campo pasó sin revisión (error silencioso), corregido bajando la confianza de la regla posicional | Un LLM con alta confianza sobre un nombre mal leído no se revisa | P1 | Usar la confianza de Tesseract de las palabras que sostienen cada valor (evidencia) para enviarlo a revisión |

## Deuda saldada hoy

* Stack Docker sin vector index ni cliente Qdrant (P0).
* `/ask` respondía con una cita real pero sin evidencia suficiente (P0).
* Sin métricas de recuperación ni de citas (P0).
* Imagen en un Python distinto del probado (P1).
* Serializador de citas duplicado en `main.py`.
