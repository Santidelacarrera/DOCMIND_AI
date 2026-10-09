"""Multi-tenant isolation, proven from the outside.

The route-table test is the safety net: it enumerates every endpoint FastAPI registers, so a
future endpoint that forgets tenant scoping fails here even if nobody wrote a test for it.
"""

import io
import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute

from app import main, worker
from app.core import settings
from app.db import SessionLocal
from app.models import Document, ExtractionField, JobStatus, ProcessingJob
from app.storage import LocalStorageProvider, StorageAccessError, owned_key, storage
from tests.conftest import make_pdf

# Endpoints that are not tenant-scoped by design.
PUBLIC_OR_USER_LEVEL = {
    "/health", "/ready", "/metrics",
    "/api/v1/auth/register", "/api/v1/auth/login", "/api/v1/auth/logout", "/api/v1/auth/me",
    "/api/v1/organizations", "/api/v1/invitations/accept",
}
# Minimal valid bodies so request validation (422) cannot mask a missing authorization check.
BODIES: dict[tuple[str, str], Any] = {
    ("POST", "/api/v1/invitations"): {"email": "new@example.com", "role": "MEMBER"},
    ("POST", "/api/v1/schemas"): {"type": "object", "properties": {}},
    ("POST", "/api/v1/schemas/{schema_id}/versions"): {"type": "object", "properties": {}},
    ("POST", "/api/v1/api-keys"): ["documents:read"],
    ("POST", "/api/v1/webhooks"): {"url": "https://example.com/hook", "events": ["document.completed"]},
    ("PATCH", "/api/v1/extraction-fields/{field_id}"): "hacked",
}
EXTRA_QUERY = {
    ("POST", "/api/v1/members"): "&email=x@example.com&role=MEMBER",
    ("PATCH", "/api/v1/members/{user_id}"): "&role=ADMIN",
    ("POST", "/api/v1/projects"): "&name=evil",
    ("POST", "/api/v1/schemas"): "&name=evil",
    ("POST", "/api/v1/api-keys"): "&name=evil",
    ("POST", "/api/v1/documents"): "&project_id={project_id}",
}


class World:
    """Two tenants; the victim owns one of everything."""

    def __init__(self, register: Any, client: Any) -> None:
        self.client = client
        self.victim, self.attacker = register("victim"), register("attacker")
        v = self.victim
        self.project = v.project()
        upload = v.upload(self.project, make_pdf(2), "victim-secret-contract.pdf").json()
        self.document = upload["document_id"]
        worker.process_document.run(upload["job_id"])  # type: ignore[attr-defined]
        extraction = client.get(
            f"/api/v1/documents/{self.document}/extraction?organization_id={v.org}", headers=v.headers
        ).json()
        self.field = extraction["fields"][0]["id"]
        self.schema = client.post(
            f"/api/v1/schemas?organization_id={v.org}&name=victim-schema",
            json={"type": "object", "properties": {"x": {}}}, headers=v.headers,
        ).json()["id"]
        self.api_key = client.post(
            f"/api/v1/api-keys?organization_id={v.org}&name=k", json=["documents:read"], headers=v.headers
        ).json()["id"]
        self.webhook = client.post(
            f"/api/v1/webhooks?organization_id={v.org}",
            json={"url": "https://example.com/hook", "events": ["document.completed"]}, headers=v.headers,
        ).json()["id"]
        self.invitation = client.post(
            f"/api/v1/invitations?organization_id={v.org}",
            json={"email": "guest@example.com", "role": "MEMBER"}, headers=v.headers,
        ).json()["id"]
        self.user = client.get("/api/v1/auth/me", headers=v.headers).json()["id"]
        self.ids = {
            "document_id": self.document, "field_id": self.field, "schema_id": self.schema,
            "key_id": self.api_key, "webhook_id": self.webhook, "invitation_id": self.invitation,
            "user_id": self.user, "project_id": self.project,
        }
        self.secrets = [
            self.document, "victim-secret-contract", self.schema, "victim-schema", self.webhook,
            "guest@example.com", self.invitation, v.email,
        ]

    def request(self, route: APIRoute, method: str, headers: dict[str, str], org: str) -> Any:
        template = route.path
        path = template
        for name, value in self.ids.items():
            path = path.replace("{" + name + "}", value)
        extra = EXTRA_QUERY.get((method, template), "").replace("{project_id}", self.project)
        url = f"{path}?organization_id={org}{extra}"
        if (method, template) == ("POST", "/api/v1/documents"):
            return self.client.post(
                url, files={"file": ("x.pdf", make_pdf(), "application/pdf")}, headers=headers
            )
        body = BODIES.get((method, template))
        if body is not None:
            return self.client.request(
                method, url, content=json.dumps(body), headers={**headers, "Content-Type": "application/json"}
            )
        return self.client.request(method, url, headers=headers)


