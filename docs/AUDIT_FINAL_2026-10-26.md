# Auditoría final de seguridad e integración — 2026-10-26

Continúa `docs/AUDIT_PHASE2_2026-10-26.md`. Todo se ejecutó en un sandbox local con datos sintéticos. **Nada está confirmado en Git**: no hubo commit, push, merge, tag, release ni cambio de licencia.

## 1. Estado inicial y final de Git

* **Inicial:** rama `feat/docmind-ai-autonomo-2026-10-12`, HEAD `ce16695`, 27 archivos modificados y 24 entradas sin seguimiento. Diff revisado antes de tocar nada.
* **Final:** misma rama y mismo HEAD, sin commits nuevos. 29 archivos modificados y 27 entradas sin seguimiento (incluye `backend/.coverage` generado por la cobertura; se elimina al cerrar).
* `backend/app/provenance.py` y `backend/tests/test_provenance.py` cambiaron en disco tras la última lectura. Se revisaron línea a línea; ver sección 2.

## 2. Revisión de `provenance.py` y sus pruebas

**La versión en disco era correcta en lo que probaba, pero tenía defectos reproducibles de falsa evidencia.** Seis casos los reproducían, y las 22 pruebas existentes no los cubrían:

| Caso | Resultado anterior | Riesgo |
|---|---|---|
| Valor `Acme` con texto `Acmeville Logistics` | `exact` | Un proveedor quedaba verificado con otro nombre |
| `Juan` en `Juana García` | `exact` | Lo mismo con nombres de personas |
| Entero `1` dentro de `12` y `4.10` | `exact` | Cualquier cifra corta quedaba respaldada |
| `15.0` en un texto que contiene un `15` cualquiera | `normalized` | Número sin contexto |
| `USD` con un símbolo `$` | `normalized` | `$` es USD, CAD, AUD, MXN… |
| `2026` dentro de una referencia | `exact` | Años presentes en casi cualquier documento |

**Corrección** (`app/provenance.py`):
* coincidencia exacta por palabras completas, sin coincidir dentro de una palabra más larga;
* tokens numéricos más estrictos: `1 2024` son dos números, no `12024`;
* enteros cortos y años quedan marcados como `weak` y generan una incidencia `weak_evidence` que manda el campo a revisión;
* `$` ya no respalda `USD`; solo `€`, `£` y `¥` respaldan su código.

**Pruebas:** 12 nuevas. Con el código anterior, **11 fallan**; con el nuevo, las 34 pasan. Es decir, protegen los defectos reales. Incluye una prueba extremo a extremo (`Juan Pérez` frente a `Juan Pérezita` deja el campo en revisión).

## 3. Pruebas ejecutadas

| Comando | Resultado |
|---|---|
| `pytest -q -p no:cacheprovider --cov=app` (SQLite, venv con `.[dev]`) | **493 passed, 18 skipped**, cobertura 91 % |
| `TEST_DATABASE_URL=postgresql+psycopg://…/docmind pytest -q` (PostgreSQL 16) | **494 passed, 17 skipped** |
| `ruff check .` (como CI) | All checks passed |
| `mypy app` | Success: 0 errores en 46 archivos |
| `mypy tests` | 81 errores; ver sección 7 |
| `bandit -q -r app -ll` | Sin hallazgos medios ni altos |
| `pip-audit` (venv limpio) | No known vulnerabilities found (`contextrag` y `docmind-api` no están en PyPI y se omiten) |
| `npm audit --omit=dev` | 0 vulnerabilities |
| `npx tsc --noEmit`, `npx eslint .` | OK |
| `npm run build` | Verificado en la fase anterior; el código de interfaz no cambió después |
| `npx playwright test` (7 e2e, stack local) | **7 passed** |
| `scripts/contextrag_live_check.py` (ContextRAG como proceso HTTP aparte) | **Éxito**; ver sección 6 |

## 4. Pruebas omitidas y causa

Motivos leídos con `pytest -rs` sobre SQLite (18 omitidas):

| Causa | Omitidas | Qué habría que instalar o configurar |
|---|---:|---|
| `test_docker_integration.py`: requiere Docker Compose | 11 | Docker en marcha con el stack completo |
| spaCy: modelos `en_core_web_sm` y `es_core_news_sm` no instalados | 5 | `pip install '.[ner]'` y los dos modelos |
| `fastembed` no instalado | 1 | `pip install '.[embeddings]'` (descarga de pesos del modelo) |
| Bloqueo de filas `SKIP LOCKED`: solo PostgreSQL | 1 | Ejecutar con `TEST_DATABASE_URL` |

