"""RAG quality gate and the sufficient-evidence rule behind /ask.

The gate floors are the measured baseline of ``python -m app.evaluation.rag`` (hashing embedder,
see docs/evaluation/rag-report.md) minus no slack on the safety metrics: zero cross-tenant leaks,
every unanswerable or foreign question abstains, every quoted excerpt is in the chunk it cites.
"""

import uuid
from typing import Any

import pytest

from app.evaluation import rag
from app.ingestion.answering import ABSTENTION, answer, retrieve
from app.ingestion.docxgen import docx_bytes
from app.ingestion.embeddings import HashingEmbedder
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.vectorstore import QdrantStore

pytest.importorskip("qdrant_client")


@pytest.fixture(scope="module")
def report() -> dict:
    return rag.run()


def test_rag_safety_metrics_have_no_slack(report: dict) -> None:
    m = report["metrics"]
    assert m["cross_tenant_leaks"] == 0
    assert m["abstention_accuracy"] == 1.0
    assert m["faithfulness"] == 1.0
    assert m["citation_precision"] == 1.0


def test_rag_quality_does_not_regress_below_the_baseline(report: dict) -> None:
    m = report["metrics"]
    assert m["recall@5"] >= 0.95
    assert m["mrr"] >= 0.9
    assert m["citation_accuracy"] >= 0.85
    assert m["false_abstention_rate"] <= 0.15
    assert report["by_category"]["factual"]["correct"] == report["by_category"]["factual"]["questions"]


def test_every_corpus_question_has_a_well_formed_ground_truth() -> None:
    corpus = rag.load()
    files = {(d["tenant"], d["file"]): len(d["pages"]) for d in corpus["documents"]}
    for q in corpus["questions"]:
        assert q["category"] in rag.ANSWERABLE | rag.MUST_ABSTAIN
        if q["category"] in rag.ANSWERABLE:
            assert q["relevant"] and q["contains"]
            for doc, page in q["relevant"]:
                assert 1 <= page <= files[(q["tenant"], doc)]
        else:
            assert q["relevant"] == []


def _one_doc_pipeline(pages: list[list[str]]) -> tuple[IngestionPipeline, str]:
    from qdrant_client import QdrantClient

    tenant = str(uuid.uuid4())
    pipeline = IngestionPipeline(QdrantStore(QdrantClient(":memory:"), "t"), HashingEmbedder(256), max_chars=500, overlap=60)
    pipeline.ingest(tenant_id=tenant, document_id=str(uuid.uuid4()), content=docx_bytes(pages), source_name="codigo.docx")
    return pipeline, tenant


PAGES = [["CÓDIGO DE CONDUCTA", "Los regalos de proveedores con un valor superior a 50 euros deben declararse al comité de ética."]]


def test_a_sentence_sharing_only_a_generic_word_is_not_evidence() -> None:
    """Regression: 'deben' alone used to be enough to quote an unrelated sentence as the answer."""
    pipeline, tenant = _one_doc_pipeline(PAGES)
    question = "¿Cada cuántos días deben cambiarse las contraseñas?"
    assert retrieve(pipeline, tenant_id=tenant, question=question), "the chunk is retrieved..."
    result = answer(pipeline, tenant_id=tenant, question=question)
    assert result.abstained and result.text == ABSTENTION and result.citations == ()  # ...but is not evidence


def test_min_coverage_zero_restores_the_any_shared_term_behaviour() -> None:
    pipeline, tenant = _one_doc_pipeline(PAGES)
    result = answer(pipeline, tenant_id=tenant, question="¿Cada cuántos días deben cambiarse las contraseñas?", min_coverage=0)
    assert not result.abstained


def test_a_well_supported_question_is_answered_with_its_fragment() -> None:
    pipeline, tenant = _one_doc_pipeline(PAGES)
    result = answer(pipeline, tenant_id=tenant, question="¿A partir de qué valor deben declararse los regalos de proveedores?")
    assert not result.abstained and "50 euros" in result.text
    cite = result.citations[0]
    assert (cite.document, cite.page, cite.chunk_index) == ("codigo.docx", 1, 0)
    assert cite.excerpt in result.text


