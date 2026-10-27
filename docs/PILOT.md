# Guía de evaluación para un piloto controlado

Esta guía sirve para que una organización decida si quiere un piloto. **Describe lo que está probado, lo que no y lo que el piloto debe verificar.** No es una declaración de cumplimiento normativo, de certificación ni de idoneidad para datos de producción.

## 1. Qué es

Un servicio multitenant que extrae campos de PDF y DOCX, registra la evidencia de cada valor (página y fragmento), exige revisión humana cuando hay dudas, y opcionalmente publica documentos enmascarados en ContextRAG_Engine para recuperación.

## 2. Qué está verificado (sandbox, datos sintéticos)

Cifras de `docs/AUDIT_FINAL_2026-10-26.md`. Todas se ejecutaron; ninguna es un resultado simulado.

| Área | Evidencia |
|---|---|
| Aislamiento entre organizaciones | 15 pruebas negativas: doble membresía, ids ajenos, claves de otra organización o revocadas o caducadas, tokens caducados o firmados con otra clave, miembros eliminados, logout, cola de revisión, registros de auditoría, cuerpos de error sin ids ajenos |
| Procesamiento | Reintentos, estados, recuperación tras reinicio simulado, cupo bajo concurrencia, broker caído en la subida, fallo de almacenamiento sin fila huérfana |
| Procedencia de valores | 34 pruebas; el valor debe aparecer como palabras completas; números cortos y años quedan en revisión |
| Sincronización con ContextRAG | 33 pruebas en proceso (PostgreSQL 16 y SQLite) y **un ensayo en vivo** con ContextRAG como proceso HTTP aparte: creación, borrado, caída y recuperación |
| Almacenamiento | 11 pruebas; modo simulación; ninguna eliminación ante fallo de listado |
| Migraciones | 0004–0007 suben y bajan en PostgreSQL 16 |
| Interfaz | 7 e2e de Playwright pasan en un stack local con el backend actual |
| Carga local | 100 documentos, concurrencia 16: 100 completados, 0 fallos, p95 de subida 0.17 s, ~218 documentos/min (sandbox, 4 vCPU, 16 GB; ver `docs/AUDIT_PRODUCTION_2026-10-27.md`) |
| Métricas | Etiquetas por plantilla de ruta; rutas no coincidentes agrupadas (`unmatched`) |
| Configuración | Cualquier entorno distinto de `development`/`test` se trata como producción |
| Proceso worker muerto con SIGKILL | Un proceso hijo muerto durante una entrega libera su evento tras el plazo y la entrega ocurre una sola vez (prueba con proceso real) |

## 3. Qué no está verificado

* **Precisión con un modelo real.** Las cifras de `docs/evaluation/baseline-report.md` son de reglas, no de un LLM.
* **Documentos reales.** Todo es sintético.
* **PostgreSQL 17** (el de CI) y **despliegue de ContextRAG**. El ensayo en vivo usa un proceso local, no un despliegue.
* **CI remoto:** su resultado no se ha consultado.
* **Caídas reales de un worker en mitad de una entrega:** probadas con un proceso hijo real y SIGKILL en el entorno de pruebas; no en un despliegue.
* **RLS:** no implementado. Diseño y prototipo en `docs/RLS_DESIGN.md`.
* **Celery beat:** no ejecutado en el sandbox.
* **Acceso a `/api/v1/usage`** para cualquier miembro (decisión pendiente, ver `docs/AUDIT_PRODUCTION_2026-10-27.md`).
* **Carga de producción**, copias de seguridad y restauración en un entorno real.
* **OCR en escaneos reales** de baja calidad; solo hay casos sintéticos.
* **Cumplimiento normativo.** No hay evaluación jurídica ni certificación.

## 4. Criterios para aceptar un piloto

Antes de subir datos reales, el piloto debe cumplir todo esto:

1. Datos **autorizados y anonimizados** para la evaluación inicial; sin datos personales de terceros sin base legal.
2. Un **contrato de tratamiento de datos (DPA)** con quien opere el servicio, y con el proveedor de LLM si se usa uno externo.
3. Configuración de producción validada: `ENVIRONMENT=staging`, HTTPS, almacenamiento privado (S3 con cifrado), antivirus activo, SMTP, claves de servicio. La aplicación se niega a arrancar si falta algo de esto.
4. Un responsable que revise cada extracción marcada para revisión antes de usarla.
5. Un plan de retención y eliminación acordado (ver `docs/DATA_RETENTION.md`).

## 5. Evaluar la calidad de extracción

* Usar el procedimiento de `docs/EVALUATION.md` con un conjunto **etiquetado por la propia organización**.
* Medir por tipo de documento y por campo. No usar el porcentaje de confianza como probabilidad: no está calibrado.
* Medir la **revisión**: cuántos campos llegan a revisión y cuántos errores se escapan sin revisión (en el baseline, ninguno; en un LLM real, desconocido).

## 6. Observabilidad y operación

* Logs JSON sin contenido de documentos ni mensajes de excepción (solo tipo y origen).
* Métricas Prometheus en `/metrics` de la API (el worker aún no se expone; ver pendientes).
* `GET /api/v1/contextrag/events` para el estado de la sincronización, si está activa.
* Reconciliación diaria de almacenamiento en modo **solo informe**; el borrado requiere `STORAGE_RECONCILE_DELETE=true` y tiene tope de seguridad.

## 7. Copia de seguridad y recuperación

**No probado.** Requisitos mínimos para el piloto:

* Copia de PostgreSQL con prueba de restauración en un entorno separado.
* Versionado y cifrado del bucket de documentos; la base de datos y el bucket deben copiarse en el mismo punto temporal, o la reconciliación detectará huérfanos o filas sin objeto.
* Probar la restauración antes de confiar en ella.

## 8. Procedimiento de incidentes

1. **Sospecha de fuga entre organizaciones:** desactivar la clave de API afectada y la sincronización (`CONTEXTRAG_SYNC_ENABLED=false`); revisar `audit_logs` y `GET /api/v1/contextrag/events`; no reenviar datos hasta entender la causa. Notificar según el DPA.
2. **Documento que no debía indexarse:** eliminarlo en DOCMIND (el borrado encola la retirada en ContextRAG) y verificar con `GET /api/v1/contextrag/events` que la retirada está `done`.
3. **Claves comprometidas:** revocar en DOCMIND (`DELETE /api/v1/api-keys/{id}`), rotar la clave del tenant en ContextRAG.
4. **Vulnerabilidad en el código:** seguir `SECURITY.md` (reporte privado).

## 9. Limitaciones conocidas

Ver `docs/AUDIT_PHASE2_2026-10-26.md`, sección 8, y `docs/CONTEXTRAG_INTEGRATION.md`.

---
Relacionado: [[empresa]]
