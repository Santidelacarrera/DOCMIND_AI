# Lista de salida a producción

Estado a 2026-10-31. Lo marcado **[hecho]** está en el repositorio y verificado en CI; lo marcado **[tuyo]** requiere una cuenta, un secreto o una decisión que no puede tomar un asistente.

## 1. Datos y LLM

* **[hecho]** Proveedor Gemini (`LLM_PROVIDER=gemini`), validación de la respuesta y revisión humana obligatoria de `governing_law` y `counterparty` (`ALWAYS_REVIEW_FIELDS`).
* **Decisión vigente:** API gratuita de Gemini, coste registrado 0. **Límite:** Google puede usar el contenido de la capa gratuita para mejorar sus productos. Úsala solo con documentos sintéticos o no confidenciales.
* **[tuyo] Antes del primer cliente real:** pasar a la capa de pago o a Vertex AI (sin uso para entrenamiento, acuerdo de tratamiento de datos), poner los precios vigentes en `GEMINI_INPUT_PRICE_PER_1M` / `GEMINI_OUTPUT_PRICE_PER_1M` y documentar proveedor, región y retención (ver `docs/DATA_RETENTION.md`).
* **[tuyo]** Medir con 30–50 documentos reales anonimizados (`docs/EVALUATION.md`): el 96 % de F1 es sobre el corpus sintético.

## 2. Secretos y configuración (staging y producción)

| Variable | Qué hacer |
|---|---|
| `ENVIRONMENT` | `staging` o `production`: activa las validaciones de arranque |
| `JWT_SECRET` | valor aleatorio largo (`python -c "import secrets;print(secrets.token_urlsafe(64))"`), distinto en cada entorno |
| `GEMINI_API_KEY` | clave del entorno, desde el gestor de secretos, nunca en el repositorio |
| `DATABASE_URL` | PostgreSQL gestionado, usuario **sin** superusuario ni `BYPASSRLS` |
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

* **[tuyo]** Copias automáticas de PostgreSQL (diarias, retención ≥ 14 días, restauración probada una vez) y versionado del almacenamiento de objetos.
* **[hecho]** Métricas Prometheus y trazas OpenTelemetry (`docs/OBSERVABILITY.md`). **[tuyo]** Conectar alertas: tasa de 5xx, trabajos fallidos, cola creciente, gasto del LLM.
* **[tuyo]** Con la capa de pago de Gemini, fijar un tope de gasto en la consola de Google.

## 5. Prueba de humo

```bash
python backend/scripts/staging_smoke.py --base-url https://staging.example.com
```

Registra dos organizaciones, sube una factura sintética, espera el procesamiento, lee la extracción, comprueba el control de exportación y que la segunda organización no ve el documento. Debe terminar con `smoke test passed`. No se ha ejecutado contra un despliegue real todavía.

## 6. Pendiente de implementación

* **Row-Level Security** (`docs/RLS_DESIGN.md`): decidido (SECURITY DEFINER, por fases) y **no implementado**. El aislamiento actual depende de la aplicación y de sus pruebas negativas (`test_cross_org_negative.py`). El diseño prohíbe activaciones parciales y exige probarlo sobre PostgreSQL 17 con el rol `docmind_app`, así que no se hace sin un entorno donde probarlo.
* **Licencia:** propietaria (`LICENSE`); falta la revisión de un abogado y confirmar el titular definitivo antes de vender.
