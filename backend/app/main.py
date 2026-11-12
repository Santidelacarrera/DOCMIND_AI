import logging
from secrets import compare_digest

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import DisconnectionError, OperationalError
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app import request_context, rls
from app.core import settings
from app.db import get_db
from app.limits import MaxBodySizeMiddleware
from app.logging_config import configure_logging
from app.metrics import PrometheusMiddleware, metrics_response
from app.telemetry import configure_tracing

configure_logging()
logger = logging.getLogger("docmind")
_config = settings()
_production = _config.is_production
app = FastAPI(
    title="DocMind AI",
    version="0.1.0",
    # Interactive docs and the schema are not exposed in staging/production.
    docs_url=None if _production else "/docs",
    redoc_url=None if _production else "/redoc",
    openapi_url=None if _production else "/openapi.json",
)
configure_tracing(app)


@app.exception_handler(OperationalError)
@app.exception_handler(DisconnectionError)
async def _database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    """PostgreSQL unreachable or the connection dropped: a retryable 503 with a stable code, not a 500.
    /ready then fails too, so the orchestrator stops routing traffic here until the database is back.
    Nothing is half-written: every request runs in one transaction that never committed."""
    logger.error("database unavailable", extra={"error": type(exc).__name__, "path": request.url.path})
    return JSONResponse({"detail": "DATABASE_UNAVAILABLE"}, status_code=503, headers={"Retry-After": "5"})
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _config.cors_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "X-Request-Id"],
    expose_headers=["X-Request-Id"],
    allow_credentials=True,
    max_age=600,
)
app.add_middleware(PrometheusMiddleware)

# Auth endpoints that establish a session are exempt from CSRF because no session exists yet.
CSRF_EXEMPT_PATHS = {"/api/v1/auth/login", "/api/v1/auth/register"}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if (
            settings().csrf_enabled
            and request.url.path not in CSRF_EXEMPT_PATHS
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not request.headers.get("authorization")
            and request.cookies.get("access_token")
        ):
            csrf_cookie = request.cookies.get("csrf_token", "")
            csrf_header = request.headers.get("x-csrf-token", "")
            if not csrf_cookie or not compare_digest(csrf_cookie.encode(), csrf_header.encode()):
                return Response(status_code=403, content="CSRF_FAILED")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Resource-Policy"] = "same-site"
        if "Content-Security-Policy" not in response.headers and not request.url.path.startswith(
            ("/docs", "/redoc")
        ):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            )
            response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        if settings().effective_cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


class RequestIdMiddleware(BaseHTTPMiddleware):
    """One identifier per request: echoed in the response and written to the audit trail and logs."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request_context.from_header(request.headers.get(request_context.REQUEST_ID_HEADER))
        token = request_context.current_request_id.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_context.current_request_id.reset(token)
        response.headers[request_context.REQUEST_ID_HEADER] = request_id
        return response


app.add_middleware(RequestIdMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    MaxBodySizeMiddleware,
    upload_limit=settings().max_upload_bytes,
    upload_path="/api/v1/documents",
)



@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(select(1))
    if settings().rls_enforced:
        problems = rls.role_problems(db)
        if problems:
            # A connection that bypasses the policies must never take traffic as if it did not.
            logger.error("RLS_ENFORCED but the database role bypasses row-level security: %s", "; ".join(problems))
            raise HTTPException(503, "RLS_ROLE_UNSAFE")
    return {"status": "ready"}


@app.get("/metrics")
def metrics(authorization: str | None = Header(default=None)) -> Response:
    # Scraped by Prometheus on the internal network. The network restriction at the ingress is the primary
    # control; METRICS_TOKEN, when set, adds a bearer check so that a misrouted request is still refused.
    token = settings().metrics_token
    if token is not None:
        scheme, _, value = (authorization or "").partition(" ")
        presented = value if scheme == "Bearer" else ""
        if not presented or not compare_digest(presented.encode(), token.get_secret_value().encode()):
            raise HTTPException(401, "METRICS_TOKEN_REQUIRED")
    return metrics_response()


# Routes live in app.routers (one module per domain); helpers in app.api_support. Included in the
# original declaration order, so path matching is unchanged.
from app.api_support import (  # noqa: F401  (re-exported: tests and scripts use main.<name>)
    MAX_FIELD_VALUE_BYTES,
    MAX_SCHEMA_BYTES,
    Name,
    audit,
    content_disposition,
    live_document,
    neutralize_formula,
    organization_id,
    sanitize_filename,
)
from app.routers import admin, ask, auth, conversations, documents, members, projects, schemas
from app.worker import process_document, process_vector_deletions  # noqa: F401  (re-exported for tests)

# Routes are appended to the app's own flat route list rather than through include_router: in this
# FastAPI version include_router adds one opaque _IncludedRouter per router, which would hide the
# individual routes from app.routes -- the list the tenant-isolation tests walk, OpenAPI is built
# from, and the metrics route-template label depends on. Runtime behaviour is the pre-split one.
for _router in (auth.router, members.router, projects.router, schemas.router, admin.router, documents.router, ask.router,
                conversations.router):
    app.router.routes.extend(_router.routes)
