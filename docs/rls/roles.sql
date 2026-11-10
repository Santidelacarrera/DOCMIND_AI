-- Row-Level Security, phase 1: roles (docs/RLS_DESIGN.md section 4). Run once, as an administrator,
-- on the target database. Passwords come from your secret manager: replace the placeholders, never
-- commit real values. Nothing here changes how the application behaves until phase 3 points
-- DATABASE_URL at docmind_app.

-- Application role: subject to every policy. Never SUPERUSER, never BYPASSRLS.
CREATE ROLE docmind_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD '<from-secret-manager>';
-- Maintenance role for tasks that cross organizations (retention, reconciliation, outbox, stuck-job
-- recovery). Separate credentials; never used by the API request path.
CREATE ROLE docmind_system LOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD '<from-secret-manager>';

GRANT CONNECT ON DATABASE docmind TO docmind_app, docmind_system;
GRANT USAGE ON SCHEMA public TO docmind_app, docmind_system;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO docmind_app, docmind_system;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO docmind_app, docmind_system;
-- Tables created by later migrations (run by the owner role) get the same grants.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO docmind_app, docmind_system;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO docmind_app, docmind_system;

-- Verify (both must return false for docmind_app):
-- SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname IN ('docmind_app', 'docmind_system');
