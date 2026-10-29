"""Phase 3B: per-project authorization for people and API keys, with negative tests.

Two organizations, three projects in the first one, synthetic documents only. Every project's document
answers the same question with a different figure, so a leak shows up as the wrong figure or the wrong
document name.
"""

import logging
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app import main, vector_index, worker
from app.core import settings
from app.db import SessionLocal
from app.evaluation.pdfgen import digital_pdf
from app.ingestion.embeddings import HashingEmbedder
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.vectorstore import PROJECT_KEY, QdrantStore
from app.models import OrganizationMember, Role

qdrant_client = pytest.importorskip("qdrant_client")
models = pytest.importorskip("qdrant_client.models")
EMBED = HashingEmbedder(64)
COLLECTION = "project-auth"
QUESTION = "What is the monthly rent for the local?"
FIGURES = {"p1": "1100", "p2": "2200", "p3": "3300"}
WORD = {"p1": "alpha", "p2": "beta", "p3": "gamma"}  # a distinctive word per project, used in targeted questions
NAMES = {"p1": "contrato-p1-alfa.pdf", "p2": "contrato-p2-beta.pdf", "p3": "contrato-p3-gamma.pdf"}


def lines(key: str) -> list[str]:
    return [f"Lease clause {WORD[key]}. The monthly rent for the local is {FIGURES[key]} euros under this lease."]


def targeted(key: str) -> str:
    return f"What is the monthly rent for the {WORD[key]} local?"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Any:
    client = qdrant_client.QdrantClient(":memory:")
    store = QdrantStore(client, COLLECTION)
    pipeline = IngestionPipeline(store, EMBED, max_chars=400, overlap=40)
    monkeypatch.setattr(settings(), "qdrant_url", "https://qdrant.test")
    monkeypatch.setattr(vector_index, "get_store", lambda: store)
    monkeypatch.setattr(vector_index, "get_pipeline", lambda: pipeline)
    monkeypatch.setattr(worker.index_document, "delay", lambda org, doc: None)
    monkeypatch.setattr(main.process_vector_deletions, "delay", lambda: None)

    class Env:
        pass

    e = Env()
    e.client, e.store = client, store  # type: ignore[attr-defined]
    return e


