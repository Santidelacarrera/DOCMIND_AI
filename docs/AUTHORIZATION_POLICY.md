# Política de acceso a los totales de uso

**Estado: decidida el 2026-10-30 (opción A) e implementada.** `GET /api/v1/usage` exige rol `admin` para personas; las claves API siguen rigiéndose por el scope `usage:read`. Las secciones 1–3 describen la política anterior.

## 1. Qué puede ver hoy un viewer

`GET /api/v1/usage?organization_id=…` exige ser miembro con cualquier rol (incluido `VIEWER`), porque `membership(..., scope="usage:read")` solo aplica el `scope` a claves API. La respuesta contiene exactamente:

| Campo | Contenido | Sensibilidad |
|---|---|---|
| `plan` | Plan de la organización (`FREE`, …) | Comercial |
| `metrics.documents_processed` | Número total de documentos procesados | Volumen de negocio |
| `metrics.pages_processed` | Número total de páginas procesadas | Volumen y coste |
| `metrics.pages_reserved` | Páginas reservadas por subidas en curso o recién hechas | Mecanismo interno de cupo |
| `free_pages_limit` | Límite gratuito **global** del sistema (`FREE_PAGES_PER_MONTH`), no de la organización | Configuración del operador |

No contiene identificadores de documento, nombres, correos ni importes. Lo verifica `tests/test_usage_visibility.py` (un viewer recibe 200 con `plan` y `metrics`).

## 2. Riesgos de la política actual

* Un viewer conoce el volumen total de documentos y páginas, que puede servir de inteligencia comercial dentro de una organización con roles de solo lectura para terceros (por ejemplo, un auditor externo con rol `VIEWER`).
* `pages_reserved` revela el estado del cupo en curso, algo que no necesita ver un viewer.
* El `scope` aparenta una restricción que para personas no existe: quien lea `usage:read` podría creer que restringe a quien tiene el rol. Ver la matriz de `docs/AUTHORIZATION_MATRIX.md`.

## 3. Impacto de cambiar la política

Cambiar `scope="usage:read"` por `Role.admin` afecta a cualquier integración o panel que hoy lea el uso con un viewer. No hay en el repositorio ningún cliente de la API que lo haga.

## 4. Alternativa propuesta (no aplicada)

**Opción A (recomendada):** `GET /api/v1/usage` exige `Role.admin`, igual que `audit-logs`, `webhooks`, `api-keys` y `members`. Los viewers dejan de verlo.

**Opción B:** mantener el acceso de viewers, pero devolver solo `plan`. Quitar `pages_reserved` y los totales.

**Opción C:** mantener la política actual y documentarla como decisión aceptada.

## 5. Pruebas que habría que añadir si se aprueba la opción A

* Un viewer recibe 403; un member recibe 403; un admin recibe 200.
* Una API key con `usage:read` sigue sin acceder si su rol no es admin (las claves se rigen por el scope y por el rol de su creador; hay que decidir cuál aplica).
* Un admin de otra organización recibe 404 o 403.
* La respuesta no contiene `pages_reserved` (si se aprueba la opción B).

La prueba `tests/test_usage_visibility.py` documenta el comportamiento actual y debe reescribirse al aprobar la opción.

## 6. Decisión

**Opción A aprobada** (propietario, 2026-10-30) e implementada en `app/main.py`. Las pruebas de la sección 5 están en `tests/test_usage_visibility.py`; `pages_reserved` se mantiene en la respuesta porque ahora solo lo ven administradores.

---
Relacionado: [[empresa]]
