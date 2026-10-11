"""vector index state on documents and the vector_deletions outbox

Revision ID: 0005_vector_index_state
Revises: 0004_reliability_review_traceability
"""

import sqlalchemy as sa

import app.models  # noqa: F401 -- registers metadata
from alembic import op
from app.db import Base

revision = "0005_vector_index_state"
down_revision = "0004_reliability_review_traceability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("documents")}
    if "indexed_at" not in columns:
        op.add_column("documents", sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True))
    if "index_error" not in columns:
        op.add_column("documents", sa.Column("index_error", sa.String(80), nullable=True))
    # create_all skips tables that already exist (fresh installs built them in 0001).
    Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables["vector_deletions"]])


def downgrade() -> None:
    op.drop_table("vector_deletions")
    op.drop_column("documents", "index_error")
    op.drop_column("documents", "indexed_at")
