# Instalación

Probado el 2026-11-10 en Windows 11 + Docker Desktop 29.8 y en Linux (CI). Todo funciona sin cuentas ni coste.

## Opción A — Stack completo con Docker (recomendada)

Requisitos: Docker Engine con Compose v2, unos 4 GB de RAM libres, puertos 3000, 8000 libres.

```bash
git clone https://github.com/Santidelacarrera/DOCMIND_AI.git
cd DOCMIND_AI
cp .env.example .env
docker compose up --build --wait
```

Levanta `db` (PostgreSQL 17), `redis`, `qdrant`, `api` (aplica las migraciones al arrancar), `worker`, `scheduler` (Celery beat) y `web`.

Comprobación de extremo a extremo (dos organizaciones, subida, procesamiento, extracción, exportación, aislamiento y preguntas con cita):

```bash
pip install httpx && python backend/scripts/staging_smoke.py --base-url http://localhost:8000 --ask
```

Debe terminar con `smoke test passed`. Después: <http://localhost:3000>, crear cuenta (contraseña ≥ 12 caracteres), crear un proyecto, subir un PDF o Word, revisar y preguntar en **Ask**.

Opcional, observabilidad: `docker compose --profile observability up -d` (Prometheus :9090, Grafana :3001, OTel collector :4318).

## Opción B — Solo backend, sin Docker

Requisitos: Python 3.12. Opcional: `tesseract-ocr` y `poppler-utils` (PDF escaneados), `redis-server` (pruebas de cola).

```bash
cd backend
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
pytest                                               # SQLite y dobles en memoria
python -m app.ingestion.demo                         # demo offline: 10/10 citas correctas
```

## Opción C — Frontend en local contra la API

```bash
cd frontend
npm ci
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

## Modelos de embeddings

Por defecto el stack usa `EMBEDDING_PROVIDER=hashing` (léxico, sin descargas). Para respuestas semánticas en español:

```bash
pip install -e '.[embeddings]'
# en .env: EMBEDDING_PROVIDER=local, EMBEDDING_LOCAL_FILES_ONLY=false la primera vez (descarga ~120 MB)
```

El modelo por defecto es `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`. Cambiar de modelo sobre un índice existente se rechaza (`EMBEDDING_MODEL_MISMATCH`): reindexar o mantener el modelo anterior. Ver [ENVIRONMENT.md](ENVIRONMENT.md).

## Producción

No uses `docker-compose.yml` tal cual: ver [DEPLOYMENT.md](DEPLOYMENT.md), [STAGING.md](STAGING.md) y [PRODUCTION_CHECKLIST.md](PRODUCTION_CHECKLIST.md).
