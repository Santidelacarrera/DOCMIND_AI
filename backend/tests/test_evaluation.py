"""Extraction quality: metric definitions, corpus integrity, and a reproducible regression gate.

The gate runs the real pipeline over the reference corpus with the offline baseline
extractor and fails if quality drops below ``eval_corpus/baseline_expected.json``.
"""

import json
import shutil
from pathlib import Path

import pytest

from app import worker
from app.evaluation import corpus, metrics, runner
from app.evaluation.baseline import RuleBasedProvider, parse_amount, parse_date
from app.processing import extraction_envelope

EXPECTED = corpus.CORPUS_DIR / "baseline_expected.json"


# ------------------------------------------------------------------------- metric definitions
def test_value_matching_ignores_formatting_but_not_content() -> None:
    assert metrics.values_match("ACME Inc.", " acme  inc ")
    assert metrics.values_match(4536, 4536.004) and not metrics.values_match(4536, 4536.1)
    assert metrics.values_match(None, "") and not metrics.values_match("0", None)
    assert metrics.values_match("María", "maría") and not metrics.values_match("INV-1", "INV-7")


def test_field_scoring_counts_hits_misses_and_hallucinations() -> None:
    assert metrics.score_field("a", "a") == metrics.Counts(tp=1)
    assert metrics.score_field("a", None) == metrics.Counts(fn=1)  # missed
    assert metrics.score_field("a", "b") == metrics.Counts(fp=1, fn=1)  # wrong = both
    assert metrics.score_field(None, "x") == metrics.Counts(fp=1)  # hallucinated
    assert metrics.score_field(None, None) == metrics.Counts()  # correctly empty
    doc = metrics.score_document({"a": 1}, {"a": 1, "extra": "y"})
    assert doc["extra"] == metrics.Counts(fp=1)


def test_precision_recall_f1_are_computed_per_field_and_overall() -> None:
    docs = [
        {"a": metrics.Counts(tp=1), "b": metrics.Counts(fn=1)},
        {"a": metrics.Counts(fp=1, fn=1), "b": metrics.Counts(tp=1)},
    ]
    result = metrics.aggregate(docs)
    assert result["micro"] == {"tp": 2, "fp": 1, "fn": 2, "precision": 0.6667, "recall": 0.5, "f1": 0.5714}
    assert result["per_field"]["a"]["precision"] == 0.5 and result["per_field"]["b"]["recall"] == 0.5


def test_ocr_error_rates() -> None:
    assert metrics.cer("Invoice 12345", "Invoice 12345") == 0
    assert metrics.cer("abcd", "abxd") == 0.25
    assert metrics.wer("total due 100", "total due 1OO") == pytest.approx(1 / 3)
    assert metrics.cer("a  b\n c", "a b c") == 0  # whitespace-insensitive
    assert metrics.edit_distance("kitten", "sitting") == 3


def test_calibration_and_review_routing() -> None:
    o = metrics.Observation
    observations = [
        o("d", "f1", True, 0.95, False), o("d", "f2", True, 0.92, False),
        o("d", "f3", False, 0.4, True), o("d", "f4", False, 0.9, False),  # one silent error
    ]
    routing = metrics.review_routing(observations)
    assert routing["raw_accuracy"] == 0.5 and routing["error_capture_rate"] == 0.5
    assert routing["review_load"] == 0.25 and routing["auto_accept_accuracy"] == pytest.approx(0.6667, abs=1e-4)
    assert routing["silent_errors"] == ["d:f4"]
    cal = metrics.calibration(observations)
    assert cal["n"] == 4 and cal["bins"][0]["accuracy"] == 0.0 and cal["ece"] is not None
    assert metrics.percentile([1, 2, 3, 4, 5], 0.5) == 3


# ------------------------------------------------------------------------- baseline parsing
def test_baseline_parsers_handle_regional_formats() -> None:
    assert parse_amount("1.089,00 €") == 1089.00 and parse_amount("$4,536.00") == 4536.00
    assert parse_amount("EUR 2.310,00") == 2310.00 and parse_amount("425,500.00.") == 425500.00
    assert parse_date("2025-03-14") == ("2025-03-14", True)
    assert parse_date("3 March 2025") == ("2025-03-03", True)
    assert parse_date("03/04/2025") == ("2025-04-03", False)  # DD/MM guessed => lower confidence
    assert parse_date("31/02/2025")[0] is None and parse_date("soon") == (None, False)


# ------------------------------------------------------------------------- corpus integrity
def test_manifest_matches_the_generator_and_covers_every_case() -> None:
    manifest = corpus.load()
    assert [d["id"] for d in manifest["documents"]] == [s.id for s in corpus.SPECS]
    kinds = {d["kind"] for d in manifest["documents"]}
    assert kinds == {corpus.DIGITAL, corpus.SCANNED, corpus.MULTIPAGE, corpus.MALFORMED, corpus.ADVERSARIAL}
    for entry in manifest["documents"]:
        assert (corpus.CORPUS_DIR / entry["file"]).exists(), entry["id"]
        if entry["kind"] == corpus.MALFORMED:
            assert entry["expect_reject"] and not entry["truth"]
        else:
            schema = manifest["schemas"][entry["doc_type"]]["properties"]
            assert set(entry["truth"]) == set(schema), entry["id"]  # every schema field is annotated


