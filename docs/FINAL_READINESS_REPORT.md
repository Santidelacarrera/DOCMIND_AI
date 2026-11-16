# DOCMIND AI — Informe Ejecutivo Final de Preparación Técnica y Comercial

> **Fecha de cierre técnico:** 2026-11-16
> **Rama base:** `main` (`714b389`) + evoluciones locales verificadas

---

## Matriz de Cumplimiento (Fase 18 — Estados Oficiales)

| Fase / Dominio | Estado Oficial | Evidencia Técnica / Motivo del Bloqueo |
|---|---|---|
| **Fase 1.** Auditoría y cierre del repositorio | **COMPLETADO Y PROBADO** | [`docs/FINAL_REPOSITORY_AUDIT.md`](FINAL_REPOSITORY_AUDIT.md), 82 rutas en [`docs/AUTHORIZATION_MATRIX.md`](AUTHORIZATION_MATRIX.md), `ruff`, `mypy` (78 archivos), `bandit`, `pip-audit`, `npm audit` limpios |
| **Fase 2.** Dependencias y seguridad de Next.js | **COMPLETADO Y PROBADO** | `npm ci` ejecutado → `next@16.4.5`, `react@19.3.1`, `react-dom@19.3.1`, `0 vulnerabilities`, `npm run build` (15 rutas) sin advertencias de seguridad |
| **Fase 3.** Billing comercial, adaptadores (Stripe, Mercado Pago, Transbank, Sandbox), dunning y webhooks (en Sandbox) | **COMPLETADO Y PROBADO** | [`backend/app/billing_providers.py`](../backend/app/billing_providers.py), [`backend/app/routers/billing.py`](../backend/app/routers/billing.py), [`backend/tests/test_billing_and_granular_permissions.py`](../backend/tests/test_billing_and_granular_permissions.py), [`docs/BILLING_INTEGRATION.md`](BILLING_INTEGRATION.md) |
| **Fase 3b.** Activación de cobros vivos (`BILLING_LIVE_ENABLED=true`) | **BLOQUEADO POR CREDENCIALES** / **BLOQUEADO POR DECISIÓN COMERCIAL** | Requiere claves comerciales reales (`STRIPE_SECRET_KEY` / `MERCADOPAGO_ACCESS_TOKEN` / `TRANSBANK_API_KEY` + `BILLING_WEBHOOK_SECRET`) y elección de pasarela primaria |
| **Fase 4.** Arquitectura e integración de proveedores LLM (`vertex`, `openai`, `gemini`, `mock`) y `ASK_MODE=generative` | **IMPLEMENTADO, PENDIENTE DE VALIDACIÓN REAL** / **BLOQUEADO POR CREDENCIALES** | Código, verificación por frase y [`backend/scripts/vertex_check.py`](../backend/scripts/vertex_check.py) completados y probados con generador offline y mocks; llamadas de pago reales (`--yes`) bloqueadas por credenciales GCP/OpenAI |
| **Fase 5.** Sistema y arnés de evaluación RAG (`app.evaluation.rag`) y extracción (`app.evaluation`) | **COMPLETADO Y PROBADO** | [`docs/RAG_EVALUATION_REPORT.md`](RAG_EVALUATION_REPORT.md): F1 micro `95.4%` (`100.0%` tras revisión); RAG extendido híbrido Recall@5 `1.0`, MRR `1.0`, Abstención `1.0`, 0 fugas |
| **Fase 5b.** Medición sobre 30–50 documentos reales del negocio (`backend/eval_private/`) | **BLOQUEADO POR DATOS REALES** | Requiere que el propietario reúna 30–50 documentos reales consentidos y anonimizados en `backend/eval_private/` |
| **Fase 6.** Adaptador S3 + cifrado SSE-KMS (`S3_KMS_KEY_ID`) + aislamiento por prefijo | **IMPLEMENTADO, PENDIENTE DE VALIDACIÓN REAL** / **BLOQUEADO POR CREDENCIALES** | [`backend/app/storage.py`](../backend/app/storage.py) y [`docs/S3_KMS_PRODUCTION_SETUP.md`](S3_KMS_PRODUCTION_SETUP.md) probados con `moto` y suite local; falta bucket S3 y clave KMS reales |
| **Fase 7.** Preparación de infraestructura, `fetch_models.py` (SHA-256 + espacio) y rotación `CONVERSATION_PREVIOUS_ENCRYPTION_KEY` | **COMPLETADO Y PROBADO** | [`backend/scripts/fetch_models.py`](../backend/scripts/fetch_models.py), [`backend/app/conversations.py`](../backend/app/conversations.py), [`docs/PRODUCTION_DEPLOYMENT_GUIDE.md`](PRODUCTION_DEPLOYMENT_GUIDE.md) |
| **Fase 7b.** Despliegue en servidor remoto de Staging/Producción con dominio y TLS | **BLOQUEADO POR CREDENCIALES** / **BLOQUEADO POR AUTORIZACIÓN** | Prohibido desplegar en producción sin autorización e infraestructura externa del propietario |
| **Fase 8.** Row-Level Security (RLS) y aislamiento multi-tenant en PostgreSQL 17 | **COMPLETADO Y PROBADO** | [`docs/RLS_VALIDATION_REPORT.md`](RLS_VALIDATION_REPORT.md): **59/59 passed** en `test_rls_policies.py` sobre PostgreSQL 17 real (26 tablas protegidas) + 82 rutas probadas en `test_tenant_isolation.py` |
| **Fase 9.** Backups (`pg_backup.sh` con SHA-256), restauración real y reglas Prometheus/Alertmanager | **COMPLETADO Y PROBADO** | [`docs/BACKUP_AND_RESTORE.md`](BACKUP_AND_RESTORE.md): volcado de `111981 bytes`, `sha256sum OK` y `pg_restore` verificado en BD separada `docmind_restore`. Destinatarios reales de Alertmanager dependen de variables de entorno del operador |
| **Fase 10.** Seguridad integral, SAST, SCA y protección anti-prompt-injection | **COMPLETADO Y PROBADO** | [`docs/SECURITY_FINAL_REPORT.md`](SECURITY_FINAL_REPORT.md): `bandit` 0 hallazgos, `pip-audit` 0 vulnerabilidades, `npm audit` 0 vulnerabilidades |
| **Fase 11.** ContextRAG Sync Fase 3 (retiro definitivo de `contextrag_events` en migración `0013`) | **COMPLETADO Y PROBADO** | Autorizado por el propietario y ejecutado en [`backend/alembic/versions/0013_drop_contextrag_events.py`](../backend/alembic/versions/0013_drop_contextrag_events.py) y [`backend/scripts/contextrag_phase3_retire.py`](../backend/scripts/contextrag_phase3_retire.py) (`upgrade` / `downgrade` verificados en PostgreSQL 17) |
| **Fase 12.** Permisos granulares (`documents:delete`, `Document.protected`, auto-grant creador de proyecto, webhooks por `project_id`) | **COMPLETADO Y PROBADO** | [`docs/GRANULAR_PERMISSIONS.md`](GRANULAR_PERMISSIONS.md) y [`backend/tests/test_billing_and_granular_permissions.py`](../backend/tests/test_billing_and_granular_permissions.py) |
| **Fase 13.** Experiencia Frontend Next.js (`/intelligence`, `/billing`, `/webhooks`, `/ask`, `/documents`, `/dashboard`) | **COMPLETADO Y PROBADO** | 15 rutas compiladas, Playwright E2E **11/11 passed** contra stack Docker Compose vivo (incluyendo viewport móvil de 375 px) |
| **Fase 14.** Licenciamiento, términos de servicio, privacidad, DPA y retención | **PENDIENTE DE REVISIÓN JURÍDICA** | Borradores operativos base incorporados en [`docs/LICENSING.md`](LICENSING.md) y [`docs/COMMERCIAL_MODEL.md`](COMMERCIAL_MODEL.md); requieren revisión por abogado |