def _scoped_routes() -> list[tuple[APIRoute, str]]:
    return [
        (route, method)
        for route in main.app.routes
        if isinstance(route, APIRoute)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"})
        if any(p.name == "organization_id" for p in route.dependant.query_params)
    ]


def test_every_route_is_either_known_public_or_tenant_scoped() -> None:
    unscoped = {
        route.path
        for route in main.app.routes
        if isinstance(route, APIRoute)
        and not any(p.name == "organization_id" for p in route.dependant.query_params)
    }
    assert unscoped <= PUBLIC_OR_USER_LEVEL, (
        f"endpoints without organization scoping: {sorted(unscoped - PUBLIC_OR_USER_LEVEL)}"
    )
    assert len(_scoped_routes()) >= 30


def test_outsider_is_rejected_by_every_scoped_endpoint(client, register) -> None:
    world = World(register, client)
    checked = 0
    for route, method in _scoped_routes():
        response = world.request(route, method, world.attacker.headers, world.victim.org)
        assert response.status_code == 403, (method, route.path, response.status_code, response.text[:200])
        checked += 1
    assert checked == len(_scoped_routes())


def test_unauthenticated_requests_are_rejected_everywhere(client, register) -> None:
    world = World(register, client)
    for route, method in _scoped_routes():
        response = world.request(route, method, {}, world.victim.org)
        assert response.status_code == 401, (method, route.path, response.status_code)


def test_foreign_resource_ids_never_resolve_through_the_attackers_own_org(client, register) -> None:
    """The subtler attack: be a legitimate member of *your* org but name *their* object ids."""
    world = World(register, client)
    mine = world.attacker.org
    for route, method in _scoped_routes():
        if not re.search(r"\{\w+\}", route.path):
            continue
        response = world.request(route, method, world.attacker.headers, mine)
        assert response.status_code in {403, 404}, (method, route.path, response.status_code)
        leaked = [s for s in world.secrets if s in response.text]
        assert not leaked, (method, route.path, leaked)
    # State really is untouched after all those attempts.
    with SessionLocal() as db:
        document = db.get(Document, uuid.UUID(world.document))
        assert document is not None and document.deleted_at is None
        field = db.get(ExtractionField, uuid.UUID(world.field))
        assert field is not None and field.value != "hacked" and not field.manually_verified


def test_collections_only_ever_contain_the_callers_own_data(client, register) -> None:
    world = World(register, client)
    a = world.attacker
    own_doc = a.upload(a.project("own"), make_pdf(), "mine.pdf").json()["document_id"]
    for route, method in _scoped_routes():
        if method != "GET" or re.search(r"\{\w+\}", route.path):
            continue
        response = world.request(route, method, a.headers, a.org)
        assert response.status_code == 200, route.path
        leaked = [s for s in world.secrets if s in response.text]
        assert not leaked, (route.path, leaked)
    listing = client.get(f"/api/v1/documents?organization_id={a.org}", headers=a.headers).json()
    assert [d["id"] for d in listing] == [own_doc]


