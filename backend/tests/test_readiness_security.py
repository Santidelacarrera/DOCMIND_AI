"""Bypass attempts from the final readiness audit (docs/FINAL_PRODUCTION_READINESS.md), each one a
regression test for a gap that was found and closed:

* new versions bypassed the FREE plan's page quota;
* a conversation kept quoting documents that were later deleted, or whose case the user lost access to;
* conversations must survive signing out and in again, and stay the user's own.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import delete, select

from app.core import settings
from app.db import SessionLocal
from app.evaluation.pdfgen import digital_pdf
from app.models import OrganizationMember, ProjectMember, Role
from app.routers.conversations import WITHDRAWN
from tests.conftest import PASSWORD
from tests.test_case_query import ALPHA_TEXT, QUESTION, env, ingest  # noqa: F401  (env is a fixture)

pytest.importorskip("qdrant_client")


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    monkeypatch.setattr(settings(), "conversation_encryption_key", SecretStr("readiness-test-secret-0123456789abcdef"))


def _uid(client: Any, acct: Any) -> uuid.UUID:
    return uuid.UUID(client.get("/api/v1/auth/me", headers=acct.headers).json()["id"])


def _ask(client: Any, acct: Any, conv: str, org: str | None = None) -> Any:
    return client.post(f"/api/v1/conversations/{conv}/ask?organization_id={org or acct.org}",
                       json={"question": QUESTION}, headers=acct.headers)


def _turns(client: Any, acct: Any, conv: str, org: str | None = None) -> list[dict[str, Any]]:
    response = client.get(f"/api/v1/conversations/{conv}?organization_id={org or acct.org}", headers=acct.headers)
    assert response.status_code == 200, response.text
    return list(response.json()["turns"])


def test_new_versions_count_against_the_free_page_quota(register: Any, client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    acct = register("quota")
    project = acct.project("q")
    upload = acct.upload(project, digital_pdf([["Pagina uno."], ["Pagina dos."]]), "q.pdf")
    assert upload.status_code == 202, upload.text
    monkeypatch.setattr(settings(), "free_pages_per_month", 3)  # 2 used; a 2-page version would make 4
    version = client.post(
        f"/api/v1/documents/{upload.json()['document_id']}/versions?organization_id={acct.org}",
        files={"file": ("q2.pdf", digital_pdf([["Otra uno."], ["Otra dos."]]), "application/pdf")},
        headers=acct.headers,
    )
    assert version.status_code in (402, 409), version.text  # 409 if the first job is still queued
    if version.status_code == 409:
        from app import worker

        worker.process_document.run(upload.json()["job_id"])  # type: ignore[attr-defined]
        version = client.post(
            f"/api/v1/documents/{upload.json()['document_id']}/versions?organization_id={acct.org}",
            files={"file": ("q2.pdf", digital_pdf([["Otra uno."], ["Otra dos."]]), "application/pdf")},
            headers=acct.headers,
        )
    assert version.status_code == 402 and version.json()["detail"] == "QUOTA_EXCEEDED"


def test_answers_quoting_a_deleted_document_are_withdrawn(register: Any, client: Any, env: Any) -> None:  # noqa: F811
    acct, project, doc = ingest(register, env, ALPHA_TEXT)
    conv = client.post(f"/api/v1/conversations?organization_id={acct.org}", json={"project_id": project},
                       headers=acct.headers).json()["id"]
    assert "1200" in _ask(client, acct, conv).json()["answer"]
    assert client.delete(f"/api/v1/documents/{doc}?organization_id={acct.org}", headers=acct.headers).status_code == 204
    turns = _turns(client, acct, conv)
    assistant = turns[1]
    assert assistant["withdrawn"] is True and assistant["text"] == WITHDRAWN and assistant["citations"] == []
    assert all("1200" not in str(t) for t in turns)


def test_answers_from_a_case_the_user_lost_access_to_are_withdrawn(register: Any, client: Any, env: Any) -> None:  # noqa: F811
    owner, project, _ = ingest(register, env, ALPHA_TEXT)
    member = register("member")
    member_id = _uid(client, member)
    with SessionLocal() as db:
        db.add(OrganizationMember(organization_id=uuid.UUID(owner.org), user_id=member_id, role=Role.member))
        db.add(ProjectMember(project_id=uuid.UUID(project), user_id=member_id, access="read"))
        db.commit()
    conv = client.post(f"/api/v1/conversations?organization_id={owner.org}", json={"project_id": None},
                       headers=member.headers).json()["id"]
    first = _ask(client, member, conv, owner.org)
    assert first.status_code == 200 and "1200" in first.json()["answer"]
    with SessionLocal() as db:
        db.execute(delete(ProjectMember).where(ProjectMember.user_id == member_id))
        db.commit()
    turns = _turns(client, member, conv, owner.org)
    assert turns[1]["withdrawn"] is True and all("1200" not in str(t) for t in turns)
    # And the withdrawn turn is not fed back as memory: a new question cannot see the case either.
    again = _ask(client, member, conv, owner.org).json()
    assert again["abstained"] is True and "1200" not in again["answer"]


def test_conversations_survive_signing_out_and_in_and_stay_private(register: Any, client: Any, env: Any) -> None:  # noqa: F811
    acct, project, _ = ingest(register, env, ALPHA_TEXT)
    conv = client.post(f"/api/v1/conversations?organization_id={acct.org}", json={"project_id": project},
                       headers=acct.headers).json()["id"]
    _ask(client, acct, conv)
    client.post("/api/v1/auth/logout", headers=acct.headers)
    client.cookies.clear()
    login = client.post("/api/v1/auth/login", json={"email": acct.email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    fresh = {"Authorization": f"Bearer {login.json()['access_token']}"}
    client.cookies.clear()
    body = client.get(f"/api/v1/conversations/{conv}?organization_id={acct.org}", headers=fresh).json()
    assert [t["role"] for t in body["turns"]] == ["user", "assistant"] and "1200" in body["turns"][1]["text"]
    with SessionLocal() as db:  # stored encrypted, not in clear text
        from app.models import ConversationMessage

        payloads = db.scalars(select(ConversationMessage.payload)).all()
    assert payloads and all(b"1200" not in p for p in payloads)


def test_a_database_outage_is_a_retryable_503_not_a_500(client: Any, register: Any, monkeypatch: pytest.MonkeyPatch,
                                                         tmp_path: Any) -> None:
    """The database becomes unreachable: every connection attempt fails, as with PostgreSQL down."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import db as app_db

    acct = register("db-down")
    unreachable = create_engine(f"sqlite:///{tmp_path / 'missing-dir' / 'db.sqlite'}")  # cannot be opened
    monkeypatch.setattr(app_db, "SessionLocal", sessionmaker(bind=unreachable))
    ready = client.get("/ready")
    listed = client.get(f"/api/v1/projects?organization_id={acct.org}", headers=acct.headers)
    for response in (ready, listed):
        assert response.status_code == 503 and response.json()["detail"] == "DATABASE_UNAVAILABLE", response.text
        assert response.headers["retry-after"] == "5"
    monkeypatch.undo()
    assert client.get("/ready").status_code == 200  # back as soon as the database is
