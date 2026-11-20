# Cumplimiento Técnico — Ley N° 21.719 (Protección de Datos Personales, Chile)

**Entrada en vigencia:** 1 de diciembre de 2026.  
**Rol habitual de DocMind AI (SaaS B2B):** *Encargado de Tratamiento* (procesa documentos por cuenta de la organización cliente, que actúa como *Responsable de Datos*), y *Responsable* respecto de las cuentas de usuario y facturación propias.

---

## 1. Matriz de cumplimiento técnico implementado vs. pendiente legal/operativo

| Obligación Ley N° 21.719 | Qué ya implementa el código de DocMind AI | Qué debe cerrar el operador / equipo legal antes del 1 dic 2026 |
|---|---|---|
| **Licitud y finalidad (Arts. 3, 12)** | Aislamiento por organización (`organization_id`) y proyecto (`project_id`) con RBAC (`owner`, `admin`, `member`, `viewer`) y **PostgreSQL RLS**. | Publicar Política de Privacidad y firmar **Contrato de Encargo de Tratamiento (DPA)** con cada cliente B2B indicando finalidad exclusiva de procesamiento documental. |
| **Minimización y protección desde el diseño (Art. 14 quáter)** | Enmascaramiento determinista e irreversible de PII en [pii.py](../backend/app/ingestion/pii.py) y [entities.py](../backend/app/ingestion/entities.py) (incluyendo **RUT chileno** módulo 11, emails, teléfonos, IBAN, tarjetas, nombres y direcciones) antes de indexar en Qdrant. | Documentar configuración activa de `PII_DETECTORS` por cliente y definir si se habilita `spacy` NER además de `rules`. |
| **Seguridad y cifrado (Art. 14 quáter)** | Contraseñas con **Argon2id**, cookies `HttpOnly`/`Secure`, CSRF double-submit, conversaciones cifradas con **AES-GCM por organización** (`CONVERSATION_ENCRYPTION_KEY` + rotación `CONVERSATION_PREVIOUS_ENCRYPTION_KEY`), claves API hasheadas con SHA-256, defensa contra prompt injection (`<document>`, `<passage>`, `<memory>`), validación SSRF en webhooks. | Activar `ENVIRONMENT=production`, ClamAV (`ANTIVIRUS_PROVIDER=clamav`), S3 con cifrado KMS, TLS/dominio propio y backups cifrados con retención acotada. |
| **Transferencia internacional de datos (Arts. 27–28)** | Endpoint `GET /api/v1/privacy/policy-summary` alerta explícitamente (`third_party_ai_transfer_warning: true`) si se usa `LLM_PROVIDER=gemini` (Google AI Studio). Embeddings corren **100% locales** (`EMBEDDING_PROVIDER=local`, ONNX) sin salida a terceros. | **Crítico antes de clientes reales:** migrar de `LLM_PROVIDER=gemini` (AI Studio) a **`LLM_PROVIDER=vertex`** (Google Cloud Vertex AI con DPA / términos de privacidad empresariales y sin entrenamiento con datos del cliente) e informar subencargados (Railway, Google Cloud). |
| **Derechos ARCOP: Acceso y Portabilidad (Arts. 5 y 9)** | `GET /api/v1/privacy/me/export` devuelve JSON estructurado (`schema_version: "cl-21719-v1"`) con perfil, documentos subidos, historial `/ask`, conversaciones descifradas propias y auditoría (sin contraseñas ni secretos). | Habilitar canal visible de atención de solicitudes ARCOP (plazo legal de respuesta: **30 días corridos**) y procedimiento interno para solicitudes dirigidas por titulares a clientes B2B. |
| **Derechos ARCOP: Rectificación (Art. 6)** | Edición humana de campos extraídos (`PATCH /api/v1/extraction-fields/{id}`) con registro de motivo y verificación manual. | Documentar el flujo de rectificación en los Términos de Servicio. |
| **Derechos ARCOP: Supresión / Cancelación (Art. 7)** | `DELETE /api/v1/privacy/me/history` elimina inmediatamente historial `/ask` y conversaciones del usuario; `DELETE /api/v1/documents/{id}` elimina archivo y texto de inmediato y programa purga en Qdrant y BD (`RETENTION_DELETED_DAYS`). | Definir plazos máximos de retención contractuales (`RETENTION_DELETED_DAYS`, `RETENTION_DOCUMENT_DAYS`, `CONVERSATION_RETENTION_DAYS`) y políticas de expiración en backups. |
| **Trazabilidad y registro de operaciones (Art. 14 quinquies)** | Tabla `audit_logs` registra accesos, subidas, borrados, exportaciones de privacidad (`privacy.data_export`, `privacy.history_erased`) y eventos de autenticación sin almacenar contenido documental. | Mantener el **Registro de Actividades de Tratamiento (RAT)** y un protocolo de **Notificación de Brechas de Seguridad** a la Agencia de Protección de Datos Personales y a clientes afectados sin dilación indebida. |

---

## 2. Checklist inmediato previo al 1 de diciembre de 2026

1. **Migrar staging/producción a Vertex AI (`LLM_PROVIDER=vertex`)** antes de cargar documentos reales de clientes (dejar Google AI Studio solo para datos sintéticos).
2. **Activar `ENVIRONMENT=production`** con `RLS_ENFORCED=true`, `COOKIE_SECURE=true`, `SMTP_*` real, `ANTIVIRUS_PROVIDER=clamav` y dominio HTTPS propio.
3. **Publicar documentos legales**: Política de Privacidad adaptada a Ley N° 21.719, DPA B2B con cláusula de subencargados (Railway + Google Cloud) y correo de contacto de privacidad (ej. `privacidad@tudominio.cl`).
4. **Definir retención**: configurar `RETENTION_DELETED_DAYS`, `CONVERSATION_RETENTION_DAYS` y expiración de backups acorde al DPA.
