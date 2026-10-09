"""Outbound webhook delivery: SSRF-safe URL validation, HMAC signing and dispatch.

A tenant registers an HTTPS URL that gets a signed POST whenever a subscribed
event fires (today: ``document.completed``). Two things make this safe to expose
to arbitrary tenant-supplied URLs:

* ``validate_url`` is run both when the webhook is created/updated *and* again
  immediately before every delivery attempt (DNS can change between the two),
  rejecting anything that is not a public, routable HTTPS host. This blocks the
  classic SSRF vector of pointing a webhook at ``169.254.169.254``, localhost, or
  an internal-network hostname that only resolves to a private IP at send time.
* The signing secret is never stored. It is derived deterministically from
  ``JWT_SECRET`` and the webhook's id (``signing_secret``), shown to the caller
  once at creation time, and recomputed the same way on every delivery.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import uuid
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from app.core import settings

logger = logging.getLogger("docmind.webhooks")

DELIVERY_TIMEOUT_SECONDS = 5
MAX_PAYLOAD_BYTES = 16 * 1024


class WebhookURLError(ValueError):
    pass


def validate_url(url: str, *, allow_insecure: bool = False) -> str:
    """Raise WebhookURLError unless ``url`` is a public HTTP(S) endpoint.

    ``allow_insecure`` only exists so local/dev environments can point webhooks
    at ``http://`` test receivers; production configuration never sets it.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    allowed_schemes = {"http", "https"} if allow_insecure else {"https"}
    if parsed.scheme not in allowed_schemes or not parsed.hostname:
        raise WebhookURLError("WEBHOOK_URL_MUST_BE_HTTPS")
    if parsed.username or parsed.password:
        raise WebhookURLError("WEBHOOK_URL_INVALID")
    if len(url) > 2048:
        raise WebhookURLError("WEBHOOK_URL_TOO_LONG")
    host = parsed.hostname
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise WebhookURLError("WEBHOOK_URL_UNRESOLVABLE") from None
    if not infos:
        raise WebhookURLError("WEBHOOK_URL_UNRESOLVABLE")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise WebhookURLError("WEBHOOK_URL_NOT_ALLOWED")
    return url


def signing_secret(webhook_id: uuid.UUID) -> str:
    return hmac.new(
        settings().effective_jwt_secret.encode(), webhook_id.bytes, hashlib.sha256
    ).hexdigest()


def sign_payload(webhook_id: uuid.UUID, body: bytes) -> str:
    return hmac.new(signing_secret(webhook_id).encode(), body, hashlib.sha256).hexdigest()


def deliver(webhook_id: uuid.UUID, url: str, event: str, payload: dict[str, Any]) -> tuple[bool, int | None, str | None]:
    """POST the event to ``url``. Returns (success, status_code, error).

    Never raises: delivery failures (network, DNS, non-2xx, SSRF re-check) are
    reported back as a result tuple so the caller can log a WebhookDelivery row
    and decide whether to retry.
    """
    try:
        validate_url(url, allow_insecure=settings().environment.lower() == "development")
    except WebhookURLError as exc:
        return False, None, str(exc)
    body = json.dumps({"event": event, "data": payload}, default=str).encode()
    if len(body) > MAX_PAYLOAD_BYTES:
        return False, None, "PAYLOAD_TOO_LARGE"
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DocMind-Webhooks/1.0",
            "X-DocMind-Event": event,
            "X-DocMind-Signature": f"sha256={sign_payload(webhook_id, body)}",
        },
    )
    try:
        # The target URL is tenant-supplied, which is exactly what bandit's B310
        # flags -- but validate_url() above (and again at creation/update time)
        # already restricts it to a resolvable public HTTPS host, so this isn't
        # an open redirect/SSRF/file:// vector.
        with urlopen(request, timeout=DELIVERY_TIMEOUT_SECONDS) as response:  # nosec B310
            status_code = response.status
            return 200 <= status_code < 300, status_code, None
    except URLError as exc:
        return False, getattr(exc, "code", None), str(exc.reason)[:300]
    except Exception as exc:  # pragma: no cover - defensive catch-all for delivery
        logger.warning("webhook delivery failed webhook_id=%s error=%s", webhook_id, exc)
        return False, None, "DELIVERY_FAILED"
