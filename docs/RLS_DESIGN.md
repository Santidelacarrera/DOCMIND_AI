# Diseño de Row-Level Security para DOCMIND_AI

**Estado: diseño y prototipo verificado. No implementado en el esquema ni en el código de la aplicación.**
Implementarlo cambia cómo se abren las sesiones, qué rol usa la aplicación y qué consultas pueden ejecutarse sin contexto, así que requiere tu decisión antes de tocar nada (ver sección 9).

## 1. Situación actual

El aislamiento entre organizaciones depende de:
* filtros `organization_id` escritos a mano en cada consulta,
* una comprobación de membresía (`membership()`) antes de cada operación,
* para claves API, un `ApiPrincipal` cuya organización se compara con la de la petición.

La auditoría de esta fase (`docs/AUDIT_FINAL_2026-10-26.md`, y pruebas `test_cross_org_negative.py`) no encontró consultas sin filtro en las rutas, pero **una sola consulta olvidada expone datos de todos los tenants**, y el código no tiene una red de seguridad en la base de datos.

Además, la conexión de la aplicación usa el mismo rol que las migraciones. En las pruebas es un superusuario (`postgres`). En producción **no debe serlo**: el prototipo demuestra que un superusuario salta RLS incluso con `FORCE`.

## 2. Objetivo

Que una consulta que olvide el filtro devuelva cero filas o falle, en vez de devolver datos de otro tenant. RLS es defensa en profundidad: no sustituye a la comprobación de membresía.

## 3. Mecanismo verificado (prototipo, PostgreSQL 16.15)

Reproducible con `docs/rls/prototype_checks.sql` sobre una base de pruebas (no sobre el esquema de DOCMIND):

| Comprobación | Resultado observado | Consecuencia para el diseño |
|---|---|---|
| Aislamiento con `SET LOCAL app.org_id` dentro de una transacción | 1 fila (solo la de la organización) | El mecanismo funciona |
| Sin contexto | 0 filas, sin error | Fallo cerrado si una consulta no fija organización |
| Tras el `COMMIT` de un `SET LOCAL` | 0 filas | `SET LOCAL` no sobrevive a la transacción: seguro con pool |
| `SET` de sesión, en la siguiente petición | **1 fila: se filtra** | **Prohibido** usar `SET` de sesión |
| `UPDATE` cruzado | 0 filas afectadas | `USING` también limita escrituras |
| Propietario no superusuario con `FORCE` y sin contexto | 0 filas | Propietario de tablas afectado con `FORCE` |
| Superusuario sin contexto | 2 filas | **El rol de la aplicación no puede ser superusuario** |

No se probó: `INSERT` cruzado (requiere `WITH CHECK` explícito) ni comportamiento con SQLAlchemy y su pool real. Ambos son parte de la implementación, no del prototipo.

## 4. Roles de PostgreSQL

| Rol | Atributos | Uso | Permisos |
|---|---|---|---|
| `docmind_owner` | `NOLOGIN`, propietario de las tablas | Migraciones (Alembic) | DDL; no lo usa la aplicación |
| `docmind_app` | `LOGIN`, sin `SUPERUSER`, sin `BYPASSRLS` | API y workers en su camino normal | `SELECT/INSERT/UPDATE/DELETE` sobre las tablas, sujeto a RLS |
| `docmind_system` | `LOGIN`, `BYPASSRLS` | Tareas que recorren todas las organizaciones: reconciliación, purga, entrega del outbox | Solo las tareas de sistema; credenciales separadas |

`FORCE ROW LEVEL SECURITY` en cada tabla tenant, para que incluso el propietario quede sujeto a la política cuando no es superusuario.

## 5. Contexto de organización en cada transacción

