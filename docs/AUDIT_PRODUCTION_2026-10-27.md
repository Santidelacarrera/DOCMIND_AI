# Auditoría final y preparación para producción — 2026-10-27

Continúa `docs/AUDIT_FINAL_2026-10-26.md`. Todo se ejecutó en un sandbox local con datos sintéticos. **No se hizo commit, push, merge, tag ni release. La licencia y la titularidad no se han tocado.**

## Veredicto

**NO APTO PARA PRODUCCIÓN. APTO PARA PILOTO CONTROLADO** con datos sintéticos o anonimizados y autorizados, revisión humana de todo campo marcado, y sin sincronización con ContextRAG salvo en un entorno de pruebas. Justificación en la sección 10.

## 1. Estado de Git

* Rama `feat/docmind-ai-autonomo-2026-10-12`, HEAD `ce16695` (igual que al inicio). Sin commits, push, merge, tag ni release.
* Inicio de esta ronda: 29 archivos modificados y 27 sin seguimiento.
* Final: 31 modificados y 34 sin seguimiento (las pruebas, scripts y documentos nuevos de esta ronda). `backend/.coverage` y `backend/dump.rdb` generados por las herramientas se eliminaron al cerrar.
* Revisado antes de tocar nada: `provenance.py` y `test_provenance.py`. Están incluidos en la suite completa de la ronda anterior: su última modificación es 20:59 y esa suite terminó a las 21:19, según las fechas de los archivos de salida.

## 2. Archivos modificados en esta ronda

| Archivo | Motivo |
|---|---|
| `backend/app/metrics.py` | Etiqueta `route` por plantilla; rutas no coincidentes agrupadas como `unmatched` |
| `backend/app/core.py` | `is_production` (fail-closed); secreto de firma de respaldo solo en desarrollo y prueba |
| `backend/app/main.py` | `_production` usa `is_production` |
| `backend/app/security.py` | Comparación de caducidad de claves API con `as_aware_utc` (ya corregido antes; sin cambio nuevo) |
| `backend/app/reconcile.py` | `rows_without_object` cuenta solo documentos vivos |
| `backend/app/contextrag_sync.py` | Código de error `BAD_RESPONSE` conservado |
| `backend/scripts/contextrag_live_check.py` | Comando de reanudación sin `disown` |
| `backend/tests/test_metrics_labels.py` (nuevo) | Regresión de la etiqueta de métricas |
| `backend/tests/test_config_fail_closed.py` (nuevo) | Regresión de la configuración |
| `backend/tests/test_outbox_process_kill.py` (nuevo) | Muerte real de un proceso con SIGKILL durante una entrega |
| `backend/tests/test_usage_visibility.py` (nuevo) | Documenta la política actual de uso (decisión pendiente) |
| `backend/tests/test_resilience.py` | Fallo de lectura en descarga |
| `backend/tests/test_reconcile.py` | Regresión del contador de filas sin objeto |
| `backend/tests/test_contextrag_sync.py` | Respuesta 200 con cuerpo no JSON |
| `docs/RLS_DESIGN.md` (nuevo) | Diseño de RLS, sin implementar |
| `docs/rls/prototype_checks.sql` (nuevo) | Prototipo reproducible del mecanismo de RLS |
| `docs/DEPLOYMENT.md` | Procedimiento local verificado y lo no verificado |
| `docs/DATA_RETENTION.md`, `docs/PILOT.md`, `docs/CONTEXTRAG_INTEGRATION.md`, `SECURITY.md` | Solo hechos confirmados |
| `docs/AUDIT_PRODUCTION_2026-10-27.md` (nuevo) | Este informe |

## 3. Defectos encontrados y corregidos

