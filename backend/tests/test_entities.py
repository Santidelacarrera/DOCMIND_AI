"""Names, addresses and tenant terms: rule detector, spaCy adapter, configuration, pipeline."""

from pathlib import Path

import pytest

from app.core import Settings
from app.evaluation.pdfgen import digital_pdf
from app.ingestion import entities, pii
from app.ingestion.embeddings import HashingEmbedder
from app.ingestion.entities import (
    DetectorUnavailable,
    RuleConfig,
    RuleEntityDetector,
    SpacyEntityDetector,
    build_detectors,
    load_terms,
)
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.vectorstore import QdrantStore

qdrant_client = pytest.importorskip("qdrant_client")
TENANT = "11111111-1111-4111-8111-111111111111"
DOC = "22222222-2222-4222-8222-222222222222"


def masker(**cfg: object) -> pii.PiiMasker:
    return pii.PiiMasker(extra=[RuleEntityDetector(RuleConfig(**cfg))])  # type: ignore[arg-type]


# (text, secret strings that must NOT survive masking)
MUST_MASK = [
    ("Applicant Name: María José Fernández de la Torre\nCountry: Spain", ["María José", "Fernández", "Torre"]),
    ("Full name: Oluwaseun Adeyemi-Clarke", ["Oluwaseun", "Adeyemi"]),
    ("Nombre: Ana de Armas", ["Ana de Armas"]),
    ("Cliente: Juan Pérez García", ["Juan Pérez"]),
    ("Bill to: Jane Q Public\n", ["Jane"]),
    ("Dr. Smith will review the file", ["Smith"]),
    ("Sra. García confirmó la reunión", ["García"]),
    ("Mr John Doe signed", ["John Doe"]),
    ("This was signed by Maria Lopez on site", ["Maria Lopez"]),
    ("Firmado por Carlos Ruiz Zafón en Madrid", ["Carlos Ruiz"]),
    ("Contact: Priya Raman, Head of Sales", ["Priya Raman"]),
    ("Sede: Calle del Puerto 14, 3º B, 46001 Valencia", ["Puerto 14", "46001"]),
    ("Avenida de la Constitución 22", ["Constitución 22"]),
    ("Ship to 742 Evergreen Terrace, Springfield, IL 62704", ["742 Evergreen", "62704"]),
    ("Office: 10 Downing Street, London", ["10 Downing"]),
    ("Write to P.O. Box 4521", ["4521"]),
    ("Return to SW1A 2AA please", ["SW1A 2AA"]),
    ("Domicilio: Plaza Mayor 1\n28012 Madrid", ["Plaza Mayor 1"]),
    ("Lugar,\n28012 Madrid", ["28012"]),
]
MUST_KEEP = [
    "Bill to: Globex Corporation",
    "Cliente: Hotel Las Palmas S.A.",
    "Customer: Contoso Pharmaceuticals Ltd",
    "Supplier: Kestrel Components B.V.",
    "Order 12345 Items shipped",
    "Total Due: $4,536.00 on 2025-03-14",
    "Invoice Number: INV-2025-0412",
    "company name: acme (lower case, not a label match)",
    "The street was quiet and the avenue was long.",
    "Section 4 describes the Party of the First Part.",
    "Payment terms are thirty days.",
]


@pytest.mark.parametrize(("sample", "secrets"), MUST_MASK)
def test_rules_mask_names_and_addresses(sample: str, secrets: list[str]) -> None:
    result = masker().mask(sample)
    for secret in secrets:
        assert secret not in result.text, (secret, result.text)
    assert result.counts, sample
    assert masker().residual(result.text) == {}


@pytest.mark.parametrize("sample", MUST_KEEP)
def test_rules_leave_organisations_and_ordinary_text_alone(sample: str) -> None:
    result = masker().mask(sample)
    assert result.counts == {}, (sample, result.text)
    assert result.text == pii.PiiMasker().mask(sample).text


def test_measured_recall_and_false_positive_rate_on_the_annotated_set() -> None:
    """Honest numbers for docs/INGESTION.md; thresholds guard against regressions."""
    leaked = sum(any(s in masker().mask(t).text for s in secrets) for t, secrets in MUST_MASK)
    false_positives = sum(bool(masker().mask(t).counts) for t in MUST_KEEP)
    recall = 1 - leaked / len(MUST_MASK)
    assert recall >= 0.95, f"recall {recall:.2f} ({leaked} leaked)"
    assert false_positives == 0, f"{false_positives} false positives"


def test_labels_only_trigger_at_a_line_start_and_names_need_capitals() -> None:
    assert masker().mask("the company name: Alpha Beta").counts == {}
    assert masker().mask("name: lowercase person").counts == {}
    assert masker().mask("Name: Alpha Beta").counts == {"PERSON": 1}


def test_gazetteer_masks_known_terms_case_insensitively_on_word_boundaries() -> None:
    m = masker(terms=["Project Falcon", "Okonkwo"])
    assert m.mask("project falcon budget; okonkwo said so").text == "[TERM_1] budget; [TERM_2] said so"
    assert m.mask("Okonkwoism is not a name").counts == {}  # word boundary


def test_terms_load_from_setting_and_file(tmp_path: Path) -> None:
    file = tmp_path / "terms.txt"
    file.write_text("# customers\nAcme Retail\n\nZed Corp\n", encoding="utf-8")
    assert load_terms("Falcon, Eagle", file) == ["Falcon", "Eagle", "Acme Retail", "Zed Corp"]


def test_names_or_addresses_can_be_switched_off() -> None:
    assert masker(mask_names=False).mask("Name: Maria Lopez, 10 Downing Street").counts == {"ADDRESS": 1}
    assert masker(mask_addresses=False).mask("Name: Maria Lopez, 10 Downing Street").counts == {"PERSON": 1}


