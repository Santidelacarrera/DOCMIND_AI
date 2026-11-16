"""Tests for document intelligence (entities, summaries, comparison, templates, tags) and SaaS billing/plans."""

import io
import uuid

import pytest
from openpyxl import load_workbook
from qdrant_client import QdrantClient

from app import vector_index, worker
from app.core import settings
from app.evaluation.pdfgen import digital_pdf
from app.ingestion.embeddings import HashingEmbedder
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.vectorstore import QdrantStore
from app.models import OrganizationMember, Role


@pytest.fixture
def qdrant_env(monkeypatch: pytest.MonkeyPatch):
    client = QdrantClient(":memory:")
    store = QdrantStore(client, "intel_test")
    pipeline = IngestionPipeline(store, HashingEmbedder(256))
    monkeypatch.setattr(settings(), "qdrant_url", "http://in-memory")
    monkeypatch.setattr(vector_index, "get_store", lambda: store)
    monkeypatch.setattr(vector_index, "get_pipeline", lambda: pipeline)
    return pipeline


def _upload_and_process(acct, project: str, pages: list[list[str]], filename: str) -> str:
    up = acct.upload(project, digital_pdf(pages), filename)
    assert up.status_code == 202, up.text
    worker.process_document.run(up.json()["job_id"])  # type: ignore[attr-defined]
    return str(up.json()["document_id"])


def test_templates_classification_entities_search_and_summary(register, client, qdrant_env) -> None:
    acct = register("intel")
    project = acct.project("legal")

    tpl = client.get(f"/api/v1/schemas/templates?organization_id={acct.org}", headers=acct.headers)
    assert tpl.status_code == 200
    assert any(t["id"] == "dte_cl" for t in tpl.json())

    doc_id = _upload_and_process(
        acct,
        project,
        [[
            "Contrato de Servicios Comerciales con Industrias Valdivia.",
            "Fecha de vigencia: 2026-03-15. Monto total: USD 12,500.00.",
            "Contacto: legal@valdivia.cl. Firmado por: Ana Torres.",
            "Governing law: Chile. Termination notice is 30 days.",
        ]],
        "contrato_valdivia.pdf",
    )

    detail = client.get(f"/api/v1/documents/{doc_id}?organization_id={acct.org}", headers=acct.headers).json()
    assert detail["category"] == "contract"
    assert "contract" in detail["tags"]

    # Search and filter
    hits = client.get(
        f"/api/v1/documents?organization_id={acct.org}&q=Valdivia&category=contract",
        headers=acct.headers,
    ).json()
    assert [d["id"] for d in hits] == [doc_id]

    # Manual metadata update
    patched = client.patch(
        f"/api/v1/documents/{doc_id}/metadata?organization_id={acct.org}",
        json={"category": "policy", "tags": ["Priority", "Q1"]},
        headers=acct.headers,
    )
    assert patched.status_code == 200
    assert patched.json()["category"] == "policy"
    assert patched.json()["tags"] == ["priority", "q1"]

    # Deterministic entities
    ents = client.get(f"/api/v1/documents/{doc_id}/entities?organization_id={acct.org}", headers=acct.headers)
    assert ents.status_code == 200
    body = ents.json()["entities"]
    assert any(d["value"] == "2026-03-15" for d in body["dates"])
    assert any("12,500" in a["value"] for a in body["amounts"])
    assert any(e["value"] == "legal@valdivia.cl" for e in body["emails"])

    # Grounded summary (single doc & project)
    sum_doc = client.post(
        f"/api/v1/documents/{doc_id}/summarize?organization_id={acct.org}",
        json={"max_sentences": 3},
        headers=acct.headers,
    )
    assert sum_doc.status_code == 200
    assert not sum_doc.json()["abstained"]
    assert sum_doc.json()["citations"][0]["document_id"] == doc_id

    sum_proj = client.post(
        f"/api/v1/projects/{project}/summarize?organization_id={acct.org}",
        json={"max_sentences": 3},
        headers=acct.headers,
    )
    assert sum_proj.status_code == 200
    assert sum_proj.json()["documents_included"] == 1


def test_document_comparison_and_exports(register, client) -> None:
    acct = register("cmp")
    project = acct.project("contracts")
    d1 = _upload_and_process(
        acct, project, [["Renewal fee is 1000 USD per year. Notice period is 30 days."]], "v1.pdf"
    )
    d2 = _upload_and_process(
        acct, project, [["Renewal fee is 2500 USD per year. Notice period is 60 days."]], "v2.pdf"
    )

    cmp_json = client.post(
        f"/api/v1/documents/compare?organization_id={acct.org}",
        json={"document_ids": [d1, d2], "topic": "renewal fee notice"},
        headers=acct.headers,
    )
    assert cmp_json.status_code == 200
    data = cmp_json.json()
    assert data["summary"]["documents_compared"] == 2
    assert data["summary"]["conflicts_found"] >= 1

    cmp_csv = client.post(
        f"/api/v1/documents/compare?organization_id={acct.org}&format=csv",
        json={"document_ids": [d1, d2]},
        headers=acct.headers,
    )
    assert cmp_csv.status_code == 200 and "text/csv" in cmp_csv.headers["content-type"]

    cmp_xlsx = client.post(
        f"/api/v1/documents/compare?organization_id={acct.org}&format=xlsx",
        json={"document_ids": [d1, d2]},
        headers=acct.headers,
    )
    assert cmp_xlsx.status_code == 200
    wb = load_workbook(io.BytesIO(cmp_xlsx.content))
    assert wb.active is not None and wb.active.title == "Comparison"


