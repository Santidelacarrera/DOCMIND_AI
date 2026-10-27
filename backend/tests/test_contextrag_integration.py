"""DOCMIND_AI -> ContextRAG_Engine handoff.

* The first block needs nothing but this repository (ContextRAG is faked at the HTTP boundary).
* ``test_real_contextrag_*`` run the same flows against the actual ContextRAG application, in
  process, and are skipped unless ``contextrag`` is importable (see docs/CONTEXTRAG_INTEGRATION.md).
"""

import uuid

import httpx
import pytest
from sqlalchemy import select

from app import worker
from app.db import SessionLocal
from app.integrations import contextrag as cr
from app.models import Document

PII_PAGE = "Contract with Acme. Contact jane.doe@example.org about invoice 12345."
PAGES = [PII_PAGE, "Second page: the warranty lasts 24 months.", "   "]


def _processed(register, monkeypatch, pages=PAGES, label="acme"):
    class OCR:
        def extract_pages(self, content: bytes) -> list[str]:
            return pages

    monkeypatch.setattr(worker, "TesseractProvider", OCR)
    from tests.conftest import make_pdf

    acct = register(label)
    upload = acct.upload(acct.project(), make_pdf(len(pages))).json()
    worker.process_document.run(upload["job_id"])  # type: ignore[attr-defined]
    return acct, upload["document_id"]


def _handoff(acct, document_id, **kwargs):
    with SessionLocal() as db:
        return cr.build_handoff(db, uuid.UUID(acct.org), uuid.UUID(document_id), **kwargs)


# --------------------------------------------------------------------------- building
def test_handoff_carries_the_contract_fields(register, monkeypatch) -> None:
    acct, document_id = _processed(register, monkeypatch)
    handoff = _handoff(acct, document_id, correlation_id="corr-123", acl=("legal",))
    assert handoff.contract == "docmind-contextrag/1" and handoff.event == "upsert"
    assert handoff.tenant_id == acct.org and handoff.document_id == document_id
    assert (
        handoff.version == 1
        and handoff.status == "processed"
        and handoff.correlation_id == "corr-123"
    )
    assert handoff.acl == ["legal"] and handoff.source_name == "a.pdf"
    assert [p.page for p in handoff.pages] == [1, 2, 3]
    assert {p.page_basis for p in handoff.pages} == {"physical"}
    assert len(handoff.content_sha256) == 64 and handoff.metadata["page_count"] == 3


def test_text_is_masked_before_it_leaves(register, monkeypatch) -> None:
    acct, document_id = _processed(register, monkeypatch)
    handoff = _handoff(acct, document_id)
    assert "jane.doe@example.org" not in handoff.pages[0].text
    assert "[EMAIL" in handoff.pages[0].text
    assert handoff.metadata["pii_masked"] is True and handoff.metadata["pii_items_masked"] >= 1
    raw = _handoff(acct, document_id, mask_pii=False)  # explicit operator choice only
    assert "jane.doe@example.org" in raw.pages[0].text


def test_unprocessed_foreign_and_deleted_documents_are_refused(
    register, client, monkeypatch
) -> None:
    acct, document_id = _processed(register, monkeypatch)
    other = register("other")
    with SessionLocal() as db, pytest.raises(cr.HandoffRefused) as foreign:
        cr.build_handoff(db, uuid.UUID(other.org), uuid.UUID(document_id))
    assert (
        foreign.value.code == "DOCUMENT_NOT_FOUND"
    )  # another tenant's id is indistinguishable from missing

    fresh = acct.upload(
        acct.project("p2"), __import__("tests.conftest", fromlist=["x"]).make_pdf(2)
    ).json()
    with SessionLocal() as db, pytest.raises(cr.HandoffRefused) as pending:
        cr.build_handoff(db, uuid.UUID(acct.org), uuid.UUID(fresh["document_id"]))
    assert pending.value.code == "DOCUMENT_NOT_PROCESSED"

    deleted = client.delete(
        f"/api/v1/documents/{document_id}?organization_id={acct.org}", headers=acct.headers
    )
    assert deleted.status_code in (200, 204)
    with SessionLocal() as db, pytest.raises(cr.HandoffRefused) as gone:
        cr.build_handoff(db, uuid.UUID(acct.org), uuid.UUID(document_id))
    assert gone.value.code == "DOCUMENT_NOT_FOUND"


