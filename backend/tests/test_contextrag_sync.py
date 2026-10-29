"""DOCMIND -> ContextRAG synchronisation: outbox, delivery, retries, deletion and recovery.

ContextRAG_Engine runs in process (its real FastAPI app behind an HTTP transport). A thin wrapper
injects failures: refused connections, and a "successful" delete that removes nothing.
Skipped entirely when the ``contextrag`` package is not installed.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app import contextrag_sync as sync
from app import worker
from app.core import settings
from app.db import SessionLocal
from app.integrations import contextrag as cr
from app.models import ContextRagEvent, OrganizationMember, Role, utcnow
from app.retention import purge_expired

pytest.importorskip("contextrag.api.server", reason="ContextRAG_Engine is not installed")


def _real_app():
    from contextrag import factory
    from contextrag.api.auth import KeyStore, hash_key
    from contextrag.api.server import create_app
    from fastapi.testclient import TestClient

    entries = {
        hash_key("admin-a"): {"id": "docmind-a", "tenant": "tenant-a", "role": "admin"},
        hash_key("admin-b"): {"id": "docmind-b", "tenant": "tenant-b", "role": "admin"},
        hash_key("reader-a"): {"id": "ana", "tenant": "tenant-a", "role": "reader"},
    }
    return TestClient(create_app(factory.offline(), keystore=KeyStore(entries)))


class Transport:
    """Forwards requests to ContextRAG. ``fail_ingest`` refuses the next N ingests;
    ``lie_on_delete`` answers 200 to DELETE without deleting anything."""

    def __init__(self, app) -> None:
        self.app = app
        self.fail_ingest = 0
        self.lie_on_delete = False
        self.ingested: list[str] = []

    def request(self, method: str, url: str, **kwargs):
        if method == "POST" and url.endswith("/ingest"):
            if self.fail_ingest > 0:
                self.fail_ingest -= 1
                raise httpx.ConnectError("connection refused")
            self.ingested.extend(d["doc_id"] for d in kwargs["json"]["documents"])
        if method == "DELETE" and self.lie_on_delete:
            return httpx.Response(200, json={"tenant": "tenant-a"})
        return self.app.request(method, url, **kwargs)

    def get(self, url, **kwargs):
        return self.app.get(url, **kwargs)


@pytest.fixture
def real_app():
    return _real_app()


@pytest.fixture
def sync_env(monkeypatch, real_app):
    """Sync enabled against the in-process ContextRAG. ``env['keys']`` maps organization -> key."""
    cfg = settings()
    monkeypatch.setattr(cfg, "contextrag_sync_enabled", True)
    monkeypatch.setattr(cfg, "contextrag_url", "http://localhost:8000")
    monkeypatch.setattr(cfg, "contextrag_keys_file", "unused-in-tests")
    monkeypatch.setattr(cfg, "contextrag_max_attempts", 25)
    env = {"transport": Transport(real_app), "keys": {}}

    def factory():
        return cr.ContextRagClient("http://localhost:8000", env["keys"], client=env["transport"])

    monkeypatch.setattr(sync, "build_client", factory)
    return env


def _set_pages(monkeypatch, pages: list[str]) -> None:
    class OCR:
        def extract_pages(self, content: bytes) -> list[str]:
            return pages

    monkeypatch.setattr(worker, "TesseractProvider", OCR)


def _processed(register, monkeypatch, pages: list[str], label: str = "a"):
    from tests.conftest import make_pdf as pdf

    _set_pages(monkeypatch, pages)
    acct = register(label)
    upload = acct.upload(acct.project(), pdf(len(pages))).json()
    worker.process_document.run(upload["job_id"])  # type: ignore[attr-defined]
    return acct, upload["document_id"]


def _reprocess(acct, doc: str) -> None:
    response = acct.client.post(
        f"/api/v1/documents/{doc}/process?organization_id={acct.org}", headers=acct.headers
    )
    assert response.status_code in (200, 202), response.text
    worker.process_document.run(response.json()["job_id"])  # type: ignore[attr-defined]


def _events(org: str, document: str) -> list[ContextRagEvent]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(ContextRagEvent).where(
                    ContextRagEvent.organization_id == uuid.UUID(org),
                    ContextRagEvent.document_id == uuid.UUID(document),
                )
            ).all()
        )


def _later(minutes: int) -> datetime:
    return utcnow() + timedelta(minutes=minutes)


def _query(app, key: str, question: str) -> str:
    return app.post("/query", json={"question": question}, headers={"X-API-Key": key}).text


def _docs(app, key: str) -> set[str]:
    return {
        d["doc_id"] for d in app.get("/documents", headers={"X-API-Key": key}).json()["documents"]
    }


# --------------------------------------------------------------------------- 1. create / retrieve
def test_processed_document_is_delivered_and_becomes_retrievable(
    register, monkeypatch, sync_env, real_app
) -> None:
    acct, doc = _processed(
        register, monkeypatch, ["The warranty period is 24 months from delivery."]
    )
    sync_env["keys"][acct.org] = "admin-a"
    [event] = _events(acct.org, doc)
    assert (event.kind, event.status) == ("upsert", "pending")
    assert sync.process_due(now=_later(1))["done"] == 1
    assert _docs(real_app, "admin-a") == {cr.page_doc_id(doc, 1)}
    assert "24 months" in _query(real_app, "reader-a", "How long is the warranty period?")


# --------------------------------------------------------------------------- 2. update
def test_changed_content_is_resynchronised_and_the_old_fact_disappears(
    register, monkeypatch, sync_env, real_app
) -> None:
    acct, doc = _processed(
        register, monkeypatch, ["The warranty period is 24 months from delivery."]
    )
    sync_env["keys"][acct.org] = "admin-a"
    sync.process_due(now=_later(1))
    _set_pages(monkeypatch, ["The warranty period is 36 months from delivery."])
    _reprocess(acct, doc)
    assert sum(1 for e in _events(acct.org, doc) if e.status == "pending") == 1
    assert sync.process_due(now=_later(2))["done"] == 1
    answer = _query(real_app, "reader-a", "How long is the warranty period?")
    assert "36 months" in answer and "24 months" not in answer


def test_unchanged_reprocessing_creates_no_new_event(register, monkeypatch, sync_env) -> None:
    acct, doc = _processed(register, monkeypatch, ["Same text twice."])
    before = len(_events(acct.org, doc))
    _reprocess(acct, doc)
    assert len(_events(acct.org, doc)) == before


# --------------------------------------------------------------------------- 3-4. outage, retries
def test_service_outage_is_retried_and_recovers(register, monkeypatch, sync_env) -> None:
    acct, doc = _processed(register, monkeypatch, ["Recover me after the outage."])
    sync_env["keys"][acct.org] = "admin-a"
    sync_env["transport"].fail_ingest = 2
    first = sync.process_due(now=_later(1))
    assert first["retried"] == 1
    [event] = _events(acct.org, doc)
    assert (event.status, event.attempts, event.last_error) == ("pending", 1, "ConnectError")
    assert sync.process_due(now=_later(10))["retried"] == 1  # still down, after the first backoff
    assert sync.process_due(now=_later(180))["done"] == 1  # back after the backoff
    assert [e.status for e in _events(acct.org, doc)] == ["done"]


def test_retries_never_duplicate_documents_or_pages(
    register, monkeypatch, sync_env, real_app
) -> None:
    acct, doc = _processed(register, monkeypatch, ["Once is enough.", "Second page."])
    sync_env["keys"][acct.org] = "admin-a"
    sync.process_due(now=_later(1))
    ingested = list(sync_env["transport"].ingested)
    # Simulate a crash after ContextRAG accepted the write but before DOCMIND recorded "done".
    with SessionLocal.begin() as db:
        row = db.scalar(
            select(ContextRagEvent).where(ContextRagEvent.document_id == uuid.UUID(doc))
        )
        row.status, row.done_at, row.started_at = "processing", None, utcnow() - timedelta(hours=2)
    assert sync.process_due(now=_later(3))["done"] == 1
    assert len(_docs(real_app, "admin-a")) == 2  # two pages, not four
    assert sync_env["transport"].ingested[len(ingested) :] == [
        cr.page_doc_id(doc, 1),
        cr.page_doc_id(doc, 2),
    ]


# --------------------------------------------------------------------------- 5-7. deletion
def test_deleting_in_docmind_removes_everything_from_contextrag(
    register, monkeypatch, sync_env, real_app, client
) -> None:
    acct, doc = _processed(register, monkeypatch, ["Confidential: the launch code is ZEBRA-7731."])
    sync_env["keys"][acct.org] = "admin-a"
    sync.process_due(now=_later(1))
    assert "ZEBRA" in _query(real_app, "reader-a", "What is the launch code?")
    assert client.delete(
        f"/api/v1/documents/{doc}?organization_id={acct.org}", headers=acct.headers
    ).status_code in (200, 204)
    assert any(e.kind == "retire" and e.status == "pending" for e in _events(acct.org, doc))
    assert sync.process_due(now=_later(2))["done"] == 1
    assert _docs(real_app, "admin-a") == set()
    assert "ZEBRA" not in _query(real_app, "reader-a", "What is the launch code?")


def test_a_success_status_alone_does_not_count_as_deleted(
    register, monkeypatch, sync_env, real_app, client
) -> None:
    acct, doc = _processed(register, monkeypatch, ["Stays until really deleted."])
    sync_env["keys"][acct.org] = "admin-a"
    sync.process_due(now=_later(1))
    client.delete(f"/api/v1/documents/{doc}?organization_id={acct.org}", headers=acct.headers)
    sync_env["transport"].lie_on_delete = True
    result = sync.process_due(now=_later(2))
    assert result["done"] == 0 and result["retried"] == 1
    [retire] = [e for e in _events(acct.org, doc) if e.kind == "retire"]
    assert retire.status == "pending" and retire.last_error == "VERIFY_FAILED"
    sync_env["transport"].lie_on_delete = False
    assert sync.process_due(now=_later(300))["done"] == 1
    assert _docs(real_app, "admin-a") == set()


def test_deleted_document_cannot_be_resurrected_by_a_pending_upsert(
    register, monkeypatch, sync_env, real_app, client
) -> None:
    acct, doc = _processed(register, monkeypatch, ["Never published after deletion."])
    sync_env["keys"][acct.org] = "admin-a"
    # The upsert is still pending when the document is deleted.
    client.delete(f"/api/v1/documents/{doc}?organization_id={acct.org}", headers=acct.headers)
    sync.process_due(now=_later(1))  # upsert is cancelled, retire completes
    sync.process_due(now=_later(2))
    assert (
        cr.page_doc_id(doc, 1) not in sync_env["transport"].ingested
    )  # no write was ever attempted
    upsert = next(e for e in _events(acct.org, doc) if e.kind == "upsert")
    assert (upsert.status, upsert.last_error) == ("cancelled", "DOCUMENT_DELETED")
    assert _docs(real_app, "admin-a") == set()


# --------------------------------------------------------------------------- 8. revocation / permissions
def test_a_removed_member_cannot_reach_documents_through_the_sync_path(
    register, monkeypatch, sync_env, client
) -> None:
    """DOCMIND has no per-user ACL toward ContextRAG: access is the tenant key. A removed member's
    token stops working in DOCMIND immediately, and no event is created on their behalf."""
    acct, doc = _processed(register, monkeypatch, ["Owner-only material."])
    member = register("member")
    with SessionLocal() as db:
        db.add(
            OrganizationMember(
                organization_id=uuid.UUID(acct.org),
                user_id=uuid.UUID(
                    member.client.get("/api/v1/auth/me", headers=member.headers).json()["id"]
                ),
                role=Role.member,
            )
        )
        db.commit()
    # Phase 3C: a member reads a document only through a project grant.
    project = client.get(f"/api/v1/documents/{doc}?organization_id={acct.org}", headers=acct.headers).json()["project_id"]
    uid = member.client.get("/api/v1/auth/me", headers=member.headers).json()["id"]
    grant = client.put(
        f"/api/v1/projects/{project}/members?organization_id={acct.org}",
        json={"user_id": uid, "access": "read"}, headers=acct.headers,
    )
    assert grant.status_code == 200, grant.text
    assert (
        client.get(
            f"/api/v1/documents/{doc}?organization_id={acct.org}", headers=member.headers
        ).status_code
        == 200
    )
    assert (
        client.delete(
            f"/api/v1/members/{uid}?organization_id={acct.org}", headers=acct.headers
        ).status_code
        == 204
    )
    assert client.get(
        f"/api/v1/documents/{doc}?organization_id={acct.org}", headers=member.headers
    ).status_code in (403, 404)


def test_pending_delivery_for_an_organization_without_a_key_fails_visibly(
    register, monkeypatch, sync_env, real_app
) -> None:
    acct, doc = _processed(register, monkeypatch, ["No key for this tenant."])
    other = register("other")
    sync_env["keys"] = {other.org: "admin-b"}  # keys exist only for another organization
    result = sync.process_due(now=_later(1))
    assert result["failed"] == 1
    [event] = _events(acct.org, doc)
    assert (event.status, event.last_error) == ("failed", "NO_KEY_FOR_TENANT")
    assert sync_env["transport"].ingested == []  # nothing left the system
    assert _docs(real_app, "admin-b") == set()


# --------------------------------------------------------------------------- 9-10. restarts, pending tasks
def test_claims_left_by_a_restarted_worker_are_released(register, monkeypatch, sync_env) -> None:
    acct, doc = _processed(register, monkeypatch, ["Recovered after a restart."])
    sync_env["keys"][acct.org] = "admin-a"
    with SessionLocal.begin() as db:
        row = db.scalar(
            select(ContextRagEvent).where(ContextRagEvent.document_id == uuid.UUID(doc))
        )
        row.status, row.started_at = "processing", utcnow() - timedelta(hours=3)
    result = sync.process_due(now=_later(0))
    assert result["released"] == 1 and result["done"] == 1


def test_a_stale_worker_cannot_finish_a_claim_it_no_longer_owns(
    register, monkeypatch, sync_env
) -> None:
    acct, doc = _processed(register, monkeypatch, ["Fencing."])
    sync_env["keys"][acct.org] = "admin-a"
    [event] = _events(acct.org, doc)
    with SessionLocal.begin() as db:  # a live claim by a different worker
        row = db.get(ContextRagEvent, event.id)
        row.status, row.claim_token, row.started_at = "processing", "current-owner", utcnow()
    stale = sync._Claim(
        event.id,
        "upsert",
        uuid.UUID(acct.org),
        uuid.UUID(doc),
        event.dedupe_key,
        event.correlation_id,
        0,
        "not-the-current-token",
    )
    sync._finalize(stale, "done", None, utcnow(), 25)  # not the current owner: ignored
    [after] = _events(acct.org, doc)
    assert after.status == "processing" and after.claim_token == "current-owner"


def test_retire_and_upsert_for_one_document_never_run_concurrently(
    register, monkeypatch, sync_env, client
) -> None:
    acct, doc = _processed(register, monkeypatch, ["Ordering."])
    sync_env["keys"][acct.org] = "admin-a"
    with SessionLocal.begin() as db:
        row = db.scalar(
            select(ContextRagEvent).where(ContextRagEvent.document_id == uuid.UUID(doc))
        )
        row.status, row.started_at = "processing", utcnow()
    client.delete(f"/api/v1/documents/{doc}?organization_id={acct.org}", headers=acct.headers)
    result = sync.process_due(now=_later(0))
    assert result["done"] == 0  # the busy document is skipped entirely, not interleaved
    assert result["retried"] == 0 and result["failed"] == 0


# --------------------------------------------------------------------------- 11. isolation
def test_events_and_deliveries_never_cross_organizations(
    register, monkeypatch, sync_env, real_app
) -> None:
    a, doc_a = _processed(register, monkeypatch, ["Org A private fact: 4417."], label="a")
    b, doc_b = _processed(register, monkeypatch, ["Org B fact: 9001."], label="b")
    sync_env["keys"] = {a.org: "admin-a", b.org: "admin-b"}
    sync.process_due(now=_later(1))
    assert _docs(real_app, "admin-a") == {cr.page_doc_id(doc_a, 1)}
    assert _docs(real_app, "admin-b") == {cr.page_doc_id(doc_b, 1)}
    assert "4417" not in _query(
        real_app, "admin-b", "What is the fact?"
    )  # A's text is invisible to B
    assert "9001" not in _query(
        real_app, "reader-a", "What is the fact?"
    )  # B's text is invisible to A


# --------------------------------------------------------------------------- 12. reconciliation, dedupe
def test_enqueueing_twice_creates_one_event(register, monkeypatch, sync_env) -> None:
    acct, doc = _processed(register, monkeypatch, ["Idempotent enqueue."])
    with SessionLocal.begin() as db:
        sync.enqueue_upsert(db, uuid.UUID(acct.org), uuid.UUID(doc))
        sync.enqueue_retire(db, uuid.UUID(acct.org), uuid.UUID(doc))
        sync.enqueue_retire(db, uuid.UUID(acct.org), uuid.UUID(doc))
    kinds = sorted(e.kind for e in _events(acct.org, doc))
    assert kinds == ["retire", "upsert"]


def test_reconcile_recreates_missing_events_for_live_and_deleted_documents(
    register, monkeypatch, sync_env, client
) -> None:
    acct, doc = _processed(register, monkeypatch, ["Lost outbox row."])
    other, gone = _processed(
        register, monkeypatch, ["Deleted before the outbox existed."], label="b"
    )
    client.delete(f"/api/v1/documents/{gone}?organization_id={other.org}", headers=other.headers)
    with SessionLocal.begin() as db:
        db.query(ContextRagEvent).delete()
    created = sync.reconcile()
    assert created["upsert"] == 1  # the live document
    assert created["retire"] == 1  # the deleted one whose retirement was never recorded
    assert len(_events(acct.org, doc)) == 1 and len(_events(other.org, gone)) == 1


# --------------------------------------------------------------------------- expiry, admin view
def test_retention_expiry_queues_retirement(register, monkeypatch, sync_env) -> None:
    acct, doc = _processed(register, monkeypatch, ["Expires soon."])
    monkeypatch.setattr(settings(), "retention_document_days", 1)
    with SessionLocal.begin() as db:
        document = db.get(Document_model(), uuid.UUID(doc))
        document.created_at = utcnow() - timedelta(days=3)
    purge_expired(now=utcnow())
    assert any(e.kind == "retire" for e in _events(acct.org, doc))


def Document_model():
    from app.models import Document

    return Document


def test_administrators_see_status_and_members_and_keys_cannot_read_it_as_a_viewer(
    register, monkeypatch, sync_env, client
) -> None:
    acct, _ = _processed(register, monkeypatch, ["Status page."])
    response = client.get(
        f"/api/v1/contextrag/events?organization_id={acct.org}", headers=acct.headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["counts"].get("pending", 0) >= 1 and "text" not in json_keys(body)
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
            f"/api/v1/contextrag/events?organization_id={acct.org}", headers=viewer.headers
        ).status_code
        == 403
    )
    key = client.post(
        f"/api/v1/api-keys?organization_id={acct.org}&name=k",
        json=["documents:read"],
        headers=acct.headers,
    ).json()
    assert (
        client.get(
            f"/api/v1/contextrag/events?organization_id={acct.org}",
            headers={"Authorization": f"Bearer {key['key']}"},
        ).status_code
        == 403
    )


def json_keys(value, out=None) -> set[str]:
    out = set() if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(k)
            json_keys(v, out)
    elif isinstance(value, list):
        for item in value:
            json_keys(item, out)
    return out


# --------------------------------------------------------------------------- configuration policy
def test_raw_text_requires_an_explicit_flag() -> None:
    from pydantic import ValidationError

    from app.core import Settings

    with pytest.raises(ValidationError):
        Settings(contextrag_content_mode="raw")
    assert (
        Settings(
            contextrag_content_mode="raw", contextrag_allow_raw_text=True
        ).contextrag_content_mode
        == "raw"
    )
    assert Settings().contextrag_content_mode == "masked"  # the default never sends unmasked text


def test_two_workers_never_claim_the_same_event(register, monkeypatch, sync_env) -> None:
    """PostgreSQL row locks: while one worker holds a claim (uncommitted), a second worker skips it."""
    if SessionLocal().get_bind().dialect.name != "postgresql":
        pytest.skip("row-level locking (SKIP LOCKED) is a PostgreSQL behaviour")
    import threading

    _processed(register, monkeypatch, ["Concurrent claim."])
    holding = threading.Event()
    release = threading.Event()
    first_claims: list[str] = []

    def first_worker() -> None:
        with SessionLocal.begin() as db:
            first_claims.extend(c.kind for c in sync._claim_due(db, _later(0), 10))
            holding.set()
            release.wait(10)  # keep the lock while the second worker tries

    thread = threading.Thread(target=first_worker)
    thread.start()
    assert holding.wait(10)
    with SessionLocal.begin() as db:
        second = sync._claim_due(db, _later(0), 10)
    release.set()
    thread.join(10)
    assert first_claims == ["upsert"]
    assert second == []  # the locked row was skipped, not claimed twice


# --------------------------------------------------------------------------- Phase 3-4: failure modes
class Status:
    """Answers every request with one HTTP status (or raises a transport error) -- a stand-in for a broken peer."""

    def __init__(self, status: int | None = None, error: Exception | None = None) -> None:
        self.status = status
        self.error = error

    def request(self, method: str, url: str, **kwargs):
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, json={"detail": "x"})

    def get(self, url, **kwargs):
        return httpx.Response(self.status or 200, json={"detail": "x"})


@pytest.mark.parametrize(
    ("transport", "expected_status", "expected_error"),
    [
        (Status(error=httpx.ReadTimeout("slow")), "pending", "ReadTimeout"),  # timeout: retried
        (
            Status(error=httpx.ConnectError("refused")),
            "pending",
            "ConnectError",
        ),  # unavailable: retried
        (Status(status=503), "pending", "503"),  # server error: retried
        (Status(status=429), "pending", "429"),  # rate limited: retried
        (
            Status(status=401),
            "failed",
            "HTTP_401",
        ),  # bad key: visible at once, not retried in a loop
        (Status(status=422), "failed", "HTTP_422"),  # rejected request: retrying will not help
    ],
)
def test_peer_failures_are_classified_as_retry_or_visible_failure(
    register, monkeypatch, sync_env, transport, expected_status, expected_error
) -> None:
    acct, doc = _processed(register, monkeypatch, ["Failure classes."])
    sync_env["keys"][acct.org] = "admin-a"
    monkeypatch.setattr(
        sync,
        "build_client",
        lambda: cr.ContextRagClient("http://localhost:8000", sync_env["keys"], client=transport),
    )  # type: ignore[arg-type]
    sync.process_due(now=_later(1))
    [event] = _events(acct.org, doc)
    assert event.status == expected_status, event.last_error
    assert event.last_error == expected_error
    assert event.status != "processing"  # never left claimed


def test_a_retry_storm_is_bounded_by_backoff(register, monkeypatch, sync_env) -> None:
    acct, doc = _processed(register, monkeypatch, ["Bounded retries."])
    sync_env["keys"][acct.org] = "admin-a"
    monkeypatch.setattr(
        sync,
        "build_client",
        lambda: cr.ContextRagClient(
            "http://localhost:8000", sync_env["keys"], client=Status(status=503)
        ),
    )  # type: ignore[arg-type]
    now = utcnow()
    for step in range(10):
        sync.process_due(now=now + timedelta(minutes=step))  # runs every minute, as beat does
    [event] = _events(acct.org, doc)
    # Backoff 2, 4, 8... minutes: over ten minutes the attempts are due at 0, 2 and 6 minutes.
    assert event.attempts == 3
    stored = (
        event.next_attempt_at
        if event.next_attempt_at.tzinfo
        else event.next_attempt_at.replace(tzinfo=UTC)
    )
    assert stored > now + timedelta(
        minutes=9
    )  # SQLite returns naive datetimes; PostgreSQL returns aware ones


def test_events_scheduled_in_the_future_are_not_delivered_early(
    register, monkeypatch, sync_env, real_app
) -> None:
    acct, doc = _processed(register, monkeypatch, ["Not yet."])
    sync_env["keys"][acct.org] = "admin-a"
    with SessionLocal.begin() as db:
        row = db.scalar(
            select(ContextRagEvent).where(ContextRagEvent.document_id == uuid.UUID(doc))
        )
        row.next_attempt_at = utcnow() + timedelta(hours=2)
    assert sync.process_due(now=utcnow())["done"] == 0
    assert _docs(real_app, "admin-a") == set()


def test_out_of_order_content_versions_deliver_only_the_current_one(
    register, monkeypatch, sync_env, real_app
) -> None:
    """Two content changes queue two upserts. The older one must be superseded, never delivered after the newer."""
    acct, doc = _processed(register, monkeypatch, ["The warranty period is 12 months."])
    sync_env["keys"][acct.org] = "admin-a"
    _set_pages(monkeypatch, ["The warranty period is 48 months."])
    _reprocess(acct, doc)
    result = sync.process_due(now=_later(1))
    statuses = sorted((e.kind, e.status, e.last_error) for e in _events(acct.org, doc))
    assert ("upsert", "cancelled", "SUPERSEDED") in statuses
    assert ("upsert", "done", None) in statuses
    assert result["cancelled"] == 1 and result["done"] == 1
    answer = _query(real_app, "reader-a", "How long is the warranty period?")
    assert "48 months" in answer and "12 months" not in answer


def test_enqueueing_a_retirement_again_does_not_reopen_a_completed_one(
    register, monkeypatch, sync_env, client
) -> None:
    acct, doc = _processed(register, monkeypatch, ["Idempotent deletion."])
    sync_env["keys"][acct.org] = "admin-a"
    sync.process_due(now=_later(1))
    client.delete(f"/api/v1/documents/{doc}?organization_id={acct.org}", headers=acct.headers)
    sync.process_due(now=_later(2))
    with SessionLocal.begin() as db:
        sync.enqueue_retire(
            db, uuid.UUID(acct.org), uuid.UUID(doc)
        )  # e.g. a reconciliation pass runs again
    retire = next(e for e in _events(acct.org, doc) if e.kind == "retire")
    assert retire.status == "done"  # not reopened, so no second round trip is needed
    assert sync.process_due(now=_later(3))["done"] == 0


def test_a_deleted_document_is_not_resurrected_by_repeated_reconciliation(
    register, monkeypatch, sync_env, real_app, client
) -> None:
    acct, doc = _processed(register, monkeypatch, ["Stays deleted."])
    sync_env["keys"][acct.org] = "admin-a"
    sync.process_due(now=_later(1))
    client.delete(f"/api/v1/documents/{doc}?organization_id={acct.org}", headers=acct.headers)
    sync.process_due(now=_later(2))
    for _ in range(3):
        sync.reconcile()  # the hourly task, run repeatedly
        sync.process_due(now=_later(3))
    assert _docs(real_app, "admin-a") == set()
    upserts = [e for e in _events(acct.org, doc) if e.kind == "upsert"]
    assert all(e.status != "done" or e.last_error is None for e in upserts)
    assert not any(
        e.status == "pending" and e.kind == "upsert" for e in upserts
    )  # nothing left to publish


def test_repeated_reconciliation_creates_no_duplicates(register, monkeypatch, sync_env) -> None:
    acct, doc = _processed(register, monkeypatch, ["Reconciled once."])
    before = len(_events(acct.org, doc))
    for _ in range(3):
        created = sync.reconcile()
        assert created["upsert"] == 0 and created["retire"] == 0
    assert len(_events(acct.org, doc)) == before


def test_a_stuck_processing_event_is_recovered_after_the_timeout(
    register, monkeypatch, sync_env
) -> None:
    acct, doc = _processed(register, monkeypatch, ["Stuck."])
    sync_env["keys"][acct.org] = "admin-a"
    with SessionLocal.begin() as db:
        row = db.scalar(
            select(ContextRagEvent).where(ContextRagEvent.document_id == uuid.UUID(doc))
        )
        row.status, row.started_at, row.claim_token = (
            "processing",
            utcnow() - timedelta(minutes=20),
            "dead",
        )
    result = sync.process_due(now=utcnow())
    # Release and re-claim happen in the same pass: the recovered event is delivered at once.
    assert result["released"] == 1 and result["done"] == 1
    assert [e.status for e in _events(acct.org, doc)] == ["done"]


def test_a_200_with_a_body_that_is_not_json_is_a_visible_failure(
    register, monkeypatch, sync_env
) -> None:
    """A peer that answers 200 with garbage did not ingest anything: it must never be recorded as done."""
    acct, doc = _processed(register, monkeypatch, ["Garbage answer."])
    sync_env["keys"][acct.org] = "admin-a"

    class Garbage:
        def request(self, method, url, **kwargs):
            return httpx.Response(200, text="<html>maintenance page</html>")

    monkeypatch.setattr(
        sync,
        "build_client",
        lambda: cr.ContextRagClient("http://localhost:8000", sync_env["keys"], client=Garbage()),
    )  # type: ignore[arg-type]
    sync.process_due(now=_later(1))
    [event] = _events(acct.org, doc)
    assert event.status == "failed"
    assert event.last_error == "BAD_RESPONSE"  # the reason is kept; the body never is
    assert event.status != "done"


# --------------------------------------------------------------------------- pass budget (Celery hard limit)
class _SlowPeer:
    """Every ingest takes ``seconds`` of the fake clock. Stands in for a peer that answers slowly."""

    def __init__(self, clock: "_FakeClock", seconds: float) -> None:
        self.clock = clock
        self.seconds = seconds
        self.calls = 0

    def request(self, method: str, url: str, **kwargs):
        self.calls += 1
        self.clock.now += self.seconds
        if method == "POST" and url.endswith("/ingest"):
            return httpx.Response(
                200, json={"tenant": "t", "documents": [{"doc_id": "x", "unchanged": False}]}
            )
        return httpx.Response(200, json={"tenant": "t", "documents": []})

    def get(self, url, **kwargs):
        return self.request("GET", url)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_a_pass_stops_within_its_budget_and_returns_unattempted_claims_to_pending(
    register, monkeypatch, sync_env
) -> None:
    orgs = []
    for index in range(6):
        acct, doc = _processed(
            register, monkeypatch, [f"Budget document {index}."], label=f"budget{index}"
        )
        orgs.append((acct, doc))
    clock = _FakeClock()
    peer = _SlowPeer(
        clock, seconds=15.0
    )  # an upsert makes two calls (ingest, then page check): 30 s each
    keys = {acct.org: "admin-budget" for acct, _ in orgs}

    result = sync.process_due(
        now=_later(1),
        client_factory=lambda: cr.ContextRagClient("http://localhost:8000", keys, client=peer),  # type: ignore[arg-type]
        budget_seconds=60.0,
        clock=clock,
    )
    assert result["done"] == 2  # 0-30 s and 30-60 s
    assert result["deferred"] == 4
    statuses = [_events(acct.org, doc)[0] for acct, doc in orgs]
    assert not any(
        e.status == "processing" for e in statuses
    )  # nothing left claimed by a pass that ended
    assert all(
        e.attempts == 0 for e in statuses if e.status == "pending"
    )  # deferred work counts no attempt
    assert sum(1 for e in statuses if e.status == "done") == 2

    # Later passes, each with a fresh budget, finish the rest; every pass respects its budget.
    delivered = 2
    passes = 1
    while delivered < 6:
        clock.now = 0.0
        more = sync.process_due(
            now=_later(2 + passes),
            client_factory=lambda: cr.ContextRagClient("http://localhost:8000", keys, client=peer),  # type: ignore[arg-type]
            budget_seconds=60.0,
            clock=clock,
        )
        assert more["done"] + more["deferred"] >= 1
        assert more["done"] <= 2  # never more than the budget allows in one pass
        delivered += more["done"]
        passes += 1
        assert passes <= 6, "the queue must drain within a bounded number of passes"
    assert all(_events(acct.org, doc)[0].status == "done" for acct, doc in orgs)


def test_the_default_budget_leaves_room_under_celery_hard_limit() -> None:
    """Worst case for one pass: the budget, plus the last event's calls (each up to the HTTP timeout).
    That must stay below Celery's hard limit (processing_timeout_seconds), or the task is killed mid-pass."""
    import inspect

    from app.integrations import contextrag as cr_module

    budget = inspect.signature(sync.process_due).parameters["budget_seconds"].default
    http_timeout = (
        inspect.signature(cr_module.ContextRagClient.__init__).parameters["timeout"].default
    )
    worst_case = budget + 2 * http_timeout  # the last event can start just before the deadline
    assert worst_case < settings().processing_timeout_seconds
