"""Semantic evidence in /ask (ADR-003) and the guard against mixing embedding models.

A tiny deterministic "semantic" embedder maps synonyms to the same axis, so a paraphrase with no
words in common with the document is close in meaning, and an unrelated question is orthogonal.
No model is downloaded.
"""

import math
import uuid
from collections.abc import Sequence

import pytest

from app.ingestion import answering
from app.ingestion.answering import ABSTENTION, CALIBRATED_SIMILARITY, answer
from app.ingestion.docxgen import docx_bytes
from app.ingestion.embeddings import HashingEmbedder
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.vectorstore import EmbeddingModelMismatch, QdrantStore

qdrant_client = pytest.importorskip("qdrant_client")

CONCEPTS = [
    {"precio", "cuesta", "coste", "importe"},
    {"contrato", "contratado", "servicio", "servicios", "acuerdo"},
    {"total", "entero"},
    {"euros", "48.000"},
    {"color", "favorito", "gerente"},
    {"vacaciones", "días", "empleados"},
]


class ConceptEmbedder:
    """Bag of concepts: synonyms share one axis. Semantic in the sense /ask cares about."""

    external = False
    semantic = True

    def __init__(self, name: str = "stub-semantic") -> None:
        self.name = name
        self.dimension = len(CONCEPTS) + 1

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for text in texts:
            words = {w.strip("¿?.,:;").lower() for w in text.split()}
            vec = [float(len(words & c)) for c in CONCEPTS] + [0.01]
            norm = math.sqrt(sum(v * v for v in vec))
            out.append([v / norm for v in vec])
        return out


PAGES = [["CONTRATO DE SERVICIOS", "El precio total del contrato es de 48.000 euros, pagaderos trimestralmente."]]
PARAPHRASE = "¿Qué importe tiene el acuerdo?"  # no word in common with the document sentence


def _pipeline(embedder: object, name: str = "c") -> tuple[IngestionPipeline, str]:
    tenant = str(uuid.uuid4())
    p = IngestionPipeline(QdrantStore(qdrant_client.QdrantClient(":memory:"), name), embedder, max_chars=500, overlap=60)  # type: ignore[arg-type]
    p.ingest(tenant_id=tenant, document_id=str(uuid.uuid4()), content=docx_bytes(PAGES), source_name="contrato.docx")
    return p, tenant


def test_a_paraphrase_without_shared_words_is_answered_with_a_semantic_embedder() -> None:
    pipeline, tenant = _pipeline(ConceptEmbedder())
    assert answer(pipeline, tenant_id=tenant, question=PARAPHRASE, min_similarity=1.01).abstained  # unreachable threshold: abstains
    result = answer(pipeline, tenant_id=tenant, question=PARAPHRASE, min_similarity=0.5)
    assert not result.abstained and "48.000 euros" in result.text
    assert result.citations[0].document == "contrato.docx" and result.citations[0].excerpt in result.text


def test_an_unrelated_question_still_abstains_with_a_semantic_embedder() -> None:
    pipeline, tenant = _pipeline(ConceptEmbedder())
    result = answer(pipeline, tenant_id=tenant, question="¿Cuál es el color favorito del gerente?", min_similarity=0.5)
    assert result.abstained and result.text == ABSTENTION


def test_without_a_calibrated_threshold_the_lexical_rule_alone_applies() -> None:
    """An uncalibrated model (bge-small-en scored unrelated Spanish sentences above 0.5) must not
    be trusted with a similarity threshold measured on another model."""
    pipeline, tenant = _pipeline(ConceptEmbedder("uncalibrated-model"))
    assert "uncalibrated-model" not in CALIBRATED_SIMILARITY
    assert answer(pipeline, tenant_id=tenant, question=PARAPHRASE).abstained


def test_the_calibrated_threshold_is_used_for_its_own_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(answering.CALIBRATED_SIMILARITY, "calibrated-model", 0.5)
    pipeline, tenant = _pipeline(ConceptEmbedder("calibrated-model"))
    assert not answer(pipeline, tenant_id=tenant, question=PARAPHRASE).abstained


def test_a_lexical_embedder_never_takes_the_semantic_path() -> None:
    pipeline, tenant = _pipeline(HashingEmbedder(256))
    assert answer(pipeline, tenant_id=tenant, question=PARAPHRASE, min_similarity=0.0).abstained


def test_indexing_with_another_model_of_the_same_dimension_is_refused() -> None:
    client = qdrant_client.QdrantClient(":memory:")
    first = IngestionPipeline(QdrantStore(client, "shared"), ConceptEmbedder("model-a"), max_chars=500, overlap=60)  # type: ignore[arg-type]
    tenant = str(uuid.uuid4())
    first.ingest(tenant_id=tenant, document_id=str(uuid.uuid4()), content=docx_bytes(PAGES), source_name="a.docx")
    second = IngestionPipeline(QdrantStore(client, "shared"), ConceptEmbedder("model-b"), max_chars=500, overlap=60)  # type: ignore[arg-type]
    with pytest.raises(EmbeddingModelMismatch):
        second.ingest(tenant_id=tenant, document_id=str(uuid.uuid4()), content=docx_bytes(PAGES), source_name="b.docx")
    with pytest.raises(EmbeddingModelMismatch):  # and querying one space against another is refused too
        second.query_vector(tenant_id=tenant, question=PARAPHRASE)


def test_the_api_reports_a_model_mismatch_as_503(client, register, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import vector_index
    from app.core import settings

    acct = register()
    acct.project()  # with no readable project /ask abstains before it ever queries the index

    class Broken:
        embedder = ConceptEmbedder()

        def query_vector(self, **_: object) -> list[float]:
            raise EmbeddingModelMismatch()

    monkeypatch.setattr(settings(), "qdrant_url", "https://qdrant.test")
    monkeypatch.setattr(vector_index, "get_pipeline", lambda: Broken())
    r = client.post(f"/api/v1/ask?organization_id={acct.org}", json={"question": PARAPHRASE}, headers=acct.headers)
    assert (r.status_code, r.json()["detail"]) == (503, "EMBEDDING_MODEL_MISMATCH")
