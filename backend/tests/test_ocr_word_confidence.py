"""Word-level OCR confidence: a value supported by a doubtful OCR word goes to review.

Data shaped like ``pytesseract.image_to_data(..., output_type=DICT)``; no Tesseract needed.
"""

from app.ocr import low_confidence_words, normalise_word
from app.provenance import evidence_for, ocr_word_issues

# Measured on eval_corpus/pdfs/cl_factura_scan_05.pdf (Tesseract 5.5): "Ejemplo" was read as
# "Eiemplo" with confidence 8 on a page whose other words scored 90+.
DATA = {
    "text": ["Ferreteria", "Eiemplo", "Centro", "SpA", "Total:", "$", "2.380.000", "", "©"],
    "conf": [96, 8, 95, 93, 96, 91, 95, -1, 12],
}
PAGE = "Ferreteria Eiemplo Centro SpA\nTotal: $ 2.380.000"


def test_low_confidence_words_are_collected_normalised_and_without_punctuation() -> None:
    assert low_confidence_words(DATA, 0.6) == {"eiemplo"}  # "©" is not a word; -1 is "no word"
    assert low_confidence_words(DATA, 0.0) == set()
    assert normalise_word("“Invoice:") == "invoice"


def test_a_value_resting_on_a_doubtful_word_is_flagged() -> None:
    evidence = evidence_for({"entidad": "Ferreteria Eiemplo Centro SpA", "monto": 2380000}, [PAGE])
    issues = ocr_word_issues(evidence, [low_confidence_words(DATA, 0.6)])
    assert [(i["field"], i["rule"]) for i in issues] == [("entidad", "ocr_low_confidence")]


def test_values_read_with_confidence_are_not_flagged() -> None:
    evidence = evidence_for({"monto": 2380000, "tipo": "Centro SpA"}, [PAGE])
    assert ocr_word_issues(evidence, [low_confidence_words(DATA, 0.6)]) == []


def test_values_not_found_or_on_pages_without_ocr_data_are_ignored_here() -> None:
    evidence = evidence_for({"x": "not in the page", "entidad": "Ferreteria Eiemplo Centro SpA"}, [PAGE])
    assert ocr_word_issues(evidence, []) == []  # digital document: no OCR words at all
