"""Prometheus metrics: an HTTP middleware for the API and counters/histograms the
Celery worker updates directly. Exposed at ``GET /metrics`` in ``app.main``.
"""

import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

HTTP_REQUESTS_TOTAL = Counter(
    "docmind_http_requests_total",
    "HTTP requests handled by the API",
    ["method", "route", "status"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "docmind_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
)
DOCUMENTS_PROCESSED_TOTAL = Counter(
    "docmind_documents_processed_total",
    "Documents that finished processing, by terminal status",
    ["status"],
)
PROCESSING_DURATION_SECONDS = Histogram(
    "docmind_document_processing_duration_seconds",
    "End-to-end processing time per document, from job pickup to terminal state",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200),
)
EXTRACTION_REQUIRES_REVIEW_TOTAL = Counter(
    "docmind_extraction_requires_review_total",
    "Extraction runs flagged for manual review by the validation layer",
)
JOBS_RECOVERED_TOTAL = Counter(
    "docmind_jobs_recovered_total",
    "Stuck jobs handled by the recovery task, by outcome (requeued or failed)",
    ["outcome"],
)
RETENTION_PURGED_TOTAL = Counter(
    "docmind_retention_purged_total",
    "Records removed by the retention job, by kind",
    ["kind"],
)
WEBHOOK_DELIVERIES_TOTAL = Counter(
    "docmind_webhook_deliveries_total",
    "Outbound webhook delivery attempts",
    ["success"],
)


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Labels requests by route *template* (``/api/v1/documents/{id}``), not the
    raw path, so per-document URLs don't blow up Prometheus's label cardinality.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start
        route = request.scope.get("route")
        template = route.path if route is not None else request.url.path
        HTTP_REQUESTS_TOTAL.labels(request.method, template, str(response.status_code)).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(request.method, template).observe(duration)
        return response


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
