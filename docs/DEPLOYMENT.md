# Deployment

`docker-compose.yml` is development-only. Staging uses `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d` with secrets injected by the platform; it must set `ENVIRONMENT=staging`, `COOKIE_SECURE=true`, `JWT_SECRET`, `DATABASE_URL`, `REDIS_URL`, exact `CORS_ORIGINS`, and `ANTIVIRUS_PROVIDER=clamav`. Run `alembic upgrade head` before rolling API and worker replicas.

Production requires managed PostgreSQL and Redis, private encrypted object storage, ClamAV (or an equivalent provider implementation), TLS termination/reverse proxy, secret manager, centralized structured logs/metrics and alerting. The application sets browser security headers; the ingress owns certificates, TLS redirects and request-size limits.

Backups are an infrastructure responsibility: take encrypted PostgreSQL point-in-time backups and storage versioned backups. Define RPO/RTO with the operator and rehearse restores in staging: restore a timestamped PostgreSQL backup into an isolated database, restore matching object-storage versions, run migrations, then verify `/ready` and a tenant-scoped document download. Do not claim a backup exists merely because this procedure exists.

## Procedimiento reproducible (estado verificado el 2026-10-27)

Cada paso indica si se ejecutó tal cual en el sandbox. Lo marcado como **no verificado** no debe darse por probado.

### Variables y secretos requeridos

| Variable | Valor | Obligatoria en staging/producción |
|---|---|---|
| `ENVIRONMENT` | `staging` o `production` | Sí. Cualquier valor que no sea `development` o `test` se trata como producción (`Settings.is_production`) |
| `JWT_SECRET` | ≥32 caracteres, del gestor de secretos | Sí. Sin él el arranque se niega (verificado: `tests/test_config_fail_closed.py`) |
| `DATABASE_URL` | PostgreSQL gestionado | Sí |
| `REDIS_URL` | Redis gestionado con contraseña | Sí |
| `COOKIE_SECURE` | `true` | Sí |
| `CORS_ORIGINS` | Origen HTTPS exacto | Sí |
| `FRONTEND_BASE_URL` | URL HTTPS | Sí |
| `STORAGE_PROVIDER` | `s3` con `S3_BUCKET`, `S3_REGION`, `S3_KMS_KEY_ID` | Sí (`local` se rechaza) |
| `ANTIVIRUS_PROVIDER` | `clamav` | Sí (`disabled` se rechaza) |
| `SMTP_HOST` | servidor SMTP | Sí |
| `CONTEXTRAG_SYNC_ENABLED` | `false` por defecto | No |
| `CONTEXTRAG_URL`, `CONTEXTRAG_KEYS_FILE`, `CONTEXTRAG_ALLOWED_HOSTS` | solo si la sincronización está activa | Sí si `true` |
| `CONTEXTRAG_CONTENT_MODE` | `masked` (por defecto) | `raw` se rechaza en staging/producción |
| `STORAGE_RECONCILE_DELETE` | `false` por defecto | Dejar en `false` salvo decisión explícita |

Ver `.env.example` para la lista completa y los valores de desarrollo.

### Local, verificado (sandbox)

1. PostgreSQL 16 y Redis locales (sin Docker en el sandbox).
2. `alembic upgrade head` con `DATABASE_URL` apuntando a la base. Verificado: sube, baja a `base` (queda solo `alembic_version`) y vuelve a subir (22 tablas).
3. `uvicorn app.main:app --port 8000` y `celery -A app.worker.celery_app worker --concurrency=2`. Verificado.
4. `cd frontend && npm ci && npm run build && npm run start -- -p 3000`. Build y `npm run start` verificados; el build se ejecutó en la fase anterior y el código de la interfaz no cambió después.
5. *(Histórico)* ContextRAG como proceso aparte y `scripts/contextrag_live_check.py`. Retirados el 2026-11-12: las capacidades están dentro de DOCMIND (ADR-005, `CONTEXTRAG_SYNC_RETIREMENT_PLAN.md`).

### No verificado

