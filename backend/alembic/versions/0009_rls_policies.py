"""Row-Level Security, phase 2: policies enabled without FORCE (docs/RLS_DESIGN.md, app/rls.py).

No behaviour change while the application connects as the table owner: without FORCE, PostgreSQL does
not apply policies to the owner. They apply to any other role (``docmind_app`` from
``docs/rls/roles.sql``), which is what ``tests/test_rls_policies.py`` verifies on the real schema.

Idempotent (``DROP POLICY IF EXISTS`` before each create). PostgreSQL only; a no-op elsewhere.
Downgrade drops the policies and disables RLS: no data is touched in either direction.
"""

from alembic import op
from app import rls

revision = "0009_rls_policies"
down_revision = "0008_project_authorization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in rls.policy_statements():
        op.execute(statement)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in rls.drop_statements():
        op.execute(statement)
