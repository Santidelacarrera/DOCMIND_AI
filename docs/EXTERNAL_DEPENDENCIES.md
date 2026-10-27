# Dependencias externas: qué se puede validar aquí y qué no

Este documento separa lo que se ejecutó de verdad en el sandbox de lo que requiere infraestructura que no estaba disponible. Ningún resultado de la columna "No disponible" es una prueba: son validaciones pendientes.

Fecha de la última comprobación: 2026-10-27. Rama `feat/docmind-ai-autonomo-2026-10-12`.

| Dependencia | Función en DOCMIND | Estado en el sandbox | Qué sí está probado | Qué falta | Cómo validarla |
|---|---|---|---|---|---|
| PostgreSQL 16 | Base de datos, RLS en diseño, migraciones 0001–0007 | **Disponible** (16.15 local, puerto 5433) | Suite completa en PG16 (513 pasan, 17 omitidas); migraciones head→base→head | — | `DATABASE_URL=postgresql+psycopg://… pytest` y `alembic upgrade head` |
| PostgreSQL 17 | Versión objetivo de producción (según el plan) | **No disponible** | Nada | Toda la suite y las migraciones en PG17 | Levantar PG17 y repetir la suite y `alembic` |
| Redis | Broker y backend de Celery | **Disponible** (`redis-server` local) | Beat real: `deliver-contextrag-events`, `recover-stuck-jobs`, `process-vector-deletions` enviados y terminados con éxito (`backend/scripts/beat_smoke.sh`) | Ventanas largas de beat (3600 s y 86400 s) | `BEAT_SMOKE_SECONDS=75 backend/scripts/beat_smoke.sh`; para las tareas largas, ejecutar beat al menos 1 h o disparar las tareas a mano |
| Celery worker | Procesa documentos y entregas ContextRAG | **Disponible** (local, `--concurrency=2`) | Entrega real con Redis y PG, prueba de SIGKILL en mitad de la entrega (`tests/test_outbox_process_kill.py`) | Reinicio del contenedor en plataforma | Igual que Redis |
| Docker / Compose | Entorno de staging (`docker-compose.yml` + `docker-compose.prod.yml`) | **No disponible** (CLI instalada, daemon inaccesible) | Nada del despliegue: solo revisión estática del fichero | Construcción de imágenes, red interna `backend`, healthchecks | `docker compose -f docker-compose.yml -f docker-compose.prod.yml config` y después `up` en un host con Docker |
| Object storage S3 | Almacenamiento de originales y versiones | **No disponible** (sin endpoint S3 ni MinIO) | Almacenamiento y reconciliación con un proveedor simulado; fingerprints y modo simulación | Permisos del bucket, cifrado en reposo, cuotas, latencia | Bucket de prueba con política mínima; ejecutar `reconcile_storage` en modo simulación contra él |
| KMS (`S3_KMS_KEY_ID`) | Cifrado SSE-KMS de los objetos | **No disponible** | Solo que el parámetro se envía en la petición (`app/storage.py`) | Que la clave exista, que el rol tenga `kms:Decrypt`/`GenerateDataKey`, y que la subida funcione | Subida y lectura de un objeto de prueba con la clave del entorno |
| ClamAV (clamd) | Antivirus de los ficheros subidos | **No disponible** (sin `clamd`) | Ruta de decisión con proveedor simulado | Escaneo real de un EICAR y un fichero limpio | Levantar `clamd`, subir EICAR (debe rechazarse) y un PDF limpio (debe aceptarse) |
| Tesseract OCR | Texto de PDFs escaneados | **Binario presente** (`/usr/bin/tesseract`) | Las pruebas usan un OCR simulado; el binario no se ha usado sobre documentos reales | Calidad de extracción en documentos reales | Solo con documentos de prueba aprobados; no usar datos reales en el sandbox |
| SMTP | Correos de invitación | **No disponible** (sin listener) | Proveedor de consola en desarrollo; la configuración exige `SMTP_HOST` fuera de desarrollo | Entrega real, TLS, remitente | Servidor SMTP de prueba (p. ej. un relay de staging) |
| LLM (proveedor de extracción) | Extracción de campos | **No disponible** (sin credenciales ni llamadas reales) | Ruta de extracción con respuestas simuladas; benchmark con calibración calculada | Precisión real, latencia y coste; calibración real | Ejecutar la evaluación de `docs/EVALUATION.md` con el proveedor real sobre el corpus de desarrollo; no sobre documentos reales sin aprobación |
| ContextRAG | Índice vectorial de destino de la sincronización | **Instancia local de prueba** (ensayo en vivo registrado en `docs/evaluation/contextrag-live-check-2026-10-26.json`); **no desplegada** | Contrato HTTP, claves por tenant, reintentos, reanudación tras caída y reinicio | Despliegue real, claves gestionadas fuera del repositorio, límites de carga | Ver `docs/CONTEXTRAG_INTEGRATION.md`, sección "Procedimiento para validar con ContextRAG real" |
| Remote CI (GitHub Actions) | Ejecución independiente de la suite | **No ejecutada** (no hay acceso a Actions desde el sandbox) | Todo se ha ejecutado localmente | Confirmación en un runner limpio, versiones de sistema | Ejecutar el workflow en la rama y revisar el resultado |
| Navegador (Playwright) | Pruebas e2e del frontend | **Disponible** (Chromium preinstalado) | Suite e2e local | Otros navegadores y viewports reales | `npm run test:e2e` con el stack levantado |

## Lo que no ejecuta esta prueba

* Ninguna carga real: `scripts/load_smoke.py` solo da una orden de magnitud.
* Ningún documento real o confidencial. Todos los PDFs son sintéticos (`backend/eval_corpus/`).
* Ninguna operación de borrado de vectores ni de objetos en un entorno real. La reconciliación de almacenamiento se ha probado en modo simulación.

## Criterio para decidir qué validar primero

1. Antes de un piloto con datos no reales: PostgreSQL 17, Docker Compose y SMTP de staging. Son las piezas que cambian el comportamiento de la aplicación sin tocar datos.
2. Antes de cualquier dato real: S3 + KMS, ClamAV, y la decisión del proveedor LLM aprobada por el responsable.
3. Antes de activar la sincronización ContextRAG en un entorno compartido: despliegue de ContextRAG con claves de prueba y la prueba de caída descrita en `CONTEXTRAG_INTEGRATION.md`.
