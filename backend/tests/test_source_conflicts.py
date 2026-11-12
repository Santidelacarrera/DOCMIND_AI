"""Possible disagreements between sources (app.rag.conflicts, ported from ContextRAG_Engine).

The detector is structural: it must flag two near-identical statements whose date or figure differs,
and must not flag complementary facts, different subjects, or one page agreeing with itself.
"""

import uuid
from typing import Any

import pytest

from app.ingestion import answering
from app.ingestion.docxgen import docx_bytes
from app.ingestion.embeddings import HashingEmbedder
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.vectorstore import QdrantStore
from app.rag import conflicts

qdrant_client = pytest.importorskip("qdrant_client")


def hit(text: str, doc: str = "d1", page: int = 1, name: str = "a.pdf") -> dict[str, Any]:
    return {"text": text, "document_id": doc, "page": page, "source_name": name}


def test_different_dates_for_the_same_statement_are_flagged() -> None:
    found = conflicts.detect("¿Cuándo es la audiencia de juicio?", [
        hit("La audiencia de juicio del expediente se celebrará el 5 de mayo de 2026 en la sala dos.", "d1", 2),
        hit("La audiencia de juicio del expediente se celebrará el 9 de junio de 2026 en la sala dos.", "d2", 4, "b.pdf"),
    ])
    assert len(found) == 1 and found[0].kind == "value_mismatch"
    body = found[0].to_dict()
    assert body["needs_human_review"] is True
    assert {(s["document"], s["page"]) for s in body["sources"]} == {("a.pdf", 2), ("b.pdf", 4)}


def test_different_amounts_are_flagged_and_more_detail_is_not() -> None:
    question = "¿Cuál es el precio total del contrato?"
    assert conflicts.detect(question, [
        hit("El precio total del contrato de servicios es de 48.000 euros anuales.", "d1"),
        hit("El precio total del contrato de servicios es de 52.000 euros anuales.", "d2"),
    ])
    # The same figure, one source adding a date: a superset of values is detail, not disagreement.
    assert not conflicts.detect(question, [
        hit("El precio total del contrato de servicios es de 48.000 euros anuales.", "d1"),
        hit("El precio total del contrato de servicios es de 48.000 euros anuales desde 2026.", "d2"),
    ])


def test_complementary_facts_and_other_subjects_are_not_conflicts() -> None:
    assert not conflicts.detect("¿Qué fechas tiene el contrato?", [
        hit("El contrato fue firmado el 5 de mayo de 2025 por ambas partes.", "d1"),
        hit("El contrato vence el 5 de mayo de 2029 salvo renovación expresa.", "d2"),
    ])
    assert not conflicts.detect("¿Cuándo es la audiencia?", [
        hit("La audiencia del expediente Alfa se celebrará el 5 de mayo de 2026.", "d1"),
        hit("La audiencia del expediente Bravo se celebrará el 9 de junio de 2026.", "d2"),
    ])


def test_one_page_does_not_disagree_with_itself() -> None:
    text = ("El plazo de pago es de 30 días desde la factura. "
            "El plazo de pago es de 60 días desde la factura.")
    assert not conflicts.detect("¿Cuál es el plazo de pago?", [hit(text, "d1", 3)])


def test_a_negated_statement_is_flagged() -> None:
    found = conflicts.detect("¿Se permite el teletrabajo?", [
        hit("Los empleados del área técnica pueden realizar teletrabajo dos días por semana.", "d1"),
        hit("Los empleados del área técnica no pueden realizar teletrabajo dos días por semana.", "d2"),
    ])
    assert [c.kind for c in found] == ["negation"]


def test_answers_report_conflicts_with_the_cited_source() -> None:
    store = QdrantStore(qdrant_client.QdrantClient(":memory:"), "conf")
    p = IngestionPipeline(store, HashingEmbedder(256), max_chars=500, overlap=0)
    tenant = str(uuid.uuid4())
    for name, amount in (("contrato_2025.docx", "48.000"), ("anexo_2025.docx", "52.000")):
        p.ingest(tenant_id=tenant, document_id=str(uuid.uuid4()), source_name=name,
                 content=docx_bytes([[f"El precio total del contrato de servicios es de {amount} euros anuales."]]))
    result = answering.answer(p, tenant_id=tenant, question="¿Cuál es el precio total del contrato de servicios?")
    assert not result.abstained and result.conflicts
    sources = {s.document for c in result.conflicts for s in (c.a, c.b)}
    assert sources == {"contrato_2025.docx", "anexo_2025.docx"}
    quiet = answering.answer(p, tenant_id=tenant, question="¿Cuál es el precio total del contrato de servicios?",
                             detect_conflicts=False)
    assert quiet.conflicts == () and quiet.text == result.text


def test_a_heading_run_into_the_sentence_does_not_hide_the_conflict() -> None:
    # PDF text extraction often joins a page heading and the first sentence without punctuation.
    found = conflicts.detect("¿Cuándo es la audiencia de juicio?", [
        hit("ACTA DE REUNIÓN DE MARZO La audiencia de juicio del expediente se celebrará el 5 de mayo de 2026.", "d1"),
        hit("ACTA DE REUNIÓN DE ABRIL La audiencia de juicio del expediente se celebrará el 9 de junio de 2026.", "d2"),
    ])
    assert [c.kind for c in found] == ["value_mismatch"]
    assert conflicts.strip_heading("El contrato con ACME SA vence en mayo.") == "El contrato con ACME SA vence en mayo."