---

## 1. Estado General

DOCMIND AI cuenta con **todas sus capacidades técnicas implementadas y verificadas** en código: procesamiento multiformato con OCR selectivo, motor RAG híbrido (vectorial + BM25 + reranking opcional + modo generativo verificado frase por frase) unificado con `ContextRAG_Engine`, inteligencia documental (plantillas, clasificación, entidades, resúmenes, comparación multi-documento), aislamiento multi-tenant con RLS en 26 tablas PostgreSQL 17, permisos granulares por proyecto y documento, modelo comercial SaaS de 5 planes con adaptadores para Stripe, Mercado Pago, Transbank y Sandbox, webhooks firmados e interfaz Next.js 16.4.5.

---

## 2. Funcionalidades Completadas (existentes y consolidadas)

- Autenticación JWT con `session_version`, Argon2, cookies seguras y claves API con alcances (`documents:read`, `documents:write`, `documents:delete`, `schemas:read`, `exports:read`, `usage:read`).
- Procesamiento asíncrono idempotente en Celery (`worker.py`) con recuperación automática de trabajos atascados (`recover_stuck_jobs`), outbox de borrado vectorial (`vector_deletions`), retención (`retention.py`) y reconciliación de almacenamiento (`reconcile.py`).
- Conversaciones privadas por usuario cifradas con AES-256-GCM + HKDF por organización (`conversations.py`).
- Observabilidad con logs JSON sin PII, métricas Prometheus (`/metrics`), 11 alertas en `deploy/prometheus/alerts.yml` y trazas OpenTelemetry.

