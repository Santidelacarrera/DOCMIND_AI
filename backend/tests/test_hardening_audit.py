"""Adversarial probes from the security audit; each pins a behaviour that must never regress."""

import logging
import threading
import uuid

import pytest

from app import worker
from app.db import SessionLocal
from app.models import ProcessingJob
from app.storage import storage
from tests.conftest import make_pdf

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HTML = b"<html><script>alert(1)</script></html>"
ZIP = b"PK\x03\x04" + b"\x00" * 64
EXE = b"MZ" + b"\x00" * 64


def _objects() -> int:
    return len(list(storage.list_objects()))


@pytest.mark.parametrize(
    "payload", [PNG, JPEG, GIF, SVG, HTML, ZIP, EXE], ids=lambda p: p[:6].hex()
)
@pytest.mark.parametrize("filename", ["scan.pdf", "scan.png", "scan.docx", "scan"])
def test_non_document_content_is_rejected_whatever_the_name_or_declared_type(
    register, payload, filename
) -> None:
    acct = register()
    project = acct.project()
    before = _objects()
    response = acct.client.post(
        f"/api/v1/documents?organization_id={acct.org}&project_id={project}",
        files={"file": (filename, payload, "application/pdf")},
        headers=acct.headers,
    )
    assert response.status_code in (400, 413, 415, 422), response.text
    assert _objects() == before  # nothing was stored


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd.pdf",
        "..\\..\\windows\\x.pdf",
        "a\x00.pdf",
        "x" * 400 + ".pdf",
        "evil" + chr(0x202E) + "fdp.exe.pdf",
        "a\r\nSet-Cookie: x=1.pdf",
    ],
)
def test_hostile_filenames_never_reach_storage_keys_or_headers(register, name) -> None:
    acct = register()
    response = acct.upload(acct.project(), make_pdf(), filename=name)
    if response.status_code != 202:
        assert response.status_code in (400, 422)
        return
    document_id = response.json()["document_id"]
    keys = [k for k, _ in storage.list_objects()]
    assert keys and all(
        k.startswith(f"{acct.org}/") and ".." not in k and "\\" not in k for k in keys
    )
    download = acct.client.get(
        f"/api/v1/documents/{document_id}/download?organization_id={acct.org}", headers=acct.headers
    )
    assert download.status_code == 200
    disposition = download.headers.get("content-disposition", "")
    assert "\r" not in disposition and "\n" not in disposition and "\x00" not in disposition
    assert "Set-Cookie" not in "".join(
        f"{k}:{v}" for k, v in download.headers.items() if k.lower() != "content-disposition"
    )
    assert download.headers.get("x-content-type-options") == "nosniff"


def test_inline_download_is_sandboxed(register) -> None:
    acct = register()
    upload = acct.upload(acct.project()).json()
    response = acct.client.get(
        f"/api/v1/documents/{upload['document_id']}/download?organization_id={acct.org}&inline=true",
        headers=acct.headers,
    )
    assert response.status_code == 200
    csp = response.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp


def test_concurrent_identical_uploads_create_one_document(register) -> None:
    acct = register()
    project = acct.project()
    content = make_pdf()
    statuses: list[int] = []
    ids: list[str] = []

    def go() -> None:
        r = acct.upload(project, content)
        statuses.append(r.status_code)
        body = r.json()
        ids.append(str(body.get("document_id") or body.get("existing_document_id") or body))

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert statuses.count(202) == 1, statuses
    assert set(statuses) <= {202, 409}
    assert len(set(ids)) == 1  # every attempt points at the same document
    assert _objects() == 1  # losers removed their object: no orphans


CANARY = "CANARY-4111111111111111-jane.doe@example.org"


def test_document_text_never_reaches_logs_or_stored_errors(
    register, client, monkeypatch, caplog
) -> None:
    class OCR:
        def extract_pages(self, content: bytes) -> list[str]:
            return [f"Patient {CANARY}"]

    class Exploding:
        def extract(self, text: str, schema: dict, prompt_instructions: str | None = None) -> dict:
            raise RuntimeError(f"provider echoed the document: {text}")

    monkeypatch.setattr(worker, "TesseractProvider", OCR)
    monkeypatch.setattr(worker, "llm", lambda: Exploding())
    acct = register()
    upload = acct.upload(acct.project()).json()
    with caplog.at_level(logging.DEBUG):
        worker.process_document.run(upload["job_id"])  # type: ignore[attr-defined]

    assert CANARY not in caplog.text
    assert "jane.doe" not in caplog.text
    with SessionLocal() as db:
        job = db.get(ProcessingJob, uuid.UUID(upload["job_id"]))
        assert job is not None
        stored = " ".join(str(v) for v in (job.failure_code, job.failure_message))
        assert "jane.doe" not in stored and "CANARY" not in stored
    status = client.get(
        f"/api/v1/documents/{upload['document_id']}/status?organization_id={acct.org}",
        headers=acct.headers,
    ).text
    assert "jane.doe" not in status


def test_safe_exception_text_keeps_frames_and_types_but_not_messages() -> None:
    from app.logging_config import safe_exception_text

    try:
        try:
            raise ValueError("secret-cause 4111111111111111")
        except ValueError as cause:
            raise RuntimeError("secret-message jane.doe@example.org") from cause
    except RuntimeError as exc:
        import sys

        text = safe_exception_text(sys.exc_info())
        assert exc  # keep the handler variable used
    assert "RuntimeError" in text and "ValueError" in text
    assert "secret" not in text and "4111" not in text and "jane.doe" not in text
    assert "test_hardening_audit.py" in text  # the frames are still there for debugging
