"""granular schema selection, confidence/validation, invitations, webhooks

Adds the columns and tables behind four features:
- Pinning the schema version a user picked at upload/reprocess time
  (processing_jobs.requested_schema_version_id).
- Business-rule validation findings alongside each extraction run
  (extraction_runs.validation_issues / requires_review).
- Email invitations (invitations table).
- Outbound webhooks (webhooks, webhook_deliveries tables).

Revision ID: 0003_schemas_review_collab_observability
Revises: 0002_session_version
"""

import sqlalchemy as sa

import app.models  # noqa: F401 -- registers metadata for the create_all below
from alembic import op
from app.db import Base

revision = "0003_schemas_review_collab_observability"
down_revision = "0002_session_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "processing_jobs",
        sa.Column("requested_schema_version_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_processing_jobs_requested_schema_version_id",
        "processing_jobs",
        ["requested_schema_version_id"],
    )
    op.create_foreign_key(
        "fk_processing_jobs_requested_schema_version_id",
        "processing_jobs",
        "extraction_schema_versions",
        ["requested_schema_version_id"],
        ["id"],
    )

    op.add_column(
        "extraction_runs",
        sa.Column("validation_issues", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "extraction_runs",
        sa.Column("requires_review", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_extraction_runs_requires_review", "extraction_runs", ["requires_review"])
    op.alter_column("extraction_runs", "validation_issues", server_default=None)
    op.alter_column("extraction_runs", "requires_review", server_default=None)

    # New tables (invitations, webhooks, webhook_deliveries): create_all skips any
    # table that already exists, so this only adds what's missing.
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[
            Base.metadata.tables["invitations"],
            Base.metadata.tables["webhooks"],
            Base.metadata.tables["webhook_deliveries"],
        ],
    )


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
    op.drop_table("webhooks")
    op.drop_table("invitations")
    op.drop_index("ix_extraction_runs_requires_review", table_name="extraction_runs")
    op.drop_column("extraction_runs", "requires_review")
    op.drop_column("extraction_runs", "validation_issues")
    op.drop_constraint(
        "fk_processing_jobs_requested_schema_version_id", "processing_jobs", type_="foreignkey"
    )
    op.drop_index("ix_processing_jobs_requested_schema_version_id", table_name="processing_jobs")
    op.drop_column("processing_jobs", "requested_schema_version_id")
