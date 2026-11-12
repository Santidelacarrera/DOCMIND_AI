"""Operational modules that only run against real infrastructure in production:
SMTP invitations (app.notifications) and OpenTelemetry wiring (app.telemetry).

Neither talks to a network here: smtplib.SMTP and the OpenTelemetry entry points
are replaced by recorders, so the tests check what the code asks of them.
"""

import sys
import types
from typing import ClassVar, Self

import pytest
from pydantic import SecretStr

from app import notifications, telemetry
from app.core import Settings


class FakeSMTP:
    instances: ClassVar[list["FakeSMTP"]] = []

    def __init__(self, host: str, port: int, timeout: int) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[tuple] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append(("quit",))

    def starttls(self) -> None:
        self.calls.append(("starttls",))

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user, password))

    def send_message(self, message) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(("send", message["To"], message["From"], message["Subject"], message.get_content()))


def _use_settings(monkeypatch: pytest.MonkeyPatch, module: types.ModuleType, **values: object) -> Settings:
    config = Settings(**values)
    monkeypatch.setattr(module, "settings", lambda: config)
    return config


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.instances = []
    monkeypatch.setattr(notifications.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_smtp_sends_with_tls_and_login(monkeypatch: pytest.MonkeyPatch, fake_smtp: type[FakeSMTP]) -> None:
    _use_settings(
        monkeypatch, notifications, smtp_host="mail.example.test", smtp_port=2525,
        smtp_username="mailer", smtp_password=SecretStr("pw-test"), smtp_from="DocMind <no-reply@example.test>",
    )
    notifications.SMTPEmailProvider().send("ana@example.test", "Hola", "Cuerpo")

    (server,) = fake_smtp.instances
    assert (server.host, server.port, server.timeout) == ("mail.example.test", 2525, 10)
    assert server.calls[0] == ("starttls",)
    assert server.calls[1] == ("login", "mailer", "pw-test")
    _, to, sender, subject, body = server.calls[2]
    assert (to, sender, subject, body.strip()) == ("ana@example.test", "DocMind <no-reply@example.test>", "Hola", "Cuerpo")
    assert server.calls[-1] == ("quit",)


def test_smtp_without_tls_or_credentials(monkeypatch: pytest.MonkeyPatch, fake_smtp: type[FakeSMTP]) -> None:
    _use_settings(monkeypatch, notifications, smtp_host="relay.internal", smtp_use_tls=False)
    notifications.SMTPEmailProvider().send("a@example.test", "s", "b")
    kinds = [call[0] for call in fake_smtp.instances[0].calls]
    assert kinds == ["send", "quit"]


def test_smtp_login_with_empty_password(monkeypatch: pytest.MonkeyPatch, fake_smtp: type[FakeSMTP]) -> None:
    _use_settings(monkeypatch, notifications, smtp_host="relay.internal", smtp_username="u")
    notifications.SMTPEmailProvider().send("a@example.test", "s", "b")
    assert ("login", "u", "") in fake_smtp.instances[0].calls


def test_smtp_refuses_without_host(monkeypatch: pytest.MonkeyPatch, fake_smtp: type[FakeSMTP]) -> None:
    _use_settings(monkeypatch, notifications, smtp_host=None)
    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        notifications.SMTPEmailProvider().send("a@example.test", "s", "b")
    assert fake_smtp.instances == []


def test_provider_selection_follows_smtp_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_settings(monkeypatch, notifications, smtp_host=None)
    assert isinstance(notifications.build_email_provider(), notifications.ConsoleEmailProvider)
    _use_settings(monkeypatch, notifications, smtp_host="mail.example.test")
    assert isinstance(notifications.build_email_provider(), notifications.SMTPEmailProvider)


def test_invitation_email_mentions_inviter_role_link_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_settings(monkeypatch, notifications, invitation_expiry_hours=48)
    subject, body = notifications.invitation_email("Acme", "jefa@example.test", "EDITOR", "https://app.test/i/abc")
    assert "Acme" in subject
    assert "jefa@example.test" in body and "editor" in body
    assert "https://app.test/i/abc" in body and "48 hours" in body


# --- telemetry -------------------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []


def _install_fake_otel(monkeypatch: pytest.MonkeyPatch, recorder: Recorder, *, celery: bool = True) -> None:
    """Replace the OpenTelemetry entry points configure_* imports with recorders."""

    def module(name: str, **attrs: object) -> None:
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)

    def instrumentor(label: str) -> type:
        class Fake:
            def instrument(self) -> None:
                recorder.events.append(label)

            @staticmethod
            def instrument_app(app: object) -> None:
                recorder.events.append(f"{label}:app")

        return Fake

    module("opentelemetry.trace", set_tracer_provider=lambda provider: recorder.events.append(f"provider:{provider}"))
    monkeypatch.setattr(sys.modules["opentelemetry"], "trace", sys.modules["opentelemetry.trace"], raising=False)
    module("opentelemetry.instrumentation.sqlalchemy", SQLAlchemyInstrumentor=instrumentor("sqlalchemy"))
    module("opentelemetry.instrumentation.fastapi", FastAPIInstrumentor=instrumentor("fastapi"))
    if celery:
        module("opentelemetry.instrumentation.celery", CeleryInstrumentor=instrumentor("celery"))
    else:
        monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.celery", None)  # import raises ImportError
    monkeypatch.setattr(telemetry, "_build_provider", lambda: "P")