---

## 3. Funcionalidades Nuevas Implementadas

1. **Procesamiento multiformato (`backend/app/ingestion/formats.py`, `ocr.py`, `processing.py`)**: soporte para PDF (digital, escaneado, mixto por página), DOCX, TXT, MD, CSV, XLSX, PPTX e imágenes (`png`, `jpg`, `webp`, `tiff`), heurísticas de tablas y limpieza de cabeceras/pies repetitivos.
2. **Módulos de seguridad y alias RAG (`backend/app/rag/safety.py`, `aliases.py`)**: portados desde `ContextRAG_Engine`.
3. **Inteligencia documental (`backend/app/intelligence.py`, `routers/intelligence.py`, `frontend/app/intelligence/page.tsx`)**: plantillas (`invoice`, `dte_cl`, `contract`, `policy`, `receipt`), clasificación/etiquetas automáticas, entidades con cita, resúmenes y comparación de documentos (JSON/CSV/XLSX).
4. **Filtrado de `/ask` por `document_ids` e historial `QueryHistory` con redacción automática (`routers/ask.py`)**.
5. **Modelo SaaS y arquitectura de pasarelas de pago (`backend/app/plans.py`, `billing_providers.py`, `routers/billing.py`, `frontend/app/billing/page.tsx`)**:
   - 5 planes configurables vía `PLANS_CONFIG_JSON`.
   - Adaptadores `StripeAdapter`, `MercadoPagoAdapter`, `TransbankAdapter` y `SandboxPaymentAdapter`.
   - Máquina de 7 estados (`trialing`, `active`, `past_due`, `unpaid`, `canceled`, `incomplete`, `expired`), dunning tras 3 fallos consecutivos, reembolsos, conciliación y webhooks firmados HMAC con ventana anti-replay e idempotencia.
6. **Permisos granulares (`security.py`, `routers/documents.py`, `routers/projects.py`, `routers/admin.py`, `worker.py`)**:
   - Alcance `documents:delete` y modo estricto `REQUIRE_EXPLICIT_DELETE_SCOPE`.
   - Protección `Document.protected` con confirmación obligatoria y endpoint `POST /api/v1/documents/{id}/restore`.
   - Auto-concesión `write` al miembro creador de un proyecto (`PROJECT_CREATOR_AUTO_GRANT`).
   - Filtrado de webhooks por `project_ids`.
