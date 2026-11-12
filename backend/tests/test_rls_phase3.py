"""RLS phase 3 (migration 0010, app/rls.py): what changes when the app connects as docmind_app.

Sessions here are the application's own (``app_db.SessionLocal``), on purpose: conftest swaps a test
module's ``SessionLocal`` name for docmind_system, and these tests are about what docmind_app sees.

The rest of the suite proves the product still works that way (CI job backend-postgres-rls). This
file proves the policies are really in force there, that the pre-context lookups return only an
organization id, and that /ready refuses a connection that would bypass the policies.
"""

from typing import Any

import pytest
from sqlalchemy import text

from app import db as app_db
from app import rls
from app.core import settings
from app.db import engine
from tests.conftest import RLS_ENFORCED

postgres = pytest.mark.skipif(engine.dialect.name != "postgresql", reason="Row-Level Security is PostgreSQL only")
enforced = pytest.mark.skipif(not RLS_ENFORCED, reason="needs TEST_RLS_ENFORCED=1 (CI job backend-postgres-rls)")


def _two_orgs(register: Any) -> tuple[Any, Any]:
    a, b = register("alpha"), register("beta")
    for account in (a, b):
        assert account.upload(account.project()).status_code == 202
    return a, b


def test_role_problems_outside_postgres_is_explicit() -> None:
    if engine.dialect.name == "postgresql":
        pytest.skip("SQLite only")
    with app_db.SessionLocal() as db:
        assert rls.role_problems(db) == ["not PostgreSQL: no row-level security"]
        assert rls.organization_of(db, "docmind_job_org", "anything") is None


def test_unknown_lookup_is_refused() -> None:
    with app_db.SessionLocal() as db:
        assert rls.organization_of(db, "pg_read_file", "/etc/passwd") is None


@postgres
@enforced
def test_app_role_is_safe_and_ready(client: Any) -> None:
    with app_db.SessionLocal() as db:
        assert db.scalar(text("SELECT current_user")) == "docmind_app"
        assert rls.role_problems(db) == []
    assert client.get("/ready").status_code == 200


@postgres
@pytest.mark.skipif(RLS_ENFORCED, reason="the app role is docmind_app here")
def test_ready_refuses_an_owner_or_superuser_connection(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with app_db.SessionLocal() as db:
        problems = rls.role_problems(db)
    assert problems, "the CI owner role must be reported (SUPERUSER and/or owns tables)"
    monkeypatch.setattr(settings(), "rls_enforced", True)
    response = client.get("/ready")
    assert response.status_code == 503 and response.json()["detail"] == "RLS_ROLE_UNSAFE"


@postgres
@enforced
def test_policies_hide_every_row_without_context(register: Any) -> None:
    _two_orgs(register)
    with app_db.SessionLocal() as db:
        for table in ("documents", "projects", "organization_members", "organizations", "processing_jobs", "audit_logs"):
            assert db.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table  # nosec B608 - constants


@postgres
@enforced
def test_context_shows_one_organization_only(register: Any) -> None:
    a, b = _two_orgs(register)
    with app_db.SessionLocal() as db:
        rls.set_context(db, a.org)
        orgs = {str(row) for row in db.scalars(text("SELECT organization_id FROM documents"))}
        assert orgs == {a.org}
        assert db.scalar(text("SELECT count(*) FROM organizations")) == 1
        with pytest.raises(Exception, match="row-level security"):
            db.execute(
                text("INSERT INTO projects (id, organization_id, name, created_at) "
                     "VALUES (gen_random_uuid(), :org, 'smuggled', now())"),
                {"org": b.org},
            )
    rls.clear_context()


@postgres
@enforced
def test_user_context_shows_own_memberships_in_every_organization(register: Any, client: Any) -> None:
    a, b = _two_orgs(register)
    created = client.post("/api/v1/organizations?name=second", headers=a.headers)
    assert created.status_code == 201, created.text
    listed = client.get("/api/v1/organizations", headers=a.headers).json()
    assert {o["id"] for o in listed} == {a.org, created.json()["id"]}
    assert b.org not in {o["id"] for o in listed}
    with app_db.SessionLocal() as db:
        me = db.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": a.email})
        rls.set_user(db, me)
        # Own rows across organizations, nobody else's.
        members = db.execute(text("SELECT user_id, organization_id FROM organization_members")).all()
        assert {str(row.user_id) for row in members} == {str(me)}
        assert len(members) == 2
    rls.clear_context()


@postgres
@enforced
def test_lookups_return_only_the_organization(register: Any, client: Any) -> None:
    a, _ = _two_orgs(register)
    with app_db.SessionLocal() as db:
        job = db.scalar(text("SELECT docmind_job_org(gen_random_uuid())"))
        assert job is None  # unknown key: nothing, and no error
        functions = db.execute(text(
            "SELECT proname, prosecdef, pg_get_function_result(oid) AS result FROM pg_proc "
            "WHERE proname LIKE 'docmind\\_%'"
        )).all()
    assert {f.proname for f in functions} == set(rls.LOOKUPS)
    assert all(f.prosecdef and f.result == "uuid" for f in functions)
    created = client.post(
        f"/api/v1/api-keys?organization_id={a.org}&name=ci", json=["documents:read"], headers=a.headers
    )
    assert created.status_code == 201, created.text
    key = {"Authorization": f"Bearer {created.json()['key']}"}
    # The key row is invisible until its organization is the context: this works only through
    # the docmind_api_key_org lookup.
    listed = client.get(f"/api/v1/documents?organization_id={a.org}", headers=key)
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 1
