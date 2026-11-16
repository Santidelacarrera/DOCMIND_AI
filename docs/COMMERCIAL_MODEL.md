# DOCMIND AI — Modelo Comercial SaaS y Preparación Empresarial

## 1. Propuesta de Valor

**DOCMIND AI** es una plataforma SaaS B2B multi-tenant de inteligencia documental diseñada para organizaciones que procesan documentos sensibles (contratos, facturas, DTE, pólizas, informes y planillas) y requieren:

- **Cero alucinaciones sin respaldo:** cada dato extraído y cada respuesta de RAG cita exactamente el documento, página y fragmento literal, y se abstiene cuando no existe evidencia suficiente.
- **Aislamiento multi-tenant por capas:** separación en API, proyectos/expedientes, Qdrant, almacenamiento de archivos, Redis y PostgreSQL Row-Level Security (RLS).
- **Control humano y trazabilidad:** umbral de confianza, confianza OCR por palabra, procedencia por campo y flujo obligatorio de revisión y aprobación antes de exportar.
- **Procesamiento híbrido y local-first:** embeddings locales ONNX (`fastembed`) y modo extractivo determinista sin costo por consulta, más soporte opcional de Vertex AI / OpenAI / Gemini con verificación sentencia a sentencia.

## 2. Casos de Uso Objetivo

1. **Estudios jurídicos y áreas legales:** comparación de contratos, detección de cláusulas contradictorias, control de acceso por expediente y preguntas trazables.
2. **Finanzas y contabilidad (incl. DTE Chile):** extracción estructurada de facturas/boletas, validación módulo-11 de RUT chileno, exportación a JSON/CSV/XLSX protegida contra inyección de fórmulas.
3. **Seguros, cumplimiento y auditoría:** revisión de pólizas, extracción determinista de fechas, montos e identificadores, retención configurable y bitácora de auditoría.

## 3. Planes Comerciales Configurables (Referencia Inicial)

Los límites y precios de referencia están definidos en [`backend/app/plans.py`](../backend/app/plans.py) y son personalizables sin cambios de código mediante `PLANS_CONFIG_JSON` o `custom_limits` por suscripción. *Los precios mostrados son valores de referencia configurables, no tarifas definitivas ni cobros activos.*

| Plan | Precio ref. mensual | Usuarios | Documentos activos | Almacenamiento | Archivo máx. | Páginas / mes | Consultas / mes | Tokens IA / mes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **Free / Trial** | \$0 (ref.) | 5 | 50 | 100 MB | 10 MB | 20 | 200 | 100.000 |
| **Starter** | \$49 USD (ref.) | 10 | 500 | 1 GB | 25 MB | 500 | 1.000 | 1.000.000 |
| **Professional** | \$149 USD (ref.) | 30 | 5.000 | 10 GB | 50 MB | 5.000 | 10.000 | 10.000.000 |
| **Business** | \$399 USD (ref.) | 100 | 25.000 | 50 GB | 100 MB | 25.000 | 50.000 | 50.000.000 |
| **Enterprise** | Contrato | Custom | Custom | Custom | 100 MB+ | Custom | Custom | Custom |

### Medición, Alertas y Pasarela de Pago
- **Medición por periodo (`YYYY-MM`):** páginas reservadas/procesadas, documentos activos, bytes almacenados, consultas `/ask`, tokens IA y costo estimado.
- **Alertas y bloqueos:** aviso automático al superar el umbral configurable (`alert_threshold_pct`, 80% por defecto) y bloqueo controlado `402 QUOTA_EXCEEDED` al alcanzar el 100%.
- **Arquitectura lista para pagos:** `OrganizationSubscription` almacena únicamente identificadores externos (`payment_provider`, `external_customer_id`, `external_subscription_id`) y nunca datos bancarios ni tarjetas. Los cobros reales están desactivados hasta configurar un proveedor autorizado.