def test_invalid_correlation_id_is_refused(register, monkeypatch) -> None:
    acct, document_id = _processed(register, monkeypatch)
    with SessionLocal() as db, pytest.raises(cr.HandoffRefused):
        cr.build_handoff(
            db, uuid.UUID(acct.org), uuid.UUID(document_id), correlation_id="bad id\nX-Evil: 1"
        )


def test_doc_id_roundtrip() -> None:
    document = str(uuid.uuid4())
    assert cr.parse_doc_id(cr.page_doc_id(document, 7)) == (document, 7)
    assert cr.parse_doc_id("policy.md") is None


# --------------------------------------------------------------------------- the client (faked HTTP)
class FakeContextRag:
    """Mimics the verified behaviour: key -> tenant, per-tenant store, replace-on-ingest."""

    def __init__(self, keys: dict[str, str]) -> None:
        self.keys = keys  # api key -> tenant
        self.store: dict[str, dict[str, dict]] = {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import json

        self.requests.append(request)
        tenant = self.keys.get(request.headers.get("x-api-key", ""))
        if tenant is None:
            return httpx.Response(401, json={"detail": "invalid or missing X-API-Key"})
        docs = self.store.setdefault(tenant, {})
        if request.method == "POST" and request.url.path == "/ingest":
            out = []
            for d in json.loads(request.content)["documents"]:
                same = docs.get(d["doc_id"], {}).get("text") == d["text"]
                docs[d["doc_id"]] = d
                out.append({"doc_id": d["doc_id"], "unchanged": same})
            return httpx.Response(200, json={"tenant": tenant, "documents": out})
        if request.method == "GET" and request.url.path == "/documents":
            return httpx.Response(
                200, json={"tenant": tenant, "documents": [{"doc_id": k} for k in docs]}
            )
        if request.method == "DELETE" and request.url.path.startswith("/documents/"):
            docs.pop(request.url.path.removeprefix("/documents/"), None)
            return httpx.Response(200, json={"tenant": tenant})
        return httpx.Response(404)


def _client(fake: FakeContextRag, keys: dict[str, str]) -> cr.ContextRagClient:
    return cr.ContextRagClient(
        "http://localhost:8000", keys, client=httpx.Client(transport=httpx.MockTransport(fake))
    )


def test_upsert_writes_pages_with_the_tenant_key_and_never_x_tenant(register, monkeypatch) -> None:
    acct, document_id = _processed(register, monkeypatch)
    fake = FakeContextRag({"key-acme": "acme-tenant"})
    client = _client(fake, {acct.org: "key-acme"})
    result = client.upsert(_handoff(acct, document_id, correlation_id="corr-9"))
    assert (result.upserted, result.skipped_empty, result.retired) == (
        2,
        1,
        0,
    )  # blank page 3 is not sent
    assert set(fake.store["acme-tenant"]) == {
        cr.page_doc_id(document_id, 1),
        cr.page_doc_id(document_id, 2),
    }
    assert all("x-tenant" not in r.headers for r in fake.requests)
    assert all(r.headers["x-request-id"] == "corr-9" for r in fake.requests)
    assert "jane.doe" not in str(fake.store)  # masked text is what was sent
    again = client.upsert(_handoff(acct, document_id))
    assert (again.upserted, again.unchanged) == (0, 2)  # idempotent


def test_update_to_a_shorter_document_retires_the_stale_pages(register, monkeypatch) -> None:
    acct, document_id = _processed(register, monkeypatch, pages=["one", "two", "three"])
    fake = FakeContextRag({"k": "t"})
    client = _client(fake, {acct.org: "k"})
    client.upsert(_handoff(acct, document_id))
    assert len(fake.store["t"]) == 3
    shorter = _handoff(acct, document_id).model_copy(
        update={"pages": _handoff(acct, document_id).pages[:1]}
    )
    assert client.upsert(shorter).retired == 2
    assert set(fake.store["t"]) == {cr.page_doc_id(document_id, 1)}


def test_retire_removes_every_page_and_only_this_document(register, monkeypatch) -> None:
    acct, document_id = _processed(register, monkeypatch, pages=["one", "two"])
    fake = FakeContextRag({"k": "t"})
    fake.store["t"] = {"unrelated.md": {"text": "keep me"}}
    client = _client(fake, {acct.org: "k"})
    client.upsert(_handoff(acct, document_id))
    event = cr.RetireEvent(
        tenant_id=acct.org, document_id=document_id, correlation_id="corr-del", reason="deleted"
    )
    assert client.retire(event) == 2
    assert set(fake.store["t"]) == {"unrelated.md"}
    assert client.retire(event) == 0  # idempotent


def test_missing_key_fails_closed_without_any_request(register, monkeypatch) -> None:
    acct, document_id = _processed(register, monkeypatch)
    fake = FakeContextRag({"key-other": "other-tenant"})
    client = _client(fake, {"some-other-org": "key-other"})  # no key for acct.org
    with pytest.raises(cr.ContextRagRejected) as refused:
        client.upsert(_handoff(acct, document_id))
    assert refused.value.code == "NO_KEY_FOR_TENANT"
    assert fake.requests == [] and fake.store == {}  # nothing reached any tenant


def test_error_classes(register, monkeypatch) -> None:
    acct, document_id = _processed(register, monkeypatch)
    for status, expected in (
        (503, cr.ContextRagUnavailable),
        (429, cr.ContextRagUnavailable),
        (401, cr.ContextRagRejected),
    ):
        client = cr.ContextRagClient(
            "http://localhost:8000",
            {acct.org: "k"},
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda r, s=status: httpx.Response(s, text="doc text jane.doe")
                )
            ),
        )
        with pytest.raises(expected) as caught:
            client.upsert(_handoff(acct, document_id))
        assert "jane.doe" not in str(caught.value)


