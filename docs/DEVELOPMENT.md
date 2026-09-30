# Development

Copy `.env.example` to `.env`, set a unique `POSTGRES_PASSWORD` and `JWT_SECRET`, then run `docker compose up --build`.

Run migrations with `docker compose exec api alembic upgrade head`. Local host PostgreSQL driver loading may be blocked by Windows application policy; run integration tests in Docker in that case.

Frontend: `cd frontend && npm ci && npm run lint && npm run typecheck && npm run build`.
Backend: `cd backend && pip install -e '.[dev]' && ruff check . && pytest`.
