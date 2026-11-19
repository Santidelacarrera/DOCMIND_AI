"""Tests for multi-step RAG query planning, multi-query fusion, and grounded reasoning verification."""

from __future__ import annotations

import uuid

from app.evaluation.pdfgen import digital_pdf
from app.ingestion.answering import answer
from app.ingestion.embeddings import HashingEmbedder
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.vectorstore import QdrantStore
from app.rag.generation import REASONING_SYSTEM_PROMPT, ExtractiveGenerator, generate_verified
from app.rag.reasoning import merge_multi_query_hits, plan_queries
from app.rag.verification import verify_grounded


class _FakeLLM:
    name = "gemini"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        if self._responses:
            return self._responses.pop(0)
        return '{"queries": ["fallback"]}'


def test_plan_queries_decomposes_and_resolves_memory() -> None:
    llm = _FakeLLM(['{"queries": ["penalidad contrato Alfa", "penalidad contrato Beta"]}'])
    planned = plan_queries(
        "¿Y cuál tiene mayor penalidad entre esos dos?",
        llm,
        memory="user: Háblame del contrato Alfa y Beta.\nassistant: Ambos tienen cláusulas de multa [1][2].",
    )
    assert planned == ["penalidad contrato Alfa", "penalidad contrato Beta"]
    assert "<memory>" in llm.calls[0][1]


def test_plan_queries_falls_back_on_extractive_or_bad_json() -> None:
    assert plan_queries("plazo de pago", ExtractiveGenerator()) == ["plazo de pago"]
    bad_llm = _FakeLLM(["not json at all"])
    assert plan_queries("plazo de pago", bad_llm) == ["plazo de pago"]


def test_merge_multi_query_hits_deduplicates_and_fuses() -> None:
    h1 = {"document_id": "d1", "chunk_index": 0, "score": 0.9, "text": "Contrato A"}
    h2 = {"document_id": "d2", "chunk_index": 0, "score": 0.85, "text": "Contrato B"}
    h1_dup = {"document_id": "d1", "chunk_index": 0, "score": 0.7, "text": "Contrato A"}
    merged = merge_multi_query_hits([[h1, h2], [h1_dup]], limit=5)
    assert len(merged) == 2
    assert merged[0]["document_id"] == "d1"


def test_verify_grounded_allows_cross_passage_synthesis_but_blocks_invented_numbers() -> None:
    passages = [
        "El Contrato Alfa establece una renta mensual de 1200 USD con preaviso de 30 días.",
        "El Contrato Beta fija una renta mensual de 1500 USD y un preaviso de 60 días.",
    ]
    answer_text = (
        "Alfa tiene renta mensual de 1200 USD y Beta de 1500 USD, mientras el preaviso sube de 30 a 60 días [1][2]. "
        "Además el depósito de garantía es de 9999 USD [1]. "
        "Esta frase no tiene cita."
    )
    verdicts = verify_grounded(answer_text, passages)
    assert len(verdicts) == 3
    assert verdicts[0].supported is True
    assert verdicts[0].citations == (1, 2)
    assert verdicts[1].supported is False
    assert "9999" in verdicts[1].reason
    assert verdicts[2].supported is False
    assert verdicts[2].reason == "no citation"


def test_generate_verified_uses_reasoning_prompt_for_llm() -> None:
    llm = _FakeLLM([
        "El contrato Alfa cuesta 1200 USD al mes y el contrato Beta cuesta 1500 USD al mes [1][2]."
    ])
    hits = [
        {"document_id": "d1", "source_name": "alfa.pdf", "page": 1, "score": 0.9,
         "text": "Contrato Alfa: renta mensual de 1200 USD."},
        {"document_id": "d2", "source_name": "beta.pdf", "page": 2, "score": 0.88,
         "text": "Contrato Beta: renta mensual de 1500 USD."},
    ]
    res = generate_verified("Compara la renta de Alfa y Beta", hits, llm, max_chars=4000, threshold=0.95)
    assert llm.calls[0][0] == REASONING_SYSTEM_PROMPT
    assert "[1][2]" in res.text
    assert res.dropped == 0


