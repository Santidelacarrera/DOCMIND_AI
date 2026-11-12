"""Conversations (ADR-005 delivery 3): private, encrypted per organization, scoped like /ask, erasable."""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app import conversations
from app.core import settings
from app.db import SessionLocal
from app.models import ConversationMessage, OrganizationMember, Role, utcnow
from tests.test_case_query import ALPHA_TEXT, QUESTION, env, ingest  # noqa: F401  (env is a fixture)

pytest.importorskip("qdrant_client")


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    monkeypatch.setattr(settings(), "conversation_encryption_key", SecretStr("test-conversation-secret-0123456789"))


def _uid(acct: Any) -> str:
    return acct.client.get("/api/v1/auth/me", headers=acct.headers).json()["id"]


def _join(acct: Any, org_id: str, role: Role = Role.member) -> None:
    with SessionLocal() as db:
        db.add(OrganizationMember(organization_id=uuid.UUID(org_id), user_id=uuid.UUID(_uid(acct)), role=role))
        db.commit()


def new(client: Any, acct: Any, project: str | None = None, *, org: str | None = None) -> Any:
    return client.post(f"/api/v1/conversations?organization_id={org or acct.org}",
                       json={"project_id": project}, headers=acct.headers)


def ask(client: Any, acct: Any, conv: str, question: str = QUESTION, *, org: str | None = None) -> Any:
    return client.post(f"/api/v1/conversations/{conv}/ask?organization_id={org or acct.org}",
                       json={"question": question}, headers=acct.headers)


def test_turns_are_stored_encrypted_and_returned_in_order(register: Any, client: Any, env: Any) -> None:  # noqa: F811
    acct, project, _ = ingest(register, env, ALPHA_TEXT)
    created = new(client, acct, project)
    assert created.status_code == 201, created.text
    conv = created.json()["id"]
    first = ask(client, acct, conv)
    assert first.status_code == 200, first.text
    assert "1200" in first.json()["answer"] and first.json()["seq"] == 2
    ask(client, acct, conv, "What is the colour of the sky?")
    body = client.get(f"/api/v1/conversations/{conv}?organization_id={acct.org}", headers=acct.headers).json()
    assert [(t["seq"], t["role"]) for t in body["turns"]] == [(1, "user"), (2, "assistant"), (3, "user"), (4, "assistant")]
    assert body["turns"][0]["text"] == QUESTION and body["turns"][1]["citations"][0]["page"] == 1
    assert body["turns"][3]["abstained"] is True
    with SessionLocal() as db:
        payloads = db.scalars(select(ConversationMessage.payload)).all()
    assert payloads and all(b"Madrid" not in p and b"1200" not in p for p in payloads)  # never clear text
    listed = client.get(f"/api/v1/conversations?organization_id={acct.org}", headers=acct.headers).json()
    assert [(c["id"], c["messages"]) for c in listed] == [(conv, 4)]


def test_a_conversation_is_private_to_its_user(register: Any, client: Any, env: Any) -> None:  # noqa: F811
    owner, project, _ = ingest(register, env, ALPHA_TEXT)
    colleague, admin, outsider = register("colleague"), register("admin"), register("outsider")
    _join(colleague, owner.org)
    _join(admin, owner.org, Role.admin)
    conv = new(client, owner, project).json()["id"]
    ask(client, owner, conv)
    for other in (colleague, admin, outsider):
        for response in (
            client.get(f"/api/v1/conversations/{conv}?organization_id={owner.org}", headers=other.headers),
            ask(client, other, conv, org=owner.org),
            client.delete(f"/api/v1/conversations/{conv}?organization_id={owner.org}", headers=other.headers),
        ):
            assert response.status_code in (403, 404), response.text
        listed = client.get(f"/api/v1/conversations?organization_id={owner.org}", headers=other.headers)
        assert listed.status_code == 403 or listed.json() == []
    # Through the outsider's own organization the id is just as unknown.
    assert client.get(f"/api/v1/conversations/{conv}?organization_id={outsider.org}",
                      headers=outsider.headers).status_code == 404


def test_case_conversations_answer_from_that_case_and_stop_when_access_goes(register: Any, client: Any, env: Any) -> None:  # noqa: F811
    owner, project, _ = ingest(register, env, ALPHA_TEXT)
    other_case = owner.project("other-case")
    member = register("member")
    _join(member, owner.org, Role.member)
    # Members without a project grant cannot open a conversation on that case (same 404 as a missing case).
    assert new(client, member, project, org=owner.org).status_code == 404
    conv = new(client, owner, other_case).json()["id"]
    assert ask(client, owner, conv).json()["abstained"] is True  # the lease is in another case
    # Access is re-checked on every question: a member whose role is lowered loses the case.
    with SessionLocal() as db:
        row = db.scalar(select(OrganizationMember).where(OrganizationMember.organization_id == uuid.UUID(owner.org),
                                                          OrganizationMember.user_id == uuid.UUID(_uid(owner))))
        row.role = Role.member
        db.commit()
    assert ask(client, owner, conv).status_code == 404


def test_api_keys_have_no_conversations(register: Any, client: Any) -> None:
    acct = register("keys")
    created = client.post(f"/api/v1/api-keys?organization_id={acct.org}&name=k", json=["documents:read"], headers=acct.headers)
    key = {"Authorization": f"Bearer {created.json()['key']}"}
    assert client.post(f"/api/v1/conversations?organization_id={acct.org}", json={}, headers=key).status_code == 403


def test_without_a_key_conversations_are_disabled_not_stored_in_clear(register: Any, client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings(), "conversation_encryption_key", None)
    acct = register("nokey")
    response = new(client, acct)
    assert response.status_code == 503 and response.json()["detail"] == "CONVERSATIONS_DISABLED"


def test_sealed_turns_cannot_be_moved_or_opened_by_another_organization() -> None:
    org, other, conv = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    sealed = conversations.seal(org, conv, 1, "user", {"text": "hola"})
    assert conversations.open_turn(org, conv, 1, "user", sealed) == {"text": "hola"}
    for args in ((other, conv, 1, "user"), (org, uuid.uuid4(), 1, "user"), (org, conv, 2, "user"), (org, conv, 1, "assistant")):
        with pytest.raises(conversations.ConversationIntegrityError):
            conversations.open_turn(*args, sealed)
    tampered = sealed[:-1] + bytes([sealed[-1] ^ 1])
    with pytest.raises(conversations.ConversationIntegrityError):
        conversations.open_turn(org, conv, 1, "user", tampered)


def test_delete_erases_every_turn_and_retention_purges_idle_conversations(register: Any, client: Any, env: Any) -> None:  # noqa: F811
    from app.retention import purge_expired

    acct, project, _ = ingest(register, env, ALPHA_TEXT)
    gone = new(client, acct, project).json()["id"]
    ask(client, acct, gone)
    assert client.delete(f"/api/v1/conversations/{gone}?organization_id={acct.org}", headers=acct.headers).status_code == 204
    with SessionLocal() as db:
        assert db.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == uuid.UUID(gone))).all() == []
    assert client.get(f"/api/v1/conversations/{gone}?organization_id={acct.org}", headers=acct.headers).status_code == 404
    idle = new(client, acct, project).json()["id"]
    ask(client, acct, idle)
    later = utcnow() + timedelta(days=settings().conversation_retention_days + 1)
    assert purge_expired(later)["conversations"] >= 1
    assert client.get(f"/api/v1/conversations/{idle}?organization_id={acct.org}", headers=acct.headers).status_code == 404
