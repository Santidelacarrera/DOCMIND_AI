# Informe de Validación de Row-Level Security (RLS) y Aislamiento Multi-Tenant

> **Estado:** Políticas RLS en PostgreSQL 17, funciones `SECURITY DEFINER`, separación de roles `docmind_app` (`NOBYPASSRLS`, `NOSUPERUSER`) y `docmind_system`, y pruebas negativas entre organizaciones y proyectos **COMPLETADAS Y PROBADAS EN POSTGRESQL 17 REAL**.

---

## 1. Cobertura de tablas protegidas por RLS (`backend/app/rls.py`)

Todas las tablas con datos de organizaciones están protegidas por la política `docmind_tenant_isolation`:

- **Tablas con `organization_id` directo (`TENANT_TABLES` + `TENANT_TABLES_0011` + `TENANT_TABLES_0012`) — 18 tablas**:
  `api_keys`, `audit_logs`, `documents`, `extraction_runs`, `extraction_schemas`, `invitations`, `organization_members`, `processing_jobs`, `projects`, `usage_records`, `vector_deletions`, `webhooks`, `conversations`, `conversation_messages`, `organization_subscriptions`, `query_history`, `billing_transactions`, `billing_webhook_events` (`contextrag_events` retirada en Fase 3, migración `0013_drop_contextrag_events`).
- **Tablas hijas filtradas por existencia del padre visible (`CHILD_TABLES`) — 7 tablas**:
  `document_pages`, `document_versions`, `extraction_fields`, `extraction_schema_versions`, `processing_steps`, `project_members`, `webhook_deliveries`.
- **Tablas de identidad/raíz (`organizations`)**:
  Política basada en membresía del usuario (`app.user_id`) o contexto activo (`app.org_id`).

---

## 2. Invariantes de aislamiento comprobados

1. **Sin contexto (`app.org_id` vacío)**: toda consulta `SELECT` sobre cualquiera de las 26 tablas protegidas devuelve `0` filas.
2. **Con contexto de Organización A**: ninguna consulta `SELECT`, `UPDATE` o `DELETE` ejecutada como `docmind_app` puede leer, modificar ni borrar filas de la Organización B (`rowcount == 0`).
3. **Inserción cruzada (`WITH CHECK`)**: cualquier intento de insertar una fila con `organization_id` distinto al contexto activo falla con error de violación de política RLS (`InsufficientPrivilege`).
4. **Conexiones reutilizadas del pool**: `set_config('app.org_id', ..., true)` y `set_config('app.user_id', ..., true)` son locales a la transacción; al cerrarse la transacción, la conexión no arrastra el contexto al siguiente request.
5. **Workers asíncronos y tareas programadas**:
   - `worker.process_document` resuelve la organización mediante la función `SECURITY DEFINER` `docmind_job_org(uuid)` y fija `rls.CURRENT_ORG` antes de reclamar y procesar el trabajo.
   - Las tareas globales de mantenimiento (`recover_stuck_jobs`, `purge_expired_data`, `reconcile_storage`) utilizan `SystemSessionLocal` (`DATABASE_SYSTEM_URL`, rol `docmind_system`).
6. **Guardia en `/ready`**: cuando `RLS_ENFORCED=true`, el endpoint `/ready` comprueba `rls.role_problems(db)` y responde `503 RLS_ROLE_UNSAFE` si la aplicación estuviera conectada como superusuario, con `BYPASSRLS` o como propietario de las tablas.

---

## 3. Evidencia de pruebas ejecutadas

- **`tests/test_rls_policies.py` sobre PostgreSQL 17 real**: **59 passed in 107.25s** (0 fallos, 0 omitidos). Todas las tablas protegidas (incluidas `organization_subscriptions`, `query_history`, `billing_transactions` y `billing_webhook_events`) fueron verificadas con el rol sin privilegios `docmind_rls_probe` (`NOSUPERUSER NOBYPASSRLS`).
- **`TEST_RLS_ENFORCED=1` sobre PostgreSQL 17 real**: ejecución de extremo a extremo conectando la aplicación como `docmind_app` y el mantenimiento como `docmind_system`.
- **`tests/test_tenant_isolation.py`**: recorrido automático de las **82 rutas** registradas en FastAPI, verificando que un segundo tenant recibe `403` o `404` en cada endpoint y que no existe ninguna ruta sin comprobación de autorización.
- **`tests/test_document_authorization.py` y `tests/test_project_authorization.py`**: aislamiento entre proyectos dentro de una misma organización para usuarios y claves API.
