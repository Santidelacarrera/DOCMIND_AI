# Contrato de Encargo de Tratamiento de Datos Personales (DPA) — Borrador Base

> **Nota legal:** Este documento es un borrador técnico-operativo estructurado conforme a la **Ley N° 21.719** (que modifica la Ley N° 19.628 sobre Protección de la Vida Privada, Chile, vigencia **1 de diciembre de 2026**). Debe ser revisado y completado por asesoría legal antes de su firma comercial.

---

## 1. Partes y Roles

- **Responsable de Datos (Cliente):** `[RAZÓN SOCIAL DEL CLIENTE]`, RUT `[XX.XXX.XXX-X]`, domicilio en `[DIRECCIÓN]`, en adelante el **Responsable**.
- **Encargado de Tratamiento (Proveedor):** `[RAZÓN SOCIAL OPERADORA DE DOCMIND AI]`, RUT `[XX.XXX.XXX-X]`, domicilio en `[DIRECCIÓN]`, en adelante el **Encargado**.

Respecto de los documentos, metadatos y consultas cargados en el workspace de DocMind AI, el Cliente actúa como **Responsable** y el Proveedor actúa exclusivamente como **Encargado de Tratamiento** siguiendo las instrucciones documentadas del Responsable (Art. 15 bis, Ley N° 21.719).

---

## 2. Objeto, Finalidad y Naturaleza del Tratamiento

- **Finalidad exclusiva:** Ingesta, extracción estructurada de campos, indexación semántica, búsqueda documental con citas verificables y auditoría interna del workspace contratado por el Responsable.
- **Prohibición de uso secundario:** El Encargado **no utilizará** los documentos ni datos personales del Responsable para entrenar modelos propios ni de terceros, ni para fines publicitarios o comerciales distintos de la prestación del servicio contratado.
- **Categorías de datos tratados:** Según los documentos cargados por el Responsable (habitualmente datos identificatorios como nombre, RUT, correo electrónico, teléfono, dirección, datos contractuales, laborales o financieros contenidos en PDFs/DOCX).

---

## 3. Obligaciones del Encargado (Ley N° 21.719)

El Encargado se obliga a:

1. **Tratamiento bajo instrucciones:** Tratar los datos personales únicamente conforme a los Términos de Servicio y configuraciones del workspace del Responsable.
2. **Deber de confidencialidad:** Garantizar que el personal autorizado con acceso técnico esté sujeto a obligación legal o contractual de secreto y confidencialidad.
3. **Seguridad y privacidad desde el diseño (Art. 14 quáter):** Mantener las medidas técnicas y organizativas descritas en el **Anexo A**.
4. **Asistencia en derechos ARCOP (Arts. 5–9):** Proveer herramientas técnicas de autoservicio (`GET /api/v1/privacy/me/export`, `DELETE /api/v1/privacy/me/history`, `DELETE /api/v1/documents/{id}` y edición humana de campos) y asistir al Responsable para responder solicitudes de titulares dentro del plazo legal de **30 días corridos**.
5. **Notificación de brechas de seguridad (Art. 14 quinquies):** Notificar al Responsable sin dilación indebida (y dentro de un máximo de **24 horas** desde su confirmación) cualquier incidente de seguridad que afecte datos personales bajo su custodia, entregando la información técnica disponible para que el Responsable cumpla su deber ante la Agencia de Protección de Datos Personales.
6. **Devolución o supresión al término del contrato:** Al finalizar la suscripción, poner a disposición del Responsable la exportación de datos y eliminar de forma irreversible los documentos, índices vectoriales y copias activas conforme a los plazos de retención pactados.

---

## 4. Subencargados Autorizados y Transferencia Internacional (Arts. 27–28)

El Responsable autoriza el uso de los siguientes subencargados técnicos en entornos de producción:

| Subencargado | Función | Ubicación / Transferencia | Salvaguarda contractual |
|---|---|---|---|
| **Railway Corporation** | Infraestructura de cómputo, PostgreSQL, Redis, Qdrant y almacenamiento S3 | EE.UU. / UE (según región elegida) | DPA + Cláusulas Contractuales Tipo |
| **Google Cloud (Vertex AI)** | Inferencia LLM empresarial (sin retención ni entrenamiento con datos del cliente) | EE.UU. / Región GCP seleccionada | Google Cloud Data Processing Addendum (CDPA) + Zero Data Retention |
| **[Proveedor SMTP, ej. Resend/Postmark/SES]** | Envío de correos transaccionales (verificación, invitaciones) | EE.UU. / UE | DPA del proveedor |

> **Regla operativa:** Los embeddings vectoriales se ejecutan **100% de forma local** dentro de la infraestructura de DocMind AI (`EMBEDDING_PROVIDER=local`, ONNX), sin transferencia de texto a terceros para indexación semántica. El modo `LLM_PROVIDER=gemini` (Google AI Studio) queda reservado exclusivamente para entornos de desarrollo/pruebas sintéticas y **no debe habilitarse** en workspaces con datos personales confidenciales de clientes reales.

El Encargado notificará con al menos **30 días de anticipación** cualquier incorporación o reemplazo de subencargados críticos.

---

## 5. Anexo A — Medidas Técnicas y Organizativas de Seguridad Implementadas

1. **Aislamiento multi-tenant:** Separación estricta por `organization_id` y `project_id`, control de acceso por roles (`owner`, `admin`, `member`, `viewer`) y soporte de **PostgreSQL Row-Level Security (RLS)**.
2. **Cifrado en tránsito y en reposo:** TLS/HTTPS obligatorio en producción; contraseñas hasheadas con **Argon2id**; conversaciones cifradas por organización con **AES-GCM** (`CONVERSATION_ENCRYPTION_KEY` con soporte de rotación); claves API almacenadas únicamente como hash SHA-256.
3. **Minimización de PII antes de indexación:** Enmascaramiento determinista e irreversible de datos personales antes de indexar fragmentos en Qdrant (incluyendo **RUT chileno** validado por módulo 11, emails, teléfonos, IBAN y tarjetas).
4. **Seguridad de aplicación y defensa IA:** Cookies `HttpOnly`/`Secure`, protección CSRF *double-submit*, escaneo antivirus de archivos subidos (`ClamAV`), validación SSRF en webhooks, neutralización de delimitadores contra *prompt injection* (`<document>`, `<passage>`, `<memory>`).
5. **Auditoría:** Registro de accesos, cargas, eliminaciones, autenticación y solicitudes ARCOP en `audit_logs` sin almacenar contenido documental en claro.

---

## 6. Contacto de Privacidad

- **Canal ARCOP y Privacidad del Encargado:** `[privacidad@tudominio.cl]`
- **Canal de Seguridad / Incidentes:** `[seguridad@tudominio.cl]`
