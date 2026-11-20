"""Tests for Chilean Ley N° 21.719 ARCOP privacy endpoints and transparency posture."""

from __future__ import annotations

import uuid
from typing import Any

from app.core import settings
from app.db import SessionLocal
from app.models import AuditLog, QueryHistory


def test_privacy_policy_summary_reports_posture_and_gemini_warning(
    register: Any, client: Any, monkeypatch: Any
) -> None:
    acct = register()
    monkeypatch.setattr(settings(), "llm_provider", "gemini")
    res = client.get(
        f"/api/v1/privacy/policy-summary?organization_id={acct.org}",
        headers={"Authorization": f"Bearer {acct.token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert "21.719" in body["framework"]
    assert body["third_party_ai_transfer_warning"] is True
    assert "access_and_portability" in body["arcop_endpoints"]


def test_privacy_export_and_history_erasure_arcop(register: Any, client: Any) -> None:
    acct_a = register()
    acct_b = register()
    org_a = uuid.UUID(acct_a.org)
    headers_a = {"Authorization": f"Bearer {acct_a.token}"}
    headers_b = {"Authorization": f"Bearer {acct_b.token}"}

    # Seed query history for user A in org A
    with SessionLocal.begin() as db:
        from sqlalchemy import select

        from app.models import User

        user_a = db.scalar(select(User).where(User.email == acct_a.email))
        assert user_a is not None
        db.add(
            QueryHistory(
                organization_id=org_a,
                user_id=user_a.id,
                question="¿Cuál es el plazo del contrato?",
                answer_preview="30 días [1].",
                abstained=False,
                mode="generative",
                citations=[],
                document_ids=[],
            )
        )

    # Cross-tenant access must be blocked
    cross = client.get(
        f"/api/v1/privacy/me/export?organization_id={acct_a.org}",
        headers=headers_b,
    )
    assert cross.status_code in (403, 404)

    # Portability export returns user's data without password hash
    exp = client.get(
        f"/api/v1/privacy/me/export?organization_id={acct_a.org}",
        headers=headers_a,
    )
    assert exp.status_code == 200
    data = exp.json()
    assert data["schema_version"] == "cl-21719-v1"
    assert data["user"]["email"] == acct_a.email
    assert "password_hash" not in data["user"]
    assert len(data["query_history"]) == 1

    # Erasure removes user's query history and logs audit event
    erased = client.delete(
        f"/api/v1/privacy/me/history?organization_id={acct_a.org}",
        headers=headers_a,
    )
    assert erased.status_code == 200
    assert erased.json()["queries_deleted"] == 1

    exp_after = client.get(
        f"/api/v1/privacy/me/export?organization_id={acct_a.org}",
        headers=headers_a,
    )
    assert exp_after.status_code == 200
    assert exp_after.json()["query_history"] == []

    with SessionLocal() as db:
        actions = {
            a.action
            for a in db.query(AuditLog)
            .filter(AuditLog.organization_id == org_a)
            .all()
        }
        assert "privacy.data_export" in actions
        assert "privacy.history_erased" in actions
