"""Case (project) queries: a question is answered only from documents of the requested project.

All documents are synthetic. Projects in the same organization share vocabulary on purpose, so
the project condition, not the wording, decides what can be cited.
"""

import logging
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app import main, vector_index, worker
from app.core import settings
from app.db import SessionLocal
from app.evaluation.pdfgen import digital_pdf
from app.ingestion.embeddings import HashingEmbedder
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.vectorstore import PROJECT_KEY, QdrantStore
from app.models import AuditLog, OrganizationMember

qdrant_client = pytest.importorskip("qdrant_client")
models = pytest.importorskip("qdrant_client.models")
EMBED = HashingEmbedder(64)
COLLECTION = "case-query"

ALPHA_TEXT = ["Alpha lease clause. The rent for the Madrid local is 1200 euros per month under this lease."]
BETA_TEXT = ["Beta lease clause. The rent for the Madrid local is 3400 euros per month under this lease."]
QUESTION = "What is the rent for the Madrid local?"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Any:
    client = qdrant_client.QdrantClient(":memory:")
    store = QdrantStore(client, COLLECTION)
    pipeline = IngestionPipeline(store, EMBED, max_chars=400, overlap=40)
    monkeypatch.setattr(settings(), "qdrant_url", "https://qdrant.test")
    monkeypatch.setattr(vector_index, "get_store", lambda: store)
    monkeypatch.setattr(vector_index, "get_pipeline", lambda: pipeline)
    monkeypatch.setattr(worker.index_document, "delay", lambda org, doc: None)
    monkeypatch.setattr(main.process_vector_deletions, "delay", lambda: None)

    class Env:
        pass

    e = Env()
    e.client, e.store, e.pipeline = client, store, pipeline  # type: ignore[attr-defined]
    return e


def ingest(register: Any, env: Any, lines: list[str], *, acct: Any = None, project: str | None = None) -> tuple[Any, str, str]:
    """Upload, process and index one synthetic document. Returns (account, project_id, document_id)."""
    acct = acct or register()
    project = project or acct.project(f"case-{uuid.uuid4().hex[:6]}")
    body = acct.upload(project, digital_pdf([lines])).json()
    worker.process_document.run(body["job_id"])  # type: ignore[attr-defined]
    assert vector_index.index_document(acct.org, body["document_id"]) == "indexed"
    return acct, project, body["document_id"]


def ask(client: Any, acct: Any, project: str, question: str = QUESTION, *, headers: dict[str, str] | None = None,
        organization: str | None = None, top_k: int = 4) -> Any:
    return client.post(
        f"/api/v1/projects/{project}/ask?organization_id={organization or acct.org}",
        json={"question": question, "top_k": top_k}, headers=headers or acct.headers,
    )


def stored_project_ids(env: Any, org: str) -> set[str | None]:
    points, _ = env.client.scroll(COLLECTION, scroll_filter=QdrantStore.tenant_filter(org), limit=500, with_payload=True)
    return {(p.payload or {}).get(PROJECT_KEY) for p in points}


# --------------------------------------------------------------------- retrieval and citations


def test_a_project_query_cites_only_its_own_project(register: Any, client: Any, env: Any) -> None:
    acct, alpha, alpha_doc = ingest(register, env, ALPHA_TEXT)
    _, beta, beta_doc = ingest(register, env, BETA_TEXT, acct=acct)

    body = ask(client, acct, alpha).json()
    assert body["abstained"] is False
    assert {c["document_id"] for c in body["citations"]} == {alpha_doc}
    assert "1200" in body["answer"] and "3400" not in body["answer"]

    body = ask(client, acct, beta).json()
    assert {c["document_id"] for c in body["citations"]} == {beta_doc}
    assert "3400" in body["answer"] and "1200" not in body["answer"]


def test_the_project_condition_is_part_of_the_vector_query(register: Any, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    acct, alpha, _ = ingest(register, env, ALPHA_TEXT)
    ingest(register, env, BETA_TEXT, acct=acct)
    seen: list[Any] = []  # the filters Qdrant was asked to apply
    original = env.client.query_points

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("query_filter"))
        return original(*args, **kwargs)

    monkeypatch.setattr(env.client, "query_points", spy)
    env.pipeline.store.search(acct.org, EMBED.embed([QUESTION])[0], limit=10, project_id=alpha)
    keys = [(c.key, c.match.value) for c in seen[-1].must if hasattr(c, "match")]
    assert (PROJECT_KEY, alpha) in keys  # enforced by Qdrant, not filtered afterwards


def test_the_store_never_returns_another_projects_points_even_when_they_score_higher(register: Any, env: Any) -> None:
    acct, _, _ = ingest(register, env, ALPHA_TEXT)
    _, beta, _ = ingest(register, env, BETA_TEXT, acct=acct)
    hits = env.store.search(acct.org, EMBED.embed([ALPHA_TEXT[0]])[0], limit=50, project_id=beta)
    assert hits and {h[PROJECT_KEY] for h in hits} == {beta}


