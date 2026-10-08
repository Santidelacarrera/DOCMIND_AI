"""Hermetic API test fixtures: SQLite, in-memory Redis, local storage, no Celery broker."""

import os
import tempfile
import uuid
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import ClassVar

_TMP = Path(tempfile.mkdtemp(prefix="docmind-tests-"))
os.environ.update(
    {
        "ENVIRONMENT": "development",
        "DATABASE_URL": f"sqlite:///{(_TMP / 'test.db').as_posix()}",
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

from app import main, rate_limit
from app.db import Base, engine


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


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    FakeRedis.counters = {}
    FakeRedis.unavailable = False
    monkeypatch.setattr(rate_limit, "Redis", FakeRedis)
    queued: list[str] = []
    monkeypatch.setattr(main.process_document, "delay", lambda job_id: queued.append(job_id))
    main.queued_jobs = queued  # type: ignore[attr-defined]
    from app import worker

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