* **Dónde se fija:** un listener `after_begin` de SQLAlchemy ejecuta `SELECT set_config('app.org_id', :org, true)`. El `true` equivale a `SET LOCAL`: el valor dura solo la transacción.
* **Origen del valor:** un `ContextVar` que fija la dependencia de la petición **después** de comprobar la membresía. Nunca se toma del cliente sin comprobar.
* **Limpieza:** el `ContextVar` se restablece en un `finally`. Como el valor es local a la transacción, una conexión devuelta al pool no conserva organización.
* **Workers:** cada transacción de un trabajo Celery fija `job.organization_id` antes de leer. Un trabajo no puede heredar el contexto de otro.
* **Política (ejemplo):**
  ```sql
  CREATE POLICY tenant_isolation ON documents
    USING (organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid)
    WITH CHECK (organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid);
  ```
  `NULLIF` evita un error de conversión cuando no hay contexto: la política devuelve cero filas.

## 6. Consultas que se ejecutan antes de conocer la organización

Estas son las dificultades reales. Cada una necesita una decisión:

| Consulta | Dónde | Propuesta |
|---|---|---|
| Usuario por id (token JWT) | `security.py:100` | Política `USING (id = app.user_id)` con `app.user_id` fijado desde el token, o función `SECURITY DEFINER` que devuelva solo el usuario |
| Usuario por correo (login, registro, invitaciones) | `main.py:303, 331, 453` | Función `SECURITY DEFINER` de solo lectura para el login, o rol `docmind_system` limitado a esas consultas |
| Clave API por hash | `security.py:146` | Función `SECURITY DEFINER` que devuelva la organización de la clave; después se fija el contexto |
| Listado de organizaciones del usuario | `main.py:390` | Política sobre `organizations`: `id IN (SELECT organization_id FROM organization_members WHERE user_id = app.user_id)` |
| Comprobación de membresía | `security.py:130` | Funciona con el contexto de la organización pedida: si el usuario es miembro, la fila es visible |
| Carga de un trabajo por id | workers | Fijar el contexto con `job.organization_id`, que requiere leer el trabajo sin contexto: función `SECURITY DEFINER` o `docmind_system` solo para esa lectura |

**Riesgo principal:** la lectura del trabajo y la clave API ocurren antes de tener contexto. Si se resuelven con `docmind_system` en código de API, el rol con `BYPASSRLS` acaba en la ruta de peticiones, que es lo que se quiere evitar. La opción segura es `SECURITY DEFINER` con búsqueda por clave única y sin devolver nada más.

## 7. Migración y rollback

1. Crear roles y conceder permisos. Sin efecto sobre el comportamiento actual.
2. Activar RLS **sin** `FORCE` y con políticas permisivas durante un periodo de pruebas en staging, comparando resultados con el código actual.
3. Activar `FORCE` y conectar la aplicación como `docmind_app`.
4. Rollback: `ALTER TABLE ... DISABLE ROW LEVEL SECURITY` y volver a la cadena de conexión anterior. Ninguna migración de datos es necesaria: es un cambio de políticas y roles.

Compatibilidad: el ORM no cambia. Las migraciones siguen usando el propietario. Hay que añadir la migración de políticas como revisión nueva de Alembic, no modificar revisiones existentes.

## 7b. Pruebas requeridas antes de activarlo

* Para cada tabla tenant: `SELECT`, `UPDATE`, `DELETE` e `INSERT` con la organización de otro tenant deben devolver cero filas o fallar.
* Sin contexto: cero filas en todas las tablas.
* Pool: dos peticiones consecutivas en la misma conexión, la segunda sin contexto, no deben ver datos de la primera.
* Ruta de los workers: cada transacción fija su organización.
* Las pruebas de aislamiento existentes deben pasar sin cambios sobre `docmind_app`.

## 8. Riesgos residuales

