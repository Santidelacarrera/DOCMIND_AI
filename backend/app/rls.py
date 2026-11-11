"""PostgreSQL Row-Level Security: policies and the per-transaction organization context.

Design: ``docs/RLS_DESIGN.md`` (approved 2026-10-30). Phases:

1. Roles ``docmind_app`` (no SUPERUSER, no BYPASSRLS) and ``docmind_system`` -- ``docs/rls/roles.sql``,
   run by an operator, because creating roles needs privileges a managed database may not grant.
2. **Policies enabled without FORCE** (migration ``0009_rls_policies``) and the context set in every
   transaction (this module). While the application still connects as the table owner, policies do
   not apply to it: behaviour is unchanged, which is the point of this phase.
3. FORCE, and the application connecting as ``docmind_app``. Requires the pre-context lookups of the
   design's section 6 (login, API key, job claim, cross-organization maintenance tasks) and a staging
   soak -- not done yet.

The context is ``app.org_id``, set with ``set_config(..., true)`` (transaction-local, so a pooled
connection never carries it into the next request). With no context every policy matches nothing.
"""

from contextvars import ContextVar
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

CURRENT_ORG: ContextVar[str | None] = ContextVar("rls_org_id", default=None)
_ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"

# Tables that carry organization_id directly.
TENANT_TABLES = (
    "api_keys", "audit_logs", "contextrag_events", "documents", "extraction_runs", "extraction_schemas",
    "invitations", "organization_members", "processing_jobs", "projects", "usage_records",
    "vector_deletions", "webhooks",
)
# Child tables: visible only when their parent row is (the parent is itself filtered).
CHILD_TABLES = {
    "document_pages": ("document_id", "documents"),
    "document_versions": ("document_id", "documents"),
    "extraction_fields": ("extraction_run_id", "extraction_runs"),
    "extraction_schema_versions": ("schema_id", "extraction_schemas"),
    "processing_steps": ("job_id", "processing_jobs"),
    "project_members": ("project_id", "projects"),
    "webhook_deliveries": ("webhook_id", "webhooks"),
}
POLICY = "docmind_tenant_isolation"


def policy_statements() -> list[str]:
    """DDL for phase 2: enable RLS (no FORCE) and one isolation policy per table."""
    out: list[str] = []
    for table in TENANT_TABLES:
        out += [
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"DROP POLICY IF EXISTS {POLICY} ON {table}",
            (f"CREATE POLICY {POLICY} ON {table} USING (organization_id = {_ORG}) "
            f"WITH CHECK (organization_id = {_ORG})"),
        ]
    for table, (fk, parent) in CHILD_TABLES.items():
        # Identifiers come from the constants above, never from input: not an injection vector.
        visible = f"EXISTS (SELECT 1 FROM {parent} p WHERE p.id = {table}.{fk})"  # nosec B608
        out += [
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"DROP POLICY IF EXISTS {POLICY} ON {table}",
            f"CREATE POLICY {POLICY} ON {table} USING ({visible}) WITH CHECK ({visible})",
        ]
    return out


def drop_statements() -> list[str]:
    return [
        stmt
        for table in (*TENANT_TABLES, *CHILD_TABLES)
        for stmt in (f"DROP POLICY IF EXISTS {POLICY} ON {table}", f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    ]


def _is_postgres(conn: Connection | Session) -> bool:
    bind = conn.get_bind() if isinstance(conn, Session) else conn
    return bind.dialect.name == "postgresql"


def set_context(db: Session, organization_id: object) -> None:
    """Scope the rest of this request (and every later transaction of it) to one organization.

    Called with the organization the caller asked for, *before* its membership is read: the
    membership row is itself behind the policy, so a non-member simply finds nothing.
    """
    value = str(organization_id)
    CURRENT_ORG.set(value)
    if _is_postgres(db):
        db.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": value})


def clear_context() -> None:
    CURRENT_ORG.set(None)


def install(session_factory: Any) -> None:
    """Re-apply the context at the start of every transaction of every session.

    ``set_config(..., true)`` ends with the transaction, and one session can run several
    transactions (commit, then more queries): without this the second one would have no context.
    """

    @event.listens_for(session_factory, "after_begin")
    def _apply(session: Session, _transaction: Any, connection: Connection) -> None:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": CURRENT_ORG.get() or ""})