def test_a_project_with_no_documents_abstains(register: Any, client: Any, env: Any) -> None:
    acct, _, _ = ingest(register, env, ALPHA_TEXT)
    empty = acct.project("empty-case")
    body = ask(client, acct, empty).json()
    assert body["abstained"] is True and body["citations"] == []


# --------------------------------------------------------------------- authorization


def test_a_project_of_another_organization_and_a_missing_project_give_the_same_404(
    register: Any, client: Any, env: Any
) -> None:
    owner, foreign_project, _ = ingest(register, env, ALPHA_TEXT)
    outsider = register("outsider")
    ingest(register, env, ["Unrelated text about another client entirely."], acct=outsider)

    # The outsider is a member of its own organization only. Its membership passes, and the
    # owner's project id then fails the organization check exactly like an identifier that does not exist.
    foreign = ask(client, outsider, foreign_project)
    missing = ask(client, outsider, str(uuid.uuid4()))
    malformed = ask(client, outsider, "not-a-uuid")
    assert foreign.status_code == missing.status_code == malformed.status_code == 404
    assert foreign.json() == missing.json() == malformed.json() == {"detail": "PROJECT_NOT_FOUND"}
    # The owner is not a member of the outsider's organization, so it is refused before any lookup.
    cross = ask(client, owner, foreign_project, organization=outsider.org)
    assert cross.status_code == 403