En PostgreSQL 16 se omitieron 17. Esto es la misma lista menos la prueba de bloqueo de filas; **lo deduzco de la lista de SQLite y no lo he impreso por separado**.

No se omite ninguna prueba de aislamiento entre organizaciones: las negativas se ejecutan en ambos motores.

## 5. Qué se probó con servicios reales, qué se simuló, qué no

| Componente | Estado |
|---|---|
| PostgreSQL 16 | **Real** (migraciones, suite completa, concurrencia) |
| Redis | **Real** en el stack e2e y en las pruebas de Celery |
| Celery worker | **Real** en el stack e2e; sin muerte forzada en medio de una entrega |
| API uvicorn | **Real** en el stack e2e y en el ensayo de carga |
| ContextRAG_Engine | **Proceso HTTP real en local** (puerto 8100) en el ensayo; **en proceso** en las pruebas; **no desplegado** |
| Tesseract/Poppler | **Reales** en el sandbox, sobre páginas en blanco y sobre PDFs sintéticos |
| LLM | **Simulado** (proveedor por reglas y `MockLLMProvider`) |
| Almacenamiento | **Local**; S3 no probado |
| PostgreSQL 17 | **No disponible** (solo 16 instalado; Docker no está en marcha) |
| CI remoto | **No consultado**: no tengo acceso al resultado |

## 6. Integración DOCMIND–ContextRAG

**Ensayo en vivo** (`backend/scripts/contextrag_live_check.py`, resultado en `docs/evaluation/contextrag-live-check-2026-10-26.json`):

| Paso | Resultado |
|---|---|
| Creación: el documento se entrega y responde la pregunta | `retrievable_after_create: true` |
| Borrado en DOCMIND → entrega de la retirada | `retrievable_after_delete: false` (el texto ya no se recupera) |
| ContextRAG detenido durante una entrega | `during_outage.retried: 1`, el evento no queda bloqueado |
| ContextRAG reiniciado y nueva pasada | `after_restart.done: 1` |
| Recuperación del contenido tras el reinicio | `retrievable_after_recovery: true` |
| Vista de administrador | `admin_view_counts: {"done": 3}` |

**Observación:** en la primera pasada, `failed: 175`. Son eventos acumulados de pruebas de otras organizaciones, sin clave en este ensayo. Quedaron en `failed` con `NO_KEY_FOR_TENANT` y **no se envió nada a ContextRAG con una clave equivocada**. Comportamiento correcto, pero conviene saber que la cola puede acumular fallos visibles.

**Pruebas de fallo en proceso** (`test_contextrag_sync.py`): 401 y 422 quedan `failed` de inmediato con `HTTP_401`/`HTTP_422`; 429 y 503 se reintentan; timeouts de lectura y conexión se reintentan; tres intentos en diez minutos con backoff de 2, 4 y 8 minutos; eventos futuros no se entregan antes de tiempo; versiones fuera de orden: la antigua se cancela (`SUPERSEDED`) y la nueva se entrega en la misma pasada; reconciliación repetida no duplica ni reabre un borrado completado.

**Configuración del adaptador:** URL validada (HTTPS salvo localhost, lista blanca de hosts); clave por tenant en `X-API-Key`, nunca en logs; redirecciones desactivadas; TLS verificado por defecto; timeout de 15 s **fijo en código, no configurable**; reintentos gestionados por el outbox, no por el cliente HTTP.

**Qué queda sin validar:** ContextRAG desplegado en un entorno real, claves reales, reinicio de un proceso worker en medio de una entrega, y ACL por documento (no existen en DOCMIND).

## 7. Mypy de tests: qué ocultan los 81 errores

Clasificación (sin ampliar el alcance):
* **`arg-type` (43) y `union-attr` (18):** tipado de fixtures y de respuestas opcionales de SQLAlchemy/openpyxl en pruebas. No afectan al comportamiento.
* **`call-arg` (4):** `_env_file` de pydantic-settings y constructores de excepciones de `openai` en pruebas.
* **`import-not-found` (6):** `contextrag` no está instalado en el entorno de CI; las pruebas lo importan dentro de funciones con `importorskip`.
* **`type-arg` (9), `attr-defined` (3), `index` (2), `operator` (1), `assignment` (1), `truthy-function` (1):** en su mayoría tipado de pruebas. El caso `truthy-function` era una aserción sin efecto (`assert io.BytesIO`) y **se eliminó**.
* **Ningún error de mypy en tests apunta a un fallo del código de aplicación.**

