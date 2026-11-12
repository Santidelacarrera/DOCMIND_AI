# Lista de salida a producción

Estado a 2026-11-12 (revisado tras RLS fase 3, Vertex AI, copias programadas y alertas). Lo marcado **[hecho]** está en el repositorio y verificado en CI; lo marcado **[tuyo]** requiere una cuenta, un secreto o una decisión que no puede tomar un asistente.

## 1. Datos y LLM

* **[hecho]** Proveedor Gemini (`LLM_PROVIDER=gemini`), validación de la respuesta y revisión humana obligatoria de `governing_law` y `counterparty` (`ALWAYS_REVIEW_FIELDS`).
* **Decisión vigente:** API gratuita de Gemini, coste registrado 0. **Límite:** Google puede usar el contenido de la capa gratuita para mejorar sus productos. Úsala solo con documentos sintéticos o no confidenciales.
* **[hecho]** Proveedor Vertex AI (`LLM_PROVIDER=vertex`) listo y probado sin red; pasos en `docs/DEPLOYMENT.md`, «LLM para documentos reales».
* **[tuyo] Antes del primer cliente real:** crear el proyecto de Vertex AI (cuenta de servicio, región) o pasar a la capa de pago (sin uso para entrenamiento, acuerdo de tratamiento de datos), poner los precios vigentes en `GEMINI_INPUT_PRICE_PER_1M` / `GEMINI_OUTPUT_PRICE_PER_1M` y documentar proveedor, región y retención (ver `docs/DATA_RETENTION.md`).
* **[tuyo]** Medir con 30–50 documentos reales anonimizados (`docs/EVALUATION.md`): el 96 % de F1 es sobre el corpus sintético.

## 2. Secretos y configuración (staging y producción)

| Variable | Qué hacer |
|---|---|
| `ENVIRONMENT` | `staging` o `production`: activa las validaciones de arranque |
| `JWT_SECRET` | valor aleatorio largo (`python -c "import secrets;print(secrets.token_urlsafe(64))"`), distinto en cada entorno |
| `GEMINI_API_KEY` | clave del entorno, desde el gestor de secretos, nunca en el repositorio |
| `DATABASE_URL` | PostgreSQL gestionado, usuario `docmind_app` (**sin** superusuario, `BYPASSRLS` ni propiedad de tablas) |
| `DATABASE_SYSTEM_URL`, `RLS_ENFORCED=true` | `docmind_system` para las tareas de mantenimiento; activa RLS fase 3 (`docs/RLS_DESIGN.md`, «Activación») |
| `VERTEX_PROJECT`, `VERTEX_LOCATION` | con `LLM_PROVIDER=vertex`; credenciales por cuenta de servicio |
| `REDIS_URL` | Redis gestionado con contraseña |
| `STORAGE_PROVIDER` y `S3_*` / `AWS_*` | almacenamiento privado con versionado y cifrado; la misma inyección para API y worker |
| `COOKIE_SECURE=true`, `CORS_ORIGINS` | solo el dominio real, con HTTPS |
| `ANTIVIRUS_PROVIDER=clamav` | con ClamAV accesible |

Cómo validar sin mostrar valores: `docs/STAGING.md`, sección «Avoiding split configuration».

## 3. Base de datos

1. Crear la base y el usuario de la aplicación.
2. `docker compose exec api alembic upgrade head` como paso de despliegue, antes de reiniciar workers.
3. **[tuyo]** Confirmar a qué base conecta producción: el `.env` local apunta a Supabase, que no resolvió por DNS cuando se probó.

## 4. Copias de seguridad y monitorización

* **[hecho]** Procedimiento de copia y restauración de PostgreSQL probado en local (`docs/DEPLOYMENT.md`), y reconstrucción del índice vectorial sin LLM (`backend/scripts/reindex_vectors.py`, ≈ 31 min por 10 000 documentos y proceso).
* **[hecho]** Servicio de copias programadas para la base incluida (`--profile backup`: diarias, verificadas, 14 días), probado con restauración.
* **[tuyo]** Con base gestionada, activar sus copias (PITR, ≥ 14 días); en ambos casos, copiar fuera del servidor y activar el versionado del bucket.
* **[hecho]** Métricas Prometheus, trazas OpenTelemetry y 11 reglas de alerta probadas con `promtool` (`deploy/prometheus/alerts.yml`: 5xx, latencia, trabajos fallidos, vectores sin borrar, gasto del LLM). **[tuyo]** Conectarlas a Prometheus/Alertmanager y elegir a quién avisan.
* **[tuyo]** Con la capa de pago de Gemini, fijar un tope de gasto en la consola de Google.

## 5. Prueba de humo

```bash
python backend/scripts/staging_smoke.py --base-url https://staging.example.com
```

Registra dos organizaciones, sube una factura sintética, espera el procesamiento, lee la extracción, comprueba el control de exportación y que la segunda organización no ve el documento; con `--ask` además pregunta en lenguaje natural y exige una cita. Debe terminar con `smoke test passed`. **[hecho]** contra el stack Docker local; **[tuyo]** contra staging real.

## 6. Pendiente de implementación

* **Row-Level Security** (`docs/RLS_DESIGN.md`, ADR-004): fases 1–3 **[hecho]**; la suite completa pasa con la app conectada como `docmind_app` (job `backend-postgres-rls`). **[tuyo]** Activarla en staging con los pasos de «Activación», un día de tráfico, y después en producción.
* **Licencia:** propietaria (`LICENSE`); falta la revisión de un abogado y confirmar el titular definitivo antes de vender.