def test_digital_documents_regenerate_byte_for_byte(tmp_path: Path) -> None:
    corpus.build(tmp_path, only_digital=True)
    for spec in corpus.SPECS:
        if spec.render == "scanned":
            continue
        assert (tmp_path / "pdfs" / f"{spec.id}.pdf").read_bytes() == (
            corpus.CORPUS_DIR / "pdfs" / f"{spec.id}.pdf"
        ).read_bytes(), spec.id
    assert (tmp_path / "manifest.json").read_text() == (corpus.CORPUS_DIR / "manifest.json").read_text()


def test_the_baseline_is_deterministic() -> None:
    spec = next(s for s in corpus.SPECS if s.id == "inv_digital_01")
    text = "\n".join(corpus.page_text(spec))
    envelope = extraction_envelope(corpus.SCHEMAS["invoice"])
    first = RuleBasedProvider().extract(text, envelope)
    assert first == RuleBasedProvider().extract(text, envelope)
    assert first["fields"]["total_amount"] == 4536.0


# ------------------------------------------------------------------------- regression gate
@pytest.fixture(scope="module")
def report():  # type: ignore[no-untyped-def]
    """One full run of the corpus through API + worker (module-scoped: it takes a few seconds)."""
    import os
    import tempfile

    from fastapi.testclient import TestClient

    from app import main
    from app.db import Base, engine

    mp = pytest.MonkeyPatch()
    mp.setattr(worker, "llm", worker.llm)  # restored on teardown, whatever the runner does
    mp.setattr(main.process_document, "delay", lambda job_id: None)
    mp.setattr(worker.dispatch_webhooks, "delay", lambda *a, **k: None)
    from app import rate_limit

    class _Ok:
        @classmethod
        def from_url(cls, *_a: object, **_k: object) -> "_Ok":
            return cls()

        def pipeline(self, transaction: bool = True) -> "_Ok":
            return self

        def incr(self, key: str) -> None: ...
        def expire(self, *a: object, **k: object) -> None: ...
        def execute(self) -> list[int]:
            return [1]

    mp.setattr(rate_limit, "Redis", _Ok)
    mp.setattr(worker, "TesseractProvider", __import__("app.ocr", fromlist=["x"]).TesseractProvider)
    mp.setenv("PYTHONHASHSEED", "0")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    del os, tempfile
    try:
        with TestClient(main.app) as client:
            yield runner.run_evaluation(client, RuleBasedProvider(), provider_name="baseline-rules")
    finally:
        mp.undo()


needs_ocr_stack = pytest.mark.skipif(
    shutil.which("tesseract") is None or shutil.which("pdftoppm") is None,
    reason="Tesseract and Poppler are required to evaluate scanned documents",
)


@needs_ocr_stack
def test_quality_does_not_regress_below_the_recorded_baseline(report) -> None:
    expected = json.loads(EXPECTED.read_text())
    got = report["extraction"]
    # Exactly reproducible: no OCR involved.
    for kind in ("digital", "multipage", "adversarial"):
        assert got["by_kind"][kind] == expected["by_kind"][kind], kind
    # OCR-dependent numbers get a tolerance for Tesseract version differences.
    floor = expected["floors"]
    assert got["micro"]["f1"] >= floor["micro_f1"]
    assert got["micro"]["precision"] >= floor["micro_precision"]
    assert report["review_routing"]["error_capture_rate"] >= floor["error_capture_rate"]
    assert report["review_routing"]["auto_accept_accuracy"] >= floor["auto_accept_accuracy"]
    assert len(report["review_routing"]["silent_errors"]) <= floor["max_silent_errors"]
    assert report["rejections"]["correct"] == report["rejections"]["total"] == expected["rejections_total"]
    assert report["text_quality"]["ocr_mean_cer"] <= floor["max_ocr_mean_cer"]


@needs_ocr_stack
def test_report_is_complete_and_the_review_pass_improves_accuracy(report) -> None:
    for key in ("extraction", "error_attribution", "text_quality", "confidence", "review_routing",
                "rejections", "prompt_injection", "timing", "cost", "review_workflow"):
        assert key in report, key
    wf = report["review_workflow"]
    assert wf["accuracy_after_review"] >= wf["accuracy_before_review"]
    assert wf["fields_corrected"] > 0 and wf["reasons"]
    assert all(c["reason"] for c in wf["corrections"])
    assert report["prompt_injection"][0]["unexpected_fields"] == []
    assert report["prompt_injection"][0]["values_unchanged"] is True
    markdown = runner.to_markdown(runner.strip_internal(report))
    assert "## Review, correction and export" in markdown and "ocr_misread" in markdown


@needs_ocr_stack
def test_lowres_scan_is_caught_by_ocr_confidence_not_left_to_chance(report) -> None:
    by_id = {d["id"]: d for d in report["extraction"]["documents"]}
    assert by_id["scan_invoice_lowres"]["requires_review"] is True
    assert by_id["scan_invoice_clean"]["requires_review"] is False
    cer = {d["id"]: d["cer"] for d in report["text_quality"]["documents"]}
    assert cer["scan_invoice_clean"] < 0.02 < cer["scan_invoice_lowres"]