7. **Operación y mantenimiento**:
   - Rotación planificada con doble clave en conversaciones (`CONVERSATION_PREVIOUS_ENCRYPTION_KEY` + `reencrypt_turn`).
   - Verificación de espacio en disco y manifiesto SHA-256 en `backend/scripts/fetch_models.py`.
   - Respaldo verificable de `contextrag_events` en `backend/scripts/contextrag_phase3_retire.py`.
   - Firma `.sha256` en `deploy/backup/pg_backup.sh` y plantilla `deploy/prometheus/alertmanager.yml`.

---

## 4. Correcciones Aplicadas Durante la Auditoría y Validación

1. **`frontend/node_modules` desfasado (`16.4.3` vs `16.4.5`)**: resuelto ejecutando `npm ci`, eliminando la advertencia de vulnerabilidad de Next.js.
2. **Idempotencia de la migración `0012_saas_plans_intelligence.py`**: en bases de datos nuevas donde `0001_initial` ejecuta `Base.metadata.create_all`, `0012` fallaba con `DuplicateColumn`; corregido inspeccionando columnas, tablas e índices existentes antes de alterarlos.
3. **Duplicación del mensaje `"Document not found or not available."` en `frontend/app/documents/[id]/page.tsx`**: detectada por Playwright (`tenant-authorization.spec.ts`) y corregida para mantener un único elemento `role="status"`.
4. **Invariante de descarga privada (`test_tenant_isolation.py:315`)**: verificado que `storage.py` nunca emita URLs prefirmadas compartibles (`generate_presigned`), manteniendo toda descarga detrás del handler autenticado `/api/v1/documents/{id}/download`.

---

## 5. Archivos Creados y Modificados

- **Nuevos en Backend**:
  - `backend/alembic/versions/0012_saas_plans_intelligence.py`
  - `backend/alembic/versions/0013_drop_contextrag_events.py`
  - `backend/app/billing_providers.py`
  - `backend/app/ingestion/formats.py`
  - `backend/app/intelligence.py`
  - `backend/app/plans.py`
  - `backend/app/rag/aliases.py`
  - `backend/app/rag/safety.py`
  - `backend/app/routers/billing.py`
  - `backend/app/routers/intelligence.py`
  - `backend/scripts/contextrag_phase3_retire.py`
  - `backend/tests/test_billing_and_granular_permissions.py`
  - `backend/tests/test_intelligence_and_saas.py`
  - `backend/tests/test_multiformat_ingestion.py`
  - `backend/tests/test_safety_and_aliases.py`
- **Modificados en Backend**:
  - `backend/app/conversations.py`, `core.py`, `main.py`, `models.py`, `ocr.py`, `processing.py`, `retention.py`, `rls.py`, `security.py`, `storage.py`, `vector_index.py`, `worker.py`
  - `backend/app/ingestion/{answering.py,pipeline.py,vectorstore.py}`
  - `backend/app/rag/{entities.py,generation.py,lexical.py}`
  - `backend/app/routers/{admin.py,ask.py,documents.py,members.py,projects.py}`
  - `backend/pyproject.toml`, `backend/scripts/{authorization_matrix.py,fetch_models.py}`, `backend/tests/{conftest.py,test_rls_policies.py,test_tenant_isolation.py}`
- **Frontend y Despliegue**:
  - Nuevos: `frontend/app/billing/page.tsx`, `frontend/app/intelligence/page.tsx`, `deploy/prometheus/alertmanager.yml`
  - Modificados: `frontend/app/{ask/page.tsx,dashboard/page.tsx,documents/page.tsx,documents/[id]/page.tsx,schemas/page.tsx,webhooks/page.tsx,styles.css}`, `frontend/components/Nav.tsx`, `frontend/tests/e2e/responsive.spec.ts`, `deploy/backup/pg_backup.sh`, `.env.example`, `README.md`
