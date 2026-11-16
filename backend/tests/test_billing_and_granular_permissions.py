"""Tests for commercial billing gateways/webhooks, granular permissions, key rotation and Phase 3 backup."""

import hashlib
import json
import time
import uuid
from pathlib import Path

import pytest
from pydantic import SecretStr

from app import billing_providers, conversations, worker
from app.core import settings
from app.db import SystemSessionLocal
from app.evaluation.pdfgen import digital_pdf
from app.models import OrganizationMember, Role


def _signed_webhook(client, provider: str, payload: dict, *, ts: int | None = None):
    raw = json.dumps(payload).encode("utf-8")
    sig = billing_providers.sign_webhook_payload(provider, raw, timestamp=ts)
    return client.post(
        f"/api/v1/billing/webhooks/{provider}",
        content=raw,
        headers={"Content-Type": "application/json", "X-Billing-Signature": sig},
    )


def test_payment_providers_checkout_idempotency_webhooks_dunning_and_refunds(
    register, client, monkeypatch: pytest.MonkeyPatch
) -> None:
    acct = register("billing-full")
    org = acct.org

    # 1. Provider catalog includes Stripe, Mercado Pago, Transbank, Sandbox
    prov = client.get(f"/api/v1/billing/providers?organization_id={org}", headers=acct.headers)
    assert prov.status_code == 200
    names = {p["provider"] for p in prov.json()["providers"]}
    assert {"stripe", "mercadopago", "transbank", "sandbox"} <= names

    # 2. Idempotent checkout creation across Stripe (CLP)
    chk1 = client.post(
        f"/api/v1/billing/checkout?organization_id={org}",
        json={
            "target_plan": "PROFESSIONAL",
            "billing_cycle": "monthly",
            "provider": "stripe",
            "currency": "CLP",
            "idempotency_key": "idem-key-0001",
        },
        headers=acct.headers,
    )
    assert chk1.status_code == 200, chk1.text
    assert chk1.json()["idempotent_replay"] is False
    assert chk1.json()["currency"] == "CLP"
    tx_id = chk1.json()["transaction_id"]

    chk2 = client.post(
        f"/api/v1/billing/checkout?organization_id={org}",
        json={
            "target_plan": "PROFESSIONAL",
            "billing_cycle": "monthly",
            "provider": "stripe",
            "currency": "CLP",
            "idempotency_key": "idem-key-0001",
        },
        headers=acct.headers,
    )
    assert chk2.status_code == 200
    assert chk2.json()["idempotent_replay"] is True
    assert chk2.json()["transaction_id"] == tx_id

    # 3. Temporary provider outage records failed tx and returns 503
    adapter = billing_providers.get_payment_adapter("mercadopago")
    monkeypatch.setattr(adapter, "outage_simulated", True)
    outage = client.post(
        f"/api/v1/billing/checkout?organization_id={org}",
        json={
            "target_plan": "STARTER",
            "billing_cycle": "monthly",
            "provider": "mercadopago",
            "currency": "CLP",
            "idempotency_key": "idem-outage-01",
        },
        headers=acct.headers,
    )
    assert outage.status_code == 503 and outage.json()["detail"] == "PAYMENT_PROVIDER_UNAVAILABLE"
    monkeypatch.setattr(adapter, "outage_simulated", False)

    # 4. Invalid webhook signature & expired timestamp rejected (400)
    bad_sig = client.post(
        "/api/v1/billing/webhooks/stripe",
        content=b'{"event_id":"evt_bad","event_type":"payment.succeeded","organization_id":"' + org.encode() + b'"}',
        headers={"X-Billing-Signature": "t=100,v1=deadbeef"},
    )
    assert bad_sig.status_code == 400

    stale_ts = int(time.time()) - 3600
    stale = _signed_webhook(
        client,
        "stripe",
        {"event_id": "evt_stale", "event_type": "payment.succeeded", "organization_id": org},
        ts=stale_ts,
    )
    assert stale.status_code == 400 and stale.json()["detail"] == "WEBHOOK_TIMESTAMP_OUT_OF_RANGE"

    # 5. Valid payment.succeeded webhook activates subscription and records succeeded tx
    evt_payload = {
        "event_id": "evt_pay_ok_1",
        "event_type": "payment.succeeded",
        "organization_id": org,
        "data": {
            "plan_code": "PROFESSIONAL",
            "billing_cycle": "monthly",
            "customer_id": "cus_stripe_1",
            "subscription_id": "sub_stripe_1",
            "transaction_id": "py_stripe_001",
            "amount_cents": 19900,
            "currency": "USD",
            "status": "active",
        },
    }
    ok_wh = _signed_webhook(client, "stripe", evt_payload)
    assert ok_wh.status_code == 200, ok_wh.text
    assert ok_wh.json()["duplicate"] is False
    assert ok_wh.json()["subscription_status"] == "active"
    assert ok_wh.json()["plan"] == "PROFESSIONAL"

    # 6. Duplicate webhook event_id is ignored idempotently
    dup_wh = _signed_webhook(client, "stripe", evt_payload)
    assert dup_wh.status_code == 200 and dup_wh.json()["duplicate"] is True

    # 7. Refund on the succeeded webhook transaction
    tx_list = client.get(f"/api/v1/billing/transactions?organization_id={org}", headers=acct.headers).json()
    succeeded_tx = next(t for t in tx_list["transactions"] if t["external_tx_id"] == "py_stripe_001")
    ref = client.post(
        f"/api/v1/billing/refund?organization_id={org}",
        json={"transaction_id": succeeded_tx["id"], "amount_cents": 5000, "reason": "partial_adjustment"},
        headers=acct.headers,
    )
    assert ref.status_code == 200, ref.text
    assert ref.json()["status"] == "refunded" and ref.json()["amount_cents"] == 5000

    # 8. Failed payments -> past_due (attempts 1..2) -> unpaid (attempt 3, degrades effective plan to FREE)
    for i in (1, 2):
        fail_wh = _signed_webhook(
            client,
            "stripe",
            {
                "event_id": f"evt_fail_{i}",
                "event_type": "payment.failed",
                "organization_id": org,
                "data": {"amount_cents": 19900, "currency": "USD"},
            },
        )
        assert fail_wh.status_code == 200
        assert fail_wh.json()["subscription_status"] == "past_due"
        assert fail_wh.json()["failed_payment_count"] == i

    fail_3 = _signed_webhook(
        client,
        "stripe",
        {
            "event_id": "evt_fail_3",
            "event_type": "payment.failed",
            "organization_id": org,
            "data": {"amount_cents": 19900, "currency": "USD"},
        },
    )
    assert fail_3.status_code == 200
    assert fail_3.json()["subscription_status"] == "unpaid"
    sub_view = client.get(f"/api/v1/billing/subscription?organization_id={org}", headers=acct.headers).json()
    assert sub_view["status"] == "unpaid"
    assert sub_view["plan"]["code"] == "FREE"  # degraded while unpaid

    # 9. Renewal webhook restores active status and PROFESSIONAL limits
    renew = _signed_webhook(
        client,
        "stripe",
        {
            "event_id": "evt_renew_1",
            "event_type": "subscription.renewed",
            "organization_id": org,
            "data": {"plan_code": "PROFESSIONAL", "amount_cents": 19900, "currency": "USD"},
        },
    )
    assert renew.status_code == 200 and renew.json()["subscription_status"] == "active"

    # 10. Cancel subscription & reconcile
    cancel = client.post(
        f"/api/v1/billing/subscription/cancel?organization_id={org}",
        json={"immediate": True, "reason": "test_cancel"},
        headers=acct.headers,
    )
    assert cancel.status_code == 200 and cancel.json()["status"] == "canceled"
    rec = client.post(f"/api/v1/billing/reconcile?organization_id={org}", headers=acct.headers)
    assert rec.status_code == 200 and rec.json()["effective_plan"] == "FREE"


