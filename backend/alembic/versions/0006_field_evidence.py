"""evidence status on extraction fields (provenance)

Revision ID: 0006_field_evidence
Revises: 0005_vector_index_state
"""

import sqlalchemy as sa

from alembic import op

revision = "0006_field_evidence"
down_revision = "0005_vector_index_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("extraction_fields")}
    if "evidence_status" not in columns:
        op.add_column("extraction_fields", sa.Column("evidence_status", sa.String(24), nullable=True))


def downgrade() -> None:
    op.drop_column("extraction_fields", "evidence_status")