def test_custom_labels_extend_the_defaults() -> None:
    rule = RuleEntityDetector(RuleConfig(name_labels=["Referee"]))
    assert pii.PiiMasker(extra=[rule]).mask("Referee: Ines Duarte").counts == {"PERSON": 1}


# ------------------------------------------------------------------------- spaCy
def _spacy_model(name: str) -> bool:
    try:
        import spacy

        spacy.load(name)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _spacy_model("en_core_web_sm"), reason="spaCy en_core_web_sm not installed")
def test_spacy_detector_finds_people_in_prose_but_not_organisations() -> None:
    m = pii.PiiMasker(extra=[SpacyEntityDetector("en_core_web_sm")])
    result = m.mask("Yesterday Angela Merkel and Satya Nadella met in the Microsoft offices.")
    assert "Angela Merkel" not in result.text and "Satya Nadella" not in result.text
    assert "Microsoft" in result.text
    assert result.counts.get("PERSON") == 2


@pytest.mark.skipif(not _spacy_model("en_core_web_sm"), reason="spaCy en_core_web_sm not installed")
def test_spacy_locations_are_opt_in() -> None:
    text = "The delegation flew to Berlin."
    assert "Berlin" in pii.PiiMasker(extra=[SpacyEntityDetector("en_core_web_sm")]).mask(text).text
    assert "Berlin" not in pii.PiiMasker(extra=[SpacyEntityDetector("en_core_web_sm", include_locations=True)]).mask(text).text


@pytest.mark.skipif(not _spacy_model("es_core_news_sm"), reason="spaCy es_core_news_sm not installed")
def test_spacy_spanish_model() -> None:
    m = pii.PiiMasker(extra=[SpacyEntityDetector("es_core_news_sm")])
    assert "Carlos Ruiz" not in m.mask("Ayer el señor Carlos Ruiz visitó la oficina.").text


@pytest.mark.skipif(not _spacy_model("en_core_web_sm"), reason="spaCy en_core_web_sm not installed")
def test_probabilistic_detectors_do_not_take_part_in_residual_verification() -> None:
    class Trigger:
        deterministic = False

        def detect(self, text: str) -> list[pii.Span]:
            i = text.find("[PERSON_1]")
            return [pii.Span(i, i + 10, "PERSON")] if i >= 0 else []

    m = pii.PiiMasker(extra=[RuleEntityDetector(), Trigger()])
    masked = m.mask("Nombre: Ana de Armas").text
    assert masked == "Nombre: [PERSON_1]"
    assert m.residual(masked) == {}  # the ML-style detector's opinion on placeholders is ignored


# ------------------------------------------------------------------------- configuration
def cfg(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def test_default_configuration_uses_the_rule_detector() -> None:
    detectors = build_detectors(cfg())
    assert len(detectors) == 1 and isinstance(detectors[0], RuleEntityDetector)


def test_configured_terms_reach_the_detector() -> None:
    (rule,) = build_detectors(cfg(pii_extra_terms="Falcon,Eagle"))
    assert pii.PiiMasker(extra=[rule]).mask("Falcon and Eagle").counts == {"TERM": 2}


def test_a_detector_that_cannot_start_is_fatal_not_skipped() -> None:
    with pytest.raises(DetectorUnavailable, match="not installed"):
        build_detectors(cfg(pii_detectors="rules,spacy", pii_spacy_model="xx_no_such_model_sm"))
    with pytest.raises(DetectorUnavailable, match="unknown PII detector"):
        build_detectors(cfg(pii_detectors="rules,magic"))
    assert build_detectors(cfg(pii_detectors="")) == []


@pytest.mark.skipif(not _spacy_model("en_core_web_sm"), reason="spaCy en_core_web_sm not installed")
def test_rules_and_spacy_combine_by_configuration() -> None:
    detectors = build_detectors(cfg(pii_detectors="rules,spacy", pii_spacy_model="en_core_web_sm"))
    assert {type(d).__name__ for d in detectors} == {"RuleEntityDetector", "SpacyEntityDetector"}
    out = pii.PiiMasker(extra=detectors).mask("Nombre: Ana Ruiz\nAngela Merkel visited 10 Downing Street.")
    assert "Ana Ruiz" not in out.text and "Merkel" not in out.text and "Downing" not in out.text


# ------------------------------------------------------------------------- end to end
def test_pipeline_masks_names_and_addresses_before_storage() -> None:
    client = qdrant_client.QdrantClient(":memory:")
    store = QdrantStore(client, "ner_chunks")
    pipeline = IngestionPipeline(store, HashingEmbedder(32), masker_extra=[RuleEntityDetector(RuleConfig(terms=["Project Falcon"]))],
                                 max_chars=400, overlap=40)
    report = pipeline.ingest(tenant_id=TENANT, document_id=DOC, content=digital_pdf([[
        "Applicant Name: María José Fernández", "Address: Calle del Puerto 14, 46001 Valencia",
        "Contact: maria@example.org about Project Falcon.", "The pump assembly ships in March.",
    ]]))
    assert {"PERSON", "ADDRESS", "TERM", "EMAIL"} <= set(report.pii)
    points, _ = client.scroll("ner_chunks", limit=100, with_payload=True)
    stored = " ".join(str(p.payload) for p in points)
    for secret in ("María José", "Fernández", "Puerto 14", "46001", "maria@example.org", "Project Falcon"):
        assert secret not in stored, secret
    assert "pump assembly" in stored and "[PERSON_1]" in stored and "[ADDRESS_" in stored
    assert entities.PERSON in stored
