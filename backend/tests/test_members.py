"""Membership management and document deletion."""

from pathlib import Path

from app.core import settings
from tests.conftest import make_pdf


def _add(client, owner, email, role="MEMBER"):
    return client.post(
        f"/api/v1/members?organization_id={owner.org}&email={email}&role={role}",
        headers=owner.headers,
    )


def _role_of(client, owner, email):
    rows = client.get(f"/api/v1/members?organization_id={owner.org}", headers=owner.headers).json()
    return {r["email"]: r["role"] for r in rows}.get(email)


def test_owner_manages_members(client, register) -> None:
    owner, other = register("owner"), register("other")
    assert _add(client, owner, "nobody@example.com").status_code == 404
    added = _add(client, owner, other.email, "VIEWER")
    assert added.status_code == 201 and added.json()["role"] == "VIEWER"
    assert _add(client, owner, other.email).status_code == 409
    # The new member can now see the organization, read-only.
    assert (
        client.get(f"/api/v1/projects?organization_id={owner.org}", headers=other.headers).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/projects?organization_id={owner.org}&name=x", headers=other.headers
        ).status_code
        == 403
    )
    other_id = added.json()["user_id"]
    changed = client.patch(
        f"/api/v1/members/{other_id}?organization_id={owner.org}&role=MEMBER", headers=owner.headers
    )
    assert changed.status_code == 200 and _role_of(client, owner, other.email) == "MEMBER"
    assert (
        client.delete(
            f"/api/v1/members/{other_id}?organization_id={owner.org}", headers=owner.headers
        ).status_code
        == 204
    )
    assert _role_of(client, owner, other.email) is None
    assert (
        client.get(f"/api/v1/projects?organization_id={owner.org}", headers=other.headers).status_code
        == 403
    )


def test_non_admins_cannot_manage_members(client, register) -> None:
    owner, member, outsider = register("o"), register("m"), register("x")
    _add(client, owner, member.email, "MEMBER")
    forbidden = client.post(
        f"/api/v1/members?organization_id={owner.org}&email={outsider.email}", headers=member.headers
    )
    assert forbidden.status_code == 403
    assert client.get(f"/api/v1/members?organization_id={owner.org}", headers=member.headers).status_code == 403
    assert client.get(f"/api/v1/members?organization_id={owner.org}", headers=outsider.headers).status_code == 403


def test_admin_cannot_touch_owners_and_last_owner_is_protected(client, register) -> None:
    owner, admin = register("o"), register("a")
    admin_id = _add(client, owner, admin.email, "ADMIN").json()["user_id"]
    owner_id = next(
        r["user_id"]
        for r in client.get(f"/api/v1/members?organization_id={owner.org}", headers=owner.headers).json()
        if r["email"] == owner.email
    )
    third = register("t")
    grant_owner = client.post(
        f"/api/v1/members?organization_id={owner.org}&email={third.email}&role=OWNER",
        headers=admin.headers,
    )
    assert grant_owner.status_code == 403
    assert (
        client.patch(
            f"/api/v1/members/{admin_id}?organization_id={owner.org}&role=OWNER", headers=admin.headers
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/api/v1/members/{owner_id}?organization_id={owner.org}", headers=admin.headers
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/v1/members/{owner_id}?organization_id={owner.org}&role=ADMIN", headers=owner.headers
        ).status_code
        == 409
    )
    assert (
        client.delete(
            f"/api/v1/members/{owner_id}?organization_id={owner.org}", headers=owner.headers
        ).status_code
        == 409
    )
    # Anyone may leave an organization they are not the last owner of.
    assert (
        client.delete(
            f"/api/v1/members/{admin_id}?organization_id={owner.org}", headers=admin.headers
        ).status_code
        == 204
    )


def test_api_keys_cannot_manage_members(client, register) -> None:
    owner = register()
    key = client.post(
        f"/api/v1/api-keys?organization_id={owner.org}&name=k",
        json=["documents:read"],
        headers=owner.headers,
    ).json()["key"]
    assert (
        client.get(
            f"/api/v1/members?organization_id={owner.org}", headers={"Authorization": f"Bearer {key}"}
        ).status_code
        == 403
    )


def test_delete_document_removes_object_and_hides_it(client, register) -> None:
    owner, intruder = register("o"), register("i")
    project = owner.project()
    content = make_pdf()
    doc = owner.upload(project, content).json()["document_id"]
    root = Path(settings().local_storage_path)
    stored = [p for p in root.rglob("*.pdf") if str(owner.org) in p.parts]
    assert len(stored) == 1
    base = f"/api/v1/documents/{doc}"
    assert client.delete(f"{base}?organization_id={intruder.org}", headers=intruder.headers).status_code == 404
    assert client.delete(f"{base}?organization_id={owner.org}", headers=owner.headers).status_code == 204
    assert client.delete(f"{base}?organization_id={owner.org}", headers=owner.headers).status_code == 404
    for suffix in ("", "/status", "/extraction", "/download"):
        assert (
            client.get(f"{base}{suffix}?organization_id={owner.org}", headers=owner.headers).status_code
            == 404
        ), suffix
    assert client.get(f"/api/v1/documents?organization_id={owner.org}", headers=owner.headers).json() == []
    assert not [p for p in stored if p.exists()]
    # The same file can be uploaded again after deletion.
    assert owner.upload(project, content).status_code == 202
