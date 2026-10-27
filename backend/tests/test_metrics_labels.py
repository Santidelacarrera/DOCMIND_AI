"""Metric labels must never carry request data: /metrics is unauthenticated and label values are kept
forever in Prometheus, so an attacker could both read identifiers and grow the series count indefinitely."""

import uuid

from fastapi.testclient import TestClient

from app import main


def test_unmatched_paths_never_appear_in_metrics() -> None:
    client = TestClient(main.app)
    marker = uuid.uuid4().hex
    for _ in range(3):
        client.get(f"/api/v1/organizations/{marker}/documents/{uuid.uuid4()}")
    text = client.get("/metrics").text
    assert marker not in text
    assert 'route="unmatched"' in text


def test_matched_routes_are_labelled_by_template_not_by_identifier() -> None:
    client = TestClient(main.app)
    document = uuid.uuid4()
    client.get(f"/api/v1/documents/{document}/status?organization_id={uuid.uuid4()}")
    text = client.get("/metrics").text
    assert str(document) not in text
    assert "/api/v1/documents/{document_id}/status" in text
