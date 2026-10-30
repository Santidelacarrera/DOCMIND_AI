"""Billing totals are an administrator view: people need the admin role, API keys need ``usage:read``.

Decision recorded in docs/AUTHORIZATION_POLICY.md (option A): viewers and members no longer read
usage, because volume and quota state is commercial information they do not need.
"""

import uuid

import pytest

from app.db import SessionLocal
from app.models import OrganizationMember, Role


def _join(owner, other, role: Role) -> None:
    uid = other.client.get("/api/v1/auth/me", headers=other.headers).json()["id"]
    with SessionLocal() as db:
        db.add(
            OrganizationMember(
                organization_id=uuid.UUID(owner.org), user_id=uuid.UUID(uid), role=role
            )
        )
        db.commit()


@pytest.mark.parametrize("role", [Role.viewer, Role.member])
def test_viewers_and_members_cannot_read_organization_usage(register, client, role) -> None:
    owner = register("owner")
    person = register(role.value)
    _join(owner, person, role)
    response = client.get(f"/api/v1/usage?organization_id={owner.org}", headers=person.headers)
    assert response.status_code == 403


@pytest.mark.parametrize("role", [Role.admin, Role.owner])
def test_admins_and_owners_can_read_organization_usage(register, client, role) -> None:
    owner = register("owner")
    if role is Role.owner:
        person = owner
    else:
        person = register("admin")
        _join(owner, person, role)
    response = client.get(f"/api/v1/usage?organization_id={owner.org}", headers=person.headers)
    assert response.status_code == 200
    assert set(response.json()) >= {"plan", "metrics"}


def test_an_admin_of_another_organization_cannot_read_usage(register, client) -> None:
    owner = register("owner")
    stranger = register("stranger")
    response = client.get(f"/api/v1/usage?organization_id={owner.org}", headers=stranger.headers)
    assert response.status_code in (403, 404)