def test_the_api_reads_the_threshold_from_settings() -> None:
    from app.core import Settings

    assert Settings(_env_file=None).ask_min_evidence_coverage == 0.5
    with pytest.raises(ValueError):
        Settings(_env_file=None, ask_min_evidence_coverage=1.5)


def test_a_corpus_of_real_files_can_be_evaluated(tmp_path) -> None:
    """docs/REAL_DOCUMENT_EVALUATION.md: documents given by path, questions as in the bundled corpora."""
    import json

    from app.evaluation import rag
    from app.evaluation.pdfgen import digital_pdf

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "poliza.pdf").write_bytes(
        digital_pdf([["CONDICIONES", "La póliza cubre daños por agua hasta 3.000 euros por siniestro."]]))
    corpus = {
        "tenants": {"real": "Cliente piloto (anonimizado)", "otro": "Otra organización"},
        "documents": [{"tenant": "real", "file": "poliza.pdf", "path": "docs/poliza.pdf"}],
        "questions": [
            {"tenant": "real", "category": "factual", "question": "¿Hasta qué importe cubre la póliza los daños por agua?",
             "relevant": [["poliza.pdf", 1]], "contains": "3.000 euros"},
            {"tenant": "otro", "category": "cross_tenant", "question": "¿Hasta qué importe cubre la póliza los daños por agua?",
             "relevant": [], "contains": ""},
        ],
    }
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf-8")
    report = rag.run(corpus_path=path)
    assert report["metrics"]["fact_accuracy"] == 1.0 and report["metrics"]["cross_tenant_leaks"] == 0
    assert rag.main(["--corpus", str(path), "--min-abstention-accuracy", "1.0"]) == 0


def _validator() -> Any:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "validate_eval_corpus.py"
    spec = importlib.util.spec_from_file_location("validate_eval_corpus", path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_the_corpus_validator_catches_mistakes_before_an_evaluation(tmp_path) -> None:
    """scripts/validate_eval_corpus.py on a synthetic corpus (a technical check, not a quality evaluation)."""
    import json

    from app.evaluation.pdfgen import digital_pdf

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.pdf").write_bytes(digital_pdf([["Portada"], ["El plazo de entrega es de 30 dias."]]))
    good = {
        "tenants": {"t1": "uno", "t2": "dos"},
        "documents": [{"tenant": "t1", "file": "a.pdf", "path": "docs/a.pdf"}],
        "questions": [
            {"tenant": "t1", "category": "factual", "question": "¿Plazo de entrega?", "relevant": [["a.pdf", 2]],
             "contains": "30 dias"},
            {"tenant": "t2", "category": "cross_tenant", "question": "¿Plazo de entrega?", "relevant": [], "contains": ""},
        ],
    }
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(good), encoding="utf-8")
    errors, warnings, summary = _validator().validate(path)
    assert errors == [] and summary["documents"] == 1 and summary["pages"] == 2
    assert any("30–50" in w for w in warnings)  # size target is reported, not hidden

    bad = json.loads(json.dumps(good))
    bad["questions"][0]["relevant"] = [["a.pdf", 7]]
    bad["questions"].append({"tenant": "t1", "category": "factual", "question": "¿Otra?", "relevant": [["a.pdf", 2]],
                             "contains": "45 dias"})
    bad["documents"].append({"tenant": "t1", "file": "missing.pdf", "path": "docs/missing.pdf"})
    path.write_text(json.dumps(bad), encoding="utf-8")
    errors, warnings, _ = _validator().validate(path)
    assert any("not page 7" in e for e in errors) and any("file not found" in e for e in errors)
    assert any("'contains' does not appear" in w for w in warnings)
    assert _validator().main([str(path)]) == 1
