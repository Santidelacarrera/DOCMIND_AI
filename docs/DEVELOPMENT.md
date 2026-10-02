# Development

## Full stack

```bash
cp .env.example .env       # local-only defaults; works as-is
docker compose up --build  # migrations, api, worker, scheduler, web, db, redis
```

The API and worker mount `./backend` and reload on change. `NEXT_PUBLIC_API_URL` is inlined into the web bundle at **build** time: rebuild the web image after changing it.

## Backend without Docker

```bash
cd backend
pip install -e '.[dev]'
ruff check . && mypy app && pytest --cov=app
```

The suite is hermetic (SQLite, in-memory Redis double, temporary local storage, no broker). Windows application-control policies can block the PostgreSQL driver; this does not affect the suite, and the Docker integration tests cover PostgreSQL.

## Docker integration and browser tests

```bash
cd backend
DOCMIND_INTEGRATION=1 pytest tests/test_docker_integration.py   # needs the compose stack running

cd ../frontend
npm ci && npx playwright install chromium
npm run test:e2e   # E2E_BASE_URL (default :3000) and E2E_API_URL (default :8000)
```

The integration fixture flushes Redis DB 0 between tests: run it only against a disposable stack (use `COMPOSE_PROJECT_NAME` to target a separate project). Rate limits are low by default; raise `RATE_LIMIT_*` when running the browser suite repeatedly.

## Migrations

```bash
docker compose exec api alembic revision --autogenerate -m "describe change"
docker compose exec api alembic upgrade head
```

`0001_initial` creates the schema from the models of its time; every later change must be an explicit, data-safe migration.

## Frontend

```bash
cd frontend && npm ci && npm run lint && npm run typecheck && npm run build
```
