# Contributing

Thanks for helping improve DocMind AI.

## Workflow

1. Branch from `main`: `feat/…`, `fix/…`, `docs/…`, `chore/…`.
2. Keep changes focused; include tests for behaviour changes.
3. Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat(api): …`).
4. Open a pull request; CI must be green before merging.

## Local checks

```bash
# backend (from backend/)
pip install -e '.[dev]'
ruff check . && mypy app && pytest --cov=app

# frontend (from frontend/)
npm ci
npm run lint && npm run typecheck && npm run build
```

The backend test-suite is hermetic (SQLite, in-memory Redis double, local storage,
no Celery broker) and needs no services. Docker-backed integration and Playwright
suites are described in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Rules of thumb

- **Every endpoint** authenticates, validates tenant membership and, for API keys, a scope.
  Add a test proving another tenant gets `403`/`404`.
- Bound every user-controlled size (strings, bodies, JSON values).
- Never log or return secrets, tokens or raw provider errors.
- Schema changes need an Alembic migration (`alembic revision -m "…"`) that is safe on
  existing data.
- Update the README and `docs/` when behaviour, configuration or operations change.
