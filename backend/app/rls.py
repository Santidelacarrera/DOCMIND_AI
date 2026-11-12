"""PostgreSQL Row-Level Security: policies and the per-transaction organization context.

Design: ``docs/RLS_DESIGN.md`` (approved 2026-10-30). Phases:

1. Roles ``docmind_app`` (no SUPERUSER, no BYPASSRLS) and ``docmind_system`` -- ``docs/rls/roles.sql``,
   run by an operator, because creating roles needs privileges a managed database may not grant.
2. **Policies enabled without FORCE** (migration ``0009_rls_policies``) and the context set in every
   transaction (this module). While the application still connects as the table owner, policies do
   not apply to it: behaviour is unchanged, which is the point of this phase.
3. The application connecting as ``docmind_app`` (``RLS_ENFORCED=true``), so the policies apply to
   it. Pieces (migration ``0010_rls_phase3``): ``SECURITY DEFINER`` lookups for what is read before
   the organization is known (API key, invitation token, job claim) that return only the
   organization id; an ``app.user_id`` context with policies on ``organizations`` and the user's own
   ``organization_members`` rows; and ``DATABASE_SYSTEM_URL`` (``docmind_system``) for the
   cross-organization maintenance tasks (``app.db.SystemSessionLocal``). Activation is a
   configuration change, after the staging soak: see ``docs/RLS_DESIGN.md``.

The contexts are ``app.org_id`` and ``app.user_id``, set with ``set_config(..., true)``
(transaction-local, so a pooled connection never carries them into the next request). With no
context every policy matches nothing.
"""

from contextvars import ContextVar
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

CURRENT_ORG: ContextVar[str | None] = ContextVar("rls_org_id", default=None)
CURRENT_USER: ContextVar[str | None] = ContextVar("rls_user_id", default=None)
_ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
_USER = "NULLIF(current_setting('app.user_id', true), '')::uuid"

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
# Tables created after 0009 (migration 0011_conversations). Kept apart so 0009 never names a table
# that does not exist yet when it runs; ``all_policy_statements`` covers every table.
TENANT_TABLES_0011 = ("conversations", "conversation_messages")


def policy_statements(
    tables: tuple[str, ...] = TENANT_TABLES, children: dict[str, tuple[str, str]] | None = None,
) -> list[str]:
    """DDL for phase 2: enable RLS (no FORCE) and one isolation policy per table."""
    children = CHILD_TABLES if children is None else children
    out: list[str] = []
    for table in tables:
        out += [
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"DROP POLICY IF EXISTS {POLICY} ON {table}",
            (f"CREATE POLICY {POLICY} ON {table} USING (organization_id = {_ORG}) "
            f"WITH CHECK (organization_id = {_ORG})"),
        ]
    for table, (fk, parent) in children.items():
        # Identifiers come from the constants above, never from input: not an injection vector.
        visible = f"EXISTS (SELECT 1 FROM {parent} p WHERE p.id = {table}.{fk})"  # nosec B608
        out += [
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"DROP POLICY IF EXISTS {POLICY} ON {table}",
            f"CREATE POLICY {POLICY} ON {table} USING ({visible}) WITH CHECK ({visible})",
        ]
    return out


def all_policy_statements() -> list[str]:
    """Every table's policy: 0009's and those of later migrations (used to build a schema at once)."""
    return [*policy_statements(), *policy_statements(TENANT_TABLES_0011, {})]


# Phase 3. Lookups made before the organization is known. Each takes one unique, secret or
# server-generated key and returns only the organization id, so the definer's rights cannot be
# used to read anything else. Owned by the migration role (the table owner, which RLS does not
# filter without FORCE).
LOOKUPS = {
    "docmind_api_key_org": ("p_key_hash text", "api_keys", "key_hash = p_key_hash"),
    "docmind_invitation_org": ("p_token_hash text", "invitations", "token_hash = p_token_hash"),
    "docmind_job_org": ("p_job_id uuid", "processing_jobs", "id = p_job_id"),
}
USER_POLICY = "docmind_own_memberships"
# Account events (login, logout, password change) belong to no organization: their audit rows have
# organization_id NULL. The application may write them but, like every other audit row outside its
# context, never read them back; operators read them as docmind_system.
AUDIT_POLICY = (
    f"CREATE POLICY {POLICY} ON audit_logs USING (organization_id = {_ORG}) "
    f"WITH CHECK (organization_id = {_ORG} OR (organization_id IS NULL AND actor_id = {_USER}))"
)
ORG_POLICY = "docmind_visible_organizations"