def test_end_to_end_generative_reasoning_in_answer() -> None:
    from qdrant_client import QdrantClient

    store = QdrantStore(QdrantClient(":memory:"), f"test-{uuid.uuid4().hex[:8]}")
    pipeline = IngestionPipeline(store, HashingEmbedder(64), max_chars=400, overlap=40)
    org = str(uuid.uuid4())
    proj = str(uuid.uuid4())
    pipeline.ingest(
        tenant_id=org,
        document_id=str(uuid.uuid4()),
        content=digital_pdf([["Contrato Alfa establece renta mensual de 1200 USD y multa de 10%."]]),
        source_name="alfa.pdf",
        project_id=proj,
    )
    pipeline.ingest(
        tenant_id=org,
        document_id=str(uuid.uuid4()),
        content=digital_pdf([["Contrato Beta establece renta mensual de 1500 USD y multa de 15%."]]),
        source_name="beta.pdf",
        project_id=proj,
    )
    llm = _FakeLLM([
        '{"queries": ["Contrato Alfa renta multa", "Contrato Beta renta multa"]}',
        "El Contrato Alfa fija renta de 1200 USD con multa de 10% y Beta fija 1500 USD con multa de 15% [1][2].",
    ])
    ans = answer(
        pipeline,
        tenant_id=org,
        project_id=proj,
        question="Compara renta y multa entre Contrato Alfa y Contrato Beta",
        answer_mode="generative",
        generator=llm,
        top_k=6,
    )
    assert ans.abstained is False
    assert ans.mode == "generative"
    assert len(ans.citations) == 2
    assert "1200" in ans.text and "1500" in ans.text


def test_order_hits_by_document_and_fetch_neighbors() -> None:
    from qdrant_client import QdrantClient

    from app.rag.reasoning import order_hits_by_document

    store = QdrantStore(QdrantClient(":memory:"), f"test-nb-{uuid.uuid4().hex[:8]}")
    pipeline = IngestionPipeline(store, HashingEmbedder(64), max_chars=220, overlap=0)
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    proj_a = str(uuid.uuid4())
    doc_a = str(uuid.uuid4())
    pipeline.ingest(
        tenant_id=org_a,
        document_id=doc_a,
        content=digital_pdf([[
            "Clausula primera sobre vigencia y condiciones generales del contrato de servicios profesionales " * 2,
            "Clausula segunda sobre penalidad de 500 USD por retraso en la entrega de los entregables pactados " * 2,
            "Clausula tercera sobre jurisdiccion aplicable y arbitraje comercial en Santiago de Chile " * 2,
        ]]),
        source_name="contrato.pdf",
        project_id=proj_a,
    )
    pipeline.ingest(
        tenant_id=org_b,
        document_id=str(uuid.uuid4()),
        content=digital_pdf([["Documento confidencial de otro tenant."]]),
        source_name="other.pdf",
        project_id=str(uuid.uuid4()),
    )
    seed = [{"document_id": doc_a, "chunk_index": 1, "page": 1, "score": 0.9}]
    neighbors = store.fetch_neighbors(org_a, seed, radius=1, project_id=proj_a)
    indices = sorted(int(n["chunk_index"]) for n in neighbors)
    assert 0 in indices and 1 in indices
    assert all(n["tenant_id"] == org_a for n in neighbors)
    assert store.fetch_neighbors(org_b, seed, radius=1) == []

    unordered = [
        {"document_id": "d1", "page": 3, "chunk_index": 5},
        {"document_id": "d2", "page": 1, "chunk_index": 0},
        {"document_id": "d1", "page": 1, "chunk_index": 0},
    ]
    ordered = order_hits_by_document(unordered)
    assert [(h["document_id"], h["page"]) for h in ordered] == [("d1", 1), ("d1", 3), ("d2", 1)]


def test_extract_pages_windowed_recovers_fields_from_late_pages() -> None:
    from app.processing import extract_pages_windowed

    class _WindowProvider:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def extract(self, text: str, schema: dict[str, object], prompt_instructions: str | None = None) -> dict[str, object]:
            self.prompts.append(text)
            if "[Page 3]" in text:
                return {
                    "fields": {"issuer": "Acme", "total": 9500},
                    "confidence": {"issuer": 0.9, "total": 0.95},
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                }
            return {
                "fields": {"issuer": "Acme", "total": None},
                "confidence": {"issuer": 0.9, "total": 0.0},
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            }

    provider = _WindowProvider()
    pages = ["Pagina uno " * 8, "Pagina dos " * 8, "Pagina tres total 9500"]
    res = extract_pages_windowed(provider, pages, {"type": "object"}, max_chars=120)
    assert len(provider.prompts) >= 2
    assert res["fields"]["total"] == 9500
    assert res["usage"]["prompt_tokens"] >= 40
