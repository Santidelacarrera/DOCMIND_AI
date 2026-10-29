"""Live checks against real infrastructure, with synthetic data only.

Each group is skipped unless its service is configured by environment variable, so the normal suite
never depends on them. What each group proves is stated in its docstring; nothing here is a substitute
for a staging environment.

    QDRANT_LIVE_URL=http://127.0.0.1:6333
    CLAMAV_LIVE_HOST=127.0.0.1 CLAMAV_LIVE_PORT=3310
    S3_LIVE_ENDPOINT=http://127.0.0.1:8333 S3_LIVE_BUCKET=dm-live   (AWS_ACCESS_KEY_ID/SECRET as needed)
    SMTP_LIVE_HOST=127.0.0.1 SMTP_LIVE_PORT=1025 MAILPIT_API=http://127.0.0.1:8025
"""

import contextlib
import os
import uuid

import httpx
import pytest

from app.core import settings
from app.evaluation.pdfgen import digital_pdf
from app.ingestion.embeddings import HashingEmbedder
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.vectorstore import PROJECT_KEY, QdrantStore

EMBED = HashingEmbedder(64)
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"  # antivirus test pattern, not malware


# --------------------------------------------------------------------- Qdrant: retrieval filters on a real server


@pytest.mark.skipif(not os.environ.get("QDRANT_LIVE_URL"), reason="QDRANT_LIVE_URL not set")
def test_qdrant_project_filters_and_backfill_on_a_real_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real Qdrant: MatchValue/MatchAny decide what is returned, a legacy point is invisible to project
    queries, and the backfill (set_payload) makes it visible again without re-embedding."""
    from qdrant_client import QdrantClient, models

    client = QdrantClient(url=os.environ["QDRANT_LIVE_URL"], timeout=30)
    name = f"dm-live-{uuid.uuid4().hex[:10]}"
    store = QdrantStore(client, name)
    pipeline = IngestionPipeline(store, EMBED, max_chars=400, overlap=40)
    org, p1, p2 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    d1, d2 = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        pipeline.ingest(tenant_id=org, document_id=d1, content=digital_pdf([["Alpha lease clause. Rent 1100 euros."]]),
                        source_name="alpha.pdf", project_id=p1)
        pipeline.ingest(tenant_id=org, document_id=d2, content=digital_pdf([["Beta lease clause. Rent 2200 euros."]]),
                        source_name="beta.pdf", project_id=p2)
        vector = EMBED.embed(["lease clause rent"])[0]

        only_p1 = store.search(org, vector, limit=50, project_id=p1)
        assert only_p1 and {h["document_id"] for h in only_p1} == {d1}
        assert store.search(org, vector, limit=50, project_ids=[]) == []  # no query at all
        both = store.search(org, vector, limit=50, project_ids=[p1, p2])
        assert {h["document_id"] for h in both} == {d1, d2}
        assert {h["document_id"] for h in store.search(org, vector, limit=50)} == {d1, d2}  # tenant-wide

        # Legacy: remove the project key from d2's points. It must vanish from every project query.
        client.delete_payload(name, keys=[PROJECT_KEY], points=models.FilterSelector(
            filter=QdrantStore.tenant_filter(org, document_id=d2)))
        assert store.count_without_project(org) >= 1
        assert {h["document_id"] for h in store.search(org, vector, limit=50, project_ids=[p1, p2])} == {d1}

        # Backfill: stamp the project again, no re-embedding, and the point is back.
        store.set_document_project(org, d2, p2)
        assert store.count_without_project(org) == 0
        assert {h["document_id"] for h in store.search(org, vector, limit=50, project_ids=[p2])} == {d2}
        store.set_document_project(org, d2, p2)  # idempotent
        assert store.count_without_project(org) == 0
    finally:
        if client.collection_exists(name):
            client.delete_collection(name)


# --------------------------------------------------------------------- ClamAV: real daemon, EICAR pattern


@pytest.mark.skipif(not os.environ.get("CLAMAV_LIVE_HOST"), reason="CLAMAV_LIVE_HOST not set")
def test_clamav_refuses_the_eicar_pattern_and_accepts_clean_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real clamd: the EICAR test pattern is refused with MALWARE_DETECTED; ordinary text is accepted."""
    from app.antivirus import ClamAVProvider

    monkeypatch.setattr(settings(), "clamav_host", os.environ["CLAMAV_LIVE_HOST"])
    monkeypatch.setattr(settings(), "clamav_port", int(os.environ.get("CLAMAV_LIVE_PORT", "3310")))
    provider = ClamAVProvider()
    with pytest.raises(RuntimeError, match="MALWARE_DETECTED"):
        provider.scan(EICAR)
    provider.scan(b"Synthetic clean paragraph about a lease.")  # no exception


# --------------------------------------------------------------------- S3-compatible storage (SSE-KMS not covered)


@pytest.mark.skipif(not os.environ.get("S3_LIVE_ENDPOINT"), reason="S3_LIVE_ENDPOINT not set")
def test_s3_storage_round_trip_against_a_live_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real S3 API: put, get, list and delete through the application's provider.

    Not covered: SSE-KMS. The live endpoint used for this check does not implement KMS, so the KMS path
    (``S3_KMS_KEY_ID``) is still unverified and needs a real KMS key in the target environment.
    """
    import boto3

    from app.storage import S3StorageProvider

    endpoint = os.environ["S3_LIVE_ENDPOINT"]
    bucket = os.environ.get("S3_LIVE_BUCKET", "dm-live")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "dmlive")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "dmlive-secret")
    raw = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1")
    with contextlib.suppress(Exception):  # the bucket may already exist from an earlier run
        raw.create_bucket(Bucket=bucket)
    monkeypatch.setattr(settings(), "s3_bucket", bucket)
    monkeypatch.setattr(settings(), "s3_endpoint_url", endpoint)
    monkeypatch.setattr(settings(), "s3_region", "us-east-1")
    monkeypatch.setattr(settings(), "s3_kms_key_id", None)
    provider = S3StorageProvider()
    key = f"{uuid.uuid4()}/{uuid.uuid4()}.pdf"
    provider.put(key, b"synthetic object")
    try:
        assert provider.get(key) == b"synthetic object"
        assert key in {k for k, _ in provider.list_objects()}
    finally:
        provider.delete(key)
    assert key not in {k for k, _ in provider.list_objects()}


# --------------------------------------------------------------------- SMTP (Mailpit)


@pytest.mark.skipif(not os.environ.get("SMTP_LIVE_HOST"), reason="SMTP_LIVE_HOST not set")
def test_smtp_delivers_an_invitation_message_to_a_real_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real SMTP server: the message is accepted and appears in the mailbox. Mailpit does not speak STARTTLS,
    so TLS is disabled for this local server only; production keeps ``SMTP_USE_TLS=true``."""
    from app.notifications import SMTPEmailProvider

    monkeypatch.setattr(settings(), "smtp_host", os.environ["SMTP_LIVE_HOST"])
    monkeypatch.setattr(settings(), "smtp_port", int(os.environ.get("SMTP_LIVE_PORT", "1025")))
    monkeypatch.setattr(settings(), "smtp_use_tls", False)
    monkeypatch.setattr(settings(), "smtp_username", None)
    subject = f"dm-live-{uuid.uuid4().hex[:8]}"
    SMTPEmailProvider().send("reviewer@example.com", subject, "Synthetic invitation body.")
    api = os.environ.get("MAILPIT_API", "http://127.0.0.1:8025")
    messages = httpx.get(f"{api}/api/v1/messages", timeout=10).json().get("messages", [])
    assert any(m.get("Subject") == subject for m in messages)