def test_granular_permissions_delete_scope_protected_docs_creator_grant_and_project_webhooks(
    register, client, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = register("gran-owner")
    member = register("gran-member")
    org = owner.org
    member_uid = uuid.UUID(client.get("/api/v1/auth/me", headers=member.headers).json()["id"])
    with SystemSessionLocal() as db:
        db.add(OrganizationMember(organization_id=uuid.UUID(org), user_id=member_uid, role=Role.member))
        db.commit()

    # 1. Non-admin member creates a project and automatically receives write access to it (not to others)
    other_proj = owner.project("owner-only")
    created = client.post(
        f"/api/v1/projects?organization_id={org}&name=member-case",
        headers=member.headers,
    )
    assert created.status_code == 201
    member_proj = created.json()["id"]

    # Member can see & upload to member_proj, cannot see other_proj, and has no admin grant privileges
    visible = {p["id"] for p in client.get(f"/api/v1/projects?organization_id={org}", headers=member.headers).json()}
    assert visible == {member_proj}
    up = client.post(
        f"/api/v1/documents?organization_id={org}&project_id={member_proj}",
        files={"file": ("case.pdf", digital_pdf([["Member project document"]]), "application/pdf")},
        headers=member.headers,
    )
    assert up.status_code == 202
    doc_id = up.json()["document_id"]
    worker.process_document.run(up.json()["job_id"])  # type: ignore[attr-defined]

    # 2. Protected document: member cannot mark protected, owner can; protected doc blocks delete without confirm
    assert (
        client.patch(
            f"/api/v1/documents/{doc_id}/metadata?organization_id={org}",
            json={"protected": True},
            headers=member.headers,
        ).status_code
        == 403
    )
    prot = client.patch(
        f"/api/v1/documents/{doc_id}/metadata?organization_id={org}",
        json={"protected": True},
        headers=owner.headers,
    )
    assert prot.status_code == 200 and prot.json()["protected"] is True

    # Member cannot delete protected doc
    assert (
        client.delete(f"/api/v1/documents/{doc_id}?organization_id={org}", headers=member.headers).status_code
        == 409
    )
    # Owner without confirm=true gets 409 DOCUMENT_PROTECTED_CONFIRM_REQUIRED
    no_conf = client.delete(f"/api/v1/documents/{doc_id}?organization_id={org}", headers=owner.headers)
    assert no_conf.status_code == 409 and no_conf.json()["detail"] == "DOCUMENT_PROTECTED_CONFIRM_REQUIRED"

    # Owner with confirm=true deletes, and can restore within retention window
    assert (
        client.delete(
            f"/api/v1/documents/{doc_id}?organization_id={org}&confirm=true", headers=owner.headers
        ).status_code
        == 204
    )
    restored = client.post(
        f"/api/v1/documents/{doc_id}/restore?organization_id={org}", headers=owner.headers
    )
    assert restored.status_code == 200 and restored.json()["restored"] is True
    # Unprotect for subsequent API key test
    client.patch(
        f"/api/v1/documents/{doc_id}/metadata?organization_id={org}",
        json={"protected": False},
        headers=owner.headers,
    )

    # 3. Granular documents:delete API key scope when REQUIRE_EXPLICIT_DELETE_SCOPE=true
    monkeypatch.setattr(settings(), "require_explicit_delete_scope", True)
    write_only_key = client.post(
        f"/api/v1/api-keys?organization_id={org}&name=write-only&project_id={member_proj}",
        json=["documents:write", "documents:read"],
        headers=owner.headers,
    ).json()["key"]
    del_key = client.post(
        f"/api/v1/api-keys?organization_id={org}&name=del-only&project_id={member_proj}",
        json=["documents:delete"],
        headers=owner.headers,
    ).json()["key"]

    assert (
        client.delete(
            f"/api/v1/documents/{doc_id}?organization_id={org}",
            headers={"Authorization": f"Bearer {write_only_key}"},
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/api/v1/documents/{doc_id}?organization_id={org}",
            headers={"Authorization": f"Bearer {del_key}"},
        ).status_code
        == 204
    )

    # 4. Webhooks filtered by project_id only fire for matching projects
    wh_p1 = client.post(
        f"/api/v1/webhooks?organization_id={org}",
        json={
            "url": "https://example.com/hook-p1",
            "events": ["document.completed"],
            "project_ids": [member_proj],
        },
        headers=owner.headers,
    )
    assert wh_p1.status_code == 201 and wh_p1.json()["project_ids"] == [member_proj]

    delivered_urls: list[str] = []

    def fake_deliver(wh_id, url, event, payload):
        delivered_urls.append(url)
        return True, 200, None

    monkeypatch.setattr(worker, "deliver_webhook_request", fake_deliver)
    up_other = owner.upload(other_proj, digital_pdf([["Other project doc"]]), "other.pdf")
    doc_other_id = up_other.json()["document_id"]
    worker.dispatch_webhooks.run(org, "document.completed", {"document_id": doc_other_id})  # type: ignore[attr-defined]
    assert delivered_urls == []  # filtered out because doc belongs to other_proj

    client.post(f"/api/v1/documents/{doc_id}/restore?organization_id={org}", headers=owner.headers)
    worker.dispatch_webhooks.run(org, "document.completed", {"document_id": doc_id})  # type: ignore[attr-defined]
    assert delivered_urls == ["https://example.com/hook-p1"]


def test_conversation_key_rotation_and_contextrag_phase3_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    org_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    key_v1 = "v1-master-encryption-key-0123456789abcdef"
    key_v2 = "v2-master-encryption-key-fedcba9876543210"

    monkeypatch.setattr(settings(), "conversation_encryption_key", SecretStr(key_v1))
    monkeypatch.setattr(settings(), "conversation_previous_encryption_key", None)
    sealed_v1 = conversations.seal(org_id, conv_id, 1, "user", {"question": "historical turn"})

    # Rotate: primary = key_v2, previous = key_v1
    monkeypatch.setattr(settings(), "conversation_encryption_key", SecretStr(key_v2))
    monkeypatch.setattr(settings(), "conversation_previous_encryption_key", SecretStr(key_v1))
    opened = conversations.open_turn(org_id, conv_id, 1, "user", sealed_v1)
    assert opened == {"question": "historical turn"}

    # Re-encrypt in place to v2 and drop previous key
    resealed_v2 = conversations.reencrypt_turn(org_id, conv_id, 1, "user", sealed_v1)
    monkeypatch.setattr(settings(), "conversation_previous_encryption_key", None)
    assert conversations.open_turn(org_id, conv_id, 1, "user", resealed_v2) == {"question": "historical turn"}

    # Phase 3 ContextRAG retirement helper verifies table dropped and exports SHA256 certificate
    from scripts.contextrag_phase3_retire import inspect_and_backup

    report = inspect_and_backup(tmp_path)
    assert report["table_exists"] is False
    assert report["phase3_drop_executed"] is True
    backup_file = Path(report["backup_file"])
    assert backup_file.exists()
    assert hashlib.sha256(backup_file.read_bytes()).hexdigest() == report["backup_sha256"]
