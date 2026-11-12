# ADR-004 — RLS fase 3: la aplicación como `docmind_app`, sin `FORCE`

**Estado:** aceptado e implementado el 2026-11-12. Probado con la suite completa como `docmind_app` en PostgreSQL 17 (job de CI `backend-postgres-rls`). **Activación en producción: pendiente de staging** (configuración, no código).

## Contexto

Las fases 1–2 (`0009_rls_policies`) dejaron las políticas activas pero sin efecto: la aplicación conecta como propietaria de las tablas y PostgreSQL no aplica RLS al propietario sin `FORCE`. El inventario de `RLS_DESIGN.md` listaba 8 cosas que se rompen cuando las políticas sí se aplican.

## Decisión

1. **La aplicación conecta como `docmind_app`**, que no es propietaria de las tablas, ni `SUPERUSER`, ni tiene `BYPASSRLS`. Para un rol que no es el propietario las políticas se aplican siempre, así que **`FORCE` no hace falta** y no se activa: con `FORCE`, las funciones `SECURITY DEFINER` (cuyo dueño es el propietario de las tablas) también quedarían filtradas. El riesgo que `FORCE` cubría, que la aplicación conecte por error como propietaria, lo cubre `/ready`: con `RLS_ENFORCED=true` responde 503 `RLS_ROLE_UNSAFE` si el rol es superusuario, tiene `BYPASSRLS` o es dueño de alguna tabla, y el contenedor no recibe tráfico.
2. **Lecturas antes de conocer la organización** con `SECURITY DEFINER` (migración `0010_rls_phase3`): `docmind_api_key_org(hash)`, `docmind_invitation_org(hash)`, `docmind_job_org(id)`. Cada una recibe una clave única, secreta o generada por el servidor, y devuelve **solo** el id de la organización; después se fija el contexto y la fila se lee con las políticas normales. Las tres usan `search_path` fijo.
3. **Contexto de usuario** `app.user_id` (fijado al autenticar con JWT) y dos políticas: un usuario ve sus propias filas de `organization_members` en todas sus organizaciones, y `organizations` es visible si es la del contexto o el usuario es miembro. Con eso funciona el selector de organizaciones sin abrir nada más.
4. **Tareas de mantenimiento entre organizaciones** (recuperación de trabajos, retención, reconciliación, outbox de vectores y de ContextRAG, reindexado) usan `SystemSessionLocal` → `DATABASE_SYSTEM_URL` (`docmind_system`, `BYPASSRLS`). Nunca en una ruta de petición. Sin `DATABASE_SYSTEM_URL` es la misma conexión de siempre, así que el comportamiento actual no cambia.
5. **Registro y creación de organización** fijan el contexto de la organización nueva antes de insertar.
6. `users` sigue sin política: es identidad global, sin columnas de tenant.

## Consecuencias

* Activar en staging/producción es configuración: ejecutar `docs/rls/roles.sql`, `alembic upgrade head` como propietario, `DATABASE_URL` → `docmind_app`, `DATABASE_SYSTEM_URL` → `docmind_system`, `RLS_ENFORCED=true`. Rollback: volver a la cadena de conexión anterior y `RLS_ENFORCED=false`; no hay migración de datos.
* Las migraciones siguen ejecutándose como propietario, no como `docmind_app`.
* Una consulta nueva que lea varias organizaciones en una ruta de petición devolverá vacío bajo RLS en vez de filtrar datos: el job `backend-postgres-rls` lo detecta.
* Las pruebas que preparan datos directamente lo hacen como `docmind_system` (`tests/conftest.py`); el código de la aplicación bajo prueba usa siempre `docmind_app`.
