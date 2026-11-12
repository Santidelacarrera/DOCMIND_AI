"""Cross-encoder reranking (app.rag.rerank): reorders, never widens, and degrades instead of failing."""

import time
import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from app.ingestion import answering
from app.ingestion.docxgen import docx_bytes
from app.ingestion.embeddings import HashingEmbedder
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.vectorstore import QdrantStore
from app.rag import rerank

qdrant_client = pytest.importorskip("qdrant_client")


class Prefers:
    """Scores 1.0 for passages containing ``word``: lets the test decide what 'more relevant' means."""

    def __init__(self, word: str) -> None:
        self.word, self.name, self.queries = word, "prefers", []

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        self.queries.append(query)
        return [1.0 if self.word in p else 0.0 for p in passages]


class Broken:
    name = "broken"

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        raise RuntimeError("model crashed")


class Slow:
    name = "slow"

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        time.sleep(1.0)
        return [0.0] * len(passages)


@pytest.fixture
def indexed() -> tuple[IngestionPipeline, str, str]:
    p = IngestionPipeline(QdrantStore(qdrant_client.QdrantClient(":memory:"), f"r{uuid.uuid4().hex[:6]}"),
                          HashingEmbedder(256), max_chars=300, overlap=0)
    tenant, case = str(uuid.uuid4()), str(uuid.uuid4())
    pages = [
        ["El contrato de mantenimiento regula el servicio técnico de los equipos de la sede central."],
        ["El servicio técnico del contrato de mantenimiento responde en un plazo de cuatro horas laborables."],
        ["La sede central del contrato alberga los equipos y el servicio de mantenimiento general."],
    ]
    p.ingest(tenant_id=tenant, document_id=str(uuid.uuid4()), content=docx_bytes(pages), source_name="c.docx",
             project_id=case)
    return p, tenant, case


def test_reranking_reorders_by_the_cross_encoder(indexed) -> None:
    p, tenant, _ = indexed
    question = "¿En qué plazo responde el servicio técnico del contrato de mantenimiento?"
    model = Prefers("cuatro horas")
    hits = answering.retrieve(p, tenant_id=tenant, question=question, top_k=3, reranker=model)
    assert "cuatro horas" in hits[0]["text"] and hits[0]["rerank"] == 1.0
    assert all("rerank" in h for h in hits)


def test_reranking_cannot_widen_the_scope(indexed) -> None:
    p, tenant, case = indexed
    other_tenant = str(uuid.uuid4())
    p.ingest(tenant_id=other_tenant, document_id=str(uuid.uuid4()), source_name="ajeno.docx",
             content=docx_bytes([["El servicio técnico del contrato de mantenimiento responde en cuatro horas ajenas."]]))
    model = Prefers("ajenas")
    for kwargs in ({}, {"project_id": case}, {"project_ids": [case]}):
        hits = answering.retrieve(p, tenant_id=tenant, question="plazo del servicio técnico", top_k=5,
                                  reranker=model, **kwargs)
        assert hits and all(h["tenant_id"] == tenant and "ajenas" not in h["text"] for h in hits)
    assert answering.retrieve(p, tenant_id=tenant, question="plazo del servicio técnico", reranker=model,
                              project_ids=[]) == []


def test_a_failing_or_slow_reranker_keeps_the_first_stage_order(indexed) -> None:
    p, tenant, _ = indexed
    question = "¿En qué plazo responde el servicio técnico?"
    baseline = answering.retrieve(p, tenant_id=tenant, question=question, top_k=3, reranker=None)
    for model in (rerank.BoundedReranker(Broken(), 2.0), rerank.BoundedReranker(Slow(), 0.05)):
        started = time.perf_counter()
        hits = answering.retrieve(p, tenant_id=tenant, question=question, top_k=3, reranker=model)
        assert [h["chunk_index"] for h in hits] == [h["chunk_index"] for h in baseline]
        assert time.perf_counter() - started < 0.9  # the timeout, not the slow model, bounds the request


def test_the_reranker_sees_the_masked_question(indexed) -> None:
    p, tenant, _ = indexed
    model = Prefers("plazo")
    answering.retrieve(p, tenant_id=tenant, question="plazo del servicio técnico, escribir a ana@example.com",
                       reranker=model)
    assert model.queries and "ana@example.com" not in model.queries[0]


def test_misconfigured_model_turns_reranking_off_instead_of_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import settings

    monkeypatch.setattr(settings(), "rerank_model", "not-a-real/reranker-model")
    monkeypatch.setattr(rerank, "_instance_key", None)
    assert rerank.configured() is None
    monkeypatch.setattr(settings(), "rerank_model", None)
    assert rerank.configured() is None


def test_bounded_reranker_validates_output() -> None:
    class Short:
        name = "short"

        def score(self, query: str, passages: Sequence[str]) -> list[float]:
            return [1.0]

    with pytest.raises(rerank.RerankerError):
        rerank.BoundedReranker(Short(), 1.0).score("q", ["a", "b"])
    hits: list[dict[str, Any]] = [{"text": "a"}, {"text": "b"}]
    assert rerank.rerank(rerank.BoundedReranker(Short(), 1.0), "q", hits, candidates=5) == hits
