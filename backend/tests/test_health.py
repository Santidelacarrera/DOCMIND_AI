from fastapi.testclient import TestClient

from app.main import app


def test_health() -> None:
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_metrics_endpoint_exposes_prometheus_text_format() -> None:
    response = TestClient(app).get("/metrics")
    assert response.status_code == 200
    assert "docmind_http_requests_total" in response.text
