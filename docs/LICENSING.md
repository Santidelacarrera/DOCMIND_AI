# Licencia y estrategia de distribución

**Estado (2026-10-31): decidido.** El propietario delegó la elección; se adopta la licencia **propietaria, todos los derechos reservados**, con titular Santiago de la Carrera (`LICENSE`). Motivo: el objetivo es vender una plataforma documental empresarial, MIT permitiría a cualquiera revenderla y AGPL complica la licencia comercial. Las licencias para clientes se otorgan por contrato aparte; las aportaciones externas ceden sus derechos (cláusula 3). **Pendiente, no es asesoramiento jurídico:** que un abogado revise el texto y el titular definitivo (persona o empresa) antes de vender. Mantener el repositorio privado. El análisis siguiente es el que motivó la decisión.

## Lo que dice el repositorio hoy

* `LICENSE`: MIT, «Copyright (c) 2026 DocMind AI contributors». El titular no es una persona ni una entidad jurídica concreta.
* El README dice «Maintained by **Nexus Security & AI Lab**» y «Released under the MIT License». Eso mezcla autoría, mantenimiento y licencia.
* Historial (59 commits): 20 de la cuenta «Claude» (asistente de código), 13 de `santidlc1`, 13 de `Santiago de la Carrera López`, 13 de dependabot.

## Riesgos que hay que resolver antes de decidir

1. **Titularidad.** No está claro quién es titular del código: la persona, Nexus Security & AI Lab, o una empresa. El copyright «DocMind AI contributors» no identifica a nadie.
2. **Código generado por IA.** Una parte del historial fue escrita con un asistente. En muchos ordenamientos el contenido generado sin autoría humana suficiente puede no ser protegible por derecho de autor; eso afecta a la exclusividad, no a la licencia. Requiere criterio jurídico.
3. **Dependencias.** Todas son permisivas (MIT, Apache-2.0, BSD, ISC) salvo componentes de copyleft débil:
   * LGPL-3.0: `psycopg`, `clamd` (Python); `@img/sharp-libvips-*` y `@img/sharp-wasm32` (npm, usados por Next para imágenes).
   * MPL-2.0: `certifi`, `pathspec`, `tqdm` (Python); `axe-core` (npm, herramienta de pruebas de accesibilidad, no se distribuye en producción).
   * CC-BY-4.0: `caniuse-lite` (datos de compatibilidad de navegadores; atribución requerida si se redistribuye).
   * No hay GPL ni AGPL en el árbol instalado. Enlazar LGPL dinámicamente no obliga a publicar el código propio; distribuir binarios de `sharp` sí exige cumplir LGPL.
4. **ContextRAG_Engine** es otro repositorio con su propia licencia. Esta integración **no fusiona** los dos repositorios.

## Opciones

| Opción | Ventajas | Inconvenientes |
|---|---|---|
| **Privado y propietario** (todos los derechos reservados) | Control total, sin obligaciones de licencia abierta, adecuado para vender a clientes | Sin revisión externa; hay que cumplir LGPL/MPL al distribuir; hay que decidir el texto de licencia comercial |
| **Abierto con MIT** (estado actual) | Adopción, contribuciones sin contrato | Cualquiera puede usar, modificar y revender el código, incluido un competidor; no protege la marca ni el servicio |
| **Abierto con copyleft (AGPL-3.0)** | Quien ofrece el servicio en red debe publicar sus cambios | Muchas empresas lo evitan; complica un modelo de licencia comercial doble |

## Recomendación

1. **Mientras no se decida la titularidad, mantener el repositorio privado.** Publicarlo con MIT concede derechos a terceros de forma irreversible para las versiones publicadas.
2. Si el objetivo es **vender una plataforma documental empresarial**, la opción más coherente es **propietaria** (texto de licencia comercial propio, revisado por abogado), con un acuerdo de contribución para cualquier aportación externa.
3. Si el objetivo es **captar comunidad**, MIT o Apache-2.0 para el núcleo, y una oferta comercial separada para la integración y el soporte.
4. Antes de cualquier decisión: registrar quién es titular de cada parte, documentar qué código se generó con IA, y revisar las obligaciones LGPL/MPL si se distribuyen binarios.

## Implicaciones prácticas

* **Repositorio privado:** los clientes reciben el software con el contrato que se les firme; no hay obligaciones de publicar el código.
* **Repositorio público con MIT:** cualquier cliente puede usar el código sin pagar; el valor comercial debe venir del servicio, el soporte o la marca.
* **Clientes y redistribuidores:** con MIT o Apache-2.0 pueden redistribuir modificaciones sin publicarlas; con AGPL, un proveedor que ofrezca el servicio en red debe publicar sus cambios; con licencia propietaria, cada redistribución debe pactarse.