@pytest.fixture
def fresh_telemetry(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    pytest.importorskip("opentelemetry")
    monkeypatch.setattr(telemetry, "_configured", False)
    return Recorder()


def test_tracing_is_a_noop_without_endpoint(monkeypatch: pytest.MonkeyPatch, fresh_telemetry: Recorder) -> None:
    _use_settings(monkeypatch, telemetry, otel_exporter_otlp_endpoint=None)
    _install_fake_otel(monkeypatch, fresh_telemetry)
    telemetry.configure_tracing(object())
    telemetry.configure_worker_tracing()
    assert fresh_telemetry.events == []
    assert telemetry._configured is False


def test_tracing_instruments_api_once(monkeypatch: pytest.MonkeyPatch, fresh_telemetry: Recorder) -> None:
    _use_settings(monkeypatch, telemetry, otel_exporter_otlp_endpoint="http://collector.test:4318/v1/traces")
    _install_fake_otel(monkeypatch, fresh_telemetry)
    telemetry.configure_tracing(object())
    telemetry.configure_tracing(object())  # second call is ignored
    assert fresh_telemetry.events == ["provider:P", "sqlalchemy", "fastapi:app"]
    assert telemetry._configured is True


def test_worker_tracing_adds_celery(monkeypatch: pytest.MonkeyPatch, fresh_telemetry: Recorder) -> None:
    _use_settings(monkeypatch, telemetry, otel_exporter_otlp_endpoint="http://collector.test:4318/v1/traces")
    _install_fake_otel(monkeypatch, fresh_telemetry)
    telemetry.configure_worker_tracing()
    assert fresh_telemetry.events == ["provider:P", "sqlalchemy", "celery"]


def test_worker_tracing_survives_missing_celery_instrumentation(
    monkeypatch: pytest.MonkeyPatch, fresh_telemetry: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    _use_settings(monkeypatch, telemetry, otel_exporter_otlp_endpoint="http://collector.test:4318/v1/traces")
    _install_fake_otel(monkeypatch, fresh_telemetry, celery=False)
    telemetry.configure_worker_tracing()
    assert fresh_telemetry.events == ["provider:P", "sqlalchemy"]
    assert "instrumentation-celery is missing" in caplog.text


def test_tracing_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, fresh_telemetry: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    _use_settings(monkeypatch, telemetry, otel_exporter_otlp_endpoint="http://collector.test:4318/v1/traces")
    _install_fake_otel(monkeypatch, fresh_telemetry)

    def broken() -> None:
        raise RuntimeError("collector misconfigured")

    monkeypatch.setattr(telemetry, "_build_provider", broken)
    telemetry.configure_tracing(object())
    assert telemetry._configured is False
    assert "failed to configure OpenTelemetry tracing" in caplog.text


def test_tracing_reports_missing_packages(
    monkeypatch: pytest.MonkeyPatch, fresh_telemetry: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    _use_settings(monkeypatch, telemetry, otel_exporter_otlp_endpoint="http://collector.test:4318/v1/traces")
    _install_fake_otel(monkeypatch, fresh_telemetry)
    monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.sqlalchemy", None)
    telemetry.configure_tracing()
    assert telemetry._configured is False
    assert "opentelemetry packages are not installed" in caplog.text


def test_real_provider_builds_with_service_name(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    _use_settings(
        monkeypatch, telemetry, otel_service_name="docmind-test",
        otel_exporter_otlp_endpoint="http://collector.test:4318/v1/traces",
    )
    provider = telemetry._build_provider()
    try:
        assert provider.resource.attributes["service.name"] == "docmind-test"
    finally:
        provider.shutdown()
