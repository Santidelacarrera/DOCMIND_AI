"""Worker pipeline: processing, schema selection, failure handling and tenant scoping."""

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app import worker
from app.db import SessionLocal
from app.models import (
    Document,
    ExtractionField,
    ExtractionRun,
    ExtractionSchema,
    ExtractionSchemaVersion,
    JobStatus,
    ProcessingJob,
    User,
)
from app.ocr import needs_ocr
from app.processing import extraction_envelope, is_strict_compatible
from tests.conftest import make_pdf


def _run_job(job_id: str) -> None:
    worker.process_document.run(job_id)  # type: ignore[attr-defined]


def _upload(register, content: bytes | None = None):
    acct = register()
    project = acct.project()
    body = acct.upload(project, content).json()
    return acct, project, body


def test_process_document_persists_extraction_and_usage(client, register) -> None:
    acct, _, body = _upload(register, make_pdf(2))
    _run_job(body["job_id"])
    status = client.get(
        f"/api/v1/documents/{body['document_id']}/status?organization_id={acct.org}",
        headers=acct.headers,
    )
    assert status.json()["status"] == "COMPLETED"
    extraction = client.get(
        f"/api/v1/documents/{body['document_id']}/extraction?organization_id={acct.org}",
        headers=acct.headers,
    ).json()
    assert {f["name"] for f in extraction["fields"]} >= {"customer_name", "total_amount"}
    usage = client.get(f"/api/v1/usage?organization_id={acct.org}", headers=acct.headers).json()
    assert usage["metrics"]["pages_processed"] == 2
    assert usage["metrics"]["documents_processed"] == 1


def test_reprocessing_a_completed_job_is_a_noop(client, register) -> None:
    _, _, body = _upload(register)
    _run_job(body["job_id"])
    _run_job(body["job_id"])
    with SessionLocal() as db:
        assert len(db.scalars(select(ExtractionRun)).all()) == 1


def test_missing_job_is_ignored() -> None:
    _run_job(str(uuid.uuid4()))


def test_corrupt_storage_object_fails_cleanly(client, register) -> None:
    _, _, body = _upload(register)
    with SessionLocal.begin() as db:
        document = db.get(Document, uuid.UUID(body["document_id"]))
        assert document is not None
        key = document.storage_key
    from app.storage import storage

    storage.put(key, b"%PDF-1.4 definitely not a pdf")
    _run_job(body["job_id"])
    with SessionLocal() as db:
        job = db.get(ProcessingJob, uuid.UUID(body["job_id"]))
        assert job is not None
        assert job.status == JobStatus.failed
        assert job.failure_code == "PROCESSING_FAILED"
        # Raw parser errors must not leak into the persisted message.
        assert "pdf" not in (job.failure_message or "").lower()


def test_schema_selection_prefers_project_schema_and_is_tenant_scoped(client, register) -> None:
    acct, project, body = _upload(register)
    other = register("other")
    org_schema = {"type": "object", "properties": {"org": {"type": "string"}}}
    project_schema = {"type": "object", "properties": {"proj": {"type": "string"}}}
    for who, pid, schema in ((acct, None, org_schema), (acct, project, project_schema), (other, None, {"type": "object", "properties": {"leak": {}}})):
        url = f"/api/v1/schemas?organization_id={who.org}&name=s"
        if pid:
            url += f"&project_id={pid}"
        assert client.post(url, json=schema, headers=who.headers).status_code == 201

    seen: list[dict[str, Any]] = []

    class Recorder:
        def extract(
            self, text: str, schema: dict[str, Any], prompt_instructions: str | None = None
        ) -> dict[str, Any]:
            seen.append(schema)
            return {"fields": {"proj": "x"}, "confidence": {"proj": 0.9}}

    original = worker.llm
    worker.llm = lambda: Recorder()  # type: ignore[assignment]
    try:
        _run_job(body["job_id"])
    finally:
        worker.llm = original
    assert seen[0]["properties"]["fields"]["properties"] == {"proj": {"type": "string"}}
    with SessionLocal() as db:
        run = db.scalar(select(ExtractionRun))
        assert run is not None and run.schema_version_id is not None
        version = db.get(ExtractionSchemaVersion, run.schema_version_id)
        assert version is not None
        schema = db.get(ExtractionSchema, version.schema_id)
        assert schema is not None and str(schema.organization_id) == acct.org
        assert db.scalars(select(ExtractionField)).all()


