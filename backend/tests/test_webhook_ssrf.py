"""Webhook delivery must not reach internal services, whatever DNS or the target server does."""

import http.server
import socket
import threading
import uuid
from typing import ClassVar

import pytest

from app import webhooks


class _Recorder(http.server.BaseHTTPRequestHandler):
    hits: ClassVar[list[str]] = []
    redirect_to: ClassVar[str | None] = None

    def do_POST(self) -> None:
        _Recorder.hits.append(self.path)
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if _Recorder.redirect_to and self.path == "/hook":
            self.send_response(302)
            self.send_header("Location", _Recorder.redirect_to)
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()

    def log_message(self, *args: object) -> None:
        return None


@pytest.fixture
def servers():
    started = []
    for _ in range(2):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
    _Recorder.hits = []
    _Recorder.redirect_to = None
    yield [s.server_address[1] for s in started]
    for server in started:
        server.shutdown()


def test_redirects_are_not_followed(servers, monkeypatch) -> None:
    """A public endpoint answering 302 -> internal URL must not make us call the internal URL."""
    first, second = servers
    # Pretend loopback is public so the delivery itself is allowed; the redirect target is the probe.
    monkeypatch.setattr(webhooks, "_blocked", lambda address: False)
    _Recorder.redirect_to = f"http://127.0.0.1:{second}/internal-secret"
    ok, status, _ = webhooks.deliver(
        uuid.uuid4(), f"http://127.0.0.1:{first}/hook", "document.completed", {}
    )
    assert ok is False and status == 302
    assert _Recorder.hits == ["/hook"]  # the redirect target was never requested


def test_connection_is_pinned_to_the_validated_address(monkeypatch) -> None:
    """DNS rebinding: the name resolves public for validation, internal for the connection."""
    answers = iter([[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]])

    def flapping(host, *args, **kwargs):
        try:
            return next(answers)
        except StopIteration:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    connected: list[tuple[str, int]] = []

    def fake_connect(address, *args, **kwargs):
        connected.append(address)
        raise OSError("blocked in test")

    monkeypatch.setattr(socket, "getaddrinfo", flapping)
    monkeypatch.setattr(socket, "create_connection", fake_connect)
    ok, _, error = webhooks.deliver(
        uuid.uuid4(), "https://rebind.example.com/hook", "document.completed", {}
    )
    assert ok is False
    assert connected == [("93.184.216.34", 443)]  # never the second (internal) answer
    assert error is not None


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "169.254.169.254",
        "192.168.1.1",
        "172.16.0.1",
        "::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "0.0.0.0",
        "100.64.0.1",
        "fc00::1",
    ],
)
def test_internal_addresses_are_blocked(address: str) -> None:
    assert webhooks._blocked(address) is True


def test_public_address_is_allowed() -> None:
    assert webhooks._blocked("93.184.216.34") is False