| # | Defecto | Gravedad | Cómo se reprodujo | Corrección | Regresión |
|---|---|---|---|---|---|
| D1 | Rutas no coincidentes aparecían como etiquetas de métrica: identificadores de otra organización visibles en `/metrics` sin autenticación y crecimiento sin límite de series | Alta | Tres peticiones a rutas inventadas con UUID: el UUID aparece en `/metrics` | Etiqueta `unmatched` | `test_metrics_labels.py` (falla con el código anterior) |
| D2 | `ENVIRONMENT=prod` (cualquier nombre distinto de `staging`/`production`) arrancaba sin `JWT_SECRET`, firmando tokens con una clave publicada en el código | Crítica | `Settings(environment='prod')` aceptado; clave de firma `local-development-only-not-for-deployment` | `is_production` fail-closed; sin secreto fuera de desarrollo y prueba | `test_config_fail_closed.py` (6 fallan con el código anterior) |
| D3 | `rows_without_object` contaba documentos borrados como inconsistencia (falso positivo para el operador) | Baja | Documento borrado: reportaba 7 filas sin objeto | Solo documentos vivos | `test_reconcile.py` (falla con el código anterior) |
| D4 | Respuesta 200 con cuerpo HTML se registraba como `HTTP_200` en vez de su causa | Baja | Servidor de prueba que responde 200 con HTML | Código `BAD_RESPONSE` | `test_contextrag_sync.py` |
| D5 | Prueba de `SIGKILL` con PostgreSQL: el hijo heredaba sockets del pool del padre y cerrarlos cortaba la conexión del padre | Error de prueba | Fallo en la suite de PostgreSQL, no en SQLite | El padre vacía el pool antes del `fork` | La prueba pasa en ambos motores |

Ya corregidos en rondas anteriores y reconfirmados: falsos positivos de evidencia (V1–V4), API key caducada con 500 en SQLite (V5), orden del lote de sincronización (V7), y el token de reclamación del outbox.

## 4. Comandos de prueba y resultados exactos

| Comando | Resultado |
|---|---|
| `pytest -q -p no:cacheprovider --cov=app --cov-report=term` (SQLite) | **513 passed, 18 skipped, 7 warnings**, cobertura **91 %** (4895 líneas, 447 sin cubrir) |
| `TEST_DATABASE_URL=postgresql+psycopg://…/docmind pytest -q` (PostgreSQL 16) | **513 passed, 17 skipped, 7 warnings** (sin `test_usage_visibility.py`, añadido después); ese archivo: **1 passed** en PostgreSQL |
| `ruff check .` | All checks passed |
| `mypy app` | Success: no issues found in 46 source files |
| `bandit -q -r app -ll` | Sin salida (sin hallazgos medios ni altos) |
| `pip-audit` (venv limpio con `.[dev]`) | No known vulnerabilities found |
| `npm audit --omit=dev` | found 0 vulnerabilities |
| `npx tsc --noEmit` | Sin errores |
| `npx eslint .` | Sin errores |
| `npm run build` | Compiled successfully |
| `npx playwright test` (7 e2e, stack local con PostgreSQL 16) | **7 passed** |
| `alembic upgrade head` → `downgrade base` → `upgrade head` (PostgreSQL 16) | Subir OK; bajar deja solo `alembic_version` (1 tabla); subir recrea 22 tablas |
| `pg_dump -Fc` + `pg_restore --no-owner` (PostgreSQL 16, base con datos de pruebas) | Conteos iguales: users 51, documents 190, extraction_fields 736, contextrag_events 178, audit_logs 324 |
| `python -m app.reconcile --dry-run` (almacenamiento local) | `objects: 183, orphans: 0, unrecognised: 0, rows_without_object: 0, deleted: 0` tras D3 |
| `scripts/contextrag_live_check.py` (ContextRAG como proceso HTTP aparte, puerto 8100) | Éxito: creación recuperable, borrado no recuperable, caída con reintento, reinicio con entrega, recuperación recuperable |
| `scripts/load_smoke.py --documents 100 --concurrency 16` | 100 completados, 0 fallos, p95 subida 0.172 s, 218 documentos/min |

