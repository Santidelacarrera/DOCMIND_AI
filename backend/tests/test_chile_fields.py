"""Chilean RUT handling across masking, validation, extraction and evaluation.

All RUTs here are fictitious; their check digits are computed, not looked up.
"""

import pytest

from app import rut
from app.evaluation import metrics as m
from app.evaluation.baseline import RuleBasedProvider, parse_amount, parse_date
from app.evaluation.corpus import DTE_CL_SCHEMA
from app.ingestion import pii
from app.validation import validate_fields


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("12.345.678-5", "12345678-5"),
        ("12345678-5", "12345678-5"),
        ("10.000.013-k", "10000013-K"),
        ("7.654.321-6", "7654321-6"),
        (" 76.123.456-0 ", "76123456-0"),
    ],
)
def test_valid_ruts_normalise_to_one_canonical_form(raw: str, canonical: str) -> None:
    assert rut.normalize(raw) == canonical and rut.is_valid(raw)


@pytest.mark.parametrize("raw", ["12.345.678-4", "7.333.444-5", "12345678", "1-9", "", "abc-k", "1.234.567.890-1"])
def test_wrong_check_digits_and_non_ruts_are_rejected(raw: str) -> None:
    assert rut.normalize(raw) is None and not rut.is_valid(raw)


def test_check_digit_covers_zero_and_k() -> None:
    assert rut.check_digit("76123456") == "0" and rut.check_digit("10000013") == "K"


def test_a_valid_rut_is_masked_and_an_invalid_one_is_left_alone() -> None:
    """Before 2026-11-10 no rule recognised a RUT, so it reached chunks, vectors and citations in clear."""
    masker = pii.PiiMasker(tenant_id="6f1d2c1e-4a0b-4f6e-9c52-0d6b1e7a5c10")
    masked = masker.mask("Cliente: Ana Ejemplo, RUT 12.345.678-5; proveedor 76123456-0.").text
    assert "12.345.678-5" not in masked and "76123456-0" not in masked
    assert pii.placeholder_counts(masked).get("CL_RUT") == 2
    assert "12.345.678-4" in masker.mask("RUT 12.345.678-4 no valido").text


def test_rut_masking_does_not_swallow_amounts_or_dates() -> None:
    masked = pii.PiiMasker(tenant_id="6f1d2c1e-4a0b-4f6e-9c52-0d6b1e7a5c10").mask(
        "Total: $ 1.190.000 al 14-03-2025, folio 4512."
    ).text
    assert "1.190.000" in masked and "14-03-2025" in masked and "CL_RUT" not in masked


def test_a_cl_rut_field_with_a_bad_check_digit_is_flagged_for_review() -> None:
    fields = {"numero_documento": "1", "monto": 10, "rut": "7333444-5"}
    issues = validate_fields(DTE_CL_SCHEMA, fields, {k: 0.99 for k in fields})
    assert [i for i in issues if i["rule"] == "cl_rut" and i["field"] == "rut"]
    fields["rut"] = "7333444-6"
    assert not [i for i in validate_fields(DTE_CL_SCHEMA, fields, {k: 0.99 for k in fields}) if i["rule"] == "cl_rut"]


def test_schemas_without_the_format_are_unaffected() -> None:
    schema = {"type": "object", "properties": {"rut": {"type": "string"}}}
    assert not [i for i in validate_fields(schema, {"rut": "7333444-5"}, {"rut": 0.99}) if i["rule"] == "cl_rut"]


def test_chilean_dates_and_amounts_parse() -> None:
    assert parse_date("14-03-2025") == ("2025-03-14", False)  # numeric: day/month order is a guess
    assert parse_date("5 de mayo de 2025") == ("2025-05-05", True)
    assert parse_amount("$ 1.190.000") == 1190000.0 and parse_amount("$ 25.990") == 25990.0


INVOICE_LINES = (
    "Comercial Andes Ejemplo SpA", "RUT: 76.123.456-0", "FACTURA ELECTRONICA", "Folio: 4512",
    "Fecha de emisión: 14-03-2025", "Señor(es): Constructora Pacífico Prueba Ltda.",
    "RUT receptor: 77.654.321-7", "Total: $ 1.190.000",
)


def test_the_baseline_reads_a_chilean_invoice_and_refuses_an_invalid_rut() -> None:
    text = "\n".join(INVOICE_LINES)
    out = RuleBasedProvider().extract(text, {"properties": {"fields": DTE_CL_SCHEMA}})
    assert out["fields"] == {
        "tipo_documento": "Factura Electrónica", "numero_documento": "4512", "fecha": "2025-03-14",
        "entidad": "Comercial Andes Ejemplo SpA", "nombre": "Constructora Pacífico Prueba Ltda.",
        "rut": "77654321-7", "monto": 1190000.0,
    }
    bad = RuleBasedProvider().extract(text.replace("77.654.321-7", "77.654.321-8"), {"properties": {"fields": DTE_CL_SCHEMA}})
    assert bad["fields"]["rut"] is None and bad["confidence"]["rut"] == 0.0


def test_errors_are_attributed_to_normalization_or_validation_not_extraction() -> None:
    rut_spec = DTE_CL_SCHEMA["properties"]["rut"]
    assert m.semantic_match("12345678-5", "12.345.678-5") and not m.values_match("12345678-5", "12.345.678-5")
    assert m.semantic_match("2025-03-14", "14-03-2025") and m.semantic_match(1190000, "$ 1.190.000")
    assert not m.semantic_match("12345678-5", "12345679-5")
    assert not m.field_valid(rut_spec, "7333444-5") and not m.field_valid(rut_spec, "12.345.678-5")
    assert m.field_valid(rut_spec, "12345678-5") and m.field_valid(rut_spec, None)