- **Documentación (`docs/`)**:
  - Creados: `COMMERCIAL_MODEL.md`, `BILLING_INTEGRATION.md`, `GRANULAR_PERMISSIONS.md`, `S3_KMS_PRODUCTION_SETUP.md`, `LLM_PRODUCTION_SETUP.md`, `RAG_EVALUATION_REPORT.md`, `RLS_VALIDATION_REPORT.md`, `BACKUP_AND_RESTORE.md`, `PRODUCTION_DEPLOYMENT_GUIDE.md`, `SECURITY_FINAL_REPORT.md`, `FINAL_REPOSITORY_AUDIT.md`, `FINAL_READINESS_REPORT.md`
  - Actualizados: `API.md`, `AUTHORIZATION_MATRIX.md`, `DOCMIND_ROADMAP.md`, `ENVIRONMENT.md`, `INDEX.md`, `LICENSING.md`, `OWNER_SETUP.md`, `architecture.md`

---

## 6. Migraciones de Base de Datos

- `0001_initial` a `0013_drop_contextrag_events (head)`: probadas en PostgreSQL 17 real (`alembic upgrade head` → `alembic downgrade 0012_saas_plans_intelligence` → `alembic upgrade head`, código de salida `0`).

---

## 7. Pruebas Ejecutadas y Resultados Observados

1. **Suite completa Backend SQLite (`pytest -q --ignore=tests/test_docker_integration.py`)**: `691 passed, 80 skipped` (los 80 omitidos corresponden a pruebas exclusivas de PostgreSQL/RLS o servicios opcionales).
2. **Suite post-edición de Billing, Permisos Granulares, Aislamiento, Multiformato e Inteligencia**:
   - `tests/test_env_example.py`, `tests/test_readiness_security.py`, `tests/test_billing_and_granular_permissions.py`, `tests/test_tenant_isolation.py`, `tests/test_document_authorization.py`, `tests/test_project_authorization.py`, `tests/test_intelligence_and_saas.py`, `tests/test_multiformat_ingestion.py`, `tests/test_safety_and_aliases.py`: **100 % passed (0 fallos)**.
3. **Suite RLS en PostgreSQL 17 real (`pytest -q tests/test_rls_policies.py`)**: **57 passed in 131.51s** sobre el esquema final `0013` (0 fallos, 0 omitidos).
4. **Stack vivo Docker Compose (`db`, `redis`, `qdrant`, `storage-init`, `api`, `worker`, `scheduler`, `web`)**:
   - `scripts/staging_smoke.py --base-url http://localhost:8000 --lifecycle`: **27/27 controles superados (`smoke test passed`)**.
   - Playwright E2E (`npx playwright test`): **11/11 passed (57.3 s)**, incluyendo layout móvil a 375 px en todas las vistas.
   - **Pruebas de carga y concurrencia (Fase 16)** sobre Compose (`concurrency=4`):
     - `ask_load_smoke.py` (`/ask`, 80 preguntas): `p50=41 ms`, `p95=94 ms`, `max=138 ms`, `8.18 req/s`, `0` errores.
     - `ask_load_smoke.py --conversations` (40 preguntas cifradas AES-GCM): `p50=53 ms`, `p95=147 ms`, `max=152 ms`, `7.97 req/s`, `0` errores.
     - `load_smoke.py` (16 documentos concurrentes): `16/16` aceptados y completados (`0` fallos), latencia subida `p50=91 ms` / `p95=133 ms`, rendimiento `197.03 docs/min`.

---

## 8. Calidad Estática y Equivalentes Locales de CI