* **Celery beat, ventanas largas.** Beat sí se ha ejecutado localmente con Redis y PostgreSQL reales (`backend/scripts/beat_smoke.sh`): `deliver-contextrag-events` (30 s), `recover-stuck-jobs` y `process-vector-deletions` (60 s) se enviaron y terminaron con éxito. **No observado:** `purge-expired-data` y `reconcile-contextrag` (3600 s) y `reconcile-storage` (86400 s), porque la ventana de la prueba fue de 75 s. Su lógica sí está probada a mano y en `tests/test_periodic_tasks.py`. Ver `docs/EXTERNAL_DEPENDENCIES.md`.
* Despliegue en staging con `docker compose -f docker-compose.yml -f docker-compose.prod.yml`. No ejecutado: el daemon de Docker se arrancó en el sandbox y se usaron contenedores sueltos (PostgreSQL 17, Qdrant, ClamAV, SMTP y S3 de prueba) para las pruebas en vivo, pero la composición completa no se ha levantado. Ver `docs/EXTERNAL_DEPENDENCIES.md`.
* Migraciones contra PostgreSQL 17 (el de CI).
* Object storage S3 real, ClamAV real, SMTP real.

### Orden de despliegue

1. Migraciones: `alembic upgrade head`, una sola vez, antes de arrancar réplicas.
2. **Modelos locales** (una vez, y de nuevo al cambiar `EMBEDDING_MODEL` o `RERANK_MODEL`). La imagen incluye `fastembed` pero no los ficheros del modelo, que van al volumen `models` montado en `/opt/models` (solo lectura en `api` y `worker`):
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm      -e EMBEDDING_LOCAL_FILES_ONLY=false -v docmind_models:/opt/models:rw      api python scripts/fetch_models.py
   ```
   El script descarga el modelo (público, sin credenciales) y lo vuelve a cargar sin red. Si sale con un código distinto de 0, no hay que arrancar: sin el modelo, la API no puede indexar ni responder.
   **Verificado** el 2026-11-12 con la imagen de producción: el modelo multilingüe y `bge-reranker-v2-m3-int8` se cargan con `--network none`.
3. API y workers.
4. Una única instancia de beat (o el planificador de la plataforma).
5. Ningún servicio ContextRAG: la sincronización está retirada (fases 1 y 2 de `docs/CONTEXTRAG_SYNC_RETIREMENT_PLAN.md`).

Comprobación posterior (con datos ficticios, nunca de clientes): `python scripts/staging_smoke.py --base-url https://<staging> --lifecycle`, y para medir latencia `python scripts/ask_load_smoke.py --api https://<staging>`.

### Backups y restauración

* **PostgreSQL, verificado:** `pg_dump -Fc docmind_e2e` y `pg_restore --no-owner` en una base nueva. Los conteos de `users`, `documents`, `extraction_fields`, `contextrag_events` y `audit_logs` coincidieron (51, 190, 736, 178, 324).
* **Almacenamiento, no verificado:** el volcado de PostgreSQL no incluye los archivos. Hay que copiar el bucket con versionado. Después, `python -m app.reconcile --dry-run` debe mostrar `orphans: 0` y `rows_without_object` sin documentos vivos.
* **Restauración completa:** restaurar la base en un entorno aislado, restaurar los objetos de la misma fecha, ejecutar migraciones, verificar `/ready` y una descarga de un documento propio. No verificada en un entorno real.
* **Base + archivos, verificado en local el 2026-11-10** (stack de `docker-compose.yml`, almacenamiento `local`, datos de prueba sintéticos). Copia y restauración en una base y un volumen **nuevos**, sin tocar los originales:

  ```bash
  # copia (en Git Bash, export MSYS_NO_PATHCONV=1 antes)
  docker compose exec -T db pg_dump -U docmind -Fc docmind > docmind.dump
  docker run --rm -v <proyecto>_documents:/data:ro -v "$PWD:/backup" alpine:3.21 tar -C /data -czf /backup/documents.tgz .
  # restauración en destino nuevo
  docker compose exec -T db psql -U docmind -d postgres -c "create database docmind_restore_check"
  docker compose exec -T db pg_restore -U docmind --no-owner -d docmind_restore_check < docmind.dump
  docker volume create <proyecto>_documents_restore
  docker run --rm -v <proyecto>_documents_restore:/data -v "$PWD:/backup:ro" alpine:3.21 tar -C /data -xzf /backup/documents.tgz
  # comprobación a nivel de aplicación contra lo restaurado
  docker run --rm --network <proyecto>_default -v <proyecto>_documents_restore:/data/documents --env-file .env     -e DATABASE_URL=postgresql+psycopg://docmind:<password>@db:5432/docmind_restore_check -e QDRANT_URL=     <proyecto>-api python -m app.reconcile --dry-run
  ```

  Resultado: conteos iguales en `users` 5, `organizations` 5, `documents` 3, `document_versions` 3, `extraction_fields` 12, `audit_logs` 15; los 3 objetos presentes con SHA-256 idéntico; `reconcile --dry-run` → `objects: 3, orphans: 0, rows_without_object: 0`. Control negativo: con el volumen vacío el mismo verificador informó 3 objetos ausentes.