def test_with_the_vector_index_disabled_the_answer_is_503_only_for_an_own_project(
    register: Any, client: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    acct, project, _ = ingest(register, env, ALPHA_TEXT)
    monkeypatch.setattr(settings(), "qdrant_url", None)
    assert ask(client, acct, project).status_code == 503
    assert ask(client, acct, str(uuid.uuid4())).status_code == 404


def test_an_existing_project_of_the_same_organization_is_404_for_a_missing_id(register: Any, client: Any, env: Any) -> None:
    acct, project, _ = ingest(register, env, ALPHA_TEXT)
    missing = ask(client, acct, str(uuid.uuid4()))
    malformed = ask(client, acct, "not-a-uuid")
    assert missing.status_code == malformed.status_code == 404
    assert missing.json() == malformed.json() == {"detail": "PROJECT_NOT_FOUND"}
    assert ask(client, acct, project).status_code == 200


def test_api_key_needs_the_documents_read_scope_and_stops_working_when_revoked(
    register: Any, client: Any, env: Any
) -> None:
    acct, project, _ = ingest(register, env, ALPHA_TEXT)
    read_key = client.post(
        f"/api/v1/api-keys?organization_id={acct.org}&name=reader&project_id={project}",
        json=["documents:read"], headers=acct.headers,
    ).json()
    headers = {"Authorization": f"Bearer {read_key['key']}"}
    assert ask(client, acct, project, headers=headers).status_code == 200

    # Phase 3B: a key created without a project list is organization-wide and cannot use case queries.
    legacy = client.post(
        f"/api/v1/api-keys?organization_id={acct.org}&name=legacy", json=["documents:read"], headers=acct.headers
    ).json()
    assert ask(client, acct, project, headers={"Authorization": f"Bearer {legacy['key']}"}).status_code == 404

    other_scope = client.post(
        f"/api/v1/api-keys?organization_id={acct.org}&name=exports", json=["exports:read"], headers=acct.headers
    ).json()
    refused = ask(client, acct, project, headers={"Authorization": f"Bearer {other_scope['key']}"})
    assert refused.status_code == 403 and refused.json()["detail"] == "INSUFFICIENT_SCOPE"

    client.delete(f"/api/v1/api-keys/{read_key['id']}?organization_id={acct.org}", headers=acct.headers)
    assert ask(client, acct, project, headers=headers).status_code in (401, 403)


def test_removing_membership_takes_effect_immediately(register: Any, client: Any, env: Any) -> None:
    acct, project, _ = ingest(register, env, ALPHA_TEXT)
    with SessionLocal.begin() as db:
        member = db.scalar(select(OrganizationMember).where(OrganizationMember.organization_id == uuid.UUID(acct.org)))
        assert member is not None
        db.delete(member)
    assert ask(client, acct, project).status_code == 403


# --------------------------------------------------------------------- unverifiable and legacy vectors


def test_vectors_without_a_project_never_match_a_project_query(register: Any, client: Any, env: Any) -> None:
    acct, project, doc = ingest(register, env, ALPHA_TEXT)
    # Simulate legacy vectors: the project key is gone from the point payload.
    env.client.delete_payload(
        COLLECTION, keys=[PROJECT_KEY],
        points=models.FilterSelector(filter=QdrantStore.tenant_filter(acct.org, document_id=doc)),
    )
    body = ask(client, acct, project).json()
    assert body["abstained"] is True and body["citations"] == []
    assert env.store.count_without_project(acct.org) >= 1


def test_backfill_stamps_legacy_vectors_is_idempotent_and_restores_answers(register: Any, client: Any, env: Any) -> None:
    acct, project, doc = ingest(register, env, ALPHA_TEXT)
    env.client.delete_payload(
        COLLECTION, keys=[PROJECT_KEY],
        points=models.FilterSelector(filter=QdrantStore.tenant_filter(acct.org, document_id=doc)),
    )
    assert ask(client, acct, project).json()["abstained"] is True

    first = vector_index.backfill_project_ids()
    assert first == {"documents": 1, "failed": 0}
    assert vector_index.unscoped_points(acct.org) == 0
    assert stored_project_ids(env, acct.org) == {project}
    assert ask(client, acct, project).json()["abstained"] is False

    second = vector_index.backfill_project_ids()
    assert second == first  # same work, same result
    assert stored_project_ids(env, acct.org) == {project}


def test_backfill_skips_deleted_documents(register: Any, client: Any, env: Any) -> None:
    _, _, doc = ingest(register, env, ALPHA_TEXT)
    with SessionLocal.begin() as db:
        from app.models import Document, utcnow

        db.get(Document, uuid.UUID(doc)).deleted_at = utcnow()  # type: ignore[union-attr]
    assert vector_index.backfill_project_ids() == {"documents": 0, "failed": 0}


def test_reindexing_keeps_the_project_and_does_not_duplicate(register: Any, client: Any, env: Any) -> None:
    acct, project, doc = ingest(register, env, ALPHA_TEXT)
    before = env.client.count(COLLECTION).count
    assert vector_index.index_document(acct.org, doc) == "indexed"
    assert env.client.count(COLLECTION).count == before
    assert stored_project_ids(env, acct.org) == {project}
    assert ask(client, acct, project).json()["abstained"] is False


def test_a_deleted_document_is_never_cited(register: Any, client: Any, env: Any) -> None:
    acct, project, doc = ingest(register, env, ALPHA_TEXT)
    assert ask(client, acct, project).json()["abstained"] is False
    assert client.delete(f"/api/v1/documents/{doc}?organization_id={acct.org}", headers=acct.headers).status_code == 204
    assert ask(client, acct, project).json()["abstained"] is True


# --------------------------------------------------------------------- contract details


def test_top_k_is_bounded(register: Any, client: Any, env: Any) -> None:
    acct, project, _ = ingest(register, env, ALPHA_TEXT)
    assert ask(client, acct, project, top_k=9).status_code == 422
    assert ask(client, acct, project, top_k=8).status_code == 200


def test_request_id_is_echoed_and_written_to_the_audit_trail(register: Any, client: Any, env: Any) -> None:
    acct, project, _ = ingest(register, env, ALPHA_TEXT)
    response = ask(client, acct, project, headers={**acct.headers, "X-Request-Id": "case-trace-001"})
    assert response.headers["X-Request-Id"] == "case-trace-001"
    with SessionLocal() as db:
        row = db.scalar(select(AuditLog).where(AuditLog.action == "document.project_ask").order_by(AuditLog.created_at.desc()))
        assert row is not None and row.target_id == project
        assert row.metadata_["request_id"] == "case-trace-001"
        assert set(row.metadata_) == {"request_id", "citations", "abstained"}  # no text in the audit record


def test_an_unsafe_request_id_is_replaced_not_trusted(register: Any, client: Any, env: Any) -> None:
    acct, project, _ = ingest(register, env, ALPHA_TEXT)
    response = ask(client, acct, project, headers={**acct.headers, "X-Request-Id": "bad id\nINJECTED"})
    echoed = response.headers["X-Request-Id"]
    assert echoed != "bad id\nINJECTED" and len(echoed) == 32


def test_logs_do_not_contain_the_question_or_document_text(register: Any, client: Any, env: Any, caplog: pytest.LogCaptureFixture) -> None:
    acct, project, _ = ingest(register, env, ["Alpha lease clause. Tenant Juan Perez Lopez, passport X1234567."])
    question = "Cual es el nombre de Juan Perez Lopez en el contrato alfa?"
    with caplog.at_level(logging.DEBUG):
        response = ask(client, acct, project, question=question, headers={**acct.headers, "X-Request-Id": "log-check-01"})
    assert response.status_code == 200
    answered = [r for r in caplog.records if r.getMessage() == "project query answered"]
    assert answered and answered[-1].request_id == "log-check-01"  # the id is a structured field...
    assert "Perez" not in caplog.text and "X1234567" not in caplog.text  # ...the content is not logged
    assert "Cual es el nombre" not in caplog.text


# --------------------------------------------------------------------- pagination


def test_pagination_is_not_offered_and_citations_are_bounded_by_top_k(register: Any, client: Any, env: Any) -> None:
    """The endpoint returns one answer, not a list of pages: the bound is ``top_k`` (1..8)."""
    acct, project, _ = ingest(register, env, ALPHA_TEXT)
    body = ask(client, acct, project, top_k=1).json()
    assert set(body) == {"answer", "abstained", "citations"}
    assert len(body["citations"]) <= 1