def test_inactive_schema_is_ignored(client, register) -> None:
    acct, _, body = _upload(register)
    client.post(
        f"/api/v1/schemas?organization_id={acct.org}&name=s",
        json={"type": "object", "properties": {"a": {}}},
        headers=acct.headers,
    )
    with SessionLocal.begin() as db:
        for schema in db.scalars(select(ExtractionSchema)):
            schema.active = False
    _run_job(body["job_id"])
    with SessionLocal() as db:
        run = db.scalar(select(ExtractionRun))
        assert run is not None and run.schema_version_id is None


def test_mock_provider_reports_low_confidence_and_flags_for_review(client, register) -> None:
    """The mock LLM always returns total_amount at 0.55 confidence, below the
    0.70 default threshold, so the run should come back requiring review."""
    acct, _, body = _upload(register)
    _run_job(body["job_id"])
    extraction = client.get(
        f"/api/v1/documents/{body['document_id']}/extraction?organization_id={acct.org}",
        headers=acct.headers,
    ).json()
    assert extraction["requires_review"] is True
    low_confidence_issues = [i for i in extraction["validation_issues"] if i["rule"] == "low_confidence"]
    assert any(i["field"] == "total_amount" for i in low_confidence_issues)
    total_amount = next(f for f in extraction["fields"] if f["name"] == "total_amount")
    assert total_amount["confidence"] == pytest.approx(0.55)


def test_schema_violation_flags_run_for_review(client, register) -> None:
    acct, project, _ = _upload(register)
    # The schema requires a `customer_name` that is an integer; the mock LLM
    # always returns it as a string, so this must fail schema validation.
    client.post(
        f"/api/v1/schemas?organization_id={acct.org}&name=strict",
        json={
            "type": "object",
            "properties": {"customer_name": {"type": "integer"}},
            "required": ["customer_name"],
        },
        headers=acct.headers,
    )
    upload = acct.upload(project)
    _run_job(upload.json()["job_id"])
    extraction = client.get(
        f"/api/v1/documents/{upload.json()['document_id']}/extraction?organization_id={acct.org}",
        headers=acct.headers,
    ).json()
    assert extraction["requires_review"] is True
    assert any(i["rule"] == "schema" for i in extraction["validation_issues"])


def test_upload_schema_id_pins_version_for_worker(client, register) -> None:
    acct, project, _ = _upload(register)
    v1_id = client.post(
        f"/api/v1/schemas?organization_id={acct.org}&name=pin-me",
        json={"type": "object", "properties": {"a": {"type": "string"}}},
        headers=acct.headers,
    ).json()["id"]
    upload = client.post(
        f"/api/v1/documents?organization_id={acct.org}&project_id={project}&schema_id={v1_id}",
        files={"file": ("pinned.pdf", make_pdf(1), "application/pdf")},
        headers=acct.headers,
    ).json()
    # Publish v2 *after* upload: the job must still resolve to the v1 it pinned.
    client.post(
        f"/api/v1/schemas/{v1_id}/versions?organization_id={acct.org}",
        json={"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}},
        headers=acct.headers,
    )
    _run_job(upload["job_id"])
    with SessionLocal() as db:
        run = db.scalar(select(ExtractionRun).where(ExtractionRun.document_id == uuid.UUID(upload["document_id"])))
        assert run is not None
        version = db.get(ExtractionSchemaVersion, run.schema_version_id)
        assert version is not None and version.version == 1


def test_envelope_and_strict_mode_detection() -> None:
    assert extraction_envelope(None)["properties"]["fields"] == {"type": "object"}
    strict = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": False}
    loose = {"type": "object", "properties": {"a": {"type": "string"}}, "required": []}
    assert is_strict_compatible(extraction_envelope(strict))
    assert not is_strict_compatible(extraction_envelope(loose))
    assert not is_strict_compatible(extraction_envelope(None))


@pytest.mark.parametrize(("text", "pages", "expected"), [("", 1, True), ("x" * 100, 1, False), ("", 0, False)])
def test_needs_ocr(text: str, pages: int, expected: bool) -> None:
    assert needs_ocr(text, pages) is expected


def test_register_creates_no_orphan_user(client) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "x@example.com", "password": "a" * 12, "organization_name": "   "},
    )
    assert response.status_code == 422
    with SessionLocal() as db:
        assert db.scalars(select(User)).all() == []


def test_document_list_reports_latest_job_status(client, register) -> None:
    acct, _, body = _upload(register)
    url = f"/api/v1/documents?organization_id={acct.org}"
    assert client.get(url, headers=acct.headers).json()[0]["status"] == "QUEUED"
    _run_job(body["job_id"])
    assert client.get(url, headers=acct.headers).json()[0]["status"] == "COMPLETED"