def test_network_failure_is_unavailable_and_message_has_no_content(register, monkeypatch) -> None:
    acct, document_id = _processed(register, monkeypatch)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connect failed for text jane.doe@example.org")

    client = cr.ContextRagClient(
        "http://localhost:8000",
        {acct.org: "k"},
        client=httpx.Client(transport=httpx.MockTransport(boom)),
    )
    with pytest.raises(cr.ContextRagUnavailable) as caught:
        client.upsert(_handoff(acct, document_id))
    assert "jane.doe" not in str(caught.value)


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("http://rag.internal.example.com", ("rag.internal.example.com",)),  # plain HTTP off-host
        ("https://evil.example.net", ("rag.internal.example.com",)),  # host not allow-listed
        ("https://rag.internal.example.com", ()),  # nothing allow-listed
        ("ftp://localhost", ()),
        ("https://", ()),
    ],
)
def test_destination_policy(url: str, allowed: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="CONTEXTRAG"):
        cr.ContextRagClient(url, {}, allowed_hosts=allowed)


def test_allowed_destination_is_accepted() -> None:
    cr.ContextRagClient(
        "https://rag.internal.example.com", {}, allowed_hosts=("rag.internal.example.com",)
    )
    cr.ContextRagClient("http://localhost:8000", {})


def test_redirects_are_not_followed(register, monkeypatch) -> None:
    acct, document_id = _processed(register, monkeypatch)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})

    client = cr.ContextRagClient("http://localhost:8000", {acct.org: "k"})
    client._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    with pytest.raises(cr.ContextRagRejected):
        client.upsert(_handoff(acct, document_id))
    assert all("169.254" not in u for u in seen)


# --------------------------------------------------------------------------- real ContextRAG, in process
@pytest.fixture
def real_app():
    pytest.importorskip("contextrag.api.server", reason="ContextRAG_Engine is not installed")
    from contextrag import factory
    from contextrag.api.auth import KeyStore, hash_key
    from contextrag.api.server import create_app
    from fastapi.testclient import TestClient

    entries = {
        hash_key("admin-a"): {"id": "docmind-a", "tenant": "tenant-a", "role": "admin"},
        hash_key("admin-b"): {"id": "docmind-b", "tenant": "tenant-b", "role": "admin"},
        hash_key("reader-a"): {"id": "ana", "tenant": "tenant-a", "role": "reader"},
        hash_key("reader-a-legal"): {
            "id": "luis",
            "tenant": "tenant-a",
            "role": "reader",
            "groups": ["legal"],
        },
    }
    return TestClient(create_app(factory.offline(), keystore=KeyStore(entries)))