## 8. Vulnerabilidades reproducibles y correcciones

| # | Hallazgo | Gravedad | Corrección | Pruebas |
|---|---|---|---|---|
| V1 | Falsa evidencia por subcadena (`Acme` en `Acmeville`) | Alta | Coincidencia por palabras completas | 4 regresiones (fallan antes) |
| V2 | Falsa evidencia por entero corto o año | Media | Marca `weak` y revisión | 3 regresiones |
| V3 | `$` respalda `USD` | Media | Solo símbolos sin ambigüedad | 1 regresión |
| V4 | Tokens numéricos con espacios fusionados (`1 2024` → `12024`) | Baja | Separación por dígitos de grupo | 2 regresiones |
| V5 | API key con `expires_at` rechazada con 500 en SQLite (comparación naive/aware) | Media | `as_aware_utc` en `lookup_api_key` | 1 regresión (fallaba con `TypeError`) |
| V6 | Eventos de ContextRAG: reclamación con marca temporal (ya corregido en fase previa) | Alta | Token | regresión existente |
| V7 | Upsert posterior del mismo documento esperaba 30 s sin motivo | Baja (latencia) | Orden secuencial dentro del lote | 1 regresión |

Cada corrección tiene su prueba de regresión. Las pruebas de V1–V4 se comprobaron **fallando con el código anterior**.

**Hallazgos sin corregir:**
* No hay RLS. Ver sección 9.
* La extracción por reglas solo devuelve subcadenas del texto, así que `not_found` nunca se produce con ella.

## 9. Riesgos de seguridad pendientes

| Gravedad | Riesgo | Por qué sigue abierto |
|---|---|---|
| **Crítico** | Sin RLS: el aislamiento depende de filtros en código | Implementarlo cambia el manejo de sesiones y el login; requiere diseño y migración. Propuesta abajo |
| **Crítico** | Precisión con un LLM real desconocida | No hay modelo real ni documentos reales autorizados |
| **Alto** | Cuenta de servicio de pruebas con privilegios de superusuario | Es solo del sandbox; producción debe usar un rol sin privilegios de propietario |
| **Alto** | ContextRAG no desplegado ni probado con red real y claves reales | Requiere infraestructura y claves de prueba |
| **Alto** | Sin pentest ni inyección SQL revisada con herramienta | Fuera del alcance de esta fase |
| **Medio** | Timeout del adaptador fijo | No configurable por variable de entorno |
| **Medio** | Interfaz de revisión sin prueba de accesibilidad automatizada | No se ejecutó axe ni equivalente |
| **Medio** | Cupo de páginas y tamaños probados solo con sintéticos | |
| **Bajo** | Logs de Tesseract y rutas del sistema en logs del servidor | Solo servidor; no llega al cliente |

### Propuesta de RLS (no implementada)

* **Diseño:** cada transacción fija `app.org_id` con `set_config`; políticas `USING (organization_id = current_setting('app.org_id')::uuid)` en documentos, campos, trabajos, eventos y claves.
* **Consecuencias:** el login y la búsqueda de usuarios por correo deben hacerse con un rol de servicio separado o con una función `SECURITY DEFINER`; los workers Celery deben fijar la organización de cada trabajo antes de leer; la reconciliación necesita un rol que vea todo.
* **Migración:** primero políticas en modo `PERMISSIVE` sin forzar, luego `FORCE ROW LEVEL SECURITY` en staging con pruebas negativas existentes, y solo después en producción. Nada de esto es destructivo, pero requiere un rol de aplicación distinto del propietario de las tablas.
* **Autorización:** esto requiere decisión tuya antes de implementar.

## 10. Calibración

**Desglose** (`docs/evaluation/baseline-report.json`, sección `confidence_breakdown`):

| Conjunto | Campos | ECE | Notas |
|---|---:|---:|---|
| Total (definición original, sin cambios) | 107 | **0.124** | Incluye abstenciones con confianza 0.0 |
| Desarrollo (19 documentos originales) | 81 | 0.088 | Igual al valor anterior de la fase previa |
| Documentos añadidos después | 26 | 0.285 | Pequeña muestra |
| Solo valores afirmados | 89 | **0.082** | Lo que la métrica debería evaluar |
| — desarrollo | 69 | 0.088 | |
| — añadidos después | 20 | 0.120 | Muestra pequeña |
| Abstenciones (valor vacío) | 18 | — | Solo **6 correctas**; 12 valores existían y el sistema no los extrajo |

