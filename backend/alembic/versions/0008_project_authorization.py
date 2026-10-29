"""Per-project authorization: project_members and api_keys.project_ids

Idempotent: each step checks the live schema first, so re-running after a partial failure is safe.

Rollback caveat: ``downgrade`` drops ``api_keys.project_ids``. Old code reads keys as organization-wide,
so after a downgrade every key that was limited to some projects becomes organization-wide again. Before
downgrading, revoke the project-limited keys or accept that widening. ``project_members`` grants are lost
on downgrade; recreate them from the audit log (``project.member.grant``) if you roll forward again.
"""

import sqlalchemy as sa

from alembic import op
from app.db import Base

revision = "0008_project_authorization"
down_revision = "0007_contextrag_outbox"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    if "project_ids" not in {c["name"] for c in _inspector().get_columns("api_keys")}:
        op.add_column("api_keys", sa.Column("project_ids", sa.JSON(), nullable=True))
    # create_all with checkfirst: creates the table and its indexes only when they are missing.
    Base.metadata.create_all(bind=op.get_bind(), tables=[Base.metadata.tables["project_members"]])


def downgrade() -> None:
    if _inspector().has_table("project_members"):
        op.drop_table("project_members")
    if "project_ids" in {c["name"] for c in _inspector().get_columns("api_keys")}:
        op.drop_column("api_keys", "project_ids")
