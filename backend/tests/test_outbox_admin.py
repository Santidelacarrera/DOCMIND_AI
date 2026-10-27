"""Administrative inspection and cancellation of outbox events.

Each refusal below is a failure mode a careless or hostile operator could hit: a viewer, an API key, a copied
identifier from another organization, a deletion that must not be cancelled, a claim in progress, a repeat.
ContextRAG is faked; no document text is sent anywhere.
"""

import uuid

import httpx
import pytest
from sqlalchemy import select

from app import contextrag_sync as sync
from app import worker
from app.core import settings
from app.db import SessionLocal
from app.integrations import contextrag as cr
from app.models import AuditLog, ContextRagEvent, OrganizationMember, Role
from tests.conftest import make_pdf


class _NoKeys:
    """A peer that must never be reached: refusals happen before any request is sent."""

    calls = 0

    def request(self, method, url, **kwargs):
        _NoKeys.calls += 1
        return httpx.Response(200, json={"documents": [], "tenant": "t"})


@pytest.fixture
def enabled(monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "contextrag_sync_enabled", True)
    monkeypatch.setattr(cfg, "contextrag_url", "http://localhost:8000")
    monkeypatch.setattr(cfg, "contextrag_keys_file", "unused")
    _NoKeys.calls = 0
    monkeypatch.setattr(
        sync,
        "build_client",
        lambda: cr.ContextRagClient("http://localhost:8000", {}, client=_NoKeys()),  # type: ignore[arg-type]
    )


def _processed(register, monkeypatch, text: str, label: str):
    class OCR:
        def extract_pages(self, content: bytes) -> list[str]:
            return [text]

    monkeypatch.setattr(worker, "TesseractProvider", OCR)
    acct = register(label)
    upload = acct.upload(acct.project(), make_pdf(1)).json()
    worker.process_document.run(upload["job_id"])  # type: ignore[attr-defined]
    return acct, upload["document_id"]


def _event(org: str, document: str, kind: str = "upsert") -> ContextRagEvent:
    with SessionLocal() as db:
        return db.scalar(
            select(ContextRagEvent).where(
                ContextRagEvent.organization_id == uuid.UUID(org),
                ContextRagEvent.document_id == uuid.UUID(document),
                ContextRagEvent.kind == kind,
            )
        )


def _failed_upsert(register, monkeypatch, enabled, label="a"):
    acct, doc = _processed(register, monkeypatch, "Failed without a key.", label)
    sync.process_due(now=sync.utcnow() + sync.timedelta(minutes=1))  # no key -> failed
    event = _event(acct.org, doc)
    assert event.status == "failed" and event.last_error == "NO_KEY_FOR_TENANT"
    return acct, doc, event


def _cancel(
    client,
    acct,
    event_id,
    *,
    confirm=None,
    reason="Operator confirmed: wrong tenant key, content obsolete.",
    headers=None,
):
    body = {"confirm": confirm if confirm is not None else event_id, "reason": reason}
    return client.post(
        f"/api/v1/contextrag/events/{event_id}/cancel?organization_id={acct.org}",
        json=body,
        headers=headers if headers is not None else acct.headers,
    )


# --------------------------------------------------------------------------- inspection
def test_admins_can_list_their_own_events_without_content(
    register, monkeypatch, enabled, client
) -> None:
    acct, _doc, event = _failed_upsert(register, monkeypatch, enabled)
    response = client.get(
        f"/api/v1/contextrag/events/list?organization_id={acct.org}&status=failed",
        headers=acct.headers,
    )
    assert response.status_code == 200
    [item] = response.json()["events"]
    assert item["event_id"] == str(event.id) and item["cancellable"] is True
    assert set(item) == {
        "event_id",
        "kind",
        "document_id",
        "status",
        "attempts",
        "last_error",
        "next_attempt_at",
        "updated_at",
        "cancellable",
    }
    assert "Failed without a key" not in response.text