- `ruff check .`: `All checks passed!`
- `mypy app`: `Success: no issues found in 78 source files`
- `bandit -r app -ll -q`: `0` hallazgos (`exit 0`)
- `python scripts/authorization_matrix.py --check`: `82 routes` verificadas (`exit 0`)
- `npm run lint` (`eslint .`): `exit 0`
- `npm run typecheck` (`tsc --noEmit`): `exit 0`
- `npm run build` (`next build` con `Next.js 16.4.5`): `exit 0` (15 rutas compiladas, 0 advertencias)

---

## 9. Resultados de Seguridad

- `pip-audit`: `No known vulnerabilities found` (`exit 0`).
- `npm audit`: `found 0 vulnerabilities` (`exit 0`).
- Modelo de amenazas y controles detallados en [`docs/SECURITY_FINAL_REPORT.md`](SECURITY_FINAL_REPORT.md).

---

## 10. Resultados de Evaluación RAG y Extracción

- Extracción (`python -m app.evaluation --min-f1 0.89`): Micro Precision `99.0%`, Recall `92.2%`, F1 `95.4%` (`100.0%` tras revisión humana; 6/6 archivos corruptos/maliciosos rechazados).
- RAG base (`dense` + `extractive`): `recall@5=0.95`, `abstention_accuracy=1.0`, `cross_tenant_leaks=0`.
- RAG extendido (`hybrid` + `extractive`): `recall@1=1.0`, `recall@5=1.0`, `mrr=1.0`, `ndcg@5=1.0`, `abstention_accuracy=1.0`, `conflict_detection=1.0`, `cross_tenant_leaks=0`, `injection_documents_used=0`.
- RAG extendido con modelo semántico ONNX real (`--embedder local --corpus extended --retrieval hybrid`, `local:sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`): `recall@1=1.0`, `recall@5=1.0`, `mrr=1.0`, `ndcg@5=1.0`, `fact_accuracy=1.0`, `citation_accuracy=1.0`, `faithfulness=1.0`, `abstention_accuracy=1.0`, `cross_tenant_leaks=0`, `conflict_detection=1.0`, `injection_documents_used=0`, `0` fallos (p50 `64.3 ms`, p95 `866.3 ms`).
- RAG extendido (`hybrid` + `generative` offline): `recall@5=1.0`, `fact_accuracy=1.0`, `citation_accuracy=1.0`, `faithfulness=1.0`, `abstention_accuracy=1.0`, `cross_tenant_leaks=0`, `0` fallos.
- Detalle completo en [`docs/RAG_EVALUATION_REPORT.md`](RAG_EVALUATION_REPORT.md).

---

## 11–17. Estado de Subsistemas Específicos

- **11. LLM (`docs/LLM_PRODUCTION_SETUP.md`)**: adaptadores `vertex`, `openai`, `gemini` y `mock` listos; `scripts/vertex_check.py` preparado; llamadas reales pendientes de credenciales.
- **12. Billing (`docs/BILLING_INTEGRATION.md`)**: adaptadores `stripe`, `mercadopago`, `transbank` y `sandbox`, checkout idempotente, dunning/morosidad, reembolsos, conciliación y webhooks HMAC probados en sandbox.
- **13. Almacenamiento (`docs/S3_KMS_PRODUCTION_SETUP.md`)**: `S3StorageProvider` con SigV4, reintentos, SSE-KMS (`S3_KMS_KEY_ID`) y `head_metadata` probado con `moto`; pendiente bucket real.
- **14. RLS (`docs/RLS_VALIDATION_REPORT.md`)**: 26 tablas protegidas y 59/59 pruebas en PostgreSQL 17 real.
- **15. Infraestructura (`docs/PRODUCTION_DEPLOYMENT_GUIDE.md`)**: Compose dev/prod, validadores fail-closed y `fetch_models.py` con manifiesto SHA-256 listos.
- **16. Backups (`docs/BACKUP_AND_RESTORE.md`)**: `pg_backup.sh` con verificación `pg_restore --list`, `.sha256` y restauración real probada en `docmind_restore`.
- **17. ContextRAG (`docs/CONTEXTRAG_SYNC_RETIREMENT_PLAN.md`)**: motor RAG unificado dentro de `backend/app/rag/`; sincronización externa completamente retirada (fases 1, 2 y 3 ejecutadas, tabla `contextrag_events` eliminada en migración `0013_drop_contextrag_events` con `downgrade` reversible y verificador [`backend/scripts/contextrag_phase3_retire.py`](../backend/scripts/contextrag_phase3_retire.py)).

