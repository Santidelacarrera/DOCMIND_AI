"""The whole document lifecycle through the API, with the real pipeline and an in-memory Qdrant:

upload -> process -> extract -> persist -> index -> ask (cited) -> upload a new version -> reprocess ->
reindex -> ask (new content cited, old content gone) -> delete -> vectors removed -> ask abstains.
"""

from typing import Any

import pytest

from app import vector_index, worker
from app.evaluation.pdfgen import digital_pdf
from tests.test_case_query import env  # noqa: F401  (fixture)

pytest.importorskip("qdrant_client")

V1 = ["Maintenance contract. The monthly fee for the Lisbon warehouse is 1800 euros under this contract."]
V2 = ["Maintenance contract. The monthly fee for the Lisbon warehouse is 2600 euros after the amendment."]
QUESTION = "What is the monthly fee for the Lisbon warehouse?"


def _points(store: Any, org: str, doc: str) -> list[Any]:
    from qdrant_client import models

    points, _ = store._c.scroll(store._collection, scroll_filter=store.tenant_filter(org, document_id=doc), limit=100,
                                with_payload=True)
    assert all(isinstance(p, models.Record) for p in points)
    return points


def _ask(client: Any, acct: Any, project: str) -> dict[str, Any]:
    response = client.post(f"/api/v1/projects/{project}/ask?organization_id={acct.org}",
                           json={"question": QUESTION}, headers=acct.headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_upload_answer_update_reindex_delete(register: Any, client: Any, env: Any) -> None:  # noqa: F811
    acct = register("lifecycle")
    project = acct.project("lisbon")
    uploaded = acct.upload(project, digital_pdf([V1]), "contract.pdf").json()
    doc = uploaded["document_id"]
    worker.process_document.run(uploaded["job_id"])  # type: ignore[attr-defined]
    status = client.get(f"/api/v1/documents/{doc}/status?organization_id={acct.org}", headers=acct.headers).json()
    assert status["status"] == "COMPLETED", status
    assert vector_index.index_document(acct.org, doc) == "indexed"
    first = _ask(client, acct, project)
    assert "1800" in first["answer"] and first["citations"][0]["document_id"] == doc

    # New version: same document id, new content, reprocessed and reindexed.
    version = client.post(f"/api/v1/documents/{doc}/versions?organization_id={acct.org}",
                          files={"file": ("contract-v2.pdf", digital_pdf([V2]), "application/pdf")}, headers=acct.headers)
    assert version.status_code == 202, version.text
    busy = client.post(f"/api/v1/documents/{doc}/versions?organization_id={acct.org}",
                       files={"file": ("contract-v3.pdf", digital_pdf([V1 + ["x"]]), "application/pdf")},
                       headers=acct.headers)
    assert busy.status_code == 409 and busy.json()["detail"] == "DOCUMENT_BUSY"
    worker.process_document.run(version.json()["job_id"])  # type: ignore[attr-defined]
    assert vector_index.index_document(acct.org, doc) == "indexed"
    texts = " ".join(str(p.payload["text"]) for p in _points(env.store, acct.org, doc))
    assert "2600" in texts and "1800" not in texts  # the previous version's chunks are gone
    second = _ask(client, acct, project)
    assert "2600" in second["answer"] and "1800" not in second["answer"]
    meta = client.get(f"/api/v1/documents/{doc}?organization_id={acct.org}", headers=acct.headers).json()
    assert meta["filename"] == "contract-v2.pdf"
    same = client.post(f"/api/v1/documents/{doc}/versions?organization_id={acct.org}",
                       files={"file": ("again.pdf", digital_pdf([V2]), "application/pdf")}, headers=acct.headers)
    assert same.status_code == 202 and same.json()["status"] == "unchanged"

    # Delete: the document disappears from answers at once and its vectors are removed by the outbox.
    assert client.delete(f"/api/v1/documents/{doc}?organization_id={acct.org}", headers=acct.headers).status_code == 204
    assert _ask(client, acct, project)["abstained"] is True
    assert vector_index.process_deletions()["deleted"] >= 1
    assert _points(env.store, acct.org, doc) == []


def test_another_tenant_cannot_upload_a_version_of_my_document(register: Any, client: Any, env: Any) -> None:  # noqa: F811
    owner, intruder = register("owner"), register("intruder")
    project = owner.project("p")
    uploaded = owner.upload(project, digital_pdf([V1]), "contract.pdf").json()
    for org in (owner.org, intruder.org):
        response = client.post(f"/api/v1/documents/{uploaded['document_id']}/versions?organization_id={org}",
                               files={"file": ("x.pdf", digital_pdf([V2]), "application/pdf")}, headers=intruder.headers)
        assert response.status_code in (403, 404), response.text


def test_a_vector_store_outage_during_a_question_is_a_retryable_503(register: Any, client: Any, env: Any,  # noqa: F811
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    acct = register("outage")
    project = acct.project("p")

    def down(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("qdrant unreachable")

    monkeypatch.setattr(env.store, "search", down)
    response = client.post(f"/api/v1/projects/{project}/ask?organization_id={acct.org}",
                           json={"question": QUESTION}, headers=acct.headers)
    assert response.status_code == 503 and response.json()["detail"] == "VECTOR_INDEX_UNAVAILABLE"
