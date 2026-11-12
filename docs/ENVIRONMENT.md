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
| `RLS_ENFORCED` | RLS fase 3 (ADR-004): `DATABASE_URL` es `docmind_app` y `/ready` responde 503 `RLS_ROLE_UNSAFE` si ese rol es superusuario, tiene `BYPASSRLS` o es dueño de tablas | `true` tras el periodo de prueba en staging |
| `DATABASE_SYSTEM_URL` | Conexión `docmind_system` solo para las tareas de mantenimiento entre organizaciones. Obligatoria con `RLS_ENFORCED=true`; vacía, se usa `DATABASE_URL` | Credenciales propias, distintas de las de la app |

## Documentos y procesamiento

| Variable | Qué hace |
|---|---|
| `MAX_UPLOAD_BYTES`, `MAX_PDF_PAGES`, `MAX_OCR_PAGES` | Límites de subida y de OCR |
| `LLM_PROVIDER` | `mock` (desarrollo), `openai`, `gemini` (capa gratuita: solo datos sintéticos), `vertex` (Gemini en Vertex AI, de pago, para documentos reales) |
| `VERTEX_PROJECT`, `VERTEX_LOCATION`, `VERTEX_MODEL` | Proyecto de Google Cloud, región (`southamerica-east1` deja los datos en São Paulo; `global` usa el endpoint global) y modelo (vacío = `GEMINI_MODEL`). Credenciales: Application Default Credentials de una cuenta de servicio, nunca una clave en `.env` |
| `GEMINI_INPUT_PRICE_PER_1M`, `GEMINI_OUTPUT_PRICE_PER_1M` | Precios vigentes, también para `vertex`: alimentan `estimated_cost` y la métrica `docmind_llm_estimated_cost_total` |
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
| `RETRIEVAL_MODE` | `dense` (por defecto, búsqueda vectorial) o `hybrid` (vectorial + BM25 fusionados con RRF, ADR-005). Medir antes de cambiarlo |
| `RETRIEVAL_CANDIDATES` | Candidatos por ranking antes de fusionar en modo `hybrid` (20) |
| `RETRIEVAL_RRF_K` | Constante k de la fusión RRF (60) |
| `RETRIEVAL_LEXICAL_CACHE_SECONDS` | Antigüedad máxima del corpus léxico en memoria por organización (60 s) |
| `RETRIEVAL_LEXICAL_MAX_CHUNKS` | Por encima de estos fragmentos una organización no carga corpus léxico y usa solo búsqueda vectorial (50 000) |
| `RERANK_MODEL` | Vacío = sin reranking (por defecto). Modelo cross-encoder de fastembed, p. ej. `BAAI/bge-reranker-v2-m3-int8` (multilingüe, ONNX, 0,57 GB). Añade de 1 a 2 s por pregunta en CPU; medido en `docs/evaluation/rerank-2026-11-12.md` |
| `RERANK_CANDIDATES` | Candidatos que reordena el reranker (20) |
| `RERANK_TIMEOUT_SECONDS` | Tiempo máximo del reranker; si se supera, se conserva el orden de la primera etapa (5) |
| `ASK_MODE` | `extractive` (por defecto: cita frases literales, sin modelo) o `generative` (el `LLM_PROVIDER` redacta y cada frase se verifica contra su fuente; una llamada al modelo por pregunta) |
| `ASK_GENERATION_THRESHOLD` | Umbral de verificación de cada frase generada (0,95). Con el verificador léxico, solo 1,0 pasa |
| `ASK_CONTEXT_CHARS` | Máximo de caracteres de fragmentos enviados al modelo (6000) |
| `ASK_GENERATION_MAX_TOKENS` | Máximo de tokens de la respuesta generada (600) |
| `CONVERSATION_ENCRYPTION_KEY` | Secreto del que se deriva la clave AES-256-GCM de cada organización (HKDF). Sin él, las conversaciones están desactivadas (503). Cambiarlo deja ilegibles las conversaciones existentes |
| `CONVERSATION_RETENTION_DAYS` | Una conversación sin actividad durante estos días se borra (90) |
| `CONVERSATION_MEMORY_TURNS` | Turnos previos que se pasan al modelo como contexto no citable en modo generativo (3) |
| `PII_MASKING_KEY`, `PII_DETECTORS` | Enmascarado de PII antes de indexar (incluye RUT chileno con dígito verificador) |

## Retención, almacenamiento y sincronización

`RETENTION_*` ([DATA_RETENTION.md](DATA_RETENTION.md)), `STORAGE_PROVIDER` y `S3_*` ([DEPLOYMENT.md](DEPLOYMENT.md)). Las variables `CONTEXTRAG_*` ya no existen y se ignoran (sincronización retirada, [CONTEXTRAG_SYNC_RETIREMENT_PLAN.md](CONTEXTRAG_SYNC_RETIREMENT_PLAN.md)).

Copias de la base incluida (`docker-compose.prod.yml --profile backup`): `BACKUP_INTERVAL_SECONDS` (86400) y `BACKUP_RETENTION_DAYS` (14).

## Nunca en el repositorio

Claves de LLM, `JWT_SECRET`, contraseñas de base de datos, Redis y SMTP, credenciales de S3. Vienen del gestor de secretos del entorno. `.env` está en `.gitignore`.
