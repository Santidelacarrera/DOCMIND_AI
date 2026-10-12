# Nota del proyecto — DocMind AI (Nexus Security & AI Lab)

> Sesión autónoma del 2026-10-12 · rama `feat/docmind-ai-autonomo-2026-10-12` · base `main` · coste $0 · **pendiente de aprobación humana**.
> Estados `aprobado_santiago` / `aprobado_sistema`: **no modificados** (los decide una persona).

## 1. Tareas del proyecto y evidencia

| # | Tarea | Estado | Evidencia |
|---|---|---|---|
| 1 | Pruebas de extracción con PDF y Word sintéticos | ✅ | `backend/tests/test_word_and_citations.py` (45 pruebas): PDF y DOCX → mismas páginas por el worker real (`test_pdf_and_word_with_the_same_content_extract_the_same_pages`), tablas, texto oculto/borrados/metadatos excluidos, archivo generado con `python-docx`, 14 casos de seguridad DOCX (macros, OLE, plantillas remotas, zip bombs, XXE/entidades, rutas, zip falso), subida por API (415/422), descarga. E2E: `frontend/tests/e2e/word-upload.spec.ts` |
| 2 | Cada respuesta cita documento y página | ✅ | `app/ingestion/answering.py` + `POST /api/v1/ask`. Invariante en el constructor (`Answer`/`Citation`): o ≥1 cita con documento y página ≥ 1, o abstención explícita sin citas. Pruebas: `test_an_answer_always_has_a_source_or_is_an_explicit_abstention`, `test_every_demo_answer_cites_the_right_document_and_page`, `test_ask_*` (5), aislamiento entre empresas, documentos borrados nunca citados, fragmentos sospechosos y texto oculto nunca usados. Fragmentos limitados a una página (`test_split_pages_respects_size_overlap_and_never_crosses_a_page`) |
| 3 | Demo con documentos ficticios | ✅ | `python -m app.ingestion.demo` → 4 documentos (PDF y Word) de 2 empresas ficticias, 10 preguntas, **10/10** con criterio estricto (primera cita = documento+página esperados **y** el dato en el texto). Informe: [[DEMO]] |
| 4 | Procesamiento local documentado | ✅ | [[LOCAL_PROCESSING]] (qué corre dónde, qué solo saldría si se configura, cómo comprobarlo). Prueba: `test_the_whole_flow_runs_without_any_network_access` bloquea `socket.connect` y `getaddrinfo` durante la demo completa |

## 2. Definición de terminado (6 condiciones)

| # | Condición | Cumplida | Comando y resultado (ejecutado el 2026-10-12) |
|---|---|---|---|
| 1 | Tareas cumplidas con evidencia | ✅ | Sección 1 |
| 2 | Pruebas automatizadas pasando | ✅ | `cd backend && pytest --cov=app --cov-fail-under=75` → **330 passed, 11 skipped**, cobertura **90,78 %** (umbral 75 %). En venv limpio (`pip install '.[dev]'`): 324 passed, 17 skipped (las 6 de diferencia: 5 de spaCy y 1 de fastembed, que solo corren si el modelo/librería está instalado). Sobre PostgreSQL (`TEST_DATABASE_URL=…`): 330 passed, 11 skipped. E2E Playwright contra la pila real (Postgres + Redis + Celery + Next.js): **6 passed** |
| 3 | Seguridad y lint sin errores altos | ✅ | `ruff check .` → All checks passed · `mypy app` → no issues in 41 files · `bandit -r app -ll` → sin hallazgos (exit 0) · `pip-audit` en venv limpio → **No known vulnerabilities found** · `npm audit --audit-level=high --omit=dev` → **found 0 vulnerabilities** · frontend `npm run lint`, `npm run typecheck`, `npm run build` → sin errores |
| 4 | README reproducible | ✅ | README → «Local quickstart (no Docker, no accounts, $0)»: instalación, pruebas, demo y evaluación; las órdenes se ejecutaron en un entorno limpio |
| 5 | Bitácora de decisiones | ✅ | Sección 3 |
| 6 | Pendientes para Santiago | ✅ | Sección 4 |

Los 11 skipped del entorno de desarrollo son exactamente las pruebas `tests/test_docker_integration.py` («Docker integration only», requieren `DOCMIND_INTEGRATION=1` y la pila Docker, que no se levantó en esta sesión; el job `docker`/`e2e` del CI las cubre). Detalle: `pytest -rs`.

## 3. Bitácora de decisiones

