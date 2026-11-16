"""SaaS plans, document intelligence metadata and query history with RLS.

Revision ID: 0012_saas_plans_intelligence
Revises: 0011_conversations
"""

import sqlalchemy as sa

import app.models  # noqa: F401 -- registers metadata
from alembic import op
from app import rls
from app.db import Base

revision = "0012_saas_plans_intelligence"
down_revision = "0011_conversations"
branch_labels = None
depends_on = None

TABLES = (
    "organization_subscriptions",
    "query_history",
    "billing_transactions",
    "billing_webhook_events",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for val in ("STARTER", "PROFESSIONAL", "BUSINESS"):
                op.execute(f"ALTER TYPE plan ADD VALUE IF NOT EXISTS '{val}'")

    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("documents")}
    to_add = [
        ("size_bytes", sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0")),
        ("category", sa.Column("category", sa.String(length=64), nullable=True)),
        ("tags", sa.Column("tags", sa.JSON(), nullable=False, server_default="[]")),
        ("tables_detected", sa.Column("tables_detected", sa.Integer(), nullable=False, server_default="0")),
        ("protected", sa.Column("protected", sa.Boolean(), nullable=False, server_default=sa.text("false"))),
    ]
    missing_cols = [(name, col) for name, col in to_add if name not in existing_cols]
    if missing_cols:
        with op.batch_alter_table("documents") as batch:
            for _name, col in missing_cols:
                batch.add_column(col)
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("documents")}
    if "ix_documents_category" not in existing_indexes:
        op.create_index("ix_documents_category", "documents", ["category"])

    webhook_cols = {c["name"] for c in inspector.get_columns("webhooks")}
    if "project_ids" not in webhook_cols:
        with op.batch_alter_table("webhooks") as batch:
            batch.add_column(sa.Column("project_ids", sa.JSON(), nullable=True))

    Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables[t] for t in TABLES], checkfirst=True)
    sub_cols = {c["name"] for c in inspector.get_columns("organization_subscriptions")}
    sub_add = [
        ("failed_payment_count", sa.Column("failed_payment_count", sa.Integer(), nullable=False, server_default="0")),
        ("cancel_at_period_end", sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.text("false"))),
        ("trial_ends_at", sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True)),
    ]
    missing_sub = [(name, col) for name, col in sub_add if name not in sub_cols]
    if missing_sub:
        with op.batch_alter_table("organization_subscriptions") as batch:
            for _name, col in missing_sub:
                batch.add_column(col)

    if bind.dialect.name == "postgresql":
        for statement in rls.policy_statements(rls.TENANT_TABLES_0012, {}):
            op.execute(statement)
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'docmind_app') THEN "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON organization_subscriptions, query_history, "
            "billing_transactions, billing_webhook_events TO docmind_app; END IF; "
            "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'docmind_system') THEN "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON organization_subscriptions, query_history, "
            "billing_transactions, billing_webhook_events TO docmind_system; END IF; END $$"
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    for tbl in ("billing_webhook_events", "billing_transactions", "query_history", "organization_subscriptions"):
        if tbl in existing_tables:
            op.drop_table(tbl)
    webhook_cols = {c["name"] for c in inspector.get_columns("webhooks")}
    if "project_ids" in webhook_cols:
        with op.batch_alter_table("webhooks") as batch:
            batch.drop_column("project_ids")
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("documents")}
    if "ix_documents_category" in existing_indexes:
        op.drop_index("ix_documents_category", table_name="documents")
    existing_cols = {c["name"] for c in inspector.get_columns("documents")}
    to_drop = [c for c in ("protected", "tables_detected", "tags", "category", "size_bytes") if c in existing_cols]
    if to_drop:
        with op.batch_alter_table("documents") as batch:
            for col in to_drop:
                batch.drop_column(col)