def phase3_statements() -> list[str]:
    out: list[str] = []
    for name, (argument, table, condition) in LOOKUPS.items():
        out.append(
            f"CREATE OR REPLACE FUNCTION {name}({argument}) RETURNS uuid LANGUAGE sql STABLE "
            f"SECURITY DEFINER SET search_path = pg_catalog, public "
            f"AS $$ SELECT organization_id FROM {table} WHERE {condition} $$"  # nosec B608 - constants
        )
    out += [
        f"DROP POLICY IF EXISTS {POLICY} ON audit_logs",
        AUDIT_POLICY,
        # A user sees their own memberships in every organization (the organization switcher),
        # and nobody else's outside the organization in context.
        f"DROP POLICY IF EXISTS {USER_POLICY} ON organization_members",
        f"CREATE POLICY {USER_POLICY} ON organization_members FOR SELECT USING (user_id = {_USER})",
        "ALTER TABLE organizations ENABLE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {ORG_POLICY} ON organizations",
        (
            f"CREATE POLICY {ORG_POLICY} ON organizations USING (id = {_ORG} OR EXISTS "
            f"(SELECT 1 FROM organization_members m WHERE m.organization_id = organizations.id "
            f"AND m.user_id = {_USER})) WITH CHECK (id = {_ORG})"  # nosec B608 - constants only
        ),
    ]
    return out


def phase3_drop_statements() -> list[str]:
    phase2_audit = next(s for s in policy_statements() if s.startswith(f"CREATE POLICY {POLICY} ON audit_logs "))
    return [
        f"DROP POLICY IF EXISTS {POLICY} ON audit_logs",
        phase2_audit,
        f"DROP POLICY IF EXISTS {ORG_POLICY} ON organizations",
        "ALTER TABLE organizations DISABLE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {USER_POLICY} ON organization_members",
        *(f"DROP FUNCTION IF EXISTS {name}({argument.split()[1]})" for name, (argument, _, _) in LOOKUPS.items()),
    ]


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


def set_user(db: Session, user_id: object) -> None:
    """The authenticated person, for the policies on ``organizations`` and own memberships."""
    value = str(user_id)
    CURRENT_USER.set(value)
    if _is_postgres(db):
        db.execute(text("SELECT set_config('app.user_id', :user, true)"), {"user": value})


def organization_of(db: Session, lookup: str, key: object) -> str | None:
    """Organization behind a key read before any context exists (``LOOKUPS``).

    Only with ``RLS_ENFORCED``: otherwise the application is the table owner, policies do not apply
    to it, the functions may not exist yet (schema older than migration 0010), and the caller
    reads the row directly as before. Returns None in that case.
    """
    from app.core import settings

    if lookup not in LOOKUPS or not settings().rls_enforced or not _is_postgres(db):
        return None
    found = db.scalar(text(f"SELECT {lookup}(:key)"), {"key": key})  # nosec B608 - name from LOOKUPS
    return str(found) if found else None


def role_problems(db: Session) -> list[str]:
    """Why the current connection would bypass the policies (empty when it is safe).

    PostgreSQL never applies RLS to superusers, to roles with BYPASSRLS, or (without FORCE) to
    the table owner. With ``RLS_ENFORCED`` the API reports not-ready on any of them.
    """
    if not _is_postgres(db):
        return ["not PostgreSQL: no row-level security"]
    row = db.execute(
        text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).one()
    owned = db.scalar(
        text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner = current_user")
    )
    problems = []
    if row.rolsuper:
        problems.append("role is SUPERUSER")
    if row.rolbypassrls:
        problems.append("role has BYPASSRLS")
    if owned:
        problems.append(f"role owns {owned} tables")
    return problems


def clear_context() -> None:
    CURRENT_ORG.set(None)
    CURRENT_USER.set(None)


def install(session_factory: Any) -> None:
    """Re-apply the context at the start of every transaction of every session.

    ``set_config(..., true)`` ends with the transaction, and one session can run several
    transactions (commit, then more queries): without this the second one would have no context.
    """

    @event.listens_for(session_factory, "after_begin")
    def _apply(session: Session, _transaction: Any, connection: Connection) -> None:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT set_config('app.org_id', :org, true), set_config('app.user_id', :user, true)"),
                {"org": CURRENT_ORG.get() or "", "user": CURRENT_USER.get() or ""},
            )
