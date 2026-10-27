"""ContextRAG outbox (contextrag_events)

Revision ID: 0007_contextrag_outbox
Revises: 0006_field_evidence
"""

import app.models  # noqa: F401 -- registers metadata
from alembic import op
from app.db import Base

revision = "0007_contextrag_outbox"
down_revision = "0006_field_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=[Base.metadata.tables["contextrag_events"]])


def downgrade() -> None:
    op.drop_table("contextrag_events")
