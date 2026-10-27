"""Structured (JSON) logging for both the API process and the Celery worker.

Plain-text logs are fine on a laptop; in staging/production they need to be
machine-parseable so they can be shipped to a log aggregator and correlated
with traces. ``configure_logging`` is idempotent and safe to call from both
``app.main`` and ``app.worker`` without double-attaching handlers.
"""

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

from app.core import settings

_RESERVED = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
    }
)


def safe_exception_text(exc_info: tuple[Any, Any, Any]) -> str:
    """Where an exception happened and what type it was -- never its message.

    Exception messages routinely embed the data being processed (a provider echoing the
    prompt, a parser quoting bytes, a database driver listing bound parameters). Documents
    are confidential, so logs keep the frames and exception classes and drop the text.
    """
    exc_type, exc, tb = exc_info
    lines = ["Traceback (messages withheld):"]
    for frame in traceback.extract_tb(tb):
        lines.append(f"  {frame.filename}:{frame.lineno} in {frame.name}")
    chain: list[str] = []
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        chain.append(type(exc).__qualname__)
        exc = exc.__cause__ or exc.__context__
    lines.append(
        "exception chain: " + " <- ".join(chain or [getattr(exc_type, "__qualname__", "?")])
    )
    return "\n".join(lines)


def _install_record_factory() -> None:
    """Redact exceptions when the record is created, i.e. before *any* handler sees it."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_docmind_redacting", False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = safe_exception_text(record.exc_info)
            record.exc_info = None
        return record

    factory._docmind_redacting = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": settings().otel_service_name,
        }
        if record.exc_text:
            payload["exception"] = record.exc_text
        elif record.exc_info:
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
    _install_record_factory()
    root = logging.getLogger()
    root.setLevel(settings().log_level.upper())
    handler = logging.StreamHandler(sys.stdout)
    if settings().log_format.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.handlers = [handler]
    # uvicorn installs its own handlers (full tracebacks); route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        server_logger = logging.getLogger(name)
        server_logger.handlers = []
        server_logger.propagate = True
    # Quiet down noisy third-party loggers unless the operator asked for debug.
    if settings().log_level.upper() != "DEBUG":
        for noisy in ("httpx", "httpcore", "botocore", "boto3", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
