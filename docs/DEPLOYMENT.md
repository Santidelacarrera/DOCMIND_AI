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
5. ContextRAG como proceso aparte: `uvicorn --factory contextrag.api.server:app_factory --port 8100` con `CONTEXTRAG_KEYS_FILE`. Verificado con el ensayo en vivo.
6. `python scripts/contextrag_live_check.py ...`. Verificado (sección de ContextRAG).

### No verificado

* **Celery beat, ventanas largas.** Beat sí se ha ejecutado localmente con Redis y PostgreSQL reales (`backend/scripts/beat_smoke.sh`): `deliver-contextrag-events` (30 s), `recover-stuck-jobs` y `process-vector-deletions` (60 s) se enviaron y terminaron con éxito. **No observado:** `purge-expired-data` y `reconcile-contextrag` (3600 s) y `reconcile-storage` (86400 s), porque la ventana de la prueba fue de 75 s. Su lógica sí está probada a mano y en `tests/test_periodic_tasks.py`. Ver `docs/EXTERNAL_DEPENDENCIES.md`.
* Despliegue en staging con `docker compose -f docker-compose.yml -f docker-compose.prod.yml`. Docker no estaba disponible.
* Migraciones contra PostgreSQL 17 (el de CI).
* Object storage S3 real, ClamAV real, SMTP real.

### Orden de despliegue

1. Migraciones: `alembic upgrade head`, una sola vez, antes de arrancar réplicas.
2. API y workers.
3. Una única instancia de beat (o el planificador de la plataforma).
4. ContextRAG solo cuando `CONTEXTRAG_SYNC_ENABLED=true`.

### Backups y restauración

* **PostgreSQL, verificado:** `pg_dump -Fc docmind_e2e` y `pg_restore --no-owner` en una base nueva. Los conteos de `users`, `documents`, `extraction_fields`, `contextrag_events` y `audit_logs` coincidieron (51, 190, 736, 178, 324).
* **Almacenamiento, no verificado:** el volcado de PostgreSQL no incluye los archivos. Hay que copiar el bucket con versionado. Después, `python -m app.reconcile --dry-run` debe mostrar `orphans: 0` y `rows_without_object` sin documentos vivos.
* **Restauración completa:** restaurar la base en un entorno aislado, restaurar los objetos de la misma fecha, ejecutar migraciones, verificar `/ready` y una descarga de un documento propio. No verificada en un entorno real.

### Observabilidad

* `/metrics` sin autenticación: restringirlo en el ingress. Las etiquetas usan la plantilla de ruta; las rutas no coincidentes se agrupan como `unmatched` (verificado con `tests/test_metrics_labels.py`).
* Logs JSON por stdout. Los mensajes de excepción no se registran, solo los marcos y los tipos.
* `GET /api/v1/contextrag/events` para administradores de cada organización.

---

## Compose overlay networks

`docker-compose.prod.yml` puts the bundled `db`/`redis` on an internal `backend` network without egress, while `api` and `worker` also join `edge` for outbound traffic to object storage, the LLM provider and ClamAV. Set `REDIS_PASSWORD` and use it in `REDIS_URL`. Prefer managed PostgreSQL/Redis in production; the bundled services are for single-host staging.

---
Relacionado: [[empresa]]