## 4. Diferenciación de Madurez por Capacidad

### Disponible en código y probado automáticamente (local/CI)
- Ingesta segura multi-formato: PDF (digital, escaneado y mixto), DOCX, TXT, MD, CSV, XLSX, PPTX e imágenes (PNG, JPG, WEBP, TIFF).
- Enmascarado PII antes de indexar (incluye RUT chileno) + sanitizado de contenido activo y anti-exfiltración en RAG.
- Recuperación híbrida (Qdrant + BM25 + RRF), reranking ONNX opcional, verificación por afirmación, conversaciones cifradas AES-GCM, citas verificables y evaluación RAG reproducible.
- Inteligencia documental: plantillas de esquemas, clasificación y etiquetado, extracción de entidades, resúmenes con citas, comparación de documentos y exportación JSON/CSV/XLSX.
- Multi-tenancy (roles, proyectos, API keys con scopes, RLS Fases 1–3) y modelo de planes/cuotas SaaS.

### Experimental / Pendiente de validación con entorno o credenciales externas
- Extracción y generación con LLM real de pago (Vertex AI / OpenAI) sobre documentos reales de clientes (`OWNER_SETUP.md`).
- Calibración de `RETRIEVAL_MODE=hybrid` y `RERANK_MODEL` sobre corpus reales anonimizados.
- Despliegue en staging con TLS, S3/KMS, ClamAV daemon, SMTP real, RLS activado (`RLS_ENFORCED=true`) y Alertmanager conectado.

### Trabajo Futuro (No incluido hoy)
- Integración en producción con pasarela de pagos (Stripe / MercadoPago webhook handlers).
- Verificador NLI neuronal (`DebertaNLI`) si las mediciones con LLM real demuestran rechazo excesivo de paráfrasis.
- SSO empresarial (SAML / OIDC).

## 5. Requisitos de Infraestructura y Costos Operacionales Estimados

| Componente | Perfil Piloto / PyME (Docker Compose) | Perfil Producción Media (Managed) | Costo mensual estimado (ref.) |
|---|---|---|---|
| **Compute (API + Worker + Web + Scheduler)** | 1 VM 4 vCPU, 8 GB RAM | 2 nodos 4 vCPU, 16 GB RAM | \$40 – \$180 USD/mes |
| **PostgreSQL 17** | Contenedor local c/ backup | Managed Postgres (2 vCPU, 4 GB, backups) | \$25 – \$90 USD/mes |
| **Redis 7** | Contenedor local | Managed Redis (TLS, 1 GB) | \$15 – \$40 USD/mes |
| **Qdrant** | Contenedor local (v1.20) | Qdrant Cloud / nodo dedicado (4 GB RAM) | \$25 – \$95 USD/mes |
| **Almacenamiento S3 + KMS** | Volumen local (solo dev) | Bucket privado S3/R2/GCS + KMS | \$5 – \$25 USD/mes |
| **LLM (Vertex AI / OpenAI)** | Modo extractivo local (\$0) | Variable según volumen de extracción y `/ask` generativo | \$0 – \$150+ USD/mes |

## 6. Requisitos de Soporte y Operación

- **Monitoreo:** `/health`, `/ready`, `/metrics` (Prometheus + 12 reglas en `deploy/prometheus/alerts.yml`), logs JSON con `X-Request-Id` y OpenTelemetry opcional.
- **Copias y recuperación:** backup diario `pg_dump` verificado (`deploy/backup/pg_backup.sh`), reconstrucción de índices Qdrant sin costo LLM vía `scripts/reindex_vectors.py`, y reconciliación de almacenamiento vía Celery beat.
- **Seguridad operativa:** rotación periódica de `JWT_SECRET` y claves API, custodia separada de `CONVERSATION_ENCRYPTION_KEY`, y revisión jurídica pendiente sobre texto final de licencia comercial (`docs/LICENSING.md`).
