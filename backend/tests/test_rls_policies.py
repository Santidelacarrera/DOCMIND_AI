"""Row-Level Security policies (migration 0009, app/rls.py) on the real schema, PostgreSQL only.

Data for two organizations is created through the API and the real worker; then every query runs as a
role without SUPERUSER or BYPASSRLS (``SET LOCAL ROLE``), which is how ``docmind_app`` will see the
database in phase 3. Skipped on SQLite; CI's ``backend-postgres`` job runs it on PostgreSQL 17.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text

from app import rls, worker
from app.core import settings
from app.db import SessionLocal, engine
from app.evaluation.pdfgen import digital_pdf

pytestmark = [
    pytest.mark.skipif(engine.dialect.name != "postgresql", reason="Row-Level Security is PostgreSQL only"),
    # Under TEST_RLS_ENFORCED the whole suite already runs as docmind_app, which cannot create the
    # probe role; these checks run in the backend-postgres job, where the app connects as owner.
    pytest.mark.skipif(settings().rls_enforced, reason="covered by the docmind_app run of the whole suite"),
]

PROBE = "docmind_rls_probe"
PROTECTED = (*rls.TENANT_TABLES, *rls.CHILD_TABLES)


@pytest.fixture(scope="module", autouse=True)
def _probe_role() -> Iterator[None]:
    with engine.begin() as conn:
        exists = conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": PROBE})
        if not exists:
            conn.execute(text(f"CREATE ROLE {PROBE} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
    yield
    with engine.begin() as conn:
        conn.execute(text(f"DROP OWNED BY {PROBE}"))
        conn.execute(text(f"DROP ROLE IF EXISTS {PROBE}"))


@pytest.fixture
def worlds(register: Any) -> dict[str, Any]:
    """Two organizations, each with a processed document (pages, run, fields, steps, audit rows)."""
    out: dict[str, Any] = {}
    for label in ("a", "b"):
        acct = register(f"rls-{label}")
        body = acct.upload(acct.project(), digital_pdf([[f"Invoice Number: INV-{label}", "Total Due: $10.00"]])).json()
        worker.process_document.run(body["job_id"])  # type: ignore[attr-defined]
        out[label] = {"org": acct.org, "document": body["document_id"]}
    with engine.begin() as conn:
        for statement in rls.policy_statements():
            conn.execute(text(statement))
        conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {PROBE}"))
        conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {PROBE}"))
    return out


def as_app(org: str | None, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    """Run ``sql`` the way the application role will: no superuser, context set like app.rls does."""
    with engine.connect() as conn, conn.begin():
        conn.execute(text(f"SET LOCAL ROLE {PROBE}"))
        conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": org or ""})
        return list(conn.execute(text(sql), params or {}).all())


def count_as_owner(sql: str, params: dict[str, Any]) -> int:
    with engine.connect() as conn:
        return int(conn.scalar(text(sql), params) or 0)


def test_the_probe_role_is_neither_superuser_nor_bypassrls() -> None:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :r"), {"r": PROBE}).one()
    assert tuple(row) == (False, False)


def test_every_protected_table_has_rls_enabled_and_a_policy(worlds: dict[str, Any]) -> None:
    with engine.connect() as conn:
        enabled = {r[0] for r in conn.execute(text("SELECT relname FROM pg_class WHERE relrowsecurity"))}
        policies = {r[0] for r in conn.execute(text("SELECT tablename FROM pg_policies WHERE policyname = :p"), {"p": rls.POLICY})}
    assert set(PROTECTED) <= enabled and set(PROTECTED) <= policies


@pytest.mark.parametrize("table", rls.TENANT_TABLES)
def test_a_tenant_table_shows_only_the_context_organization(worlds: dict[str, Any], table: str) -> None:
    a, b = worlds["a"]["org"], worlds["b"]["org"]
    seen = {str(r[0]) for r in as_app(a, f"SELECT DISTINCT organization_id FROM {table}")}
    assert seen <= {a}
    assert as_app(a, f"SELECT count(*) FROM {table} WHERE organization_id = :b", {"b": b}) == [(0,)]


@pytest.mark.parametrize(("table", "fk", "parent"), [(t, fk, p) for t, (fk, p) in rls.CHILD_TABLES.items()])
def test_a_child_table_shows_only_rows_of_visible_parents(worlds: dict[str, Any], table: str, fk: str, parent: str) -> None:
    a = worlds["a"]["org"]
    sql = f"SELECT count(*) FROM {table} c JOIN {parent} p ON p.id = c.{fk} WHERE p.organization_id = :o"
    own = count_as_owner(sql, {"o": a})
    total = count_as_owner(f"SELECT count(*) FROM {table}", {})
    assert as_app(a, f"SELECT count(*) FROM {table}") == [(own,)]
    if table in ("document_pages", "extraction_fields", "processing_steps", "document_versions"):
        assert own > 0 and total > own  # both organizations have rows: the filter really removed some


def test_the_organizations_documents_are_the_ones_its_context_sees(worlds: dict[str, Any]) -> None:
    a, b = worlds["a"], worlds["b"]
    assert {str(r[0]) for r in as_app(a["org"], "SELECT id FROM documents")} == {a["document"]}
    assert {str(r[0]) for r in as_app(b["org"], "SELECT id FROM documents")} == {b["document"]}


@pytest.mark.parametrize("table", PROTECTED)
def test_without_context_every_protected_table_is_empty(worlds: dict[str, Any], table: str) -> None:
    assert as_app(None, f"SELECT count(*) FROM {table}") == [(0,)]


def test_cross_tenant_update_and_delete_touch_nothing(worlds: dict[str, Any]) -> None:
    a, b = worlds["a"]["org"], worlds["b"]
    with engine.connect() as conn, conn.begin():
        conn.execute(text(f"SET LOCAL ROLE {PROBE}"))
        conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": a})
        assert conn.execute(text("UPDATE documents SET filename = 'x' WHERE id = :d"), {"d": b["document"]}).rowcount == 0
        assert conn.execute(text("DELETE FROM extraction_fields")).rowcount >= 0
    # B's fields are intact: the DELETE above could only reach A's rows.
    assert count_as_owner(
        "SELECT count(*) FROM extraction_fields f JOIN extraction_runs r ON r.id = f.extraction_run_id "
        "WHERE r.organization_id = :o", {"o": b["org"]},
    ) > 0


def test_cross_tenant_insert_is_rejected_by_with_check(worlds: dict[str, Any]) -> None:
    a, b = worlds["a"]["org"], worlds["b"]["org"]
    with pytest.raises(Exception, match="row-level security"):
        as_app(a, "INSERT INTO projects (id, organization_id, name, created_at, updated_at) "
                  "VALUES (:i, :o, 'intruder', now(), now()) RETURNING id", {"i": str(uuid.uuid4()), "o": b})
    # The same insert for its own organization is allowed.
    assert len(as_app(a, "INSERT INTO projects (id, organization_id, name, created_at, updated_at) "
                         "VALUES (:i, :o, 'own', now(), now()) RETURNING id", {"i": str(uuid.uuid4()), "o": a})) == 1


def test_the_context_does_not_survive_the_transaction_on_a_pooled_connection(worlds: dict[str, Any]) -> None:
    a = worlds["a"]["org"]
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(text(f"SET LOCAL ROLE {PROBE}"))
            conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": a})
            assert conn.scalar(text("SELECT count(*) FROM documents")) == 1
        with conn.begin():  # same connection, next transaction, nothing set
            conn.execute(text(f"SET LOCAL ROLE {PROBE}"))
            assert conn.scalar(text("SELECT count(*) FROM documents")) == 0


def test_the_session_listener_reapplies_the_context_in_every_transaction(worlds: dict[str, Any]) -> None:
    a = worlds["a"]["org"]
    token = rls.CURRENT_ORG.set(a)
    try:
        with SessionLocal() as session:
            assert session.scalar(text("SELECT current_setting('app.org_id', true)")) == a
            session.commit()  # ends the transaction and with it the transaction-local setting
            assert session.scalar(text("SELECT current_setting('app.org_id', true)")) == a
    finally:
        rls.CURRENT_ORG.reset(token)
    with SessionLocal() as session:
        assert session.scalar(text("SELECT current_setting('app.org_id', true)")) in ("", None)