def test_api_key_is_confined_to_its_organization(client, register) -> None:
    world = World(register, client)
    a = world.attacker
    key = client.post(
        f"/api/v1/api-keys?organization_id={a.org}&name=k",
        json=["documents:read", "exports:read", "documents:write"], headers=a.headers,
    ).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    own = client.get(f"/api/v1/documents?organization_id={a.org}", headers=headers)
    assert own.status_code == 200
    for path in (
        f"/api/v1/documents?organization_id={world.victim.org}",
        f"/api/v1/documents/{world.document}/export?organization_id={world.victim.org}",
        f"/api/v1/documents/{world.document}/download?organization_id={world.victim.org}",
    ):
        assert client.get(path, headers=headers).status_code == 403, path
    assert client.get(
        f"/api/v1/documents/{world.document}/export?organization_id={a.org}", headers=headers
    ).status_code == 404


def test_worker_never_sends_one_tenants_events_to_another_tenants_webhooks(client, register, monkeypatch) -> None:
    world = World(register, client)
    a = world.attacker
    client.post(
        f"/api/v1/webhooks?organization_id={a.org}",
        json={"url": "https://example.com/attacker", "events": ["document.completed"]}, headers=a.headers,
    )
    delivered: list[str] = []

    def fake_deliver(webhook_id: Any, url: str, event: str, payload: dict[str, Any]) -> tuple[bool, int, None]:
        delivered.append(url)
        return True, 200, None

    monkeypatch.setattr(worker, "deliver_webhook_request", fake_deliver)
    worker.dispatch_webhooks.run(world.victim.org, "document.completed", {"document_id": world.document})  # type: ignore[attr-defined]
    assert delivered == ["https://example.com/hook"]  # the victim's own hook only


# ------------------------------------------------------------------------- storage layer
def test_storage_keys_are_confined_to_their_organization_prefix() -> None:
    org, other = uuid.uuid4(), uuid.uuid4()
    assert owned_key(org, f"{org}/{uuid.uuid4()}.pdf")
    for bad in (f"{other}/x.pdf", f"{org}/../{other}/x.pdf", f"{org}\\..\\x.pdf", "x.pdf", f"{org}x/y.pdf", ""):
        with pytest.raises(StorageAccessError):
            owned_key(org, bad)


def test_local_storage_rejects_path_traversal(tmp_path: Path) -> None:
    provider = LocalStorageProvider.__new__(LocalStorageProvider)
    provider.root = (tmp_path / "root").resolve()
    provider.root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    for key in ("../outside.txt", "a/../../outside.txt", str(outside)):
        with pytest.raises(ValueError):
            provider.get(key)
        with pytest.raises(ValueError):
            provider.put(key, b"x")
        with pytest.raises(ValueError):
            provider.delete(key)
    assert outside.read_text() == "secret"


def test_tampered_storage_key_cannot_read_another_tenants_file(client, register) -> None:
    world = World(register, client)
    a = world.attacker
    mine = a.upload(a.project(), make_pdf(), "mine.pdf").json()
    with SessionLocal() as db:
        original_key = db.get(Document, uuid.UUID(world.document)).storage_key  # type: ignore[union-attr]
    # The victim's object under a key no row owns yet (storage_key is unique per row).
    victim_key = f"{world.victim.org}/{uuid.uuid4()}.pdf"
    storage.put(victim_key, storage.get(original_key))
    # A corrupted/misrouted row makes the attacker's document point at the victim's object.
    with SessionLocal.begin() as db:
        document = db.get(Document, uuid.UUID(mine["document_id"]))
        assert document is not None
        document.storage_key = victim_key
    response = client.get(
        f"/api/v1/documents/{mine['document_id']}/download?organization_id={a.org}", headers=a.headers
    )
    assert response.status_code == 404 and b"%PDF" not in response.content
    worker.process_document.run(mine["job_id"])  # type: ignore[attr-defined]
    with SessionLocal() as db:
        job = db.get(ProcessingJob, uuid.UUID(mine["job_id"]))
        assert job is not None
        assert (job.status, job.failure_code) == (JobStatus.failed, "STORAGE_OBJECT_MISSING")
    # Deleting the attacker's document must not delete the victim's object either.
    client.delete(f"/api/v1/documents/{mine['document_id']}?organization_id={a.org}", headers=a.headers)
    assert storage.get(victim_key).startswith(b"%PDF")