class World:
    """Organization A (owner, admin, members, keys) and organization B (an outsider)."""

    def __init__(self, register: Any, client: Any, env: Any) -> None:
        self.client, self.env = client, env
        self.owner = register("alpha")
        self.org = self.owner.org
        self.projects = {k: self.owner.project(f"case-{k}") for k in FIGURES}
        self.docs = {k: self.upload(self.owner.headers, self.projects[k], lines(k), NAMES[k]) for k in FIGURES}
        self.admin = self.person(register, "admin", Role.admin)
        self.reader_p1 = self.person(register, "reader", Role.member)
        self.writer_p2 = self.person(register, "writer", Role.member)
        self.nobody = self.person(register, "nobody", Role.member)
        self.viewer = self.person(register, "viewer", Role.viewer)
        self.outsider = register("outsider")
        self.outsider.user = self.client.get("/api/v1/auth/me", headers=self.outsider.headers).json()["id"]
        self.outsider_project = self.outsider.project("own")
        self.grant(self.projects["p1"], self.reader_p1.user, "read")
        self.grant(self.projects["p2"], self.writer_p2.user, "write")
        self.grant(self.projects["p1"], self.viewer.user, "read")

    def person(self, register: Any, label: str, role: Role) -> Any:
        acct = register(label)
        user = self.client.get("/api/v1/auth/me", headers=acct.headers).json()["id"]
        with SessionLocal.begin() as db:
            db.add(OrganizationMember(organization_id=uuid.UUID(self.org), user_id=uuid.UUID(user), role=role))
        acct.user = user
        return acct

    def grant(self, project: str, user: str, access: str) -> None:
        response = self.client.put(
            f"/api/v1/projects/{project}/members?organization_id={self.org}",
            json={"user_id": user, "access": access}, headers=self.owner.headers,
        )
        assert response.status_code == 200, response.text

    def key(self, scopes: list[str], projects: list[str] | None = None, name: str = "k") -> dict[str, Any]:
        query = "&".join(f"project_id={p}" for p in (projects or []))
        suffix = f"&{query}" if query else ""
        response = self.client.post(
            f"/api/v1/api-keys?organization_id={self.org}&name={name}{suffix}",
            json=scopes, headers=self.owner.headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        return {"id": body["id"], "headers": {"Authorization": f"Bearer {body['key']}"}, "secret": body["key"]}

    def upload(self, headers: dict[str, str], project: str, text: list[str], filename: str, org: str | None = None) -> str:
        org = org or self.org
        response = self.client.post(
            f"/api/v1/documents?organization_id={org}&project_id={project}",
            files={"file": (filename, digital_pdf([text]), "application/pdf")}, headers=headers,
        )
        assert response.status_code == 202, response.text
        body = response.json()
        worker.process_document.run(body["job_id"])  # type: ignore[attr-defined]
        if org == self.org:
            assert vector_index.index_document(org, body["document_id"]) == "indexed"
        return body["document_id"]

    def ask(self, headers: dict[str, str], project: str | None, org: str | None = None, question: str = QUESTION) -> Any:
        org = org or self.org
        if project is None:
            return self.client.post(f"/api/v1/ask?organization_id={org}", json={"question": question}, headers=headers)
        return self.client.post(
            f"/api/v1/projects/{project}/ask?organization_id={org}", json={"question": question}, headers=headers,
        )


@pytest.fixture
def world(register: Any, client: Any, env: Any) -> World:
    return World(register, client, env)


def cited_projects(response: Any, world: World) -> set[str]:
    by_doc = {doc: key for key, doc in world.docs.items()}
    return {by_doc[c["document_id"]] for c in response.json()["citations"]}


# --------------------------------------------------------------------- people


def test_owner_and_admin_read_every_project_of_the_organization(world: World) -> None:
    for who in (world.owner, world.admin):
        for key, project in world.projects.items():
            response = world.ask(who.headers, project)
            assert response.status_code == 200 and cited_projects(response, world) == {key}


def test_a_granted_member_reads_its_project_and_nothing_else(world: World) -> None:
    assert cited_projects(world.ask(world.reader_p1.headers, world.projects["p1"]), world) == {"p1"}
    denied = world.ask(world.reader_p1.headers, world.projects["p2"])
    missing = world.ask(world.reader_p1.headers, str(uuid.uuid4()))
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json() == {"detail": "PROJECT_NOT_FOUND"}  # no existence leak


def test_a_member_without_grants_reads_no_project_and_the_organization_ask_is_empty(world: World) -> None:
    for project in world.projects.values():
        assert world.ask(world.nobody.headers, project).status_code == 404
    org_wide = world.ask(world.nobody.headers, None).json()
    assert org_wide["abstained"] is True and org_wide["citations"] == []


def test_write_grant_reads_and_a_read_grant_cannot_upload(world: World) -> None:
    assert cited_projects(world.ask(world.writer_p2.headers, world.projects["p2"]), world) == {"p2"}
    denied = world.client.post(
        f"/api/v1/documents?organization_id={world.org}&project_id={world.projects['p1']}",
        files={"file": ("x.pdf", digital_pdf([["Synthetic upload attempt."]]), "application/pdf")},
        headers=world.reader_p1.headers,
    )
    assert denied.status_code == 403 and denied.json()["detail"] == "PROJECT_WRITE_DENIED"
    accepted = world.client.post(
        f"/api/v1/documents?organization_id={world.org}&project_id={world.projects['p2']}",
        files={"file": ("y.pdf", digital_pdf([["Synthetic upload by a writer."]]), "application/pdf")},
        headers=world.writer_p2.headers,
    )
    assert accepted.status_code == 202


def test_a_viewer_cannot_upload_even_with_a_grant(world: World) -> None:
    response = world.client.post(
        f"/api/v1/documents?organization_id={world.org}&project_id={world.projects['p1']}",
        files={"file": ("v.pdf", digital_pdf([["Synthetic viewer attempt."]]), "application/pdf")},
        headers=world.viewer.headers,
    )
    assert response.status_code == 403  # organization role: members and above


def test_revoking_a_grant_takes_effect_on_the_next_request(world: World) -> None:
    project = world.projects["p1"]
    assert world.ask(world.reader_p1.headers, project).status_code == 200
    revoked = world.client.delete(
        f"/api/v1/projects/{project}/members/{world.reader_p1.user}?organization_id={world.org}",
        headers=world.owner.headers,
    )
    assert revoked.status_code == 204
    assert world.ask(world.reader_p1.headers, project).status_code == 404
    assert world.client.delete(
        f"/api/v1/projects/{project}/members/{world.reader_p1.user}?organization_id={world.org}",
        headers=world.owner.headers,
    ).status_code == 404  # already gone


def test_only_owners_and_admins_manage_grants_and_never_an_api_key(world: World) -> None:
    body = {"user_id": world.nobody.user, "access": "read"}
    url = f"/api/v1/projects/{world.projects['p3']}/members?organization_id={world.org}"
    assert world.client.put(url, json=body, headers=world.reader_p1.headers).status_code == 403
    manager = world.key(["documents:read"], [world.projects["p3"]], name="manager")
    assert world.client.put(url, json=body, headers=manager["headers"]).status_code == 403
    assert world.client.put(url, json=body, headers=world.admin.headers).status_code == 200


def test_a_grant_requires_a_user_of_the_organization(world: World) -> None:
    response = world.client.put(
        f"/api/v1/projects/{world.projects['p1']}/members?organization_id={world.org}",
        json={"user_id": world.outsider.user, "access": "read"}, headers=world.owner.headers,
    )
    assert response.status_code == 422 and response.json()["detail"] == "USER_NOT_IN_ORGANIZATION"


# --------------------------------------------------------------------- API keys


def test_a_key_limited_to_one_project_reads_only_that_project(world: World) -> None:
    key = world.key(["documents:read"], [world.projects["p1"]], name="only-p1")
    assert cited_projects(world.ask(key["headers"], world.projects["p1"]), world) == {"p1"}
    assert world.ask(key["headers"], world.projects["p2"]).status_code == 404
    # The organization endpoint is narrowed to the key's project as well.
    assert cited_projects(world.ask(key["headers"], None), world) <= {"p1"}


def test_a_legacy_key_without_projects_cannot_use_case_queries_but_keeps_organization_access(world: World) -> None:
    legacy = world.key(["documents:read"], None, name="legacy")
    for project in world.projects.values():
        assert world.ask(legacy["headers"], project).status_code == 404
    # Organization-wide, as before this phase: the legacy key sees exactly what the owner sees.
    for key in FIGURES:
        question = targeted(key)
        assert cited_projects(world.ask(legacy["headers"], None, question=question), world) == cited_projects(
            world.ask(world.owner.headers, None, question=question), world
        )


def test_a_key_without_the_read_scope_is_refused_even_for_its_project(world: World) -> None:
    key = world.key(["exports:read"], [world.projects["p1"]], name="exports-only")
    response = world.ask(key["headers"], world.projects["p1"])
    assert response.status_code == 403 and response.json()["detail"] == "INSUFFICIENT_SCOPE"


def test_a_revoked_key_stops_working_everywhere(world: World) -> None:
    key = world.key(["documents:read"], [world.projects["p1"]], name="revoke-me")
    assert world.ask(key["headers"], world.projects["p1"]).status_code == 200
    world.client.delete(f"/api/v1/api-keys/{key['id']}?organization_id={world.org}", headers=world.owner.headers)
    assert world.ask(key["headers"], world.projects["p1"]).status_code in (401, 403)
    assert world.ask(key["headers"], None).status_code in (401, 403)


def test_a_write_key_uploads_only_into_its_project(world: World) -> None:
    key = world.key(["documents:write"], [world.projects["p1"]], name="writer-p1")
    refused = world.client.post(
        f"/api/v1/documents?organization_id={world.org}&project_id={world.projects['p2']}",
        files={"file": ("z.pdf", digital_pdf([["Synthetic key upload elsewhere."]]), "application/pdf")},
        headers=key["headers"],
    )
    assert refused.status_code == 404 and refused.json()["detail"] == "PROJECT_NOT_FOUND"
    accepted = world.client.post(
        f"/api/v1/documents?organization_id={world.org}&project_id={world.projects['p1']}",
        files={"file": ("w.pdf", digital_pdf([["Synthetic key upload here."]]), "application/pdf")},
        headers=key["headers"],
    )
    assert accepted.status_code == 202


# --------------------------------------------------------------------- tampering


def test_a_client_supplied_project_id_cannot_widen_access(world: World) -> None:
    other = world.projects["p2"]
    # The path decides the project. Query and body fields named project_id are ignored.
    response = world.client.post(
        f"/api/v1/projects/{world.projects['p1']}/ask?organization_id={world.org}&project_id={other}",
        json={"question": QUESTION, "project_id": other}, headers=world.reader_p1.headers,
    )
    assert response.status_code == 200 and cited_projects(response, world) == {"p1"}
    denied = world.client.post(
        f"/api/v1/projects/{other}/ask?organization_id={world.org}&project_id={world.projects['p1']}",
        json={"question": QUESTION}, headers=world.reader_p1.headers,
    )
    assert denied.status_code == 404


def test_a_key_cannot_widen_access_by_naming_another_project(world: World) -> None:
    key = world.key(["documents:read"], [world.projects["p1"]], name="tamper")
    response = world.client.post(
        f"/api/v1/projects/{world.projects['p2']}/ask?organization_id={world.org}&project_id={world.projects['p1']}",
        json={"question": QUESTION}, headers=key["headers"],
    )
    assert response.status_code == 404


def test_an_organization_key_or_user_cannot_be_used_in_another_organization(world: World) -> None:
    project = world.projects["p1"]
    # The outsider asks inside its own organization with a project id from another one: unknown project.
    assert world.ask(world.outsider.headers, project, org=world.outsider.org).status_code == 404
    cross = world.client.post(
        f"/api/v1/projects/{project}/ask?organization_id={world.outsider.org}",
        json={"question": QUESTION}, headers=world.owner.headers,
    )
    assert cross.status_code == 403  # the owner is not a member of the outsider's organization
    key = world.key(["documents:read"], [project], name="tenant-key")
    foreign = world.client.post(
        f"/api/v1/projects/{project}/ask?organization_id={world.outsider.org}",
        json={"question": QUESTION}, headers=key["headers"],
    )
    assert foreign.status_code == 403 and foreign.json()["detail"] == "FORBIDDEN"


# --------------------------------------------------------------------- organization endpoint and legacy vectors


def test_the_organization_endpoint_answers_only_from_the_callers_projects(world: World) -> None:
    response = world.ask(world.reader_p1.headers, None)
    assert cited_projects(response, world) == {"p1"}
    assert FIGURES["p2"] not in response.json()["answer"]


def test_legacy_vectors_without_a_project_are_excluded_from_the_organization_endpoint(world: World) -> None:
    doc = world.docs["p3"]
    world.env.client.delete_payload(
        COLLECTION, keys=[PROJECT_KEY],
        points=models.FilterSelector(filter=QdrantStore.tenant_filter(world.org, document_id=doc)),
    )
    # Without a project the vector is invisible: the gamma question cannot cite the gamma document.
    assert "p3" not in cited_projects(world.ask(world.owner.headers, None, question=targeted("p3")), world)
    assert vector_index.unscoped_points(world.org) >= 1
    assert vector_index.backfill_project_ids()["failed"] == 0
    assert "p3" in cited_projects(world.ask(world.owner.headers, None, question=targeted("p3")), world)


# --------------------------------------------------------------------- disclosure


def test_denied_responses_and_logs_do_not_reveal_names_secrets_or_questions(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    key = world.key(["documents:read"], [world.projects["p1"]], name="quiet")
    secret_question = "Which person signed the beta contract with the secret reference ZZ-42?"
    with caplog.at_level(logging.DEBUG):
        denied = world.client.post(
            f"/api/v1/projects/{world.projects['p2']}/ask?organization_id={world.org}",
            json={"question": secret_question}, headers={**key["headers"], "X-Request-Id": "deny-check-01"},
        )
        allowed = world.client.post(
            f"/api/v1/projects/{world.projects['p1']}/ask?organization_id={world.org}",
            json={"question": secret_question}, headers={**key["headers"], "X-Request-Id": "allow-check-01"},
        )
    assert denied.status_code == 404 and allowed.status_code == 200
    for body in (denied.text, allowed.text):
        assert NAMES["p2"] not in body and key["secret"] not in body
    assert denied.headers["X-Request-Id"] == "deny-check-01"
    for leaked in (NAMES["p1"], NAMES["p2"], key["secret"], "ZZ-42", "secret reference", "beta contract"):
        assert leaked not in caplog.text
    answered = [r for r in caplog.records if r.getMessage() == "project query answered"]
    assert answered and answered[-1].request_id == "allow-check-01"


def test_the_key_secret_is_shown_once_and_never_stored_in_plain_text(world: World) -> None:
    key = world.key(["documents:read"], [world.projects["p1"]], name="stored")
    with SessionLocal() as db:
        from app.models import ApiKey, AuditLog

        row = db.get(ApiKey, uuid.UUID(key["id"]))
        assert row is not None and row.key_hash != key["secret"] and key["secret"] not in row.key_hash
        audit_text = " ".join(str(a.metadata_) for a in db.scalars(select(AuditLog)).all())
        assert key["secret"] not in audit_text


# --------------------------------------------------------------------- schema and migration


def test_the_new_schema_is_declared_on_the_models() -> None:
    from app.models import ApiKey, ProjectMember

    assert "project_ids" in ApiKey.__table__.c
    assert {"project_id", "user_id", "access"} <= set(ProjectMember.__table__.c.keys())
    assert ProjectMember.__table__.c.access.nullable is False


def test_migration_0008_is_idempotent_on_the_declared_schema() -> None:
    """The upgrade only adds what is missing. Here it runs against a schema that already has everything."""
    import importlib.util
    import pathlib

    import sqlalchemy as sa

    from app.db import Base, engine

    path = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0008_project_authorization.py"
    spec = importlib.util.spec_from_file_location("m0008", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            module.upgrade()  # second run must be a no-op
        assert "project_ids" in {c["name"] for c in sa.inspect(conn).get_columns("api_keys")}


def test_an_unknown_or_expired_key_is_refused_everywhere(world: World) -> None:
    key = world.key(["documents:read"], [world.projects["p1"]], name="expires")
    unknown = {"Authorization": "Bearer dm_live_this-key-was-never-issued-0000000000000000"}
    assert world.ask(unknown, world.projects["p1"]).status_code == 401
    assert world.ask(unknown, None).status_code == 401
    import datetime

    from app.models import ApiKey

    with SessionLocal.begin() as db:
        row = db.get(ApiKey, uuid.UUID(key["id"]))
        assert row is not None
        row.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
    assert world.ask(key["headers"], world.projects["p1"]).status_code == 401
    assert world.ask(key["headers"], None).status_code == 401
