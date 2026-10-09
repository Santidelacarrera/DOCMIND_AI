"""OpenTelemetry tracing, wired up only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is
set. Local development and tests run with it unset, so importing this module
never requires a collector to be reachable: ``configure_tracing`` is a no-op in
that case, and the instrumentation calls below are individually best-effort so a
missing optional package never breaks the app or worker at import time.
"""

import logging

from app.core import settings

logger = logging.getLogger("docmind.telemetry")

_configured = False


def _build_provider():  # type: ignore[no-untyped-def]
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({SERVICE_NAME: settings().otel_service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=settings().otel_exporter_otlp_endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def configure_tracing(fastapi_app=None) -> None:  # type: ignore[no-untyped-def]
    """Set up the global tracer provider and instrument FastAPI/SQLAlchemy.

    Call once at API startup. Safe to call repeatedly or without an endpoint
    configured (it then does nothing beyond the cheap settings check).
    """
    global _configured
    if _configured or not settings().otel_exporter_otlp_endpoint:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        trace.set_tracer_provider(_build_provider())
        SQLAlchemyInstrumentor().instrument()
        if fastapi_app is not None:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(fastapi_app)
        _configured = True
        logger.info("OpenTelemetry tracing configured endpoint=%s", settings().otel_exporter_otlp_endpoint)
    except ImportError:
        logger.warning("OTEL_EXPORTER_OTLP_ENDPOINT is set but opentelemetry packages are not installed")
    except Exception:
        logger.exception("failed to configure OpenTelemetry tracing")


def configure_worker_tracing() -> None:
    """Same as configure_tracing, plus Celery task instrumentation, for app.worker."""
    if _configured or not settings().otel_exporter_otlp_endpoint:
        return
    configure_tracing()
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor

        CeleryInstrumentor().instrument()
    except ImportError:
        logger.warning("OTEL_EXPORTER_OTLP_ENDPOINT is set but opentelemetry-instrumentation-celery is missing")
    except Exception:
        logger.exception("failed to instrument Celery for tracing")
