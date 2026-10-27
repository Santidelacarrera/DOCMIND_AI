"""Documents the current policy for billing totals: any organization member can read them.

The API key scope ``usage:read`` only applies to keys, not to people. This test records the present
behaviour so a change is deliberate; see docs/AUDIT_PRODUCTION_2026-10-27.md (decision pending).
"""

import uuid

from app.db import SessionLocal
from app.models import OrganizationMember, Role


def test_a_viewer_can_read_organization_usage_totals_today(register, client) -> None:
    owner = register("owner")
    viewer = register("viewer")
    uid = viewer.client.get("/api/v1/auth/me", headers=viewer.headers).json()["id"]
    with SessionLocal() as db:
        db.add(
            OrganizationMember(
                organization_id=uuid.UUID(owner.org), user_id=uuid.UUID(uid), role=Role.viewer
            )
        )
        db.commit()
    response = client.get(f"/api/v1/usage?organization_id={owner.org}", headers=viewer.headers)
    assert response.status_code == 200
    assert set(response.json()) >= {"plan", "metrics"}