def test_ask_document_scope_and_query_history_with_redaction(register, client, qdrant_env) -> None:
    acct = register("askhist")
    other = register("outsider")
    p1 = acct.project("p1")
    d1 = _upload_and_process(acct, p1, [["Alpha project warranty duration is 24 months."]], "alpha.pdf")
    d2 = _upload_and_process(acct, p1, [["Beta project warranty duration is 12 months."]], "beta.pdf")
    vector_index.index_document(acct.org, d1)
    vector_index.index_document(acct.org, d2)

    # Scoped to d2 only: must not return d1's 24 months
    res = client.post(
        f"/api/v1/ask?organization_id={acct.org}",
        json={"question": "What is the warranty duration?", "document_ids": [d2]},
        headers=acct.headers,
    )
    assert res.status_code == 200
    assert "12 months" in res.json()["answer"]
    assert all(c["document_id"] == d2 for c in res.json()["citations"])

    # Outsider's document_id passed by acct is silently excluded (cannot leak)
    d_other = _upload_and_process(other, other.project("op"), [["Secret warranty duration 99 months."]], "other.pdf")
    vector_index.index_document(other.org, d_other)
    res_foreign = client.post(
        f"/api/v1/ask?organization_id={acct.org}",
        json={"question": "What is the warranty duration?", "document_ids": [d_other]},
        headers=acct.headers,
    )
    assert res_foreign.status_code == 200
    assert res_foreign.json()["abstained"] is True

    # Query history shows the first query, and redacts when d2 is deleted
    hist = client.get(f"/api/v1/ask/history?organization_id={acct.org}", headers=acct.headers)
    assert hist.status_code == 200
    assert len(hist.json()) >= 2
    assert any("12 months" in item["answer_preview"] for item in hist.json())

    assert client.delete(f"/api/v1/documents/{d2}?organization_id={acct.org}", headers=acct.headers).status_code == 204
    hist_after = client.get(f"/api/v1/ask/history?organization_id={acct.org}", headers=acct.headers).json()
    assert all("12 months" not in item["answer_preview"] for item in hist_after)


def test_saas_plans_quotas_alerts_and_billing(register, client, monkeypatch: pytest.MonkeyPatch) -> None:
    acct = register("saas")
    plans_resp = client.get("/api/v1/plans")
    assert plans_resp.status_code == 200
    codes = [p["code"] for p in plans_resp.json()]
    assert codes == ["FREE", "STARTER", "PROFESSIONAL", "BUSINESS", "ENTERPRISE"]

    # Usage alerts when approaching FREE page limit (20 pages)
    proj = acct.project("billing")
    monkeypatch.setattr(settings(), "free_pages_per_month", 5)
    up = acct.upload(proj, digital_pdf([["P1"], ["P2"], ["P3"], ["P4"]]), "four.pdf")
    assert up.status_code == 202  # 4/5 = 80% -> warning alert

    usage = client.get(f"/api/v1/billing/usage?organization_id={acct.org}", headers=acct.headers)
    assert usage.status_code == 200
    assert any(a["resource"] == "pages_per_month" and a["status"] == "warning" for a in usage.json()["alerts"])

    # Uploading 2 more pages exceeds FREE quota (4 + 2 = 6 > 5 -> 402)
    over = acct.upload(proj, digital_pdf([["P5"], ["P6"]]), "over.pdf")
    assert over.status_code == 402 and over.json()["detail"] == "QUOTA_EXCEEDED"

    # Checkout preview does not charge
    preview = client.post(
        f"/api/v1/billing/checkout-preview?organization_id={acct.org}",
        json={"target_plan": "PROFESSIONAL", "billing_cycle": "annual"},
        headers=acct.headers,
    )
    assert preview.status_code == 200 and preview.json()["live_charges_enabled"] is False

    # Upgrade to PROFESSIONAL unlocks 5000 pages/month
    sub = client.patch(
        f"/api/v1/billing/subscription?organization_id={acct.org}",
        json={"plan": "PROFESSIONAL", "billing_cycle": "monthly"},
        headers=acct.headers,
    )
    assert sub.status_code == 200 and sub.json()["plan"]["code"] == "PROFESSIONAL"

    ok_now = acct.upload(proj, digital_pdf([["P5"], ["P6"]]), "over.pdf")
    assert ok_now.status_code == 202

    # Statements and jobs endpoint
    stmts = client.get(f"/api/v1/billing/statements?organization_id={acct.org}", headers=acct.headers)
    assert stmts.status_code == 200 and len(stmts.json()) >= 1
    jobs = client.get(f"/api/v1/jobs?organization_id={acct.org}", headers=acct.headers)
    assert jobs.status_code == 200 and len(jobs.json()) >= 2

    # Non-owner member cannot change subscription
    member = register("nonowner")
    uid = uuid.UUID(client.get("/api/v1/auth/me", headers=member.headers).json()["id"])
    from app.db import SystemSessionLocal

    with SystemSessionLocal() as db:
        db.add(OrganizationMember(organization_id=uuid.UUID(acct.org), user_id=uid, role=Role.admin))
        db.commit()
    denied = client.patch(
        f"/api/v1/billing/subscription?organization_id={acct.org}",
        json={"plan": "ENTERPRISE"},
        headers=member.headers,
    )
    assert denied.status_code == 403

    # Query quota block
    monkeypatch.setattr(
        settings(),
        "plans_config_json",
        '{"PROFESSIONAL": {"max_queries_per_month": 0}}',
    )
    monkeypatch.setattr(settings(), "qdrant_url", "http://in-memory")
    q_blocked = client.post(
        f"/api/v1/ask?organization_id={acct.org}",
        json={"question": "test quota"},
        headers=acct.headers,
    )
    assert q_blocked.status_code == 402 and q_blocked.json()["detail"] == "QUOTA_EXCEEDED"
