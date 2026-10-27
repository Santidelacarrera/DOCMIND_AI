"""What the operational endpoints and the metrics expose.

The production controls are: /metrics restricted at the ingress (documented in docs/DEPLOYMENT.md), an optional
METRICS_TOKEN, label sets that never carry tenant or document identifiers, and logs with no document content.
"""

import logging

from fastapi.testclient import TestClient

from app import contextrag_sync, main, worker
from app.core import settings
from app.integrations import contextrag as cr
from tests.conftest import make_pdf

FORBIDDEN_LABELS = {
    "organization",
    "organization_id",
    "org",
    "tenant",
    "tenant_id",
    "document",
    "document_id",
    "user",
    "user_id",
    "email",
    "key",
    "token",
}


def _registered_metrics():
    from prometheus_client import REGISTRY

    return [m for m in REGISTRY.collect() if m.name.startswith("docmind")]


def test_no_metric_label_can_carry_tenant_document_or_user_identifiers() -> None:
    labels: set[str] = set()
    for family in _registered_metrics():
        for sample in family.samples:
            labels.update(sample.labels.keys())
    labels.discard("le")  # histogram bucket boundary, not an identifier
    assert not (labels & FORBIDDEN_LABELS), labels & FORBIDDEN_LABELS
    assert labels <= {"method", "route", "status", "outcome", "kind", "success"}, labels


def test_metrics_endpoint_is_open_when_no_token_is_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings(), "metrics_token", None)
    assert TestClient(main.app).get("/metrics").status_code == 200


def test_metrics_endpoint_requires_the_bearer_token_when_configured(monkeypatch) -> None:
    from pydantic import SecretStr

    monkeypatch.setattr(settings(), "metrics_token", SecretStr("scraper-token-0123456789abcdef"))
    client = TestClient(main.app)
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert (
        client.get(
            "/metrics", headers={"Authorization": "scraper-token-0123456789abcdef"}
        ).status_code
        == 401
    )
    ok = client.get("/metrics", headers={"Authorization": "Bearer scraper-token-0123456789abcdef"})
    assert ok.status_code == 200 and "docmind" in ok.text


def test_outbox_delivery_logs_never_contain_document_text_even_when_a_peer_echoes_it(
    register, monkeypatch, caplog
) -> None:
    """A peer that raises an error quoting the document: the logs must keep the type, not the text."""
    canary = "CANARY-PATIENT-4111-111111-jane.doe@example.org"

    class OCR:
        def extract_pages(self, content: bytes) -> list[str]:
            return [canary]

    monkeypatch.setattr(worker, "TesseractProvider", OCR)
    acct = register("logs")
    upload = acct.upload(acct.project(), make_pdf(1)).json()
    worker.process_document.run(upload["job_id"])  # type: ignore[attr-defined]

    class Echoing:
        def request(self, method, url, **kwargs):
            raise RuntimeError(f"peer rejected the content: {kwargs.get('json')}")

    cfg = settings()
    monkeypatch.setattr(cfg, "contextrag_sync_enabled", True)
    monkeypatch.setattr(cfg, "contextrag_url", "http://localhost:8000")
    monkeypatch.setattr(cfg, "contextrag_keys_file", "unused")
    monkeypatch.setattr(
        contextrag_sync,
        "build_client",
        lambda: cr.ContextRagClient("http://localhost:8000", {acct.org: "k"}, client=Echoing()),  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.DEBUG):
        contextrag_sync.process_due(
            now=contextrag_sync.utcnow() + contextrag_sync.timedelta(minutes=1)
        )
    assert canary not in caplog.text
    assert "jane.doe" not in caplog.text
