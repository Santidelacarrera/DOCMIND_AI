# Informe de la fase de seguridad multitenant, integración y piloto — 2026-10-26

Continúa `docs/AUDIT_2026-10-26.md`. Todo se verificó en un sandbox local con datos sintéticos. **Nada está confirmado en Git**: no hubo commit, push, merge, release ni despliegue.

## A. Cambios implementados y verificados

| Área | Cambio | Verificación |
|---|---|---|
| Aislamiento | 9 pruebas negativas con doble membresía, ids ajenos, claves de otra organización, claves revocadas, miembros eliminados, logout | `tests/test_cross_org_negative.py`: 9/9 |
| ContextRAG | Outbox transaccional (`contextrag_events`), entrega asíncrona, reintentos con backoff, verificación del borrado, reconciliación, cancelación de upserts de documentos borrados | `tests/test_contextrag_sync.py`: 20/20 con ContextRAG real en proceso y en PostgreSQL 16 |
| Extracción | Clave de upsert con resumen del contenido (un reprocesado cambiado sí se sincroniza) | pruebas de sincronización |
| Evidencia | Tabla de evidencia frente a corrección en el benchmark | `evaluation/metrics.py`, `runner.py`; sección G |
| Interfaz | Estado de verificación, página, fragmento, texto «Not found», advertencia de confianza no calibrada | `evidence-display.spec.ts`: 1/1 |
| Almacenamiento | Modo simulación (`--dry-run`) con una decisión por objeto; sin claves completas en el informe | `tests/test_reconcile.py`: 9/9 |
| Configuración | Texto sin enmascarar bloqueado en staging/producción; `raw` exige doble autorización | `test_raw_text_requires_an_explicit_flag` |
| Benchmark | Documentos sintéticos añadidos en la fase anterior; umbrales originales restaurados | `test_evaluation.py`: 12/12 |
| Tipado | Un error real de `None` en el adaptador de ContextRAG corregido (páginas sin texto) | `mypy app`: 0 errores |
| Carga | Script reproducible `backend/scripts/load_smoke.py` | sección I |
| Documentación | `CONTEXTRAG_INTEGRATION.md` (reescrito), `PILOT.md`, `LICENSING.md`, `API.md`, `DATA_RETENTION.md`, `SECURITY.md`, `.env.example` | revisadas contra el código |

## B. Implementado pero no probado con servicios reales

* **ContextRAG desplegado.** Solo se probó en proceso. No hay un servicio accesible ni credenciales de prueba.
* **Reconciliación de almacenamiento con un bucket.** Solo se probó con almacenamiento local.
* **Caídas reales del worker.** El reinicio se simula con un *claim* obsoleto. No se mató ningún proceso en medio de una entrega.
* **Redis caído.** No se probó.
* **PostgreSQL 17.** Solo PostgreSQL 16 está instalado.
* **Modelo LLM real.** La extracción sigue siendo por reglas.

## C. Pruebas ejecutadas

| Comando | Resultado |
|---|---|
| `pytest -q -p no:cacheprovider --cov=app` (SQLite, venv con `.[dev]`) | **458 passed, 18 skipped**, cobertura 90 % |
| `TEST_DATABASE_URL=postgresql+psycopg://…/docmind pytest -q` (PostgreSQL 16) | **459 passed, 17 skipped** |
| `alembic upgrade head` → `downgrade -1` → `upgrade head` (PostgreSQL 16) | OK; 0007 incluida |
| `ruff check .` (como en CI) | All checks passed |
| `mypy app` (venv con dependencias) | Success: 0 errores en 46 archivos |
| `mypy tests` | 81 errores en 14 archivos; **CI no ejecuta mypy sobre tests** y no se corrigieron |
| `bandit -q -r app -ll` | Sin hallazgos medios ni altos |
| `pip-audit` (venv limpio) | No known vulnerabilities found; `contextrag` y `docmind-api` no están en PyPI y se omiten |
| `npm audit --omit=dev` | 0 vulnerabilities |
| `npx tsc --noEmit`, `npx eslint .`, `npm run build` | OK |
| `npx playwright test` (7 e2e, stack local) | **7 passed** |
| Carga local: 40 documentos, concurrencia 8 | 40 completados, 0 fallos |
| Carga local: 100 documentos, concurrencia 16 | 100 completados, 0 fallos |

