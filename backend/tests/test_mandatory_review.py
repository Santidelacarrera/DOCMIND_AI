"""Contract fields the model is confidently wrong on are always sent to a person."""

import pytest

from app.core import settings
from app.validation import validate_fields

SCHEMA = {
    "type": "object",
    "properties": {"governing_law": {"type": ["string", "null"]}, "counterparty": {"type": ["string", "null"]}, "term_months": {"type": ["integer", "null"]}},
}


def _rules(issues, field):
    return {i["rule"] for i in issues if i["field"] == field}


def test_mandatory_fields_are_flagged_even_at_full_confidence() -> None:
    fields = {"governing_law": "State of New York", "counterparty": "Acme", "term_months": 24}
    issues = validate_fields(SCHEMA, fields, {"governing_law": 1.0, "counterparty": 1.0, "term_months": 1.0})
    assert "mandatory_review" in _rules(issues, "governing_law")
    assert "mandatory_review" in _rules(issues, "counterparty")
    assert _rules(issues, "term_months") == set()


def test_an_empty_mandatory_field_is_not_flagged() -> None:
    issues = validate_fields(SCHEMA, {"governing_law": None}, {"governing_law": 1.0})
    assert "mandatory_review" not in _rules(issues, "governing_law")


def test_the_list_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings(), "always_review_fields", "term_months")
    issues = validate_fields(SCHEMA, {"governing_law": "X", "term_months": 12}, {"governing_law": 1.0, "term_months": 1.0})
    assert "mandatory_review" in _rules(issues, "term_months")
    assert "mandatory_review" not in _rules(issues, "governing_law")
