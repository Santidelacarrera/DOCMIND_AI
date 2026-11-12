"""Row-Level Security, phase 3 prerequisites (docs/RLS_DESIGN.md, app/rls.py).

Adds the SECURITY DEFINER lookups used before the organization is known (API key, invitation
token, job claim), the policy that lets a user see their own memberships, and RLS on
``organizations``. Still no FORCE: while the application connects as the table owner nothing
changes. The policies apply once it connects as ``docmind_app`` (``RLS_ENFORCED=true``).

Idempotent. PostgreSQL only; a no-op elsewhere. Downgrade removes exactly what upgrade adds; no
data is touched in either direction.
"""

from alembic import op
from app import rls

revision = "0010_rls_phase3"
down_revision = "0009_rls_policies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in rls.phase3_statements():
        op.execute(statement)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in rls.phase3_drop_statements():
        op.execute(statement)