* **Qdrant:** los vectores son datos derivados y no se respaldan. Tras restaurar (o para cambiar de modelo de embeddings sobre una colección nueva) se reconstruyen con `python scripts/reindex_vectors.py [--organization-id <uuid>]`: lee los archivos guardados, no llama al LLM y no toca extracciones ni revisiones. **Medido** (2026-11-10, 12 CPU, un proceso, Qdrant en memoria, documentos de 5 páginas): 5,3 documentos/s con el modelo multilingüe, 55/s con `hashing`; unas **31 minutos para 10 000 documentos** por proceso, sin contar la red hasta Qdrant ni la lectura desde S3. No uses `POST /documents/{id}/process` para esto: repite la extracción con el LLM. `scripts/backfill_vector_projects.py` no reindexa: solo marca el proyecto en vectores existentes. **No verificado:** snapshot y restauración de Qdrant.
* **Copias programadas de la base incluida, verificado en local el 2026-11-12:** `docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile backup up -d` arranca el servicio `backup` (`deploy/backup/pg_backup.sh`): `pg_dump -Fc` diario, comprobado con `pg_restore --list` antes de guardarlo, 14 días de retención (`BACKUP_INTERVAL_SECONDS`, `BACKUP_RETENTION_DAYS`). Una copia que no se puede leer se borra y el servicio sale con error, visible en `docker compose ps`. Prueba: copia de una base con 1 000 filas, restaurada en una base nueva con 1 000/1 000. El volumen `backups` debe estar en disco cifrado y copiarse fuera del servidor; con una base gestionada, usar sus copias (PITR) en vez de este servicio.
* **S3 y KMS:** el proveedor está probado contra un S3 en proceso (`moto`, `tests/test_s3_storage.py`): ida y vuelta, `SSE-KMS` con la clave configurada en cada escritura, objeto ausente como «no encontrado» (antes subía como error 500 en descargas e indexado), listado paginado y prefijo de organización. **Sin verificar con una cuenta real:** permisos IAM, política del bucket, versionado y la clave KMS del cliente.

### LLM para documentos reales (Vertex AI)

`LLM_PROVIDER=vertex` usa Gemini en Vertex AI (de pago, sin uso para entrenamiento, bajo los términos de tratamiento de datos de Google Cloud). Pasos con la cuenta:

1. Proyecto de Google Cloud con facturación y la API de Vertex AI activada; región según residencia de datos (`southamerica-east1` es São Paulo).
2. Cuenta de servicio con el rol `roles/aiplatform.user` y nada más; credenciales por identidad de carga de trabajo o `GOOGLE_APPLICATION_CREDENTIALS` montado como secreto. Nunca una clave en `.env`.
3. `VERTEX_PROJECT`, `VERTEX_LOCATION`, `VERTEX_MODEL` (vacío = `GEMINI_MODEL`) y los precios vigentes en `GEMINI_INPUT_PRICE_PER_1M` / `GEMINI_OUTPUT_PRICE_PER_1M`.
4. Tope de gasto en la consola de Google Cloud y la alerta `DocMindLlmSpendHigh`.
5. Medir con documentos reales anonimizados antes de abrir a clientes (`docs/EVALUATION.md`).

Probado sin red (`tests/test_vertex_provider.py`): endpoint regional y global, token de credenciales cargado una vez y renovado al expirar, errores de credenciales como `LLM_UNAVAILABLE` reintentable sin filtrar rutas ni cuentas, coste estimado. **Sin verificar contra Vertex real** (sin cuenta).

### Observabilidad

* `/metrics` sin autenticación: restringirlo en el ingress. Las etiquetas usan la plantilla de ruta; las rutas no coincidentes se agrupan como `unmatched` (verificado con `tests/test_metrics_labels.py`).
* Logs JSON por stdout. Los mensajes de excepción no se registran, solo los marcos y los tipos.
* `GET /api/v1/contextrag/events` para administradores de cada organización.

---

## Compose overlay networks

`docker-compose.prod.yml` puts the bundled `db`/`redis` on an internal `backend` network without egress, while `api` and `worker` also join `edge` for outbound traffic to object storage, the LLM provider and ClamAV. Set `REDIS_PASSWORD` and use it in `REDIS_URL`. Prefer managed PostgreSQL/Redis in production; the bundled services are for single-host staging.

---
Relacionado: [[empresa]]