* Un `SET` de sesión en código futuro reabre la fuga. Mitigación: prohibirlo en revisión de código y una prueba que lo detecte.
* El rol `docmind_system` es una cuenta muy potente. Mitigación: credenciales separadas, uso solo en tareas de sistema, auditoría.
* Rendimiento: cada consulta incluye la condición de tenant. Requiere índices sobre `organization_id` en todas las tablas tenant (la mayoría existen; no verificado para todas).

## 8b. Riesgos del contexto de sesión en el pool, superusuarios y BYPASSRLS

Lo que el prototipo **sí** demuestra (ver sección 3) y lo que **no**:

| Riesgo | Demostrado | Mitigación exigida antes de activar |
|---|---|---|
| `SET` de sesión se filtra a la siguiente petición del mismo proceso | Sí (1 fila filtrada) | Prohibir `SET` sin `LOCAL` en el código (prueba que busque `SET ` en el código de sesión); fijar el contexto solo con `set_config(..., true)` |
| `SET LOCAL` expira al hacer commit | Sí | Fijar el contexto **al inicio de cada transacción**, no una vez por sesión de SQLAlchemy (una sesión puede contener varias transacciones) |
| Transacción sin contexto dentro de una sesión con contexto previo | No probado con SQLAlchemy | Listener `after_begin` que fija o borra el contexto en cada transacción; prueba con dos transacciones seguidas en la misma sesión |
| Un superusuario salta RLS con o sin `FORCE` | Sí (2 filas) | La aplicación no puede conectarse como superusuario; prueba que compruebe `rolsuper = false` y `rolbypassrls = false` en la conexión de la aplicación |
| Un rol con `BYPASSRLS` en la ruta de peticiones | No probado | Prohibido por diseño; prueba de arranque que falle si la conexión de la API tiene `BYPASSRLS` |
| Propietario de tabla sin `FORCE` | Sí (con `FORCE`, un propietario no superusuario queda sujeto a la política) | `FORCE` en todas las tablas tenant |
| Consultas previas al contexto (login, clave API, listado de organizaciones) | No probado | Resolver con `SECURITY DEFINER` acotadas (sección 6); cada una con prueba propia |
| Contexto obsoleto en un worker que reutiliza conexión | No probado | Cada transacción de un trabajo fija `job.organization_id`; prueba con dos trabajos de organizaciones distintas consecutivos en el mismo proceso |
| Escrituras cruzadas (`INSERT` con otra organización) | No probado (el prototipo no tiene `WITH CHECK` en INSERT) | `WITH CHECK` explícito en cada política; prueba de `INSERT` cruzado que falle |

### Criterios de seguridad previos a la activación

Ninguno de estos criterios está cumplido hoy. La activación parcial (por ejemplo, RLS en algunas tablas o solo en lectura) queda prohibida: una tabla protegida y otra no crean rutas de fuga que cuesta ver.

1. Prototipo repetido sobre PostgreSQL 17 (el de CI) además de 16.
2. Cada tabla tenant con `ENABLE` y `FORCE`, con `USING` y `WITH CHECK`.
3. Pruebas negativas de aislamiento ejecutadas como `docmind_app`, no como `postgres`, sobre el esquema real, con el mismo conjunto que hoy pasa a nivel de aplicación.
4. Prueba de arranque que compruebe `rolsuper = false` y `rolbypassrls = false` para la conexión de la API y de los workers.
5. Revisión de todo `SET` y de todo uso de conexiones fuera del pool de SQLAlchemy.
6. Plan de rollback probado: desactivar RLS y volver a la cadena de conexión anterior en staging.

## 9. Decisiones que requieren al propietario

1. Aprobar el reparto de roles (sección 4) y la política de credenciales de `docmind_system`.
2. Elegir entre `SECURITY DEFINER` y `docmind_system` para las consultas previas al contexto (sección 6). Recomiendo `SECURITY DEFINER`.
3. Aprobar el calendario de activación en staging antes de producción.

Sin estas decisiones no se implementa. Ningún cambio de esquema destructivo está previsto.

---
Relacionado: [[empresa]]
