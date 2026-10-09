"""Structured (JSON) logging for both the API process and the Celery worker.

Plain-text logs are fine on a laptop; in staging/production they need to be
machine-parseable so they can be shipped to a log aggregator and correlated
with traces. ``configure_logging`` is idempotent and safe to call from both
``app.main`` and ``app.worker`` without double-attaching handlers.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core import settings

_RESERVED = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
        "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
        "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
        "message",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": settings().otel_service_name,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Anything passed via `extra={...}` rides along as structured fields,
        # e.g. logger.info("document processed", extra={"document_id": ..., "org_id": ...}).
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload:
                try:
                    json.dumps(value)
                except TypeError:
                    value = str(value)
                payload[key] = value
        return json.dumps(payload, default=str)


_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True
    root = logging.getLogger()
    root.setLevel(settings().log_level.upper())
    handler = logging.StreamHandler(sys.stdout)
    if settings().log_format.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.handlers = [handler]
    # Quiet down noisy third-party loggers unless the operator asked for debug.
    if settings().log_level.upper() != "DEBUG":
        for noisy in ("httpx", "httpcore", "botocore", "boto3", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