Un fallo de la suite de pruebas durante el trabajo: `test_reconcile.py` (`TypeError`) y `test_contextrag_sync.py` (eventos atascados en `processing`). Ambos eran defectos reales del código nuevo y ya están corregidos (ver sección D).

## D. Riesgos encontrados y corregidos en esta fase

| Defecto | Gravedad | Cómo se encontró | Corrección |
|---|---|---|---|
| Comparación de `started_at` en Python (SQLite devuelve fechas sin zona) hacía que los eventos quedaran en `processing` para siempre | Alta | prueba de reintento | Token de reclamación (UUID) |
| `db.scalars` sobre dos columnas en la reconciliación de ContextRAG | Media | prueba de reconciliación | `db.execute` |
| `page.text` nulo tratado como cadena en el adaptador | Media | mypy | Cadena vacía |
| Un reemplazo de edición no se aplicó (sin `assert`) y el código no cambió | — | pruebas de reconciliación | Archivo reescrito completo |
| Aserción tautológica (`... or True`) y comprobación sobre datos equivocados | — | revisión propia | Corregidas |
| Umbrales de `baseline_expected.json` reescritos por `--update-expected`, dos veces | — | revisión del diff | Restaurados; ahora idénticos a `HEAD` |

**Sin defectos reproducibles en el aislamiento de la API.** Las nueve pruebas negativas pasaron sin cambios de código. Eso no prueba que no existan: solo prueba los casos que se diseñaron.

## E. Estado de seguridad multitenant

* **RLS: no existe en DOCMIND.** El brief lo condiciona a su existencia, así que no se supuso. El aislamiento depende de filtros `organization_id` y de la comprobación de membresía en el código. ContextRAG sí tiene RLS; DOCMIND no.
* **Recomendación:** añadir RLS como defensa en profundidad es razonable, pero requiere rediseño: cada sesión tendría que fijar la organización, y el login necesita buscar usuarios antes de conocer su organización. **No se implementó**, porque tocaría todas las transacciones y no se pudo validar con la cuenta de servicio real.
* **Cuenta de servicio:** el usuario de la base de datos en las pruebas es `postgres` (superusuario). Esto **no** representa un despliegue; la configuración de producción debe usar un rol sin privilegios de propietario.
* **Caché compartida:** no se encontraron cachés de datos por organización en el backend. Redis solo se usa para límites de frecuencia.
* **Tareas Celery:** reciben ids y derivan la organización de la fila de la base de datos. No se probó la sustitución de un id por otro en un mensaje de la cola.
* **No revisado:** inyección SQL con herramienta, pentest, revocación de sesiones en carrera, cuentas de servicio de ContextRAG.

## F. Estado de sincronización, revocación y eliminación en ContextRAG

| Requisito del brief | Estado |
|---|---|
| Crear o actualizar documentos | **Verificado en proceso** |
| Sincronizar versiones | **Verificado**: reprocesado con contenido distinto |
| Propagar cambios de permisos | **No aplicable hoy**: DOCMIND no tiene ACL por documento; la autorización de ContextRAG es la clave por tenant. Documentado como diseño pendiente |
| Revocar acceso | Revocar un miembro en DOCMIND **verificado**. En ContextRAG, la revocación es rotar la clave del tenant (no probado con despliegue) |
| Eliminar documento y fragmentos | **Verificado en proceso**: la retirada se considera completa solo tras comprobar que ya no quedan páginas |
| Reintentos sin duplicación | **Verificado** |
| Reintentos sin reactivar documentos eliminados | **Verificado**: el upsert se cancela con `DOCUMENT_DELETED` |
| Reinicio de workers | **Simulado** (claim obsoleto). No se probó con un proceso real que muera |
| Cambio de permisos con tareas pendientes | No aplicable (sin ACL); sí se prueba que el contenido se reconstruye al entregar |
| Acceso cruzado | **Verificado**: cada evento usa solo la clave de su organización |
| Reconciliación de inconsistencias | **Verificado**: crea eventos que faltan para documentos vivos y borrados |

