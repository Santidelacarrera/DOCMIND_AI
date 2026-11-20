"""Add progressive conversation summary, encrypted user semantic memory, and answer feedback tables.

Revision ID: 0014_hybrid_memory_and_feedback
Revises: 0013_drop_contextrag_events
"""

import sqlalchemy as sa

from alembic import op
from app import rls

revision = "0014_hybrid_memory_and_feedback"
down_revision = "0013_drop_contextrag_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "conversations" in tables:
        cols = {c["name"] for c in inspector.get_columns("conversations")}
        if "summary_payload" not in cols:
            op.add_column("conversations", sa.Column("summary_payload", sa.LargeBinary(), nullable=True))
        if "summary_upto_seq" not in cols:
            op.add_column(
                "conversations",
                sa.Column("summary_upto_seq", sa.Integer(), nullable=False, server_default="0"),
            )

    if "user_semantic_memories" not in tables:
        op.create_table(
            "user_semantic_memories",
            sa.Column("id", sa.UUID(), primary_key=True),
            sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True),
            sa.Column("category", sa.String(32), nullable=False, server_default="confirmed_fact"),
            sa.Column("payload", sa.LargeBinary(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0.85"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if "answer_feedback" not in tables:
        op.create_table(
            "answer_feedback",
            sa.Column("id", sa.UUID(), primary_key=True),
            sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("conversation_id", sa.UUID(), sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("turn_seq", sa.Integer(), nullable=True),
            sa.Column("question", sa.String(500), nullable=False),
            sa.Column("answer", sa.Text(), nullable=False),
            sa.Column("corrected_answer", sa.Text(), nullable=True),
            sa.Column("rating", sa.String(16), nullable=False, index=True),
            sa.Column("cited_document_ids", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        )

    if bind.dialect.name == "postgresql":
        for statement in rls.policy_statements(rls.TENANT_TABLES_0014, {}):
            op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "answer_feedback" in tables:
        op.drop_table("answer_feedback")
    if "user_semantic_memories" in tables:
        op.drop_table("user_semantic_memories")
    if "conversations" in tables:
        cols = {c["name"] for c in sa.inspect(bind).get_columns("conversations")}
        if "summary_upto_seq" in cols:
            op.drop_column("conversations", "summary_upto_seq")
        if "summary_payload" in cols:
            op.drop_column("conversations", "summary_payload")
