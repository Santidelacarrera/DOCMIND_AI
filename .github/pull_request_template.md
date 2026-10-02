## What and why

## How was it tested

- [ ] `ruff check .`, `mypy app`, `pytest` (backend)
- [ ] `npm run lint`, `npm run typecheck`, `npm run build` (frontend)
- [ ] Behaviour, API or configuration changes are reflected in `docs/` and the README

## Security checklist

- [ ] New endpoints enforce authentication, tenant membership and (for API keys) scopes
- [ ] Untrusted input is validated and size-bounded; no secrets are logged or committed
- [ ] Database changes ship with an Alembic migration
