"""Business-rule validation layer applied to LLM extractions before they are
considered final.

Two independent checks run after every extraction:

1. JSON Schema conformance — the extracted ``fields`` object is validated against
   the tenant's own schema (type, required properties, enums, numeric bounds...).
2. Confidence thresholding — any field whose confidence score falls below
   ``CONFIDENCE_REVIEW_THRESHOLD`` is flagged for human review, independent of
   whether it is otherwise schema-valid.

Neither check ever raises: a run with issues is still persisted (so nothing is
silently dropped), but it is marked ``requires_review=True`` so the UI and API
consumers can route it for manual sign-off instead of trusting it blindly.
"""

import math
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from app.core import settings


def validate_fields(
    json_schema: dict[str, Any] | None,
    fields: dict[str, Any],
    confidence: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Return a list of validation issues; an empty list means the run is clean.

    ``json_schema`` is the tenant's field-level schema (as stored on
    ``ExtractionSchemaVersion.json_schema``), not the outer ``{"fields": ...}``
    envelope used for the LLM call.
    """
    issues: list[dict[str, Any]] = []
    issues.extend(_schema_issues(json_schema, fields))
    issues.extend(_confidence_issues(fields, confidence or {}))
    return issues


def _schema_issues(json_schema: dict[str, Any] | None, fields: dict[str, Any]) -> list[dict[str, Any]]:
    if not json_schema:
        return []
    try:
        validator = Draft202012Validator(json_schema)
    except Exception:
        # A malformed tenant schema should not crash processing; surface it instead.
        return [{"field": None, "rule": "schema", "message": "SCHEMA_DEFINITION_INVALID"}]
    issues: list[dict[str, Any]] = []
    error: ValidationError
    for error in sorted(validator.iter_errors(fields), key=lambda e: list(e.path)):
        field_name = ".".join(str(p) for p in error.path) or None
        issues.append(
            {
                "field": field_name,
                "rule": "schema",
                "message": error.message[:500],
            }
        )
    return issues


def _confidence_issues(
    fields: dict[str, Any], confidence: dict[str, float]
) -> list[dict[str, Any]]:
    threshold = settings().confidence_review_threshold
    issues: list[dict[str, Any]] = []
    for name in fields:
        score = confidence.get(name)
        if score is None:
            continue
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        if score < threshold:
            issues.append(
                {
                    "field": name,
                    "rule": "low_confidence",
                    "message": f"Confidence {score:.2f} is below the {threshold:.2f} review threshold.",
                }
            )
    return issues


def clamp_confidence(value: Any) -> float | None:
    """Coerce an arbitrary LLM-reported confidence value into [0, 1] or None."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(score):
        return None
    return max(0.0, min(1.0, score))
