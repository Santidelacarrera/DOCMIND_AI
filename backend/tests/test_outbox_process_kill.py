"""A worker *process* is killed with SIGKILL while it holds an outbox claim mid-delivery.

Not a simulated claim: a child process claims a real event in the database, starts delivering to a peer
that never answers, and is killed by the parent. The parent then checks that the event is not stuck
forever in ``processing`` and that a later pass delivers it exactly once.
"""

import multiprocessing as mp
import os
import signal
import time
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app import contextrag_sync as sync
from app import worker
from app.core import settings
from app.db import SessionLocal
from app.integrations import contextrag as cr
from app.models import ContextRagEvent, utcnow
from tests.conftest import make_pdf

pytest.importorskip("contextrag.api.server", reason="ContextRAG_Engine is not installed")


class _NeverAnswers:
    """A peer that accepts the connection and then stalls: the worker is killed while waiting."""

    def request(self, method, url, **kwargs):
        time.sleep(600)


def _child(keys: dict[str, str], url: str) -> None:
    """Runs in a separate process: one real delivery pass that never completes."""
    from app.db import engine

    engine.dispose()  # do not share pooled connections with the parent after fork
    cfg = settings()
    cfg.contextrag_sync_enabled = True
    cfg.contextrag_url = url
    cfg.contextrag_keys_file = "unused"
    sync.process_due(client_factory=lambda: cr.ContextRagClient(url, keys, client=_NeverAnswers()))  # type: ignore[arg-type]


def test_sigkill_during_delivery_releases_the_claim_and_delivers_once(
    register, monkeypatch
) -> None:
    class OCR:
        def extract_pages(self, content: bytes) -> list[str]:
            return ["Killed mid-delivery, delivered later."]

    monkeypatch.setattr(worker, "TesseractProvider", OCR)
    acct = register("kill")
    upload = acct.upload(acct.project(), make_pdf(1)).json()
    worker.process_document.run(upload["job_id"])  # type: ignore[attr-defined]
    doc = uuid.UUID(upload["document_id"])
    org = acct.org
    url = "http://127.0.0.1:8000"
    # Drop the parent's pooled connections BEFORE forking: a forked child inherits the sockets, and closing
    # an inherited PostgreSQL connection sends a Terminate message that kills the parent's connection too.
    from app.db import engine

    engine.dispose()
    ctx = mp.get_context("fork")
    child = ctx.Process(target=_child, args=({org: "admin-kill"}, url))
    child.start()
    try:
        deadline = time.time() + 30
        status = None
        while time.time() < deadline:
            with SessionLocal() as db:
                row = db.scalar(select(ContextRagEvent).where(ContextRagEvent.document_id == doc))
                status = row.status if row else None
            if status == "processing":
                break
            time.sleep(0.1)
        assert status == "processing", "the child never claimed the event"
        os.kill(child.pid, signal.SIGKILL)
    finally:
        child.join(10)
    assert child.exitcode == -signal.SIGKILL
    with SessionLocal() as db:
        row = db.scalar(select(ContextRagEvent).where(ContextRagEvent.document_id == doc))
        assert (
            row is not None and row.status == "processing"
        )  # the dead worker's claim is still recorded

    # The claim is released once it is older than the stale timeout, then delivered by the next pass.
    peer_calls: list[str] = []

    class Peer:
        def request(self, method, url, **kwargs):
            peer_calls.append(method)
            return (
                httpx.Response(
                    200, json={"documents": [{"doc_id": "x", "unchanged": False}], "tenant": "t"}
                )
                if method == "POST" and url.endswith("/ingest")
                else httpx.Response(200, json={"documents": [], "tenant": "t"})
            )

    monkeypatch.setattr(settings(), "contextrag_sync_enabled", True)
    monkeypatch.setattr(settings(), "contextrag_url", url)
    monkeypatch.setattr(settings(), "contextrag_keys_file", "unused")
    later = utcnow() + timedelta(minutes=30)
    result = sync.process_due(
        now=later,
        client_factory=lambda: cr.ContextRagClient(url, {org: "admin-kill"}, client=Peer()),
    )  # type: ignore[arg-type]
    assert result["released"] == 1
    assert result["done"] == 1
    assert peer_calls.count("POST") == 1  # delivered exactly once after the crash
    with SessionLocal() as db:
        row = db.scalar(select(ContextRagEvent).where(ContextRagEvent.document_id == doc))
        assert row is not None and row.status == "done" and row.claim_token is None