**Diagnóstico:**
1. **La degradación de 0.088 a 0.124 viene de los documentos añadidos**, no del código. El desarrollo mantiene el valor previo.
2. **El mayor aporte al ECE es el intervalo 0.0, que agrupa abstenciones.** Una respuesta correcta «no hay valor» cuenta como predicción errónea con confianza cero. Es un defecto de definición de la métrica, no un modelo sobreconfiado.
3. **El problema real está en el recall:** 12 de 18 abstenciones son valores que el documento sí contenía y el sistema omitió. Eso es un fallo de extracción, no de calibración, y el ECE lo oculta.
4. **No se ajustó ninguna constante para mejorar el número.** La definición se mantiene y se reporta además la versión sobre valores afirmados.
5. **Muestras pequeñas:** con 20 valores en el grupo añadido, la diferencia 0.088 frente a 0.120 no es concluyente.
6. **Lo que la confianza no es:** las constantes del baseline son reglas escritas a mano, no probabilidades. `confidence_calibrated` se mantiene en `false`.

**Evaluación adicional necesaria:** un conjunto etiquetado de documentos reales anonimizados, separado en desarrollo y evaluación, con un LLM real; y un conjunto de evaluación que nadie del equipo haya visto al escribir las reglas.

## 11. PostgreSQL, mypy, CI y carga

* **PostgreSQL 16:** migraciones 0004–0007 suben y bajan; suite completa pasa (494). **PostgreSQL 17:** no disponible en el sandbox.
* **CI remoto:** no consultado.
* **Carga local** (sandbox 4 vCPU, 16 GB, Postgres 16, Redis, dos workers Celery, OCR real sobre páginas en blanco, LLM simulado): 40 documentos con concurrencia 8 y 100 con concurrencia 16, **0 fallos**, p95 de subida 0.18 s, unos 214 documentos por minuto. Es una medición local, no una capacidad demostrada.

## 12. Archivos modificados y motivo

**Código de aplicación:** `app/provenance.py` (reescrito con los límites de palabra, los tokens estrictos y la evidencia débil), `app/security.py` (V5), `app/contextrag_sync.py` (orden de lote y token), `app/reconcile.py` (modo simulación), `app/evaluation/metrics.py` y `runner.py` (desglose de calibración y tabla de evidencia), `app/evaluation/corpus.py` (conjunto de desarrollo), `app/integrations/contextrag.py`.

**Pruebas:** `tests/test_provenance.py` (+12), `tests/test_cross_org_negative.py` (+8), `tests/test_contextrag_sync.py` (+12), `tests/test_reconcile.py` (+5), `tests/test_resilience.py` (nuevo), `tests/test_tenant_isolation.py` (aserción sin efecto eliminada).

**Scripts:** `scripts/contextrag_live_check.py` (ensayo en vivo), `scripts/load_smoke.py`.

**Documentación:** este informe, `PILOT.md`, `CONTEXTRAG_INTEGRATION.md`, `SECURITY.md`, `docs/evaluation/*`.

## 13. Recomendación

**Apto para un piloto controlado, no apto para datos confidenciales reales ni para producción.**

Justificación:
* **A favor:** aislamiento verificado con pruebas negativas en dos motores; procedencia que ya no acepta falsa evidencia en los casos conocidos; sincronización con ContextRAG probada por red, con caída y recuperación; cero fallos en la carga local; dependencias sin vulnerabilidades conocidas.
* **En contra:** sin RLS; precisión con un LLM real desconocida; ContextRAG no desplegado; PostgreSQL 17 y CI remoto sin verificar; sin pentest; el titular del código no está resuelto.

**Condiciones para el piloto:** solo documentos sintéticos o anonimizados y autorizados; revisión humana de todo campo marcado; sincronización con ContextRAG desactivada salvo en un entorno de pruebas; PostgreSQL 17 y CI en verde antes de cualquier despliegue; decisión sobre RLS antes de ampliar el piloto.

## 14. Próximo paso recomendado

1. Ejecutar el CI remoto en PostgreSQL 17 y confirmar el resultado.
2. Desplegar ContextRAG en un entorno de pruebas con claves de prueba y repetir `scripts/contextrag_live_check.py` contra ese despliegue.
3. Decidir el diseño de RLS (sección 9).
4. Medir precisión con un LLM real sobre documentos anonimizados y autorizados.

---
Relacionado: [[empresa]]