def test_viewers_and_api_keys_cannot_list_or_cancel(register, monkeypatch, enabled, client) -> None:
    acct, doc, event = _failed_upsert(register, monkeypatch, enabled)
    viewer = register("viewer")
    uid = viewer.client.get("/api/v1/auth/me", headers=viewer.headers).json()["id"]
    with SessionLocal() as db:
        db.add(
            OrganizationMember(
                organization_id=uuid.UUID(acct.org), user_id=uuid.UUID(uid), role=Role.viewer
            )
        )
        db.commit()
    assert (
        client.get(
            f"/api/v1/contextrag/events/list?organization_id={acct.org}", headers=viewer.headers
        ).status_code
        == 403
    )
    assert _cancel(client, acct, str(event.id), headers=viewer.headers).status_code == 403
    key = client.post(
        f"/api/v1/api-keys?organization_id={acct.org}&name=k",
        json=["documents:read", "documents:write"],
        headers=acct.headers,
    ).json()
    key_headers = {"Authorization": f"Bearer {key['key']}"}
    assert (
        client.get(
            f"/api/v1/contextrag/events/list?organization_id={acct.org}", headers=key_headers
        ).status_code
        == 403
    )
    assert _cancel(client, acct, str(event.id), headers=key_headers).status_code == 403
    assert _event(acct.org, doc).status == "failed"  # nothing changed


def test_anonymous_requests_are_refused(register, monkeypatch, enabled, client) -> None:
    acct, _doc, _event_row = _failed_upsert(register, monkeypatch, enabled)
    assert (
        client.get(f"/api/v1/contextrag/events/list?organization_id={acct.org}").status_code == 401
    )


# --------------------------------------------------------------------------- isolation
def test_an_admin_of_another_organization_gets_not_found_for_this_event(
    register, monkeypatch, enabled, client
) -> None:
    acct, doc, event = _failed_upsert(register, monkeypatch, enabled, label="victim")
    attacker = register("attacker")
    response = _cancel(client, attacker, str(event.id))
    assert response.status_code == 404
    assert response.json()["detail"] == "EVENT_NOT_FOUND"  # same answer as an unknown identifier
    assert _event(acct.org, doc).status == "failed"
    listing = client.get(
        f"/api/v1/contextrag/events/list?organization_id={attacker.org}", headers=attacker.headers
    )
    assert str(event.id) not in listing.text