def _ask(app, key: str, question: str) -> dict:
    response = app.post("/query", json={"question": question}, headers={"X-API-Key": key})
    assert response.status_code == 200, response.text
    return response.json()


def _answer_text(body: dict) -> str:
    return str(body)


def test_real_contextrag_roundtrip_isolation_update_and_delete(
    register, monkeypatch, real_app
) -> None:
    app = real_app
    acct_a, doc_a = _processed(
        register, monkeypatch, pages=["The warranty period is 24 months from delivery."], label="a"
    )
    acct_b, doc_b = _processed(
        register, monkeypatch, pages=["Tenant B secret: the launch code is ZEBRA-7731."], label="b"
    )
    client = cr.ContextRagClient(
        "http://localhost:8000", {acct_a.org: "admin-a", acct_b.org: "admin-b"}, client=app
    )

    assert client.upsert(_handoff(acct_a, doc_a)).upserted == 1
    assert client.upsert(_handoff(acct_b, doc_b)).upserted == 1

    # ContextRAG lists the page documents under the tenant bound to each key, and only there.
    listed_a = {
        d["doc_id"]
        for d in app.get("/documents", headers={"X-API-Key": "admin-a"}).json()["documents"]
    }
    assert listed_a == {cr.page_doc_id(doc_a, 1)}

    # Tenant A's reader finds A's fact, cites the page document, and never sees tenant B's text.
    answer = _ask(app, "reader-a", "How long is the warranty period?")
    assert "24 months" in _answer_text(answer)
    assert "ZEBRA" not in _answer_text(_ask(app, "reader-a", "What is the launch code?"))

    # An update replaces the old text: the old fact must stop being retrievable.
    updated = _handoff(acct_a, doc_a).model_copy(
        update={
            "version": 2,
            "pages": [
                cr.PagePayload(
                    page=1,
                    page_basis="physical",
                    text="The warranty period is 36 months from delivery.",
                )
            ],
        }
    )
    client.upsert(updated)
    after = _answer_text(_ask(app, "reader-a", "How long is the warranty period?"))
    assert "36 months" in after and "24 months" not in after

    # A deletion removes it everywhere.
    assert (
        client.retire(
            cr.RetireEvent(tenant_id=acct_a.org, document_id=doc_a, correlation_id="corr-del")
        )
        == 1
    )
    gone = _answer_text(_ask(app, "reader-a", "How long is the warranty period?"))
    assert "36 months" not in gone and "24 months" not in gone
    assert app.get("/documents", headers={"X-API-Key": "admin-a"}).json()["documents"] == []
    # Tenant B is untouched by tenant A's lifecycle.
    assert {
        d["doc_id"]
        for d in app.get("/documents", headers={"X-API-Key": "admin-b"}).json()["documents"]
    } == {cr.page_doc_id(doc_b, 1)}


def test_real_contextrag_acl_is_enforced_inside_retrieval(register, monkeypatch, real_app) -> None:
    app = real_app
    acct, doc = _processed(
        register,
        monkeypatch,
        pages=["Legal only: the settlement amount is 90000 euros."],
        label="a",
    )
    client = cr.ContextRagClient("http://localhost:8000", {acct.org: "admin-a"}, client=app)
    client.upsert(_handoff(acct, doc, acl=("legal",)))
    assert "90000" not in _answer_text(
        _ask(app, "reader-a", "What is the settlement amount?")
    )  # no group
    assert "90000" in _answer_text(_ask(app, "reader-a-legal", "What is the settlement amount?"))


def test_real_contextrag_rejects_a_reader_key_for_ingest(register, monkeypatch, real_app) -> None:
    app = real_app
    acct, doc = _processed(register, monkeypatch, pages=["Anything"], label="a")
    client = cr.ContextRagClient("http://localhost:8000", {acct.org: "reader-a"}, client=app)
    with pytest.raises(cr.ContextRagRejected) as caught:
        client.upsert(_handoff(acct, doc))
    assert caught.value.status == 403


def test_document_row_is_unchanged_by_handoff(register, monkeypatch) -> None:
    acct, document_id = _processed(register, monkeypatch)
    _handoff(acct, document_id)
    with SessionLocal() as db:
        row = db.scalar(select(Document).where(Document.id == uuid.UUID(document_id)))
        assert row is not None and row.deleted_at is None
