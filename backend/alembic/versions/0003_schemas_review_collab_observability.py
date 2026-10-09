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
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # On a brand-new database, 0001_initial's create_all() already builds the
    # schema from the *current* app.models -- which, after this revision
    # merged, already defines every column/table below. So on a fresh install
    # these additions are no-ops (the same reason 0002 is a plain `pass`),
    # while a database genuinely upgrading from 0002 still needs them applied
    # for real. Guard each one on whether it already exists, rather than
    # assuming either case.
    job_columns = {c["name"] for c in inspector.get_columns("processing_jobs")}
    if "requested_schema_version_id" not in job_columns:
        op.add_column(
            "processing_jobs",
            sa.Column("requested_schema_version_id", sa.Uuid(as_uuid=True), nullable=True),
        )
    job_indexes = {ix["name"] for ix in inspector.get_indexes("processing_jobs")}
    if "ix_processing_jobs_requested_schema_version_id" not in job_indexes:
        op.create_index(
            "ix_processing_jobs_requested_schema_version_id",
            "processing_jobs",
            ["requested_schema_version_id"],
        )
    job_fks = {fk["name"] for fk in inspector.get_foreign_keys("processing_jobs")}
    if "fk_processing_jobs_requested_schema_version_id" not in job_fks:
        op.create_foreign_key(
            "fk_processing_jobs_requested_schema_version_id",
            "processing_jobs",
            "extraction_schema_versions",
            ["requested_schema_version_id"],
            ["id"],
        )

    run_columns = {c["name"] for c in inspector.get_columns("extraction_runs")}
    if "validation_issues" not in run_columns:
        op.add_column(
            "extraction_runs",
            sa.Column("validation_issues", sa.JSON(), nullable=False, server_default="[]"),
        )
        op.alter_column("extraction_runs", "validation_issues", server_default=None)
    if "requires_review" not in run_columns:
        op.add_column(
            "extraction_runs",
            sa.Column("requires_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.alter_column("extraction_runs", "requires_review", server_default=None)
    run_indexes = {ix["name"] for ix in inspector.get_indexes("extraction_runs")}
    if "ix_extraction_runs_requires_review" not in run_indexes:
        op.create_index("ix_extraction_runs_requires_review", "extraction_runs", ["requires_review"])

    # New tables (invitations, webhooks, webhook_deliveries): create_all skips any
    # table that already exists, so this only adds what's missing.
    Base.metadata.create_all(
        bind=bind,
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