def test_a_copied_organization_id_in_the_path_is_not_enough(
    register, monkeypatch, enabled, client
) -> None:
    _acct, _doc, event = _failed_upsert(register, monkeypatch, enabled, label="victim2")
    attacker = register("attacker2")
    response = client.post(
        f"/api/v1/contextrag/events/{event.id}/cancel?organization_id={attacker.org}",
        json={"confirm": str(event.id), "reason": "Trying to use my own organization"},
        headers=attacker.headers,
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- protection against accidents
def test_confirmation_must_be_the_exact_event_identifier(
    register, monkeypatch, enabled, client
) -> None:
    acct, doc, event = _failed_upsert(register, monkeypatch, enabled)
    assert (
        _cancel(client, acct, str(event.id), confirm=str(uuid.uuid4())).json()["detail"]
        == "CONFIRMATION_MISMATCH"
    )
    assert (
        _cancel(client, acct, str(event.id), confirm="").status_code == 422
    )  # not even the right length
    assert _event(acct.org, doc).status == "failed"


def test_a_reason_is_required(register, monkeypatch, enabled, client) -> None:
    acct, doc, event = _failed_upsert(register, monkeypatch, enabled)
    assert _cancel(client, acct, str(event.id), reason="ok").status_code == 422
    assert _event(acct.org, doc).status == "failed"


def test_deletions_are_never_cancellable(register, monkeypatch, enabled, client) -> None:
    """A cancelled retirement would leave a deleted document retrievable in ContextRAG. Refused, always."""
    acct, doc = _processed(register, monkeypatch, "Deleted document.", "retire")
    client.delete(f"/api/v1/documents/{doc}?organization_id={acct.org}", headers=acct.headers)
    retire = _event(acct.org, doc, kind="retire")
    response = _cancel(client, acct, str(retire.id))
    assert response.status_code == 409
    assert response.json()["detail"] == "RETIREMENT_CANNOT_BE_CANCELLED"
    assert _event(acct.org, doc, kind="retire").status == "pending"


def test_events_in_progress_or_already_final_cannot_be_cancelled(
    register, monkeypatch, enabled, client
) -> None:
    acct, _doc, event = _failed_upsert(register, monkeypatch, enabled)
    with SessionLocal.begin() as db:
        row = db.get(ContextRagEvent, event.id)
        row.status, row.claim_token = "processing", "live-worker"
    assert _cancel(client, acct, str(event.id)).json()["detail"] == "EVENT_NOT_CANCELLABLE"
    with SessionLocal.begin() as db:
        row = db.get(ContextRagEvent, event.id)
        row.status, row.claim_token = "done", None
    assert _cancel(client, acct, str(event.id)).json()["detail"] == "EVENT_NOT_CANCELLABLE"


# --------------------------------------------------------------------------- the cancellation itself
def test_a_cancelled_upsert_is_never_delivered_nor_rebuilt_by_reconciliation(
    register, monkeypatch, enabled, client
) -> None:
    acct, doc, event = _failed_upsert(register, monkeypatch, enabled)
    response = _cancel(client, acct, str(event.id))
    assert response.status_code == 200, response.text
    assert (
        response.json()["status"] == "cancelled" and response.json()["previous_status"] == "failed"
    )
    cancelled = _event(acct.org, doc)
    assert (
        cancelled.status == "cancelled"
        and cancelled.last_error == sync.CANCELLED_BY_ADMIN
        and cancelled.claim_token is None
    )

    _NoKeys.calls = 0
    result = sync.process_due(now=sync.utcnow() + sync.timedelta(days=2))
    assert result["done"] == 0 and _NoKeys.calls == 0  # never sent
    assert sync.reconcile() == {"upsert": 0, "retire": 0}  # not silently recreated
    assert _event(acct.org, doc).status == "cancelled"


def test_the_cancellation_is_audited_with_actor_reason_and_previous_state(
    register, monkeypatch, enabled, client
) -> None:
    acct, doc, event = _failed_upsert(register, monkeypatch, enabled)
    reason = "Wrong tenant key was configured; content is obsolete."
    assert _cancel(client, acct, str(event.id), reason=reason).status_code == 200
    with SessionLocal() as db:
        log = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "contextrag.event.cancel", AuditLog.target_id == str(event.id)
            )
        )
        assert log is not None
        assert log.organization_id == uuid.UUID(acct.org)
        assert log.actor_id is not None
        assert log.metadata_["reason"] == reason
        assert log.metadata_["previous_status"] == "failed"
        assert log.metadata_["document_id"] == doc
        assert "Failed without a key" not in str(log.metadata_)


def test_repeating_the_cancellation_is_refused_and_changes_nothing(
    register, monkeypatch, enabled, client
) -> None:
    acct, _doc, event = _failed_upsert(register, monkeypatch, enabled)
    assert _cancel(client, acct, str(event.id)).status_code == 200
    again = _cancel(client, acct, str(event.id))
    assert again.status_code == 409 and again.json()["detail"] == "EVENT_NOT_CANCELLABLE"
    with SessionLocal() as db:
        logs = db.scalars(
            select(AuditLog).where(
                AuditLog.action == "contextrag.event.cancel", AuditLog.target_id == str(event.id)
            )
        ).all()
        assert len(logs) == 1  # one decision, one audit record


def test_a_failed_refusal_leaves_no_audit_record(register, monkeypatch, enabled, client) -> None:
    acct, _doc, event = _failed_upsert(register, monkeypatch, enabled)
    assert _cancel(client, acct, str(event.id), confirm=str(uuid.uuid4())).status_code == 400
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(AuditLog).where(
                    AuditLog.action == "contextrag.event.cancel",
                    AuditLog.target_id == str(event.id),
                )
            )
            is None
        )