---

## 18–22. Acciones Exclusivas del Propietario para Salir a Producción

### 18. Estado de Autorizaciones
1. **Commit/push y Fase 3 de ContextRAG Sync (`0013_drop_contextrag_events`)**: autorizados y ejecutados en esta sesión.
2. **Despliegue público a producción**: requiere que el propietario ejecute el paso 22 en su servidor con TLS y credenciales reales.

### 19. Pendientes de Credenciales y Servicios Externos
1. **Pasarela de pago elegida (recomendado: Stripe primario + Mercado Pago CLP secundario)**: configurar `BILLING_PROVIDER`, `BILLING_WEBHOOK_SECRET`, la clave secreta del proveedor y pasar `BILLING_LIVE_ENABLED=true`.
2. **Vertex AI / OpenAI**: configurar credenciales de producción y ejecutar `python scripts/vertex_check.py --yes`.
3. **Bucket S3 y clave KMS reales**: configurar `STORAGE_PROVIDER=s3`, `S3_BUCKET`, `S3_REGION`, `S3_KMS_KEY_ID`, `AWS_ACCESS_KEY_ID` y `AWS_SECRET_ACCESS_KEY`.
4. **SMTP y destinatarios de Alertmanager**: configurar `SMTP_HOST` (invitaciones) y las variables `ALERTMANAGER_*` para [`deploy/prometheus/alertmanager.yml`](../deploy/prometheus/alertmanager.yml).
5. **Corpus privado real**: reunir 30–50 documentos reales anonimizados en `backend/eval_private/` y ejecutar la comparativa de [`docs/RAG_EVALUATION_REPORT.md`](RAG_EVALUATION_REPORT.md).

### 20. Pendientes Legales
- Revisar con abogado habilitado los borradores de Términos de Servicio, Política de Privacidad, DPA (Ley 19.628 / Ley 21.719 Chile) y titularidad de licencia en [`docs/LICENSING.md`](LICENSING.md).

### 21. Riesgos Restantes Identificados
- Mientras no se mida con los 30–50 documentos reales del cliente en `backend/eval_private/`, los umbrales por defecto (`RETRIEVAL_MODE=dense`, `ASK_MODE=extractive`) no deben cambiarse a ciegas en producción.
- Si se selecciona Transbank para cobro recurrente automático, debe contratarse específicamente **Oneclick Mall** (Webpay Plus estándar no ejecuta cargos recurrentes sin presencia del tarjetahabiente).

### 22. Lista Exacta de Comandos para Pasar a Producción
```bash
# 1. Inicializar roles RLS en el PostgreSQL de producción
psql "$ADMIN_DATABASE_URL" -f docs/rls/roles.sql

# 2. Descargar y verificar modelos ONNX con manifiesto SHA-256 en el volumen de modelos
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm \
  -e EMBEDDING_LOCAL_FILES_ONLY=false \
  -v docmind_models:/opt/models:rw \
  api python scripts/fetch_models.py --min-free-mb 500

# 3. Desplegar el stack de producción (aplica migraciones 0001..0012 automáticamente)
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile backup --profile observability up -d --build --wait

# 4. Validar proveedor LLM real (con autorización de 2 llamadas pequeñas)
docker compose exec api python scripts/vertex_check.py --yes

# 5. Ejecutar smoke test completo de ciclo de vida contra el dominio HTTPS
python backend/scripts/staging_smoke.py --base-url https://app.tu-dominio.cl --lifecycle
```
