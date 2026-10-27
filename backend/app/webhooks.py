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
import http.client
import ipaddress
import json
import logging
import socket
import urllib.request
import uuid
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse

from app.core import settings

logger = logging.getLogger("docmind.webhooks")

DELIVERY_TIMEOUT_SECONDS = 5
MAX_PAYLOAD_BYTES = 16 * 1024


class WebhookURLError(ValueError):
    pass


def _blocked(address: str) -> bool:
    """True for anything that is not a public, routable unicast address."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is loopback, however it is spelled
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global  # carrier-grade NAT (100.64/10) and other non-global ranges
    )


def resolve_public(url: str, *, allow_insecure: bool = False) -> tuple[str, str]:
    """Validate ``url`` and return ``(url, address)`` where ``address`` is the one IP to connect to.

    Raises WebhookURLError unless every address the host resolves to is public. The caller
    must connect to the returned address (not resolve the name again), otherwise DNS could
    answer differently the second time (rebinding) and reach an internal service.
    """
    parsed = urlparse(url)
    allowed_schemes = {"http", "https"} if allow_insecure else {"https"}
    if parsed.scheme not in allowed_schemes or not parsed.hostname:
        raise WebhookURLError("WEBHOOK_URL_MUST_BE_HTTPS")
    if parsed.username or parsed.password:
        raise WebhookURLError("WEBHOOK_URL_INVALID")
    if len(url) > 2048:
        raise WebhookURLError("WEBHOOK_URL_TOO_LONG")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except OSError:
        raise WebhookURLError("WEBHOOK_URL_UNRESOLVABLE") from None
    if not infos:
        raise WebhookURLError("WEBHOOK_URL_UNRESOLVABLE")
    addresses = [str(info[4][0]) for info in infos]
    if any(_blocked(address) for address in addresses):
        raise WebhookURLError("WEBHOOK_URL_NOT_ALLOWED")
    return url, addresses[0]


def validate_url(url: str, *, allow_insecure: bool = False) -> str:
    """Raise WebhookURLError unless ``url`` is a public HTTP(S) endpoint.

    ``allow_insecure`` only exists so local/dev environments can point webhooks
    at ``http://`` test receivers; production configuration never sets it.
    """
    return resolve_public(url, allow_insecure=allow_insecure)[0]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx from a public endpoint must not make us call whatever it points at (SSRF)."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        return None


def _pinned_handlers(address: str) -> list[urllib.request.BaseHandler]:
    class HTTPConnection(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = socket.create_connection((address, self.port), self.timeout)

    class HTTPSConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            sock = socket.create_connection((address, self.port), self.timeout)
            # Certificate and SNI are still checked against the real host name.
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
            return self.do_open(HTTPConnection, req)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
            return self.do_open(HTTPSConnection, req)

    # ProxyHandler({}): ignore HTTP(S)_PROXY. Pinning the address only means something when we
    # connect to the target ourselves; a proxy would resolve the name again on its side.
    return [urllib.request.ProxyHandler({}), HTTPHandler(), HTTPSHandler(), _NoRedirect()]


def signing_secret(webhook_id: uuid.UUID) -> str:
    return hmac.new(
        settings().effective_jwt_secret.encode(), webhook_id.bytes, hashlib.sha256
    ).hexdigest()


def sign_payload(webhook_id: uuid.UUID, body: bytes) -> str:
    return hmac.new(signing_secret(webhook_id).encode(), body, hashlib.sha256).hexdigest()


def deliver(
    webhook_id: uuid.UUID, url: str, event: str, payload: dict[str, Any]
) -> tuple[bool, int | None, str | None]:
    """POST the event to ``url``. Returns (success, status_code, error).

    Never raises: delivery failures (network, DNS, non-2xx, SSRF re-check) are
    reported back as a result tuple so the caller can log a WebhookDelivery row
    and decide whether to retry.
    """
    try:
        url, address = resolve_public(
            url, allow_insecure=settings().environment.lower() == "development"
        )
    except WebhookURLError as exc:
        return False, None, str(exc)
    body = json.dumps({"event": event, "data": payload}, default=str).encode()
    if len(body) > MAX_PAYLOAD_BYTES:
        return False, None, "PAYLOAD_TOO_LARGE"
    request = urllib.request.Request(
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
        # The target URL is tenant-supplied, which is what bandit's B310 flags. It is safe
        # here because resolve_public() above accepted only public addresses, the connection
        # goes to that exact address (no second DNS lookup) and redirects are not followed.
        opener = urllib.request.build_opener(*_pinned_handlers(address))
        with opener.open(request, timeout=DELIVERY_TIMEOUT_SECONDS) as response:  # nosec B310
            status_code = response.status
            return 200 <= status_code < 300, status_code, None
    except URLError as exc:
        return False, getattr(exc, "code", None), str(exc.reason)[:300]
    except Exception as exc:  # pragma: no cover - defensive catch-all for delivery
        logger.warning("webhook delivery failed webhook_id=%s error=%s", webhook_id, exc)
        return False, None, "DELIVERY_FAILED"
