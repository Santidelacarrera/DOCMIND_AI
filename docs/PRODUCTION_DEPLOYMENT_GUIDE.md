# Guía de Despliegue en Staging y Producción

> **Estado:** Archivos [`docker-compose.yml`](../docker-compose.yml), [`docker-compose.prod.yml`](../docker-compose.prod.yml), validaciones de entorno en [`backend/app/core.py`](../backend/app/core.py) y aprovisionamiento offline de modelos en [`backend/scripts/fetch_models.py`](../backend/scripts/fetch_models.py) **COMPLETADOS Y VERIFICADOS**.

---

## 1. Controles de arranque en `staging` y `production` (`core.py`)

El sistema falla de forma explícita e inmediata al arrancar (`ValueError`) si detecta cualquier configuración insegura fuera de `development`/`test`:

- `JWT_SECRET` ausente o con menos de 32 caracteres.
- `COOKIE_SECURE` distinto de `true`.
- `CORS_ORIGINS` vacío o con orígenes que no empiecen por `https://`.
- `FRONTEND_BASE_URL` que no empiece por `https://`.
- `ANTIVIRUS_PROVIDER=disabled`.
- `STORAGE_PROVIDER=local` (exige `s3` con `S3_BUCKET`, `S3_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`).
- `SMTP_HOST` vacío.
- `QDRANT_URL` sin `https://` o sin `QDRANT_API_KEY`.
- **`EMBEDDING_PROVIDER=hashing` prohibido tajantemente en staging y producción** (`ValueError: EMBEDDING_PROVIDER=hashing is for development only`).
- `RLS_ENFORCED=true` sin `DATABASE_SYSTEM_URL`.
- `BILLING_LIVE_ENABLED=true` con `BILLING_PROVIDER=sandbox` o sin `BILLING_WEBHOOK_SECRET` y la credencial del proveedor activo.

---

## 2. Aprovisionamiento verificable de modelos locales (`/opt/models/`)

En producción los contenedores `api` y `worker` montan el volumen `models` en `/opt/models` en modo **solo lectura (`:ro`)** y ejecutan con `EMBEDDING_LOCAL_FILES_ONLY=true`.

Antes de levantar los servicios, aprovisionar y verificar los modelos ONNX con [`backend/scripts/fetch_models.py`](../backend/scripts/fetch_models.py):

```bash
# 1. Descargar modelos, limpiar temporales, verificar espacio >= 500 MB, probar carga offline y generar models_manifest.json (SHA-256)
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm \
  -e EMBEDDING_LOCAL_FILES_ONLY=false \
  -v docmind_models:/opt/models:rw \
  api python scripts/fetch_models.py --min-free-mb 500

# 2. Verificar en cualquier momento el manifiesto SHA-256 sin tocar la red
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm \
  api python scripts/fetch_models.py --verify-only
```

---

## 3. Gestión y rotación de `CONVERSATION_ENCRYPTION_KEY`

Las conversaciones (`/api/v1/conversations`) se cifran por turno con **AES-256-GCM** y clave derivada por organización mediante **HKDF-SHA256** ([`backend/app/conversations.py`](../backend/app/conversations.py)).

### Generación inicial
```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Rotación planificada sin pérdida de historial (`CONVERSATION_PREVIOUS_ENCRYPTION_KEY`)
1. Generar una nueva clave primaria y mover la clave anterior a `CONVERSATION_PREVIOUS_ENCRYPTION_KEY`:
   ```env
   CONVERSATION_ENCRYPTION_KEY=<nueva-clave>
   CONVERSATION_PREVCRYPTION_KEY_LEGACY=... # usar CONVERSATION_PREVIOUS_ENCRYPTION_KEY=<clave-anterior>
   ```
2. Mientras `CONVERSATION_PREVIOUS_ENCRYPTION_KEY` esté definida:
   - Todo turno nuevo se cifra con `CONVERSATION_ENCRYPTION_KEY`.
   - `open_turn()` intenta abrir primero con la clave actual y, si recibe `InvalidTag`, abre con `CONVERSATION_PREVIOUS_ENCRYPTION_KEY`.
3. Para recifrar los turnos históricos en base de datos y retirar la clave antigua, usar `conversations.reencrypt_turn(organization_id, conversation_id, seq, role, payload)` en una ventana de mantenimiento y luego vaciar `CONVERSATION_PREVIOUS_ENCRYPTION_KEY`.

---

## 4. Secuencia paso a paso de despliegue en Staging / Producción

```bash
# Paso 1: Crear roles RLS en PostgreSQL (una sola vez como superusuario)
psql "$ADMIN_DATABASE_URL" -f docs/rls/roles.sql

# Paso 2: Aprovisionar y verificar modelos ONNX en el volumen compartido
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm \
  -e EMBEDDING_LOCAL_FILES_ONLY=false \
  -v docmind_models:/opt/models:rw \
  api python scripts/fetch_models.py

# Paso 3: Levantar stack de producción (ejecuta migraciones 0001..0012 automáticamente)
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile backup --profile observability up -d --build --wait

# Paso 4: Ejecutar prueba de humo de ciclo completo contra staging
cd backend
python scripts/staging_smoke.py --base-url https://staging.tu-dominio.cl --lifecycle
```
