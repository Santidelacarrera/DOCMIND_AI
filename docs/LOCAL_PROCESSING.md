# Procesamiento local — Nexus Security & AI Lab

DocMind AI procesa documentos **en tu propia máquina o servidor**. Este documento dice qué corre dónde, qué datos se quedan, qué podría salir de la máquina (solo si lo configuras) y cómo comprobarlo. Coste de todo lo descrito: **$0** (software libre, sin cuentas ni APIs de pago).

## Qué corre en local

| Etapa | Componente | Dónde corre | Red |
|---|---|---|---|
| Validación de subida (tipo, tamaño, páginas) | FastAPI + `pypdf` / `zipfile` + `defusedxml` | proceso de la API | ninguna |
| Lectura de PDF | `pypdf` | local | ninguna |
| OCR de escaneados | Tesseract + Poppler (binarios del sistema) | local | ninguna |
| Lectura de Word (`.docx`) | `zipfile` + `defusedxml` (sin Word, sin LibreOffice) | local | ninguna |
| Sanitización, enmascarado de PII, fragmentación | `app/ingestion/*` (Python puro) | local | ninguna |
| Embeddings | `hashing` (léxico, sin dependencias) o `local` (ONNX con `fastembed`, modelo pre-instalado) | local | ninguna (`EMBEDDING_LOCAL_FILES_ONLY=true` impide descargas en ejecución) |
| Base vectorial | Qdrant en memoria (`QdrantClient(":memory:")`, demos/tests) o un Qdrant que tú alojes | local / tu red | solo hacia tu Qdrant |
| Respuestas con cita | `app/ingestion/answering.py` (extractivo) | local | ninguna; **no usa ningún modelo de lenguaje** |
| Base de datos / cola | PostgreSQL y Redis que tú alojes (o SQLite en pruebas) | tu infraestructura | solo interna |

## Qué solo saldría de la máquina si tú lo activas

| Opción | Efecto | Salvaguarda |
|---|---|---|
| `LLM_PROVIDER=openai` (extracción de campos con IA) | el texto del documento va a OpenAI | desactivado por defecto (`mock`); exige clave |
| `EMBEDDING_PROVIDER=openai` | el texto **ya enmascarado** va al proveedor de embeddings | exige `EMBEDDING_ALLOW_EXTERNAL=true`, HTTPS y host en `EMBEDDING_ALLOWED_HOSTS`; se comprueba antes de leer ningún documento |
| Webhooks | se envía un aviso firmado a la URL que registres | solo URLs HTTPS públicas, validadas contra SSRF |
| `SMTP_HOST` | correos de invitación | solo si lo configuras |

Con la configuración por defecto de desarrollo (`LLM_PROVIDER=mock`, sin `QDRANT_URL`, `EMBEDDING_PROVIDER=local|hashing`) **ningún dato del documento sale del equipo**.

## Dónde quedan los datos

* Archivo original: `LOCAL_STORAGE_PATH` (disco local) o tu bucket S3 privado; clave `<organización>/<uuid>.<ext>`.
* Texto extraído y resultados: tu PostgreSQL. Vectores: tu Qdrant, **ya enmascarados**.
* Borrado y retención: [DATA_RETENTION.md](DATA_RETENTION.md); vectores huérfanos: cola `vector_deletions` ([INGESTION.md](INGESTION.md)).

## Cómo comprobarlo (reproducible)

```bash
cd backend
# 1) La demo completa (PDF + Word, 2 empresas ficticias) con el acceso a red bloqueado por la prueba:
python -m pytest -q tests/test_word_and_citations.py -k "without_any_network"
# 2) Lo mismo a mano: la demo no necesita red (puedes desconectar el equipo)
python -m app.ingestion.demo --output-dir /tmp/demo
# 3) Auditoría estática: el único fichero de la ingesta que menciona red es el embedder externo (opt-in)
grep -rlE "requests|httpx|urllib|socket|openai" app/ingestion/      # esperado: solo app/ingestion/embeddings.py
```

La prueba (1) sustituye `socket.connect` y `getaddrinfo` por funciones que fallan: si cualquier etapa intentara salir a la red, la prueba rompe.

## Límites

* El embedder `hashing` es léxico (coincidencia de palabras), no semántico; para búsqueda semántica instala un modelo local (`pip install '.[embeddings]'` y pre-provisiona `EMBEDDING_MODEL`). No se ha probado con pesos reales en este entorno.
* Las respuestas son **extractivas**: citan frases del documento; no resumen ni razonan. Es deliberado (no inventa y no cuesta).
* Word no guarda paginación: la página se calcula con saltos de página explícitos; sin ellos se estima (y la cita lo marca con `~`).

---
Relacionado: [[empresa]]
