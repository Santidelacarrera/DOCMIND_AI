"""Request identifier shared by the response header, the audit trail and the logs.

A client may send ``X-Request-Id`` to correlate its own calls. The value is accepted only if it is
short and made of safe characters; anything else is replaced, so a header can never inject log lines
or oversized values into the audit trail.
"""

import re
import uuid
from contextvars import ContextVar

REQUEST_ID_HEADER = "X-Request-Id"
_VALID = re.compile(r"[A-Za-z0-9._-]{1,64}")

current_request_id: ContextVar[str | None] = ContextVar("current_request_id", default=None)


def from_header(value: str | None) -> str:
    if value and _VALID.fullmatch(value):
        return value
    return uuid.uuid4().hex


def get() -> str | None:
    return current_request_id.get()
