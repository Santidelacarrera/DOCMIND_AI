# Pruebas

Qué se prueba, cómo ejecutarlo y qué no se omite en CI. Resultados de referencia del 2026-11-10.

| Capa | Comando (desde `backend/` salvo indicación) | Qué cubre |
|---|---|---|
| Estático | `ruff check .` · `mypy app` · `bandit -r app -ll` | Estilo, tipos estrictos, seguridad |
| Backend | `pytest --cov=app` | API, autenticación, aislamiento multi-tenant ruta a ruta, subidas, worker, reintentos, exportaciones, RAG, RUT, OCR por palabra. SQLite y dobles en memoria |
| Backend en PostgreSQL | `TEST_DATABASE_URL=postgresql+psycopg://… pytest` | Lo mismo con bloqueos de fila reales y las **políticas RLS** (`tests/test_rls_policies.py`, solo PostgreSQL) |
| Cola real | `pytest tests/test_celery_redis.py` | Celery + Redis reales; requiere `redis-server` |
| Calidad de extracción | `python -m app.evaluation --min-f1 0.89` | P/R/F1 por campo (incluye documentos chilenos), CER del OCR, atribución de errores (validación, normalización, OCR, extracción), captura de errores por la revisión |
| Calidad RAG | `python -m app.evaluation.rag --min-recall-at-5 0.95 --min-citation-accuracy 0.85 --min-abstention-accuracy 1.0` | Recall@k, precision@k, MRR, exactitud y precisión de citas, fidelidad, abstención, fugas entre tenants |
| Demo | `python -m app.ingestion.demo` | 10/10 citas correctas, sin red |
| Frontend (`frontend/`) | `npm run lint` · `npm run typecheck` · `npm run build` | |
| Navegador (`frontend/`) | `npm run test:e2e` con el stack levantado | 8 flujos, incluido `/ask` con cita, extracto y abstención |
| Stack real | `python backend/scripts/staging_smoke.py --base-url … --ask` | Extremo a extremo contra un despliegue |

## Lo que no se omite en CI

Las pruebas que necesitan `tesseract`, `pdftoppm` o `redis-server` se omiten **en local** si faltan, pero **nunca con `CI=true`** (`tests/_tools.py`): allí la falta de la herramienta hace fallar la ejecución.

## Compuertas de CI (`.github/workflows/ci.yml`)

Backend (SQLite + cobertura ≥ 75 %), backend en PostgreSQL 17, compuerta de extracción, compuerta RAG, bandit, pip-audit, frontend (lint, tipos, build, npm audit), build de Docker, e2e Playwright contra el stack Compose, CodeQL.

## Últimos resultados

| Prueba | Resultado |
|---|---|
| Backend SQLite (Windows, Python 3.12) | ver el PR de esta rama |
| RLS en PostgreSQL 17, como rol sin privilegios | 47/47 |
| Evaluación RAG, embedder multilingüe | 28/28 correctas, abstención 1,0, 0 fugas |
| Evaluación de extracción (Tesseract 5.5) | micro F1 93,0 %, captura de errores 100 %, 0 errores silenciosos |
| Playwright contra el stack | 8/8 |