**Entorno de cada medición:** 4 vCPU Intel Xeon 2.1 GHz, 16 GB RAM, Ubuntu, Python 3.13, PostgreSQL 16.15, Redis, Node y Playwright locales. Carga: RSS total de API, workers, Redis y PostgreSQL 640 MB antes y 695 MB después.

## 5. Pruebas omitidas y causa

* **SQLite, 18 omitidas:** 11 de Docker (no disponible), 5 por modelos spaCy no instalados, 1 por `fastembed` no instalado, 1 de bloqueo de filas (solo PostgreSQL).
* **PostgreSQL 16, 17 omitidas:** las mismas menos la de bloqueo de filas, que sí corre.
* **Ninguna prueba de aislamiento entre organizaciones está omitida.**

## 6. Servicios reales, simulados y no probados

| Componente | Estado |
|---|---|
| PostgreSQL 16 | **Real**: migraciones, suites, backup y restauración, concurrencia |
| Redis | **Real** en el stack de e2e y en las pruebas de Celery |
| API uvicorn y worker Celery | **Reales** en el stack de e2e y en la carga |
| Worker muerto con SIGKILL en mitad de una entrega | **Real** (proceso hijo y base de datos reales), en local |
| ContextRAG | **Proceso HTTP real en local** en el ensayo en vivo; **en proceso** en las pruebas; **no desplegado** |
| Tesseract y Poppler | **Reales** sobre páginas en blanco y PDFs sintéticos |
| LLM | **Simulado** (proveedor por reglas y `MockLLMProvider`). Precisión real no medida |
| Almacenamiento | **Local**. S3 no probado. Copia de seguridad del bucket **no verificada** |
| ClamAV, SMTP, KMS | **No probados** |
| Celery beat | **No ejecutado**. Las tareas periódicas se ejecutaron a mano, no por el planificador |
| Redis caído en producción | **No probado** de extremo a extremo |
| PostgreSQL 17 | **No disponible** |
| CI remoto | **No consultado** |
| Docker y staging | **No disponibles** |

## 7. Riesgos pendientes

| Gravedad | Riesgo | Mitigación actual | Qué lo cierra |
|---|---|---|---|
| **Crítico** | Precisión de la extracción con un LLM real desconocida | Revisión humana de todo campo marcado; benchmark por reglas | Evaluación con documentos reales anonimizados y autorizados, con LLM real |
| **Alto** | Sin RLS: el aislamiento depende del código | Pruebas negativas en dos motores, auditoría de rutas | Decisión de diseño y migración por fases (`docs/RLS_DESIGN.md`) |
| **Alto** | Celery beat no ejecutado: las entregas del outbox, la reconciliación y la recuperación de trabajos dependen de él | Tareas probadas a mano; procedimiento en `DEPLOYMENT.md` | Ejecutar beat en staging y verificar que las tareas se disparan |
| **Alto** | ContextRAG no desplegado | Ensayo en vivo en local | Desplegar ContextRAG en pruebas y repetir `contextrag_live_check.py` |
| **Alto** | CI remoto y PostgreSQL 17 sin verificar | Equivalentes locales en PostgreSQL 16 | Consultar CI y ejecutar en PostgreSQL 17 |
| **Alto** | Copia de seguridad del almacenamiento no verificada | Solo la base de datos está probada | Probar la restauración completa en un entorno aislado |
| **Medio** | Eventos `failed` del outbox se reintentan cada 6 h sin fin, y no hay comando para cancelarlos | Visibles en `GET /api/v1/contextrag/events` | **Atendido:** cancelación explícita de upserts por administrador (`POST /api/v1/contextrag/events/{id}/cancel`, con auditoría; los borrados no se cancelan). Pruebas en `tests/test_outbox_admin.py`. Política de cuándo usarla: pendiente de decisión del responsable |
| **Medio** | `/metrics` sin autenticación | Etiquetas sin datos de tenant (D1) | Restringir en el ingress |
| **Medio** | Usuarios con rol viewer leen totales de uso | Política actual, probada | Decisión del propietario (sección 8) |
| **Medio** | Timeout de ContextRAG fijo en 15 s | Verificado en código | Hacerlo configurable si se necesita |
| **Medio** | Accesibilidad de la interfaz no comprobada automáticamente | Sin prueba | Ejecutar un analizador de accesibilidad |
| **Bajo** | `mypy tests`: 81 errores, ninguno en la aplicación | Clasificados | Corregir si se quiere ejecutar mypy también sobre tests |
| **Bajo** | Logs de excepciones contienen rutas internas (solo servidor) | No llegan al cliente | Revisar el nivel de log en producción |