| Fecha | Decisión | Motivo |
|---|---|---|
| 2026-10-12 | Esta nota vive en `docs/NOTA_PROYECTO.md` (repo) | la nota del vault no es accesible desde esta sesión; así queda versionada con el código |
| 2026-10-12 | Rama `feat/docmind-ai-autonomo-2026-10-12` desde `main` actualizado | indicación explícita; la rama anterior ya estaba fusionada |
| 2026-10-12 | Word se lee con `zipfile` + `defusedxml`, sin Word/LibreOffice ni `python-docx` en producción | superficie de ataque mínima, sin dependencias pesadas, coste $0; `python-docx` solo en pruebas (dev) para validar contra archivos reales |
| 2026-10-12 | Macros, OLE, plantillas remotas y `.doc`/`.docm` se rechazan por defecto | contenido activo = riesgo; misma política que PDF (`INGEST_REJECT_ACTIVE_CONTENT`) |
| 2026-10-12 | Texto oculto (`w:vanish`) y borrados rastreados no se extraen | lugar clásico para esconder instrucciones o datos a quien revisa |
| 2026-10-12 | Páginas de Word: saltos explícitos; sin ellos, estimadas (3 000 caracteres) y marcadas con `~` | Word no guarda paginación; una cita no debe aparentar más precisión de la que tiene |
| 2026-10-12 | Fragmentos acotados a una página | para que cada cita nombre exactamente una página |
| 2026-10-12 | Respuestas **extractivas** sin modelo de lenguaje | coste $0, no puede inventar, siempre citable; el peor caso es «no encontré» |
| 2026-10-12 | Invariante «o cita o abstención» en el constructor | no depender de que cada llamador se acuerde |
| 2026-10-12 | Primera versión del criterio de la demo era laxa (aceptaba una cita correcta aunque la respuesta no contuviera el dato); se endureció y se mejoró la selección de frases (rareza de términos + raíces) | evitar un «10/10» engañoso; la comprobación estricta exige primera cita correcta **y** dato en el texto |
| 2026-10-12 | `POST /ask` inactivo (503) salvo `QDRANT_URL` | no activar infraestructura que el operador no configuró |
| 2026-10-12 | `gh` sin autenticar: push con `git` y PR con la herramienta MCP de GitHub | la instrucción permite abrir el PR; no se forzó nada |
| 2026-10-12 | Visor de Word en la interfaz no implementado | requeriría convertir a PDF (LibreOffice); se deja como pendiente |

## 4. Pendientes para Santiago

1. **Revisar y aprobar el PR; la fusión a `main` la decide una persona** (esta sesión no puede fusionar ni aprobar).
2. Probar un **modelo de embeddings local real** (`pip install '.[embeddings]'` y pre-instalar el modelo): el entorno de la sesión no puede descargar pesos; la demo usa el embedder léxico `hashing`.
3. Evaluar con **documentos reales** (consentidos y anonimizados): la demo son 10 preguntas sobre 4 documentos ficticios escritos junto con el sistema; sirve de regresión, no mide precisión real.
4. Decidir si hace falta una **pantalla de preguntas** en la interfaz (hoy solo existe la API `POST /api/v1/ask`).
5. Decidir si se quiere **vista previa de Word** en la interfaz (hoy el iframe del visor descarga el `.docx`).
6. Definir **retención y DPA** si algún día se activa un embedder o LLM externo (`EMBEDDING_PROVIDER=openai`, `LLM_PROVIDER=openai`): ambos desactivados por defecto.
7. Métricas del worker en Prometheus (limitación documentada en [[RELIABILITY]]).
8. Confirmar que el **CI de GitHub** del PR queda en verde (se ejecuta al abrir el PR; su resultado no estaba disponible al redactar esta nota).

## 5. Riesgos conocidos

* La recuperación con `hashing` es léxica: pregunta con sinónimos puede no encontrar la frase.
* Texto oculto por color o tamaño en Word no es detectable; solo `w:vanish` y borrados rastreados.
* Enmascarado de PII: estructurado + nombres/direcciones por contexto; no es anonimización garantizada ([[INGESTION]]).
* Las respuestas extractivas citan frases literales; no sintetizan entre documentos.
* Filtro por tenant en Qdrant es una frontera de aplicación (para aislamiento duro, una colección por tenant).

## 6. Texto del PR (copia)

Título: **[docmind AI] Terminado autónomo, pendiente de aprobación**. El cuerpo está en el propio PR; resume secciones 1–5 de esta nota.

---
Relacionado: [[empresa]]