def test_private_files_have_no_unauthenticated_or_unauthorized_path(client, register) -> None:
    world = World(register, client)
    url = f"/api/v1/documents/{world.document}/download?organization_id={world.victim.org}"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=world.attacker.headers).status_code == 403
    ok = client.get(url, headers=world.victim.headers)
    assert ok.status_code == 200 and ok.headers["cache-control"] == "private, no-store"
    assert "default-src 'none'" in ok.headers["content-security-policy"]
    # Nothing is served statically and no signed URLs are ever minted.
    assert not [r for r in main.app.routes if type(r).__name__ == "Mount"]
    source = "\n".join(p.read_text() for p in Path(main.__file__).parent.glob("*.py"))
    assert "generate_presigned" not in source and "StaticFiles" not in source
    for key in (f"/{world.victim.org}/", "/uploads/", "/static/"):
        assert client.get(key).status_code == 404


def test_s3_objects_are_written_private_and_encrypted(monkeypatch) -> None:
    from app import storage as storage_module

    calls: list[dict[str, Any]] = []

    class FakeClient:
        def put_object(self, **params: Any) -> None:
            calls.append(params)

    provider = storage_module.S3StorageProvider.__new__(storage_module.S3StorageProvider)
    provider.bucket, provider.kms_key_id, provider.client = "b", "kms-key", FakeClient()
    provider.put("org/doc.pdf", b"x")
    assert calls[0]["ServerSideEncryption"] == "aws:kms"
    assert "ACL" not in calls[0]  # bucket policy keeps it private; never a public-read ACL


def test_download_and_export_come_back_only_for_live_documents(client, register) -> None:
    world = World(register, client)
    v = world.victim
    assert client.delete(
        f"/api/v1/documents/{world.document}?organization_id={v.org}", headers=v.headers
    ).status_code == 204
    for path in ("download", "export", "extraction", "status"):
        assert client.get(
            f"/api/v1/documents/{world.document}/{path}?organization_id={v.org}", headers=v.headers
        ).status_code == 404, path
    assert io.BytesIO  # keep imports honest
    assert settings().environment == "development"


def test_api_key_scopes_gate_each_capability_independently(client, register) -> None:
    world = World(register, client)
    v = world.victim

    def key_with(*scopes: str) -> dict[str, str]:
        secret = client.post(
            f"/api/v1/api-keys?organization_id={v.org}&name=scoped", json=list(scopes), headers=v.headers
        ).json()["key"]
        return {"Authorization": f"Bearer {secret}"}

    read_only = key_with("documents:read")
    base = f"/api/v1/documents/{world.document}"
    assert client.get(f"{base}/extraction?organization_id={v.org}", headers=read_only).status_code == 200
    assert client.get(f"{base}/export?organization_id={v.org}", headers=read_only).status_code == 403
    assert client.delete(f"{base}?organization_id={v.org}", headers=read_only).status_code == 403
    assert client.post(f"{base}/review/approve?organization_id={v.org}", headers=read_only).status_code == 403
    exporter = key_with("exports:read")
    assert client.get(f"{base}/export?organization_id={v.org}", headers=exporter).status_code == 200
    assert client.get(f"{base}/download?organization_id={v.org}", headers=exporter).status_code == 403
