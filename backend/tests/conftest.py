"""Hermetic API test fixtures: SQLite, in-memory Redis, local storage, no Celery broker."""

import os
import tempfile
import uuid
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import ClassVar

_TMP = Path(tempfile.mkdtemp(prefix="docmind-tests-"))

# TEST_RLS_ENFORCED=1 (with a PostgreSQL TEST_DATABASE_URL whose user may create roles) runs the
# suite as RLS phase 3 runs in production: the application connects as docmind_app (not the table
# owner, no SUPERUSER, no BYPASSRLS), so every policy applies to it, and maintenance tasks as
# docmind_system. TEST_DATABASE_URL stays the owner, used only for the schema.
RLS_ENFORCED = os.environ.get("TEST_RLS_ENFORCED") == "1"
_ROLE_PASSWORD = "docmind-rls-test"  # throwaway roles of a throwaway test database


def _rls_role_urls(owner_url: str) -> tuple[str, str]:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    owner = create_engine(owner_url)
    with owner.begin() as conn:
        for role, extra in (("docmind_app", "NOBYPASSRLS"), ("docmind_system", "BYPASSRLS")):
            exists = conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role})
            verb = "ALTER" if exists else "CREATE"
            conn.execute(text(f"{verb} ROLE {role} LOGIN NOSUPERUSER {extra} PASSWORD '{_ROLE_PASSWORD}'"))
        conn.execute(text("GRANT USAGE ON SCHEMA public TO docmind_app, docmind_system"))
    owner.dispose()
    base = make_url(owner_url)
    return (
        base.set(username="docmind_app", password=_ROLE_PASSWORD).render_as_string(hide_password=False),
        base.set(username="docmind_system", password=_ROLE_PASSWORD).render_as_string(hide_password=False),
    )


_OWNER_URL = os.environ.get("TEST_DATABASE_URL")
if RLS_ENFORCED:
    if not _OWNER_URL or not _OWNER_URL.startswith("postgresql"):
        raise RuntimeError("TEST_RLS_ENFORCED=1 needs a PostgreSQL TEST_DATABASE_URL")
    _APP_URL, _SYSTEM_URL = _rls_role_urls(_OWNER_URL)
    os.environ.update({"DATABASE_SYSTEM_URL": _SYSTEM_URL, "RLS_ENFORCED": "true"})

os.environ.update(
    {
        "ENVIRONMENT": "development",
        # TEST_DATABASE_URL runs the suite on Postgres (real row locks); default is hermetic SQLite.
        "DATABASE_URL": _APP_URL if RLS_ENFORCED else os.environ.get(
            "TEST_DATABASE_URL", f"sqlite:///{(_TMP / 'test.db').as_posix()}"
        ),
        "LOCAL_STORAGE_PATH": str(_TMP / "uploads"),
        "STORAGE_PROVIDER": "local",
        "JWT_SECRET": "test-secret-test-secret-test-secret-123",
        "LLM_PROVIDER": "mock",
        "ANTIVIRUS_PROVIDER": "disabled",
        "RATE_LIMIT_LOGIN": "1000",
        "RATE_LIMIT_REGISTER": "1000",
        "RATE_LIMIT_UPLOAD": "1000",
        "RATE_LIMIT_API": "1000",
        "COOKIE_SECURE": "false",
    }
)

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app import main, rate_limit, rls
from app.db import Base, SystemSessionLocal, engine, system_engine

if RLS_ENFORCED:
    from sqlalchemy import create_engine as _create_engine
    from sqlalchemy import text as _text

    schema_engine = _create_engine(_OWNER_URL)  # type: ignore[arg-type]
else:
    schema_engine = engine


def _rebuild_schema() -> None:
    engine.dispose()
    system_engine.dispose()
    if RLS_ENFORCED:
        # Policies reference other tables (organizations -> organization_members), which
        # drop_all cannot order around: start from an empty schema instead.
        with schema_engine.begin() as conn:
            conn.execute(_text("DROP SCHEMA public CASCADE"))
            conn.execute(_text("CREATE SCHEMA public"))
            conn.execute(_text("GRANT USAGE ON SCHEMA public TO PUBLIC"))  # PostgreSQL's default
            conn.execute(_text("GRANT USAGE ON SCHEMA public TO docmind_app, docmind_system"))
    else:
        if schema_engine.dialect.name == "postgresql":
            # A database last used with TEST_RLS_ENFORCED keeps the cross-table policy on
            # organizations, which would block drop_all.
            with schema_engine.begin() as conn:
                conn.exec_driver_sql(
                    "DO $$ BEGIN IF to_regclass('public.organizations') IS NOT NULL THEN "
                    f"DROP POLICY IF EXISTS {rls.ORG_POLICY} ON organizations; END IF; END $$"
                )
        Base.metadata.drop_all(schema_engine)
    Base.metadata.create_all(schema_engine)
    if RLS_ENFORCED:
        with schema_engine.begin() as conn:
            for statement in (*rls.policy_statements(), *rls.phase3_statements()):
                conn.execute(_text(statement))
            conn.execute(_text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO docmind_app, docmind_system"
            ))
            conn.execute(_text("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO docmind_app, docmind_system"))


