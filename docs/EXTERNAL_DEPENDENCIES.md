# Dependencias externas: qué se ha validado y qué no

Este documento separa lo que se ejecutó de verdad en el sandbox de lo que no se pudo ejecutar. Una dependencia marcada como "no validado" no tiene prueba; es trabajo pendiente.

Fecha de la última comprobación: 2026-10-29. Rama `feat/docmind-ai-autonomo-2026-10-12`, cambios sin commit de las fases 3A a 3C.

## Estado por dependencia

| Dependencia | Estado | Qué se ejecutó | Qué no se validó |
|---|---|---|---|
| PostgreSQL 16.15 (local) | **Validado** | Suite completa con servicios vivos: 613 pasan, 17 omitidos, 0 fallan. Migraciones 0001–0008: subida, bajada y subida | — |
| PostgreSQL 17.11 (contenedor `postgres:17`) | **Validado** | Suite completa con los servicios vivos configurados: 613 pasan, 17 omitidos, 0 fallan. Migraciones 0001–0008: subida, bajada a `base`, subida, bajada a `0007`, subida | RLS (no implementada) |
| Redis (local) | **Validado** | Beat y worker con intervalos acortados, 6 tareas enviadas y completadas 5 veces cada una | Intervalos reales de 3600 s y 86400 s (ver abajo) |
| Celery (worker y beat) | **Validado** | Ver Redis. Prueba de SIGKILL en mitad de una entrega (`tests/test_outbox_process_kill.py`) | Reinicio del contenedor en la plataforma |
| Qdrant (contenedor `qdrant/qdrant`) | **Validado** | Filtros por proyecto (`MatchValue`, `MatchAny`), punto sin proyecto invisible, y backfill con `set_payload` sin re-embeber y de forma idempotente (`tests/test_live_services.py`) | Backfill sobre un índice con volumen real; clúster multi-nodo |
| ClamAV 1.4 (contenedor `clamav/clamav`) | **Validado tras corregir un defecto** | Patrón EICAR rechazado con `MALWARE_DETECTED`; texto limpio aceptado. Ver "Hallazgos" | Escaneo de ficheros reales de clientes; actualizaciones de firmas en producción |
| S3-compatible (SeaweedFS, API S3) | **Validado** (no es AWS S3) | Put, get, list y delete a través del proveedor de la aplicación (`tests/test_live_services.py`) | Permisos de bucket reales, cuotas, latencia, AWS |
| KMS (SSE-KMS, `S3_KMS_KEY_ID`) | **No validado** | — | Todo. Los endpoints locales probados no implementan KMS; LocalStack exige licencia en esta versión |
| SMTP (Mailpit) | **Validado** sin TLS | Un mensaje enviado llega al buzón del servidor (`tests/test_live_services.py`) | STARTTLS y autenticación: Mailpit no las ofrece. Producción usa `SMTP_USE_TLS=true` |
| Docker / Compose | **Parcial** | Daemon arrancado en el sandbox. `docker compose -f docker-compose.yml -f docker-compose.prod.yml config` valida (7 servicios). Contenedores sueltos para PostgreSQL 17, Qdrant, ClamAV, SMTP y S3 de prueba | `docker compose build` no se completa: Docker Hub responde 429 (límite de peticiones) para `python:3.12-slim` y `node:25-alpine`. Por eso `up` no se ha ejecutado. Repetir cuando el límite se libere |
| Navegador y Playwright (e2e) | **Validado** con la configuración de CI | 7 de 7 pasan con el stack levantado: API, worker, frontend compilado contra la API local, `LLM_PROVIDER=mock`, antivirus desactivado y límites de peticiones en 1000 (como en `ci.yml`) | Otros navegadores y viewports |
| Tesseract OCR | **Parcial** | Binario presente; las pruebas usan OCR simulado | Calidad sobre documentos reales (no se usan) |
| LLM (proveedor de extracción) | **No validado** | — | Precisión, latencia, coste y calibración con un proveedor real |
| ContextRAG | **No desplegado** | Ensayo en vivo local (`docs/evaluation/contextrag-live-check-2026-10-26.json`) | Despliegue real, claves gestionadas fuera del repositorio, carga |
| CI remota (GitHub Actions) | **No ejecutada desde aquí** | Todo se ejecutó localmente | Confirmación en un runner limpio |

## Ventanas largas de beat

El ensayo con intervalos acortados (`backend/scripts/beat_fast_app.py`, 15 s) envió y completó las seis tareas, incluidas `purge-expired-data`, `reconcile-contextrag` y `reconcile-storage`, con cero tracebacks. Ese ensayo cambia solo el intervalo. Que las tareas se disparen cada 3600 s y 86400 s en producción se deduce del horario configurado, pero no se ha observado en tiempo real.

## Hallazgos de la validación en vivo

1. **ClamAV: todos los escaneos fallaban.** `ClamAVProvider` pasaba `bytes` a `clamd.instream`, que necesita un objeto con `read()`. Con ClamAV activo, cada subida habría sido rechazada con `ANTIVIRUS_UNAVAILABLE`. Corregido con `io.BytesIO`. Regresión: `tests/test_antivirus.py` (7 tests, sin daemon) y `test_live_services.py` (con daemon).
2. **ClamAV: un error del escáner se contaba como malware.** Cualquier estado distinto de `OK` devolvía `MALWARE_DETECTED`. Ahora `FOUND` es malware y cualquier otro estado es `ANTIVIRUS_UNAVAILABLE` (falla cerrada).
3. **Test intermitente de Celery** (`test_transient_failure_is_retried_through_the_broker`): carrera del propio test. La subida encola el job y el worker podía ejecutarlo antes de instalar el parche. Corregido instalando el parche antes de la subida. Reproducido con una ventana ampliada: falla siempre antes de la corrección y pasa después.
4. **Worker obsoleto en el sandbox.** Un worker de una prueba anterior seguía consumiendo la misma cola de Redis y ejecutaba los jobs contra otra base de datos. Causó jobs "perdidos" sin error visible. No es un defecto del producto, pero sí un riesgo operativo: un despliegue debe tener un único consumidor por cola y entorno.

## Lo que no prueba esta validación

* Ninguna carga real: `scripts/load_smoke.py` solo da una orden de magnitud.
* Ningún documento real o confidencial. Todos los documentos son sintéticos.
* Ninguna operación de borrado sobre vectores u objetos en un entorno real con datos.

## Criterio para decidir qué validar primero

1. Antes de cualquier dato real: KMS (no validado), SMTP con TLS, y la decisión del proveedor LLM aprobada por el responsable.
2. Antes de un piloto: `docker compose … up` en un host con Docker y CI remota en un runner limpio.
3. Antes de activar la sincronización ContextRAG en un entorno compartido: despliegue de ContextRAG con claves de prueba y la prueba de caída descrita en `CONTEXTRAG_INTEGRATION.md`.