**Procedimiento reproducible:** ver `docs/CONTEXTRAG_INTEGRATION.md`, sección «Procedimiento para validar con ContextRAG real».

**Estado global: no declarado completo.** La sincronización está implementada y probada en proceso. Falta probarla contra un servicio desplegado.

## G. Calibración y evaluación de calidad

**Qué es la «confianza» hoy.** En el baseline por reglas, la confianza de cada campo es una **constante escrita a mano** según la regla que coincidió: 0.95 si aparece la etiqueta, 0.9 o 0.75 según la regla, 0.6 si la fecha es ambigua, 0.0 si no hay valor (`app/evaluation/baseline.py`). No es una probabilidad estimada. Con un LLM, la puntuación sería la que el modelo informa, sin calibrar.

**Qué mide el ECE.** Compara la media de esas constantes por intervalo con la exactitud observada. Con constantes fijas, el ECE solo dice cuánto se separan las constantes de la realidad.

**Por qué empeoró (0.088 → 0.124).** Los cuatro documentos nuevos añaden campos incorrectos, entre ellos una fecha ambigua y un total de contrato que el sistema no leyó bien. **No he desglosado todavía en qué intervalo de confianza caen**, así que no puedo afirmar cuál es la causa exacta. Lo que sí es cierto: el ECE depende de constantes escritas a mano, y ajustarlas para bajarlo sería **sobreajustar el benchmark**, algo que no se hizo.

**Qué sí es una señal útil: la evidencia textual** (benchmark de 107 campos, `docs/evaluation/baseline-report.md`):

| Estado de evidencia | Campos | Correctos | Precisión | Revisados |
|---|---:|---:|---:|---:|
| `exact` | 69 | 68 | 98.6 % | 3 |
| `normalized` | 20 | 19 | 95.0 % | 2 |
| `not_applicable` (nulos) | 18 | 6 | 33.3 % | 18 |
| `not_found` | 0 | — | — | — |

Lectura: los valores encontrados en el texto son casi siempre correctos, y los nulos siempre van a revisión. **Limitación importante:** el proveedor por reglas solo devuelve subcadenas del texto, así que `not_found` nunca se produce en el benchmark. Esa rama solo se ejercita en pruebas unitarias y con documentos adversariales. Medir su precisión requiere un LLM real.

**Por tipo de campo, calidad y dificultad:** el informe ya desglosa por tipo de documento (digital, multipágina, escaneado, adversarial) y por campo. No se añadió un desglose por dificultad.

**`confidence_calibrated: false` se mantiene.** No hay evidencia estadística suficiente para cambiarlo.

**Casos pedidos por el brief:** coincidencia exacta, normalizada, ausente, ambigua, fechas en formatos distintos, tablas, escaneados y adversariales están en el corpus. Las contradicciones y los valores de otra entidad no están cubiertos en el corpus: **pendiente**.

## H. PostgreSQL, mypy, CI y pruebas de carga

* **PostgreSQL:** las migraciones 0004–0007 suben, bajan y vuelven a subir en PostgreSQL 16. CI usa PostgreSQL 17 y **no se pudo ejecutar** aquí: solo hay binarios de 16.
* **Concurrencia en PostgreSQL:** `SKIP LOCKED` evita que dos workers reclamen el mismo evento (prueba específica, pasa).
* **mypy:** los 52 errores del informe anterior eran del entorno sin dependencias. Con el venv completo, `mypy app` pasa sin errores. El CI ejecuta `mypy app`, así que queda cubierto.
* **CI:** `ruff check .`, `mypy app`, `bandit`, `pip-audit`, `npm audit` y e2e pasan localmente con las mismas herramientas. **No se ejecutó el CI remoto.**
* **Carga (sandbox: 4 vCPU Xeon 2.1 GHz, 16 GB RAM, Postgres 16, Redis, dos procesos Celery concurrency 2, OCR de Tesseract sobre páginas en blanco, LLM simulado):**

| Ejecución | Documentos | Concurrencia | Completados | Fallos | p50 subida | p95 subida | Extremo a extremo | Throughput |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 40 | 8 | 40 | 0 | 0.061 s | 0.188 s | 11.6 s | 207 doc/min |
| 2 | 100 | 16 | 100 | 0 | 0.083 s | 0.184 s | 28.0 s | 214 doc/min |