Esto no es asesoramiento jurídico. Conviene revisarlo con un abogado antes de publicar o vender.

---

## Borradores Base para Comercialización SaaS B2B (Pendientes de Revisión Jurídica)

> **IMPORTANTE:** Los siguientes apartados constituyen los **borradores técnicos y operativos base** alineados con el funcionamiento real de DOCMIND AI. **No están jurídicamente aprobados** y requieren validación por abogado habilitado (Chile / jurisdicción de contratación) antes de su publicación o firma con clientes.

### 1. Términos de Servicio (ToS — Borrador Operativo)
- **Objeto del servicio**: licencia de acceso SaaS no exclusiva, intransferible y acotada al plan suscrito (`FREE`, `STARTER`, `PROFESSIONAL`, `BUSINESS`, `ENTERPRISE`) para procesamiento, extracción estructurada, indexación e inteligencia documental.
- **Titularidad de los documentos del cliente**: el Cliente conserva el 100 % de la propiedad intelectual y titularidad sobre los documentos subidos, sus metadatos y las exportaciones generadas. DOCMIND AI actúa únicamente como encargado del tratamiento (Data Processor).
- **No entrenamiento con datos del cliente**: los documentos, fragmentos y consultas de una organización **nunca** se utilizan para entrenar modelos para otros clientes ni se comparten entre organizaciones.
- **Uso de IA y supervisión humana**: las extracciones con confianza inferior al umbral calibrado se enrutan a la cola de revisión humana; las respuestas de `/ask` citan explícitamente documento y página o se abstienen cuando no hay evidencia suficiente. El Cliente es responsable de la validación final en decisiones jurídicas, financieras o regulatorias.
- **Suspensión por morosidad y reactivación**: tras 3 intentos fallidos de cobro (`status=unpaid`), la cuenta degrada automáticamente sus límites efectivos al nivel `FREE` sin destruir los documentos existentes; al regularizar el pago (`payment.succeeded`), se reactivan los límites contratados.

### 2. Política de Privacidad y Acuerdo de Tratamiento de Datos (DPA — Ley 19.628 / Ley 21.719 Chile y GDPR)
- **Minimización y enmascarado de PII**: antes de indexar fragmentos en Qdrant o enviar pasajes al proveedor LLM externo, el pipeline enmascara identificadores sensibles (RUT chileno validado en módulo 11, correos electrónicos, teléfonos, números de tarjeta) mediante tokens deterministas por tenant (`app/ingestion/pii.py`).
- **Aislamiento criptográfico y lógico**:
  - PostgreSQL aplica Row-Level Security (RLS) en las 26 tablas de negocio.
  - Los turnos de conversación se cifran con AES-256-GCM y clave derivada por organización mediante HKDF-SHA256 (`CONVERSATION_ENCRYPTION_KEY`).
  - Los objetos en S3 se almacenan bajo prefijo por organización y cifrado SSE-KMS (`S3_KMS_KEY_ID`).
- **Subencargados (Sub-processors) según configuración del despliegue**:
  - Infraestructura de base de datos y almacenamiento: AWS S3/KMS, Google Cloud o proveedor cloud designado en el contrato.
  - Proveedor LLM (cuando `LLM_PROVIDER=vertex` u `openai`): Google Cloud Vertex AI (en la región acordada, ej. `southamerica-east1`) u OpenAI API (sin retención para entrenamiento empresarial).

### 3. Política de Retención y Eliminación de Datos (`docs/DATA_RETENTION.md`)
- **Eliminación inmediata a petición del usuario (`DELETE /api/v1/documents/{id}`)**: borra en el acto los archivos del almacenamiento de objetos, vacía las páginas extraídas (`scrub_derived_text`), redacta el historial de consultas/conversaciones que citaban el documento y encola en la misma transacción la eliminación de vectores en Qdrant (`vector_deletions`).
- **Purga definitiva (`RETENTION_DELETED_DAYS`, 30 días por defecto)**: la tarea programada `purge_expired_data` elimina físicamente los registros residuales de documentos borrados una vez cumplido el plazo de retención.
- **Expiración automática opcional (`RETENTION_DOCUMENT_DAYS`)** y **retención de conversaciones (`CONVERSATION_RETENTION_DAYS`, 90 días por defecto)**.

### 4. Decisiones que requieren aprobación del Propietario y Abogado
1. Razón social definitiva emisora de contratos y facturas electrónicas (DTE en SII Chile: Persona natural vs. SpA / Ltda. bajo marca **Nexus Security & AI Lab**).
2. SLA comprometido en contratos `BUSINESS` / `ENTERPRISE` (disponibilidad mensual, ventana de soporte, penalidades y límite de responsabilidad civil).
3. Jurisdicción y arbitraje aplicables en los contratos B2B.

---
Relacionado: [[empresa]]
