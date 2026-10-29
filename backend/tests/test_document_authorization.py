"""Phase 3C: every document route is checked against the document's project, on the server.

Synthetic organizations, projects, documents and keys only. Read routes need ``read`` on the document's
project, write routes need ``write``. A caller without access gets exactly what a missing document
gets, so the response never reveals that a protected document exists.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app import main, vector_index, worker
from app.db import SessionLocal
from app.evaluation.pdfgen import digital_pdf
from app.models import Document, ExtractionField, OrganizationMember, Role

FILES = {"p1": "contrato-p1-alfa.pdf", "p2": "contrato-p2-beta.pdf", "p3": "contrato-p3-gamma.pdf"}


@pytest.fixture(autouse=True)
def no_background_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker.index_document, "delay", lambda org, doc: None)
    monkeypatch.setattr(main.process_vector_deletions, "delay", lambda: None)
    monkeypatch.setattr(vector_index, "enabled", lambda: False)


class World:
    def __init__(self, register: Any, client: Any) -> None:
        self.client = client
        self.owner = register("alpha")
        self.org = self.owner.org
        self.projects = {k: self.owner.project(f"case-{k}") for k in FILES}
        self.docs: dict[str, str] = {}
        self.fields: dict[str, str] = {}
        for key, project in self.projects.items():
            body = self.upload(project, FILES[key], f"Lease clause {key}. Rent 500 euros.")
            self.docs[key] = body["document_id"]
            self.process(body["job_id"])
            self.fields[key] = self.first_field(self.docs[key])
        self.admin = self.person(register, "admin", Role.admin)
        self.reader = self.person(register, "reader", Role.member)
        self.writer = self.person(register, "writer", Role.member)
        self.nobody = self.person(register, "nobody", Role.member)
        self.viewer = self.person(register, "viewer", Role.viewer)
        self.outsider = register("outsider")
        self.outsider.user = client.get("/api/v1/auth/me", headers=self.outsider.headers).json()["id"]
        self.grant(self.projects["p1"], self.reader.user, "read")
        self.grant(self.projects["p1"], self.writer.user, "write")
        self.grant(self.projects["p1"], self.viewer.user, "read")

    # --- helpers
    def upload(self, project: str, filename: str, text: str) -> dict[str, Any]:
        response = self.client.post(
            f"/api/v1/documents?organization_id={self.org}&project_id={project}",
            files={"file": (filename, digital_pdf([[text]]), "application/pdf")}, headers=self.owner.headers,
        )
        assert response.status_code == 202, response.text
        return response.json()

    def process(self, job_id: str) -> None:
        worker.process_document.run(job_id)  # type: ignore[attr-defined]

    def first_field(self, document: str) -> str:
        with SessionLocal() as db:
            from app.models import ExtractionRun

            run = db.scalar(select(ExtractionRun).where(ExtractionRun.document_id == uuid.UUID(document)))
            assert run is not None
            field = db.scalar(select(ExtractionField).where(ExtractionField.extraction_run_id == run.id))
            assert field is not None
            return str(field.id)

    def person(self, register: Any, label: str, role: Role) -> Any:
        acct = register(label)
        acct.user = self.client.get("/api/v1/auth/me", headers=acct.headers).json()["id"]
        with SessionLocal.begin() as db:
            db.add(OrganizationMember(organization_id=uuid.UUID(self.org), user_id=uuid.UUID(acct.user), role=role))
        return acct

    def grant(self, project: str, user: str, access: str) -> None:
        response = self.client.put(
            f"/api/v1/projects/{project}/members?organization_id={self.org}",
            json={"user_id": user, "access": access}, headers=self.owner.headers,
        )
        assert response.status_code == 200, response.text

    def key(self, scopes: list[str], projects: list[str] | None, name: str) -> dict[str, Any]:
        query = "".join(f"&project_id={p}" for p in (projects or []))
        response = self.client.post(
            f"/api/v1/api-keys?organization_id={self.org}&name={name}{query}",
            json=scopes, headers=self.owner.headers,
        )
        assert response.status_code == 201, response.text
        return {"headers": {"Authorization": f"Bearer {response.json()['key']}"}, "id": response.json()["id"]}

    def call(self, method: str, path: str, headers: dict[str, str], org: str | None = None, body: Any = None) -> Any:
        url = f"{path}{'&' if '?' in path else '?'}organization_id={org or self.org}"
        if body is None:
            return self.client.request(method, url, headers=headers)
        return self.client.request(method, url, json=body, headers=headers)

    def read_routes(self, doc: str, field: str) -> list[tuple[str, str]]:
        return [
            ("GET", f"/api/v1/documents/{doc}"),
            ("GET", f"/api/v1/documents/{doc}/status"),
            ("GET", f"/api/v1/documents/{doc}/download"),
            ("GET", f"/api/v1/documents/{doc}/extraction"),
            ("GET", f"/api/v1/documents/{doc}/export?format=json"),
        ]

    def write_routes(self, doc: str, field: str) -> list[tuple[str, str, Any]]:
        return [
            ("POST", f"/api/v1/documents/{doc}/process", None),
            ("PATCH", f"/api/v1/extraction-fields/{field}", "Changed"),
            ("POST", f"/api/v1/documents/{doc}/review/approve", None),
            ("DELETE", f"/api/v1/documents/{doc}", None),
        ]


@pytest.fixture
def world(register: Any, client: Any) -> World:
    return World(register, client)


def field_value(world: World, field: str) -> str | None:
    with SessionLocal() as db:
        row = db.get(ExtractionField, uuid.UUID(field))
        return row.value if row else None


def document_state(world: World, doc: str) -> bool:
    with SessionLocal() as db:
        row = db.get(Document, uuid.UUID(doc))
        return row is not None and row.deleted_at is None


# --------------------------------------------------------------------- people


def test_a_reader_reads_its_project_and_cannot_change_it(world: World) -> None:
    doc, field = world.docs["p1"], world.fields["p1"]
    for method, path in world.read_routes(doc, field):
        response = world.call(method, path, world.reader.headers)
        assert response.status_code == 200, (method, path, response.status_code)
    for method, path, body in world.write_routes(doc, field):
        response = world.call(method, path, world.reader.headers, body=body)
        assert response.status_code == 403 and response.json()["detail"] == "PROJECT_WRITE_DENIED", (method, path)
    assert document_state(world, doc) and field_value(world, field) != "Changed"


def test_a_writer_can_change_its_project(world: World) -> None:
    doc, field = world.docs["p1"], world.fields["p1"]
    assert world.call("PATCH", f"/api/v1/extraction-fields/{field}", world.writer.headers, body="Changed").status_code == 200
    assert world.call("POST", f"/api/v1/documents/{doc}/process", world.writer.headers).status_code in (200, 202)
    assert world.call("POST", f"/api/v1/documents/{doc}/review/approve", world.writer.headers).status_code in (200, 409)
    assert world.call("DELETE", f"/api/v1/documents/{doc}", world.writer.headers).status_code == 204
    assert not document_state(world, doc)


def test_a_member_without_grants_gets_the_same_answer_as_a_missing_document(world: World) -> None:
    doc, field = world.docs["p2"], world.fields["p2"]
    missing_doc, missing_field = str(uuid.uuid4()), str(uuid.uuid4())
    for method, path in world.read_routes(doc, field):
        denied = world.call(method, path, world.nobody.headers)
        missing = world.call(method, path.replace(doc, missing_doc), world.nobody.headers)
        assert denied.status_code == missing.status_code == 404, (method, path)
        assert denied.json() == missing.json(), (method, path)  # no existence leak
    for method, path, body in world.write_routes(doc, field):
        if "extraction-fields" in path:
            denied = world.call(method, path, world.nobody.headers, body=body)
            missing = world.call(method, path.replace(field, missing_field), world.nobody.headers, body=body)
        else:
            denied = world.call(method, path, world.nobody.headers, body=body)
            missing = world.call(method, path.replace(doc, missing_doc), world.nobody.headers, body=body)
        assert denied.status_code == missing.status_code == 404, (method, path)
        assert denied.json() == missing.json(), (method, path)
    assert document_state(world, doc) and field_value(world, field) != "Changed"


def test_listings_hide_documents_the_caller_cannot_read(world: World) -> None:
    listed = world.call("GET", "/api/v1/documents", world.nobody.headers).json()
    assert listed == []
    reader_ids = {d["id"] for d in world.call("GET", "/api/v1/documents", world.reader.headers).json()}
    assert reader_ids == {world.docs["p1"]}
    queue = world.call("GET", "/api/v1/review/queue", world.nobody.headers).json()
    assert all(item["document_id"] not in world.docs.values() for item in queue)


def test_owner_and_admin_keep_full_access(world: World) -> None:
    for who in (world.owner, world.admin):
        for key, doc in world.docs.items():
            assert world.call("GET", f"/api/v1/documents/{doc}", who.headers).status_code == 200, key
            assert world.call("GET", f"/api/v1/documents/{doc}/extraction", who.headers).status_code == 200, key
        listed = {d["id"] for d in world.call("GET", "/api/v1/documents", who.headers).json()}
        assert listed == set(world.docs.values())


def test_another_organization_is_refused_by_membership_on_every_route(world: World) -> None:
    doc, field = world.docs["p1"], world.fields["p1"]
    for method, path in world.read_routes(doc, field):
        response = world.call(method, path, world.outsider.headers)
        assert response.status_code in (403, 404), (method, path)
    for method, path, body in world.write_routes(doc, field):
        response = world.call(method, path, world.outsider.headers, body=body)
        assert response.status_code in (403, 404), (method, path)
    assert document_state(world, doc)


def test_revoking_a_grant_blocks_the_next_request(world: World) -> None:
    doc = world.docs["p1"]
    assert world.call("GET", f"/api/v1/documents/{doc}", world.reader.headers).status_code == 200
    revoked = world.client.delete(
        f"/api/v1/projects/{world.projects['p1']}/members/{world.reader.user}?organization_id={world.org}",
        headers=world.owner.headers,
    )
    assert revoked.status_code == 204
    assert world.call("GET", f"/api/v1/documents/{doc}", world.reader.headers).status_code == 404
    assert world.call("GET", "/api/v1/documents", world.reader.headers).json() == []


# --------------------------------------------------------------------- keys


def test_a_key_limited_to_one_project_reads_only_that_project(world: World) -> None:
    key = world.key(["documents:read"], [world.projects["p1"]], "read-p1")
    assert world.call("GET", f"/api/v1/documents/{world.docs['p1']}", key["headers"]).status_code == 200
    assert world.call("GET", f"/api/v1/documents/{world.docs['p2']}", key["headers"]).status_code == 404
    listed = {d["id"] for d in world.call("GET", "/api/v1/documents", key["headers"]).json()}
    assert listed == {world.docs["p1"]}


def test_a_key_scope_is_checked_before_the_project(world: World) -> None:
    read = world.key(["documents:read"], [world.projects["p1"]], "read-only")
    response = world.call("PATCH", f"/api/v1/extraction-fields/{world.fields['p1']}", read["headers"], body="x")
    assert response.status_code == 403 and response.json()["detail"] == "INSUFFICIENT_SCOPE"
    response = world.call("DELETE", f"/api/v1/documents/{world.docs['p1']}", read["headers"])
    assert response.status_code == 403 and response.json()["detail"] == "INSUFFICIENT_SCOPE"
    assert document_state(world, world.docs["p1"])


def test_a_write_key_changes_only_its_project(world: World) -> None:
    write = world.key(["documents:write", "documents:read"], [world.projects["p1"]], "write-p1")
    assert world.call("POST", f"/api/v1/documents/{world.docs['p1']}/process", write["headers"]).status_code in (200, 202)
    denied = world.call("DELETE", f"/api/v1/documents/{world.docs['p2']}", write["headers"])
    assert denied.status_code == 404
    assert document_state(world, world.docs["p2"])


def test_an_export_key_exports_only_its_project(world: World) -> None:
    export = world.key(["exports:read"], [world.projects["p1"]], "export-p1")
    assert world.call("GET", f"/api/v1/documents/{world.docs['p1']}/export?format=json", export["headers"]).status_code == 200
    assert world.call("GET", f"/api/v1/documents/{world.docs['p2']}/export?format=json", export["headers"]).status_code == 404


def test_a_legacy_key_keeps_organization_access_as_before(world: World) -> None:
    legacy = world.key(["documents:read"], None, "legacy")
    for doc in world.docs.values():
        assert world.call("GET", f"/api/v1/documents/{doc}", legacy["headers"]).status_code == 200


# --------------------------------------------------------------------- tampering


def test_changing_the_ids_or_adding_project_fields_does_not_widen_access(world: World) -> None:
    doc_p2 = world.docs["p2"]
    response = world.call("GET", f"/api/v1/documents/{doc_p2}?project_id={world.projects['p1']}", world.reader.headers)
    assert response.status_code == 404
    body = {"project_id": world.projects["p1"], "document_id": world.docs["p1"], "value": "Changed"}
    response = world.client.request(
        "PATCH", f"/api/v1/extraction-fields/{world.fields['p2']}?organization_id={world.org}",
        json=body, headers=world.reader.headers,
    )
    assert response.status_code == 404 and response.json()["detail"] == "FIELD_NOT_FOUND"
    assert field_value(world, world.fields["p2"]) != "Changed"


# --------------------------------------------------------------------- batch and errors


def test_listing_is_the_only_multi_document_route_and_it_filters_before_returning(world: World) -> None:
    """The API has no batch endpoint. The listing and review queue filter in the query, so hidden documents are
    neither returned nor counted (a limit applied after filtering cannot reveal how many were hidden)."""
    assert len(world.call("GET", "/api/v1/documents", world.reader.headers).json()) == 1
    assert len(world.call("GET", "/api/v1/documents", world.owner.headers).json()) == len(world.docs)


def test_error_bodies_do_not_reveal_names_keys_or_text(world: World, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.DEBUG):
        responses = [
            world.call("GET", f"/api/v1/documents/{world.docs['p2']}/download", world.nobody.headers),
            world.call("GET", f"/api/v1/documents/{world.docs['p2']}/extraction", world.nobody.headers),
            world.call("PATCH", f"/api/v1/extraction-fields/{world.fields['p2']}", world.nobody.headers, body="secret"),
        ]
    for response in responses:
        assert FILES["p2"] not in response.text and "storage" not in response.text.lower()
    assert FILES["p2"] not in caplog.text and "secret" not in caplog.text


def test_project_listing_shows_members_only_their_projects(world: World) -> None:
    def names(headers: dict[str, str]) -> set[str]:
        return {p["id"] for p in world.call("GET", "/api/v1/projects", headers).json()}

    assert names(world.owner.headers) == set(world.projects.values())
    assert names(world.admin.headers) == set(world.projects.values())
    assert names(world.reader.headers) == {world.projects["p1"]}
    assert names(world.writer.headers) == {world.projects["p1"]}
    assert names(world.nobody.headers) == set()  # no grant, no names: hidden projects are not counted either


def test_a_key_lists_only_its_projects(world: World) -> None:
    key = world.key(["documents:read"], [world.projects["p2"]], "list-p2")
    assert {p["id"] for p in world.call("GET", "/api/v1/projects", key["headers"]).json()} == {world.projects["p2"]}
    legacy = world.key(["documents:read"], None, "list-legacy")
    assert len(world.call("GET", "/api/v1/projects", legacy["headers"]).json()) == len(world.projects)


def test_the_download_route_returns_the_file_only_to_a_reader(world: World) -> None:
    allowed = world.call("GET", f"/api/v1/documents/{world.docs['p1']}/download", world.reader.headers)
    assert allowed.status_code == 200 and allowed.content.startswith(b"%PDF")
    refused = world.call("GET", f"/api/v1/documents/{world.docs['p2']}/download", world.reader.headers)
    assert refused.status_code == 404 and not refused.content.startswith(b"%PDF")
