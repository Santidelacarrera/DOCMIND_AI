# Variables de entorno

La referencia completa, con valores por defecto seguros para desarrollo, es [`.env.example`](../.env.example); la fuente de verdad es `backend/app/core.py` (`Settings`). Aquí, lo que cambia el comportamiento o la seguridad.

## Arranque y seguridad

| Variable | Qué hace | Producción |
|---|---|---|
| `ENVIRONMENT` | `development` / `staging` / `production`. Cualquier valor que no sea `development` o `test` activa validaciones que impiden arrancar sin la configuración segura | `production` |
| `JWT_SECRET` | Firma de sesiones | ≥ 32 caracteres aleatorios, distinto por entorno |
| `COOKIE_SECURE`, `CORS_ORIGINS` | Cookies solo HTTPS, orígenes permitidos | `true`, solo `https://` |
| `ANTIVIRUS_PROVIDER` | `disabled` / `clamav` | `clamav` (obligatorio en staging/producción) |
| `DATABASE_URL`, `REDIS_URL` | Conexiones | Servicios gestionados, con TLS y contraseña; usuario de base de datos sin superusuario ni `BYPASSRLS` |

## Documentos y procesamiento

| Variable | Qué hace |
|---|---|
| `MAX_UPLOAD_BYTES`, `MAX_PDF_PAGES`, `MAX_OCR_PAGES` | Límites de subida y de OCR |
| `LLM_PROVIDER` | `mock` (desarrollo), `openai`, `gemini` |
| `CONFIDENCE_REVIEW_THRESHOLD` | Campos con confianza menor van a revisión |
| `ALWAYS_REVIEW_FIELDS` | Campos que siempre revisa una persona |
| `OCR_REVIEW_THRESHOLD` | Media de confianza OCR por página bajo la cual todo el documento va a revisión |
| `OCR_WORD_REVIEW_THRESHOLD` | Un valor sostenido por una palabra OCR con confianza menor va a revisión (0,60; ver `DECISIONS_2026-11-10.md`) |
| `SCHEMA_VIOLATION_POLICY` | `review` o `reject` |

## Preguntas con cita (`/ask`)

| Variable | Qué hace |
|---|---|
| `QDRANT_URL`, `QDRANT_API_KEY`, `QDRANT_COLLECTION` | Índice vectorial. Vacío desactiva `/ask` (503) |
| `EMBEDDING_PROVIDER` | `hashing` (desarrollo, léxico, rechazado en staging/producción), `local` (fastembed), `openai` (externo, requiere `EMBEDDING_ALLOW_EXTERNAL` y `EMBEDDING_ALLOWED_HOSTS`) |
| `EMBEDDING_MODEL` | Por defecto el multilingüe MiniLM. Cambiarlo sobre un índice existente se rechaza (`EMBEDDING_MODEL_MISMATCH`) |
| `ASK_MIN_EVIDENCE_COVERAGE` | Cobertura léxica mínima de la pregunta para responder (0,5, ADR-002) |
| `ASK_MIN_SEMANTIC_SIMILARITY` | Vacío = umbral calibrado para el modelo, o ninguno. Fijarlo solo tras medir con `python -m app.evaluation.rag` (ADR-003) |
| `PII_MASKING_KEY`, `PII_DETECTORS` | Enmascarado de PII antes de indexar (incluye RUT chileno con dígito verificador) |

## Retención, almacenamiento y sincronización

`RETENTION_*` ([DATA_RETENTION.md](DATA_RETENTION.md)), `STORAGE_PROVIDER` y `S3_*` ([DEPLOYMENT.md](DEPLOYMENT.md)), `CONTEXTRAG_*` (desactivado, [CONTEXTRAG_INTEGRATION.md](CONTEXTRAG_INTEGRATION.md)).

## Nunca en el repositorio

Claves de LLM, `JWT_SECRET`, contraseñas de base de datos, Redis y SMTP, credenciales de S3. Vienen del gestor de secretos del entorno. `.env` está en `.gitignore`.