## 8. Decisiones que requieren al propietario

1. **Titularidad y licencia** (`docs/LICENSING.md`). No se ha cambiado nada; no puedo inventar el titular.
2. **Diseño de RLS**: aprobar roles, y elegir `SECURITY DEFINER` (recomendado) o un rol `docmind_system` para las consultas previas al contexto.
3. **Acceso de viewers a los totales de uso**: mantener la política actual o restringirla a administradores.
4. **Cancelación de eventos fallidos del outbox**: diseñar la operación (quién, cuándo, qué se registra).
5. **Activación de la sincronización con ContextRAG** en cualquier entorno, y con qué organizaciones.
6. **Activación del borrado de huérfanos** (`STORAGE_RECONCILE_DELETE`): recomiendo mantenerlo en `false` hasta revisar un informe real.
7. **Política de texto enmascarado** frente a `raw` para ContextRAG (`raw` está bloqueado en staging y producción).

## 9. Acciones concretas para cerrar cada bloqueo

1. **Evaluación con LLM real:** elegir proveedor y modelo; registrar versión y parámetros; ejecutar `python -m app.evaluation --provider openai` sobre un conjunto anonimizado con anotaciones de revisores; separar desarrollo y evaluación. Sin credenciales reales no se puede ejecutar aquí.
2. **RLS:** aprobar el diseño; implementar en una rama con migración `0008` por fases (roles → políticas sin `FORCE` → `FORCE` en staging); repetir las pruebas negativas con `docmind_app`.
3. **Beat:** en staging, `celery -A app.worker.celery_app beat` como única instancia; verificar en los logs que `deliver-contextrag-events` y `recover-stuck-jobs` se disparan; `GET /api/v1/contextrag/events` debe mostrar `done`.
4. **ContextRAG desplegado:** levantar ContextRAG en un entorno de pruebas con claves de prueba fuera del repositorio; `python scripts/contextrag_live_check.py --contextrag-url https://…`.
5. **CI y PostgreSQL 17:** ejecutar el workflow en GitHub y consultar el resultado; ejecutar `TEST_DATABASE_URL` con PostgreSQL 17 cuando esté disponible.
6. **Restauración completa:** copia de PostgreSQL más versionado del bucket con la misma fecha; restaurar en un entorno aislado; `python -m app.reconcile --dry-run` debe dar `orphans: 0`, `rows_without_object: 0`.
7. **Cancelación de eventos fallidos:** operación de administrador con registro de auditoría; pruebas de regresión.

## 10. Veredicto justificado

**No apto para producción.** Las condiciones del brief exigen no declarar producción con un riesgo crítico o alto sin mitigación aprobada. Hay tres sin mitigación aprobada: la precisión con un LLM real (crítico), la ausencia de RLS y la ejecución no verificada de Celery beat (altos). Además, ContextRAG no está desplegado y el CI remoto no está consultado.

**Apto para piloto controlado** porque el aislamiento entre organizaciones está demostrado con pruebas negativas en dos motores, la configuración falla de forma cerrada, la sincronización ha pasado una prueba de caída y recuperación con un servicio HTTP real, y las dependencias no tienen vulnerabilidades conocidas. Condiciones: solo datos sintéticos o anonimizados y autorizados; revisión humana de todo campo marcado; sincronización desactivada salvo en pruebas; sin decisiones de RLS ni de viewers pendientes sin revisar.

---
Relacionado: [[empresa]]
