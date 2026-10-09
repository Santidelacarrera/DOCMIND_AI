"""durable job state, idempotent runs, review sign-off and correction trail

- processing_jobs: enqueued_at / started_at / retry_count (recover work from the database alone)
- extraction_runs: job_id (unique: one run per job), reviewed_by / reviewed_at (human sign-off)
- extraction_fields: correction_reason / corrected_by / corrected_at (why a value was changed)
- documents: partial unique index on (project_id, checksum) for live documents, making
  de-duplication atomic

Every step is guarded: on a fresh database 0001's create_all() already built the current
models, so these are no-ops there.

Revision ID: 0004_reliability_review_traceability
Revises: 0003_schemas_review_collab_observability
"""

import logging

import sqlalchemy as sa

from alembic import op

revision = "0004_reliability_review_traceability"
down_revision = "0003_schemas_review_collab_observability"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")


def _columns(inspector: sa.Inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def _add(inspector: sa.Inspector, table: str, column: sa.Column) -> None:  # type: ignore[type-arg]
    if column.name not in _columns(inspector, table):
        op.add_column(table, column)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tz = sa.DateTime(timezone=True)

    _add(inspector, "processing_jobs", sa.Column("enqueued_at", tz, nullable=True))
    _add(inspector, "processing_jobs", sa.Column("started_at", tz, nullable=True))
    _add(
        inspector,
        "processing_jobs",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )

    _add(
        inspector,
        "extraction_runs",
        sa.Column("job_id", sa.Uuid(as_uuid=True), sa.ForeignKey("processing_jobs.id", ondelete="SET NULL")),
    )
    _add(
        inspector,
        "extraction_runs",
        sa.Column("reviewed_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id")),
    )
    _add(inspector, "extraction_runs", sa.Column("reviewed_at", tz, nullable=True))
    # The inspector caches its reflection, so look again now that the column exists.
    fresh = sa.inspect(op.get_bind())
    already_unique = any(
        ix["unique"] and ix["column_names"] == ["job_id"] for ix in fresh.get_indexes("extraction_runs")
    ) or any(u["column_names"] == ["job_id"] for u in fresh.get_unique_constraints("extraction_runs"))
    if not already_unique:
        op.create_index("uq_extraction_runs_job_id", "extraction_runs", ["job_id"], unique=True)

    _add(inspector, "extraction_fields", sa.Column("correction_reason", sa.String(200), nullable=True))
    _add(
        inspector,
        "extraction_fields",
        sa.Column("corrected_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
    )
    _add(inspector, "extraction_fields", sa.Column("corrected_at", tz, nullable=True))

    doc_indexes = {ix["name"] for ix in inspector.get_indexes("documents")}
    if "uq_documents_live_checksum" not in doc_indexes:
        duplicates = op.get_bind().execute(
            sa.text(
                "SELECT COUNT(*) FROM (SELECT 1 FROM documents WHERE deleted_at IS NULL "
                "GROUP BY project_id, checksum HAVING COUNT(*) > 1) d"
            )
        ).scalar()
        if duplicates:
            # Never delete tenant data from a migration. Resolve the duplicates, then re-run.
            log.warning(
                "%s duplicate live documents found; skipping uq_documents_live_checksum "
                "(the application-level check still applies)",
                duplicates,
            )
        else:
            op.create_index(
                "uq_documents_live_checksum",
                "documents",
                ["project_id", "checksum"],
                unique=True,
                postgresql_where=sa.text("deleted_at IS NULL"),
                sqlite_where=sa.text("deleted_at IS NULL"),
            )


def downgrade() -> None:
    # Fresh installs get the uniqueness from the model (an unnamed constraint dropped
    # together with its column), upgraded databases from the named index created above.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "uq_documents_live_checksum" in {ix["name"] for ix in inspector.get_indexes("documents")}:
        op.drop_index("uq_documents_live_checksum", table_name="documents")
    for column in ("corrected_at", "corrected_by", "correction_reason"):
        op.drop_column("extraction_fields", column)
    if "uq_extraction_runs_job_id" in {ix["name"] for ix in inspector.get_indexes("extraction_runs")}:
        op.drop_index("uq_extraction_runs_job_id", table_name="extraction_runs")
    for column in ("reviewed_at", "reviewed_by", "job_id"):
        op.drop_column("extraction_runs", column)
    for column in ("retry_count", "started_at", "enqueued_at"):
        op.drop_column("processing_jobs", column)
