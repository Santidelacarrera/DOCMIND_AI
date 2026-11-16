"""Retire contextrag_events table (ContextRAG Sync retirement Phase 3, authorized by owner).

Revision ID: 0013_drop_contextrag_events
Revises: 0012_saas_plans_intelligence
"""

import sqlalchemy as sa

from alembic import op
from app import rls

revision = "0013_drop_contextrag_events"
down_revision = "0012_saas_plans_intelligence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "contextrag_events" in sa.inspect(bind).get_table_names():
        op.drop_table("contextrag_events")


def downgrade() -> None:
    bind = op.get_bind()
    if "contextrag_events" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "contextrag_events",
            sa.Column("id", sa.UUID(), primary_key=True),
            sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
            sa.Column("document_id", sa.UUID(), nullable=False, index=True),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("dedupe_key", sa.String(160), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, index=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("claim_token", sa.String(36), nullable=True),
            sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.String(80), nullable=True),
            sa.Column("correlation_id", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("dedupe_key", name="uq_contextrag_event_dedupe"),
        )
    if bind.dialect.name == "postgresql":
        for statement in rls.policy_statements(("contextrag_events",), {}):
            op.execute(statement)