* **Cupo bajo concurrencia:** con el cupo FREE de 20 páginas, 40 subidas simultáneas aceptaron exactamente 20 y rechazaron 20 con 402. No hubo sobreprocesamiento.
* **No medido:** carga sostenida de horas, memoria por proceso, profundidad de colas en el tiempo, caída y recuperación de Redis, fallos del almacenamiento, saturación de workers. Las cifras no se extrapolan a producción.

## I. Archivos modificados y motivo

**Nuevos:** `backend/app/contextrag_sync.py` (outbox y entrega), `backend/app/reconcile.py` (reescrito con modo simulación), `backend/alembic/versions/0007_contextrag_outbox.py`, `backend/scripts/load_smoke.py`, `backend/tests/test_cross_org_negative.py`, `backend/tests/test_contextrag_sync.py`, `frontend/tests/e2e/evidence-display.spec.ts`, `docs/AUDIT_PHASE2_2026-10-26.md`, `docs/PILOT.md`, `docs/LICENSING.md`.

**Modificados:**
* `backend/app/core.py`: ajustes de ContextRAG y validaciones de producción.
* `backend/app/models.py`: modelo `ContextRagEvent` con `claim_token`.
* `backend/app/worker.py`: encolado al completar, tareas periódicas de entrega y reconciliación.
* `backend/app/main.py`: encolado al borrar; endpoint `GET /api/v1/contextrag/events` (solo administradores).
* `backend/app/retention.py`: encolado al expirar y al purgar.
* `backend/app/integrations/contextrag.py`: método público `pages_of` y corrección de `None`.
* `backend/app/evaluation/metrics.py`, `runner.py`: tabla de evidencia frente a corrección.
* `frontend/app/documents/[id]/page.tsx`: estado de verificación y advertencia de calibración.
* `frontend/package*.json`: `next` 16.4.3 (de la fase anterior).
* `backend/tests/test_reconcile.py`, `test_evaluation.py`, `conftest.py`: pruebas nuevas y ajustes.
* `SECURITY.md`, `docs/API.md`, `docs/DATA_RETENTION.md`, `docs/CONTEXTRAG_INTEGRATION.md`, `docs/evaluation/baseline-report.*`, `.env.example`.

## J. Recomendación de licencia y estrategia de distribución

Detalle en `docs/LICENSING.md`. Resumen:

* La licencia actual es MIT, con un titular («DocMind AI contributors») que no identifica a nadie.
* Parte del historial lo escribió un asistente de IA. Eso afecta a la protegibilidad del código, y requiere criterio jurídico.
* Las dependencias son permisivas, con LGPL (`psycopg`, `clamd`, `sharp`) y MPL (`certifi`, `pathspec`, `tqdm`, `axe-core` en desarrollo).
* **Recomendación:** mantener el repositorio **privado** y **no cambiar la licencia** hasta decidir la titularidad. Si se vende como plataforma, la opción más coherente es **propietaria**, con acuerdo de contribución para aportaciones externas. MIT deja el código usable y redistribuible sin contrato.

## K. Próximo paso recomendado

1. Desplegar ContextRAG en un entorno de pruebas con claves **de prueba** y ejecutar el procedimiento de `docs/CONTEXTRAG_INTEGRATION.md`. Es el único paso que cierra la sección F.
2. Ejecutar el CI remoto en PostgreSQL 17.
3. Medir precisión con un LLM real sobre documentos anonimizados y autorizados.
4. Decidir RLS como defensa en profundidad, con su diseño de sesiones.

## L. Estado de Git y confirmación

* Rama: `feat/docmind-ai-autonomo-2026-10-12`, en `ce16695`, sin commits nuevos.
* Modificados: 27 archivos; nuevos: 24 entradas (ver sección I).
* **No se realizó commit, push, merge, release, despliegue ni cambio de configuración remota.**
* Fuera del repositorio: los servicios locales (Postgres, Redis, API, Celery, Next) están parados; el directorio de datos está en `/var/lib/docmind-pg`. Se quitaron los enlaces de navegador añadidos a `/opt/pw-browsers`.

---
Relacionado: [[empresa]]
