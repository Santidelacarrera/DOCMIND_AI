# Solución de problemas

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `/ready` falla | Base de datos inaccesible o sin migrar | Revisar `DATABASE_URL`; `docker compose exec api alembic upgrade head` |
| `503 RATE_LIMIT_UNAVAILABLE` | Redis caído (el limitador falla cerrado) | Comprobar `REDIS_URL` y el servicio `redis` |
| Documento en `QUEUED` para siempre | Worker parado o en otro Redis | `docker compose logs worker`; beat lo reencola tras `QUEUED_STUCK_SECONDS` |
| `503 VECTOR_INDEX_DISABLED` en `/ask` | `QDRANT_URL` vacío | Configurar Qdrant (el stack de desarrollo ya lo trae) |
| `503 EMBEDDING_MODEL_MISMATCH` en `/ask`, o indexado `refused:EMBEDDING_MODEL_MISMATCH` | El índice se construyó con otro modelo de embeddings | Volver a fijar `EMBEDDING_MODEL` al modelo anterior, o reindexar todos los documentos (`POST /documents/{id}/process`) sobre una colección nueva |
| `/ask` siempre responde "No encontré información" | Documento aún no indexado, sin acceso al proyecto, o la evidencia no alcanza el umbral | Esperar al indexado; comprobar permisos del proyecto; ver ADR-002/003 |
| `EmbedderUnavailable` al arrancar el worker | `EMBEDDING_PROVIDER=local` sin `fastembed` o sin el modelo | `pip install '.[embeddings]'` y `EMBEDDING_LOCAL_FILES_ONLY=false` la primera vez, o usar `hashing` en desarrollo |
| `OCR_FAILED` | Falta Tesseract o Poppler | Instalar `tesseract-ocr poppler-utils` (la imagen Docker ya los trae) |
| Muchos campos en revisión en escaneados | Palabras leídas con baja confianza OCR | Esperado: ver `OCR_WORD_REVIEW_THRESHOLD`; mejorar la resolución del escaneo |
| El visor PDF sale en blanco | El origen web no está en `CORS_ORIGINS` (también controla `frame-ancestors`) | Añadir el origen exacto |
| El navegador llama a otra API | `NEXT_PUBLIC_API_URL` se fija al compilar | Reconstruir la imagen `web` |
| `403 CSRF_FAILED` | Falta `X-CSRF-Token` | Enviarlo igual a la cookie `csrf_token`, o usar Bearer |
| La API no arranca en staging | Validación de configuración segura | Leer el mensaje; ver [STAGING.md](STAGING.md) |
| Git Bash: `docker run -v …:/ruta` falla con rutas `C:/Program Files/Git/…` | Git Bash reescribe rutas | `export MSYS_NO_PATHCONV=1`; con heredoc, `docker exec -i` |
| Pruebas que leen el código fallan en Windows con `UnicodeDecodeError` | Caracteres no ASCII en el código fuente | Mantener `backend/app/*.py` en ASCII (usar `\u` escapes) |
