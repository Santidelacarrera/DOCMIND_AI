"""Human review: queue, correction with a reason, sign-off and export of the result."""

import csv
import io
import json
import uuid

from sqlalchemy import select

from app import worker
from app.db import SessionLocal
from app.models import ExtractionField, User


def _processed(register, client):
    acct = register()
    upload = acct.upload(acct.project()).json()
    worker.process_document.run(upload["job_id"])  # type: ignore[attr-defined]
    base = f"/api/v1/documents/{upload['document_id']}"
    data = client.get(f"{base}/extraction?organization_id={acct.org}", headers=acct.headers).json()
    return acct, upload, base, data


def _patch(client, acct, field_id: str, body: object, reason: str | None = None):
    url = f"/api/v1/extraction-fields/{field_id}?organization_id={acct.org}"
    if reason is not None:
        url += f"&reason={reason}"
    return client.patch(
        url, content=json.dumps(body), headers={**acct.headers, "Content-Type": "application/json"}
    )


def test_flagged_documents_enter_the_queue_and_leave_it_when_approved(client, register) -> None:
    acct, upload, base, data = _processed(register, client)
    queue = client.get(f"/api/v1/review/queue?organization_id={acct.org}", headers=acct.headers).json()
    assert [q["document_id"] for q in queue] == [upload["document_id"]]
    assert queue[0]["flagged_fields"] == ["total_amount"]  # mock confidence 0.55 < 0.70

    # Cannot sign off while a flagged field is still unverified.
    refused = client.post(f"{base}/review/approve?organization_id={acct.org}", headers=acct.headers)
    assert refused.status_code == 409
    assert refused.json() == {"detail": "REVIEW_INCOMPLETE", "pending_fields": ["total_amount"]}

    field = next(f for f in data["fields"] if f["name"] == "total_amount")
    assert field["needs_review"] is True
    assert _patch(client, acct, field["id"], 15000, "confirmed").status_code == 200
    approved = client.post(f"{base}/review/approve?organization_id={acct.org}", headers=acct.headers)
    assert approved.status_code == 200

    after = client.get(f"{base}/extraction?organization_id={acct.org}", headers=acct.headers).json()
    assert after["review"]["status"] == "approved" and after["review"]["pending_fields"] == []
    assert client.get(f"/api/v1/review/queue?organization_id={acct.org}", headers=acct.headers).json() == []


def test_correction_keeps_the_model_value_and_records_who_why_and_when(client, register) -> None:
    acct, _, base, data = _processed(register, client)
    field = next(f for f in data["fields"] if f["name"] == "total_amount")
    result = _patch(client, acct, field["id"], 15500.5, "ocr_misread")
    assert result.json()["changed"] is True and result.json()["correction_reason"] == "ocr_misread"

    row = next(
        f for f in client.get(f"{base}/extraction?organization_id={acct.org}", headers=acct.headers).json()["fields"]
        if f["id"] == field["id"]
    )
    assert row["value"] == 15500.5 and row["original_value"] == 15000
    assert row["manually_verified"] and row["correction_reason"] == "ocr_misread" and row["corrected_at"]
    assert row["needs_review"] is False
    with SessionLocal() as db:
        stored = db.get(ExtractionField, uuid.UUID(field["id"]))
        assert stored is not None
        user = db.scalar(select(User).where(User.email == acct.email))
        assert user is not None and stored.corrected_by == user.id


def test_unchanged_value_is_a_confirmation_and_missing_reason_is_labelled(client, register) -> None:
    acct, _, _, data = _processed(register, client)
    by_name = {f["name"]: f for f in data["fields"]}
    confirmed = _patch(client, acct, by_name["customer_name"]["id"], "Juan Pérez")
    assert confirmed.json()["changed"] is False and confirmed.json()["correction_reason"] == "confirmed"
    changed = _patch(client, acct, by_name["invoice_number"]["id"], "TEST-002")
    assert changed.json()["changed"] is True and changed.json()["correction_reason"] == "unspecified"
    too_long = _patch(client, acct, by_name["invoice_number"]["id"], "x", "r" * 201)
    assert too_long.status_code == 422


def test_a_value_can_be_cleared_with_null_but_an_empty_request_is_refused(client, register) -> None:
    acct, _, base, data = _processed(register, client)
    field = data["fields"][0]
    url = f"/api/v1/extraction-fields/{field['id']}?organization_id={acct.org}"
    assert client.patch(url, headers=acct.headers).status_code == 422  # no body at all
    assert _patch(client, acct, field["id"], None, "hallucinated_value").status_code == 200
    after = client.get(f"{base}/extraction?organization_id={acct.org}", headers=acct.headers).json()
    cleared = next(f for f in after["fields"] if f["id"] == field["id"])
    assert cleared["value"] is None and cleared["original_value"] == field["value"]


def test_viewers_cannot_correct_or_approve_and_deleted_documents_cannot_be_edited(client, register) -> None:
    acct, _, base, data = _processed(register, client)
    viewer = register("viewer")
    client.post(
        f"/api/v1/members?organization_id={acct.org}&email={viewer.email}&role=VIEWER", headers=acct.headers
    )
    field = data["fields"][0]
    assert _patch(client, viewer, field["id"], "x").status_code in {403, 404}
    assert client.post(f"{base}/review/approve?organization_id={acct.org}", headers=viewer.headers).status_code == 403
    client.delete(f"{base}?organization_id={acct.org}", headers=acct.headers)
    assert _patch(client, acct, field["id"], "x").status_code == 404


def test_export_carries_the_correction_trail(client, register) -> None:
    acct, _, base, data = _processed(register, client)
    field = next(f for f in data["fields"] if f["name"] == "total_amount")
    _patch(client, acct, field["id"], 16000, "wrong_value")
    csv_text = client.get(f"{base}/export?organization_id={acct.org}&format=csv", headers=acct.headers).text
    rows = {r["name"]: r for r in csv.DictReader(io.StringIO(csv_text))}
    assert set(rows["total_amount"]) >= {
        "name", "value", "original_value", "confidence", "manually_verified",
        "needs_review", "correction_reason", "review_status",
    }
    total = rows["total_amount"]
    assert (total["value"], total["original_value"], total["correction_reason"]) == ("16000", "15000", "wrong_value")
    assert total["manually_verified"] == "True" and total["needs_review"] == "False"
    exported = client.get(f"{base}/export?organization_id={acct.org}&format=json", headers=acct.headers).json()
    assert exported["review"]["status"] == "pending"  # corrected, but nobody has signed off yet
