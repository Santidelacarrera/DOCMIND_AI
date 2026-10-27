-- Prototype only: RLS mechanics checked on a SCRATCH database (PostgreSQL 16.15).
-- It is NOT applied to DOCMIND's schema. Reproduce with:
--   createdb rls_proto && psql -d rls_proto -At -f docs/rls/prototype_checks.sql
-- Results observed on 2026-10-27 (copied into docs/RLS_DESIGN.md):
--   isolation_A_sees|1, no_context_sees|0, after_commit_sees|0,
--   session_set_next_request_sees|1 (SESSION-level SET leaks: forbidden by the design),
--   cross_tenant_update_affected|0, owner_nonsuper_without_context|0, superuser_without_context|2
CREATE ROLE app_user LOGIN PASSWORD 'x';
CREATE ROLE table_owner NOLOGIN;
CREATE TABLE documents (id int primary key, organization_id uuid not null, body text);
ALTER TABLE documents OWNER TO table_owner;
INSERT INTO documents VALUES (1, '11111111-1111-1111-1111-111111111111', 'org A secret');
INSERT INTO documents VALUES (2, '22222222-2222-2222-2222-222222222222', 'org B secret');
GRANT SELECT, UPDATE ON documents TO app_user;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant ON documents
  USING (organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid);

SET ROLE app_user;
BEGIN; SET LOCAL app.org_id = '11111111-1111-1111-1111-111111111111';
SELECT 'isolation_A_sees' AS check, count(*) FROM documents;
COMMIT;
SELECT 'no_context_sees' AS check, count(*) FROM documents;
BEGIN; SET LOCAL app.org_id = '11111111-1111-1111-1111-111111111111'; COMMIT;
SELECT 'after_commit_sees' AS check, count(*) FROM documents;
SET app.org_id = '11111111-1111-1111-1111-111111111111';
SELECT 'session_set_next_request_sees' AS check, count(*) FROM documents;
RESET app.org_id;
BEGIN; SET LOCAL app.org_id = '11111111-1111-1111-1111-111111111111';
UPDATE documents SET body = 'x' WHERE id = 2;
SELECT 'cross_tenant_update_affected' AS check, count(*) FROM documents WHERE body = 'x';
ROLLBACK;
RESET ROLE;
-- A non-superuser owner is filtered when FORCE is set
SET ROLE table_owner;
SELECT 'owner_nonsuper_without_context' AS check, count(*) FROM documents;
RESET ROLE;
-- A superuser bypasses RLS regardless of FORCE
SELECT 'superuser_without_context' AS check, count(*) FROM documents;
