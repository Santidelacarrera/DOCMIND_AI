# DocMind AI — Roadmap priorizado (2026-11-10)

Regla: no se abren funcionalidades nuevas mientras haya un P0 abierto en el flujo principal. Hoy no queda ningún P0 de código abierto; los bloqueos que quedan dependen de cuentas, datos o decisiones del propietario.

## Bloque 1 — P0, cerrado en esta sesión

1. ~~Stack reproducible con consulta en lenguaje natural~~ — Qdrant en Compose, imagen con `.[qdrant]`, Python 3.12. **Validado** (`staging_smoke.py --ask`).
2. ~~Abstención con evidencia insuficiente~~ — `ASK_MIN_EVIDENCE_COVERAGE`. **Validado** (abstención 1,0).
3. ~~Evaluación RAG reproducible~~ — `python -m app.evaluation.rag` y compuerta en CI. **Validado** en local; CI pendiente de ejecutarse en el PR.

## Bloque 2 — P1, siguiente

| Orden | Tarea | Criterio de aceptación | Depende de |
|---|---|---|---|
| 1 | Medir RAG con embedder semántico (`--embedder local`) | Informe con las mismas 28 preguntas; decidir modelo de producción | Descargar el modelo (gratuito, ~130 MB) |
| 2 | RLS fase 1–2 (roles + políticas sin `FORCE`) | Suite negativa pasa con el rol `docmind_app` en PostgreSQL 17 | Ninguna (diseño aprobado) |
| 3 | Corpus de extracción con campos chilenos (nombre, RUT con dígito verificador, fecha, folio, monto CLP, entidad, tipo) | Ground truth + normalización de RUT y montos + F1 por campo separando errores de OCR, extracción y validación | Nada para el corpus sintético; datos reales anonimizados requieren aprobación |
| 4 | Playwright e2e sobre el stack nuevo (incluida la página `/ask` con extracto) | `npm run test:e2e` en verde contra Compose | — |
| 5 | Pruebas de OCR no omitibles en CI | PDF escaneado sintético, CER medido | — |
| 6 | Dividir `main.py` en routers | Sin cambios de contrato; la prueba de tabla de rutas pasa | — |

## Bloque 3 — P2

* Respuestas generadas con LLM, manteniendo cita obligatoria por afirmación y evaluación de fidelidad.
* Snapshots de Qdrant y medición de RTO de reindexado.
* Retirar los pasos temporales de CI y limpiar las deprecaciones de pypdf.
* Índice de vigencia de los documentos de auditoría.

## Bloqueado por el propietario

* LLM de pago o Vertex AI para documentos reales (la capa gratuita de Gemini solo admite datos sintéticos).
* Documentos reales anonimizados para evaluar.
* Cuenta S3/KMS para validar el almacenamiento de producción.
* Despliegue de staging (`staging_smoke.py --ask` contra staging).
* Revisión jurídica de la licencia.

## Actualización 2026-11-10 (tarde)

Hecho: RAG semántico calibrado (28/28 con el multilingüe), modelo multilingüe por defecto con guarda anti-mezcla, revisión por confianza OCR por palabra, RLS fases 1–2 probadas en PostgreSQL 17, corpus chileno, Playwright de `/ask`, documentación de entrega (`INSTALLATION`, `ENVIRONMENT`, `TESTING`, `TROUBLESHOOTING`, `KNOWN_LIMITATIONS`, `DEMO_EMPRESARIAL`) y `DECISIONS_2026-11-10.md`.

Siguiente, por prioridad:
1. RLS fase 3 en staging (`SECURITY DEFINER`, `docmind_system`, `FORCE`).
2. Dividir `main.py` en routers sin cambiar contratos.
3. Evaluación con documentos reales anonimizados y LLM de pago (bloqueado por el propietario: cuenta y datos).
4. Retirar los pasos temporales del CI y medir RTO de reindexado de Qdrant.