class FakeRedis:
    """Minimal Redis double covering the commands the rate limiter uses."""

    counters: ClassVar[dict[str, int]] = {}
    unavailable: ClassVar[bool] = False

    @classmethod
    def from_url(cls, *_: object, **__: object) -> "FakeRedis":
        if cls.unavailable:
            raise ConnectionError("redis down")
        return cls()

    def pipeline(self, transaction: bool = True) -> "FakeRedis":
        self._ops: list[str] = []
        return self

    def incr(self, key: str) -> None:
        self._ops.append(key)

    def expire(self, key: str, seconds: int, nx: bool = False) -> None:
        return None

    def execute(self) -> list[int]:
        key = self._ops[0]
        FakeRedis.counters[key] = FakeRedis.counters.get(key, 0) + 1
        return [FakeRedis.counters[key]]


OCR_TEXT = "Invoice TEST-001 (test_document)\nCustomer: Juan Pérez\nTotal due: 15,000"


class FakeOCR:
    """Blank test PDFs (``make_pdf``) always trigger OCR; stub out Tesseract/Poppler,
    which aren't installed in every test environment, for every test that may run
    ``worker.process_document`` against one."""

    def extract_pages(self, content: bytes) -> list[str]:
        from io import BytesIO

        from pypdf import PdfReader

        # Contains the values MockLLMProvider returns, so a clean happy path has real evidence;
        # tests of unsupported values override this (see test_provenance.py).
        return [OCR_TEXT] * len(PdfReader(BytesIO(content)).pages)


@pytest.fixture(autouse=True)
def _hermetic_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the reserved example.com domain to a public address without real DNS, so
    webhook-URL validation tests behave the same offline (CI sandboxes, laptops). Every other
    hostname (``localhost``, private ranges...) still resolves for real."""
    import socket

    real = socket.getaddrinfo

    def getaddrinfo(host: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if isinstance(host, str) and (host == "example.com" or host.endswith(".example.com")):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return real(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[None]:
    rls.clear_context()
    _rebuild_schema()
    # Recreated tables invalidate server-side prepared statements held by pooled Postgres
    # connections ("cached plan must not change result type"); start each test with fresh ones.
    engine.dispose()
    system_engine.dispose()
    # Tests arrange and inspect rows of several organizations directly: under RLS_ENFORCED they do
    # it as docmind_system, while the application code under test keeps its docmind_app sessions.
    # Its own patcher, so a test calling monkeypatch.undo() does not switch it back.
    as_system = pytest.MonkeyPatch()
    if RLS_ENFORCED and hasattr(request.module, "SessionLocal"):
        as_system.setattr(request.module, "SessionLocal", SystemSessionLocal)
    # The database is rebuilt per test; stored objects must be too, or tests see each other's files.
    from app.storage import storage as _storage

    for stale_key, _ in list(_storage.list_objects()):
        _storage.delete(stale_key)
    FakeRedis.counters = {}
    FakeRedis.unavailable = False
    monkeypatch.setattr(rate_limit, "Redis", FakeRedis)
    queued: list[str] = []
    monkeypatch.setattr(main.process_document, "delay", lambda job_id: queued.append(job_id))
    main.queued_jobs = queued  # type: ignore[attr-defined]
    from app import worker

    monkeypatch.setattr(worker, "TesseractProvider", FakeOCR)

    webhook_calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        worker.dispatch_webhooks,
        "delay",
        lambda organization_id, event, payload: webhook_calls.append((organization_id, event, payload)),
    )
    worker.dispatched_webhooks = webhook_calls  # type: ignore[attr-defined]
    from app.notifications import ConsoleEmailProvider

    ConsoleEmailProvider.sent = []
    yield
    as_system.undo()


@pytest.fixture
def fake_redis() -> type[FakeRedis]:
    return FakeRedis


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(main.app) as test_client:
        yield test_client


def make_pdf(pages: int = 1) -> bytes:
    writer = PdfWriter()
    writer.add_metadata({"/TestId": uuid.uuid4().hex})
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


class Account:
    def __init__(self, client: TestClient, token: str, organization_id: str, email: str) -> None:
        self.client = client
        self.token = token
        self.org = organization_id
        self.email = email

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def project(self, name: str = "default") -> str:
        response = self.client.post(
            f"/api/v1/projects?organization_id={self.org}&name={name}", headers=self.headers
        )
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    def upload(self, project_id: str, content: bytes | None = None, filename: str = "a.pdf"):
        return self.client.post(
            f"/api/v1/documents?organization_id={self.org}&project_id={project_id}",
            files={"file": (filename, content if content is not None else make_pdf(), "application/pdf")},
            headers=self.headers,
        )


PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def register(client: TestClient):
    def _register(label: str = "acme") -> Account:
        email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
        response = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": PASSWORD, "organization_name": label},
        )
        assert response.status_code == 201, response.text
        client.cookies.clear()
        data = response.json()
        return Account(client, data["access_token"], data["organization_id"], email)

    return _register
