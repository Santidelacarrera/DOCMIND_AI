# Auditoría Integral y Cierre Técnico del Repositorio (2026-11-16)

> **Estado:** **COMPLETADO Y PROBADO** sobre el árbol actual (`main` en `714b389` + evoluciones comerciales, multiformato, inteligencia documental, permisos granulares y pasarelas de pago).

---

## 1. Alcance auditado en Backend, Frontend y Documentación

### 1.1 Backend (`backend/`)
- **Migración `0012_saas_plans_intelligence.py`**: idempotente tanto sobre bases nuevas (`0001_initial`) como en actualizaciones desde `0011_conversations`. Añade valores `STARTER`, `PROFESSIONAL`, `BUSINESS` al enum `plan`; columnas `size_bytes`, `category`, `tags`, `tables_detected`, `protected` en `documents`; columna `project_ids` en `webhooks`; tablas `organization_subscriptions`, `query_history`, `billing_transactions` y `billing_webhook_events` con políticas RLS (`TENANT_TABLES_0012`) y concesiones a `docmind_app` y `docmind_system`.
- **`backend/app/ingestion/formats.py`**: detección y extracción segura de PDF, DOCX, TXT, MD, CSV, XLSX, PPTX e imágenes (`png`, `jpg`, `webp`, `tiff`), con guardas contra NUL bytes, zip-bombs y macros/OLE/contenido activo en OOXML (`_inspect_ooxml`), más heurísticas de tablas (`count_pdf_tables`) y limpieza de cabeceras/pies repetitivos (`strip_running_headers_footers`).
- **`backend/app/intelligence.py` y `backend/app/routers/intelligence.py`**: plantillas de esquema (`invoice`, `dte_cl`, `contract`, `policy`, `receipt`), clasificación automática y etiquetado, extracción determinista de entidades con evidencia por página, resúmenes extractivos con citas por documento y proyecto, y comparación multi-documento con detección de contradicciones exportable a JSON, CSV y XLSX.
- **`backend/app/plans.py`, `backend/app/billing_providers.py` y `backend/app/routers/billing.py`**: catálogo de 5 planes parametrizable vía `PLANS_CONFIG_JSON`, cuotas estrictas `402 QUOTA_EXCEEDED`, alertas desde el 80 %, adaptadores desacoplados (`StripeAdapter`, `MercadoPagoAdapter`, `TransbankAdapter`, `SandboxPaymentAdapter`), máquina de 7 estados normalizados (`trialing`, `active`, `past_due`, `unpaid`, `canceled`, `incomplete`, `expired`), webhooks firmados HMAC con ventana anti-replay e idempotencia, reembolsos y conciliación.
- **`backend/app/rag/safety.py` y `backend/app/rag/aliases.py`**: portados desde `ContextRAG_Engine` e integrados en generación verificada, respuestas extractivas y puerta de entidades nombradas.
- **Permisos granulares (`security.py`, `routers/documents.py`, `routers/projects.py`, `routers/admin.py`, `worker.py`)**: alcance `documents:delete`, protección `Document.protected` con confirmación obligatoria y restauración (`restore`), auto-concesión `write` al miembro creador de un proyecto (`PROJECT_CREATOR_AUTO_GRANT`), y filtrado de webhooks por `project_ids`.
- **Scripts operativos**: `fetch_models.py` (espacio en disco + manifiesto SHA-256), `contextrag_phase3_retire.py` (respaldo verificable sin `DROP TABLE`), `authorization_matrix.py` (82 rutas).

### 1.2 Frontend (`frontend/`)
- **Dependencias sincronizadas (`npm ci`)**: `next@16.4.5`, `react@19.3.1`, `react-dom@19.3.1` (`0 vulnerabilities` en `npm audit`, paridad exacta con la imagen Docker).
- **15 rutas compiladas en producción (`npm run build`)**: `/`, `/login`, `/register`, `/invitations/accept`, `/dashboard`, `/documents`, `/documents/[id]`, `/ask`, `/intelligence`, `/conversations`, `/schemas`, `/members`, `/webhooks`, `/billing`, `/_not-found`.
- **Conexión real extremo a extremo**: todas las vistas consumen endpoints reales de FastAPI, sin datos simulados hardcodeados en la interfaz, con estados de carga/error y diseño adaptable verificado a 375 px de ancho (`responsive.spec.ts`).

---

## 2. Verificación de higiene, secretos y archivos excluidos

1. **Sin secretos en el código ni en la documentación**: ningún archivo rastreado o nuevo contiene claves de API reales, tokens ni contraseñas. El archivo local `.env` permanece ignorado por `.gitignore`.
2. **Sin artefactos espurios**: directorios temporales (`.venv`, `eval_reports`, `test-results`, `playwright-report`, `.next`) están cubiertos por `.gitignore`.
3. **Sin acciones destructivas no autorizadas**: no se ejecutó `git commit`, `git push` ni `DROP TABLE contextrag_events`.
