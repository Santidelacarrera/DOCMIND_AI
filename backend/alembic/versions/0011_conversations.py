"""Conversations: private, encrypted per organization, with RLS (ADR-005 delivery 3)

Revision ID: 0011_conversations
Revises: 0010_rls_phase3
"""

import app.models  # noqa: F401 -- registers metadata
from alembic import op
from app import rls
from app.db import Base

revision = "0011_conversations"
down_revision = "0010_rls_phase3"
branch_labels = None
depends_on = None

TABLES = ("conversations", "conversation_messages")


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables[t] for t in TABLES])
    if bind.dialect.name == "postgresql":
        for statement in rls.policy_statements(rls.TENANT_TABLES_0011, {}):
            op.execute(statement)
        # The application role reads and writes these tables like the others (docs/rls/roles.sql grants
        # existing tables only); harmless when the role does not exist yet.
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'docmind_app') THEN "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON conversations, conversation_messages TO docmind_app; END IF; "
            "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'docmind_system') THEN "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON conversations, conversation_messages TO docmind_system; END IF; END $$"
        )


def downgrade() -> None:
    op.drop_table("conversation_messages")
    op.drop_table("conversations")
