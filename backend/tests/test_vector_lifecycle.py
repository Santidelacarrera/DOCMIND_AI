"""Document lifecycle <-> vector store: indexing after processing and cascade deletion."""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from app import main, vector_index, worker
from app.core import settings
from app.db import SessionLocal
from app.evaluation.pdfgen import digital_pdf
from app.ingestion.embeddings import HashingEmbedder
from app.ingestion.entities import RuleEntityDetector
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.vectorstore import QdrantStore
from app.models import Document, VectorDeletion, as_aware_utc, utcnow
from app.retention import purge_expired
from app.storage import storage

qdrant_client = pytest.importorskip("qdrant_client")
EMBED = HashingEmbedder(64)
TEXT_A = ["Project Falcon pump assembly budget. Contact: Applicant Name: Ana Ruiz, maria@example.org.", "",
          "Payment terms are thirty days and the hydraulic pump ships from Valencia."]


@pytest.fixture
def vectors(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Vector indexing switched on, backed by an in-memory Qdrant; the broker is captured."""
    client = qdrant_client.QdrantClient(":memory:")
    store = QdrantStore(client, "lifecycle")
    pipeline = IngestionPipeline(store, EMBED, masker_extra=[RuleEntityDetector()], max_chars=400, overlap=40)
    monkeypatch.setattr(settings(), "qdrant_url", "https://qdrant.test")
    monkeypatch.setattr(vector_index, "get_store", lambda: store)
    monkeypatch.setattr(vector_index, "get_pipeline", lambda: pipeline)
    queued: dict[str, list[Any]] = {"index": [], "deletions": []}
    monkeypatch.setattr(worker.index_document, "delay", lambda org, doc: queued["index"].append((org, doc)))
    monkeypatch.setattr(main.process_vector_deletions, "delay", lambda: queued["deletions"].append(1))

    class Env:
        pass

    env = Env()
    env.client, env.store, env.queued, env.pipeline = client, store, queued, pipeline  # type: ignore[attr-defined]
    env.count = lambda: client.count("lifecycle").count if client.collection_exists("lifecycle") else 0  # type: ignore[attr-defined]
    env.search = lambda org, q="pump assembly budget": store.search(org, EMBED.embed([q])[0], limit=50)  # type: ignore[attr-defined]
    return env


def upload_and_index(register: Any, vectors: Any, lines: list[str] | None = None, acct: Any = None) -> tuple[Any, dict[str, Any]]:
    acct = acct or register()
    body = acct.upload(acct.project(), digital_pdf([lines or TEXT_A])).json()
    worker.process_document.run(body["job_id"])  # type: ignore[attr-defined]
    return acct, body


def pending() -> list[VectorDeletion]:
    with SessionLocal() as db:
        return list(db.scalars(select(VectorDeletion).where(VectorDeletion.done_at.is_(None))))


# ------------------------------------------------------------------------- indexing
def test_a_processed_document_is_queued_for_indexing_then_indexed(register, vectors) -> None:
    acct, body = upload_and_index(register, vectors)
    assert vectors.queued["index"] == [(acct.org, body["document_id"])]
    assert vector_index.index_document(acct.org, body["document_id"]) == "indexed"
    hits = vectors.search(acct.org)
    assert hits and {h["tenant_id"] for h in hits} == {acct.org}
    stored = " ".join(str(h) for h in hits)
    assert "Ana Ruiz" not in stored and "maria@example.org" not in stored
    with SessionLocal() as db:
        doc = db.get(Document, uuid.UUID(body["document_id"]))
        assert doc is not None and doc.indexed_at is not None and doc.index_error is None


def test_reindexing_replaces_instead_of_duplicating(register, vectors) -> None:
    acct, body = upload_and_index(register, vectors)
    vector_index.index_document(acct.org, body["document_id"])
    first = vectors.count()
    for _ in range(2):
        vector_index.index_document(acct.org, body["document_id"])
    assert vectors.count() == first


def test_refused_documents_are_recorded_not_indexed(register, vectors) -> None:
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    writer.add_js("app.alert(1)")
    buf = io.BytesIO()
    writer.write(buf)
    acct = register()
    body = acct.upload(acct.project(), buf.getvalue()).json()
    assert vector_index.index_document(acct.org, body["document_id"]) == "refused:ACTIVE_CONTENT_REJECTED"
    with SessionLocal() as db:
        doc = db.get(Document, uuid.UUID(body["document_id"]))
        assert doc is not None and doc.index_error == "ACTIVE_CONTENT_REJECTED" and doc.indexed_at is None
    assert vectors.count() == 0


def test_a_vector_store_outage_during_indexing_is_retryable(register, vectors, monkeypatch) -> None:
    acct, body = upload_and_index(register, vectors)

    def down(*a: Any, **k: Any) -> None:
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(vectors.store, "upsert", down)
    with pytest.raises(vector_index.IndexTransientError):
        vector_index.index_document(acct.org, body["document_id"])


def test_everything_is_inert_when_vector_indexing_is_not_configured(register, client, monkeypatch) -> None:
    assert not vector_index.enabled()
    acct = register()
    body = acct.upload(acct.project(), digital_pdf([TEXT_A])).json()
    worker.process_document.run(body["job_id"])  # type: ignore[attr-defined]
    assert vector_index.index_document(acct.org, body["document_id"]) == "skipped"
    client.delete(f"/api/v1/documents/{body['document_id']}?organization_id={acct.org}", headers=acct.headers)
    assert pending() == [] and vector_index.process_deletions() == {"deleted": 0, "failed": 0}


# ------------------------------------------------------------------------- cascade deletion
def test_deleting_through_the_api_removes_the_vectors_of_that_document_only(client, register, vectors) -> None:
    acct, doomed = upload_and_index(register, vectors)
    other_doc = acct.upload(acct.project("two"), digital_pdf([["Quarterly pump assembly budget for the North region."]])).json()
    worker.process_document.run(other_doc["job_id"])  # type: ignore[attr-defined]
    stranger, stranger_doc = upload_and_index(register, vectors, TEXT_A)  # identical text, other tenant
    for a, b in ((acct, doomed), (acct, other_doc), (stranger, stranger_doc)):
        assert vector_index.index_document(a.org, b["document_id"]) == "indexed"
    before_other = {h["document_id"] for h in vectors.search(acct.org)}
    assert before_other == {doomed["document_id"], other_doc["document_id"]}

    assert client.delete(
        f"/api/v1/documents/{doomed['document_id']}?organization_id={acct.org}", headers=acct.headers
    ).status_code == 204
    assert len(pending()) == 1 and vectors.queued["deletions"]  # durable obligation + prompt kick

    assert vector_index.process_deletions() == {"deleted": 1, "failed": 0}
    assert {h["document_id"] for h in vectors.search(acct.org)} == {other_doc["document_id"]}
    assert {h["document_id"] for h in vectors.search(stranger.org)} == {stranger_doc["document_id"]}
    assert pending() == []
    assert vector_index.process_deletions() == {"deleted": 0, "failed": 0}  # idempotent


def test_a_vector_store_outage_delays_deletion_but_never_loses_it(client, register, vectors, monkeypatch) -> None:
    acct, body = upload_and_index(register, vectors)
    vector_index.index_document(acct.org, body["document_id"])
    client.delete(f"/api/v1/documents/{body['document_id']}?organization_id={acct.org}", headers=acct.headers)

    real = vectors.store.delete_document

    def down(tenant: str, doc: str) -> None:
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(vectors.store, "delete_document", down)
    now = utcnow()
    assert vector_index.process_deletions(now=now) == {"deleted": 0, "failed": 1}
    (row,) = pending()
    assert row.attempts == 1 and row.last_error == "ConnectionError" and as_aware_utc(row.next_attempt_at) > now
    assert vector_index.process_deletions(now=now) == {"deleted": 0, "failed": 0}  # backing off
    assert vectors.count() > 0  # still there, and still owed

    monkeypatch.setattr(vectors.store, "delete_document", real)  # service is back
    assert vector_index.process_deletions(now=now + timedelta(hours=2)) == {"deleted": 1, "failed": 0}
    assert vectors.count() == 0 and pending() == []


def test_the_retention_purge_cleans_vectors_even_if_the_delete_never_queued_them(register, vectors, client) -> None:
    acct, body = upload_and_index(register, vectors)
    vector_index.index_document(acct.org, body["document_id"])
    # A document marked deleted by some other path (direct edit, indexing enabled later...)
    with SessionLocal.begin() as db:
        doc = db.get(Document, uuid.UUID(body["document_id"]))
        assert doc is not None
        doc.deleted_at = utcnow() - timedelta(days=settings().retention_deleted_days + 1)
    assert pending() == [] and vectors.count() > 0
    assert purge_expired()["purged"] == 1
    assert len(pending()) == 1
    vector_index.process_deletions()
    assert vectors.count() == 0
    with SessionLocal() as db:  # the outbox row outlives the purged document row, by design
        assert db.scalar(select(func.count()).select_from(Document)) == 0


def test_automatic_expiry_queues_vector_deletion(register, vectors, monkeypatch) -> None:
    acct, body = upload_and_index(register, vectors)
    vector_index.index_document(acct.org, body["document_id"])
    monkeypatch.setattr(settings(), "retention_document_days", 90)
    assert purge_expired(utcnow() + timedelta(days=91))["expired"] == 1
    vector_index.process_deletions()
    assert vectors.count() == 0


def test_a_delete_racing_with_indexing_leaves_no_orphan_vectors(client, register, vectors, monkeypatch) -> None:
    acct, body = upload_and_index(register, vectors)
    real_ingest = vectors.pipeline.ingest

    def delete_then_ingest(**kwargs: Any) -> Any:
        # The user deletes, and the outbox runs, *before* the indexer writes its points.
        client.delete(f"/api/v1/documents/{body['document_id']}?organization_id={acct.org}", headers=acct.headers)
        vector_index.process_deletions()
        return real_ingest(**kwargs)

    monkeypatch.setattr(vectors.pipeline, "ingest", delete_then_ingest)
    assert vector_index.index_document(acct.org, body["document_id"]) == "skipped"
    assert vectors.count() == 0  # the indexer noticed and removed what it had just written


def test_requeueing_a_finished_deletion_resets_it(register, vectors) -> None:
    acct, body = upload_and_index(register, vectors)
    org, doc = uuid.UUID(acct.org), uuid.UUID(body["document_id"])
    with SessionLocal.begin() as db:
        vector_index.queue_deletion(db, org, doc)
    vector_index.process_deletions()
    with SessionLocal.begin() as db:
        vector_index.queue_deletion(db, org, doc)  # re-indexed then deleted again
        vector_index.queue_deletion(db, org, doc)  # duplicate call: still one row
    assert len(pending()) == 1


def test_the_outbox_and_the_deleted_object_are_gone_together_with_storage(client, register, vectors) -> None:
    acct, body = upload_and_index(register, vectors)
    vector_index.index_document(acct.org, body["document_id"])
    with SessionLocal() as db:
        key = db.get(Document, uuid.UUID(body["document_id"])).storage_key  # type: ignore[union-attr]
    client.delete(f"/api/v1/documents/{body['document_id']}?organization_id={acct.org}", headers=acct.headers)
    vector_index.process_deletions()
    with pytest.raises(FileNotFoundError):
        storage.get(key)
    assert vectors.count() == 0


def test_tasks_are_scheduled(register, vectors) -> None:
    assert "process-vector-deletions" in worker.celery_app.conf.beat_schedule
    assert worker.process_vector_deletions.run() == {"deleted": 0, "failed": 0}  # type: ignore[attr-defined]
