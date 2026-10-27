"""Evidence for extracted values: where in the document does the value actually appear?

A language model can return a value that is not in the document (a hallucination) or a
normalised one (``2026-04-03`` for ``3 April 2026``). This module checks the value against
the text the pipeline really extracted, page by page, and records what it found:

* ``exact``          -- the value occurs as whole words (case-insensitive, whitespace-tolerant);
* ``normalized``     -- a number or date occurs in a different notation (``1.234,50`` for 1234.5);
* ``not_found``      -- nothing in the text supports the value;
* ``not_applicable`` -- booleans, nulls, lists and objects are not located.

Only deterministic matching is used, so the cited page and snippet are always real text from the
document, never generated. Two kinds of match are weaker than they look and are flagged, so they go
to human review instead of being presented as support:

* short integers and years (``1``, ``15``, ``2026``) occur in almost any document; a match is ``weak``;
* ambiguous day/month dates are reported as ``ambiguous_date``.

A located value is *evidence*, not proof of correctness: the page may contain several candidates.
"""

import dataclasses
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any

EXACT = "exact"
NORMALIZED = "normalized"
NOT_FOUND = "not_found"
NOT_APPLICABLE = "not_applicable"

SNIPPET_RADIUS = 60

_MONTHS = {
    "jan": 1,
    "january": 1,
    "ene": 1,
    "enero": 1,
    "feb": 2,
    "february": 2,
    "febrero": 2,
    "mar": 3,
    "march": 3,
    "marzo": 3,
    "apr": 4,
    "april": 4,
    "abr": 4,
    "abril": 4,
    "may": 5,
    "mayo": 5,
    "jun": 6,
    "june": 6,
    "junio": 6,
    "jul": 7,
    "july": 7,
    "julio": 7,
    "aug": 8,
    "august": 8,
    "ago": 8,
    "agosto": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "septiembre": 9,
    "oct": 10,
    "october": 10,
    "octubre": 10,
    "nov": 11,
    "november": 11,
    "noviembre": 11,
    "dec": 12,
    "december": 12,
    "dic": 12,
    "diciembre": 12,
}
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})(?!\d)")
_TEXT_DATE = re.compile(
    r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s+(?:de\s+)?([A-Za-zÀ-ÿ]{3,10})\.?,?\s+(?:de\s+)?(\d{4})"
)
_MONTH_FIRST = re.compile(r"([A-Za-zÀ-ÿ]{3,10})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})")
# Symbols that name one currency. "$" is deliberately absent: it is USD, CAD, AUD, MXN...
_CURRENCY_SYMBOLS = {"EUR": "€", "GBP": "£", "JPY": "¥"}
# A number token: thousands groups need exactly three digits after the separator, so "1 2024" is two
# numbers, not one; a plain run of digits with an optional decimal part is the other alternative.
_NUMBER = re.compile(r"\d{1,3}(?:[.,'  ]\d{3}(?!\d))+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")


@dataclass(frozen=True)
class Evidence:
    status: str
    page: int | None = None
    snippet: str | None = None
    ambiguous_date: bool = False
    weak: bool = False  # a short integer or a year: present in almost any document


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _snippet(page_text: str, start: int, end: int) -> str:
    left = max(0, start - SNIPPET_RADIUS)
    right = min(len(page_text), end + SNIPPET_RADIUS)
    return " ".join(page_text[left:right].split())


def _parse_number(token: str) -> float | None:
    """Locale-agnostic: the last separator is the decimal point unless exactly three digits
    follow it (then it groups thousands)."""
    token = token.strip().replace(" ", "").replace(" ", "").replace("'", "")
    if not token:
        return None
    last = max(token.rfind(","), token.rfind("."))
    if last == -1:
        return float(token) if token.isdigit() else None
    tail = token[last + 1 :]
    head = token[:last].replace(",", "").replace(".", "")
    if not (head.isdigit() and tail.isdigit()):
        return None
    if len(tail) == 3 and ("," in token[:last] or "." in token[:last] or head != "0"):
        return float(head + tail)  # ``1.234`` / ``1,234`` group thousands
    return float(f"{head}.{tail}")


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = _nfkc(value).strip()
        if _NUMBER.fullmatch(stripped):
            try:
                return _parse_number(stripped)
            except ValueError:
                return None
    return None


def _is_weak_number(value: Any) -> bool:
    """A whole number that is short (day, month, count) or year-like: it occurs almost anywhere."""
    number = _as_number(value)
    if number is None or not number.is_integer():
        return False
    return abs(number) < 100 or 1900 <= number <= 2100


def _iso_date(value: Any) -> date | None:
    match = _ISO.match(value.strip()) if isinstance(value, str) else None
    if not match:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def _word_pattern(needle: str) -> re.Pattern[str] | None:
    """Whole-word, case-insensitive, whitespace-tolerant pattern for ``needle``.

    Word boundaries are required only where the needle starts or ends with a word character, so
    ``INV-2026-0042`` and ``Acme`` match whole words, while a value never matches inside a longer word
    (``Acme`` must not be evidenced by ``Acmeville``)."""
    words = _nfkc(needle).split()
    if not words:
        return None
    body = r"\s+".join(re.escape(word) for word in words)
    left = r"(?<!\w)" if words[0][0].isalnum() else ""
    right = r"(?!\w)" if words[-1][-1].isalnum() else ""
    return re.compile(left + body + right, re.IGNORECASE)


def _find_exact(needle: str, pages: list[str]) -> Evidence | None:
    pattern = _word_pattern(needle)
    if pattern is None:
        return None
    for number, page in enumerate(pages, start=1):
        text = _nfkc(page)
        match = pattern.search(text)
        if match:
            return Evidence(EXACT, number, _snippet(text, match.start(), match.end()))
    return None


def _find_number(target: float, pages: list[str]) -> Evidence | None:
    for number, page in enumerate(pages, start=1):
        text = _nfkc(page)
        for match in _NUMBER.finditer(text):
            try:
                found = _parse_number(match.group())
            except ValueError:
                continue
            if found is not None and abs(found - target) < 0.005:
                return Evidence(NORMALIZED, number, _snippet(text, match.start(), match.end()))
    return None


def _find_date(target: date, pages: list[str]) -> Evidence | None:
    for number, page in enumerate(pages, start=1):
        for match in _NUMERIC_DATE.finditer(page):
            a, b, year = int(match[1]), int(match[2]), int(match[3])
            readings = set()
            for day, month in ((a, b), (b, a)):  # day-first and month-first
                try:
                    readings.add(date(year, month, day))
                except ValueError:
                    continue
            if target in readings:
                return Evidence(
                    NORMALIZED,
                    number,
                    _snippet(page, match.start(), match.end()),
                    ambiguous_date=len(readings) > 1,
                )
        for pattern, order in ((_TEXT_DATE, (1, 2, 3)), (_MONTH_FIRST, (2, 1, 3))):
            for match in pattern.finditer(page):
                day_text, name, year_text = (match[i] for i in order)
                month_number = _MONTHS.get(name.casefold())
                try:
                    if month_number and date(int(year_text), month_number, int(day_text)) == target:
                        return Evidence(
                            NORMALIZED, number, _snippet(page, match.start(), match.end())
                        )
                except ValueError:
                    continue
    return None


def _find_currency(code: str, pages: list[str]) -> Evidence | None:
    symbol = _CURRENCY_SYMBOLS.get(code.upper())
    if not symbol:
        return None
    for number, page in enumerate(pages, start=1):
        at = page.find(symbol)
        if at != -1:
            return Evidence(NORMALIZED, number, _snippet(page, at, at + 1))
    return None


def _locate(value: Any, pages: list[str]) -> Evidence:
    text = str(value).strip()
    found = _find_exact(text, pages)
    if found:
        return found
    target_date = _iso_date(value)
    if target_date:
        found = _find_date(target_date, pages)
        if found:
            return found
    if len(text) == 3:
        found = _find_currency(text, pages)
        if found:
            return found
    target_number = _as_number(value)
    if target_number is not None:
        found = _find_number(target_number, pages)
        if found:
            return found
    return Evidence(NOT_FOUND)


def locate(value: Any, pages: list[str]) -> Evidence:
    """Find ``value`` in the extracted page texts (1-based page numbers)."""
    if value is None or isinstance(value, (bool, list, dict)):
        return Evidence(NOT_APPLICABLE)
    if not str(value).strip():
        return Evidence(NOT_APPLICABLE)
    found = _locate(value, pages)
    if found.status in (EXACT, NORMALIZED) and _is_weak_number(value):
        return dataclasses.replace(found, weak=True)
    return found


def evidence_for(fields: dict[str, Any], pages: list[str]) -> dict[str, Evidence]:
    return {name: locate(value, pages) for name, value in fields.items()}


def evidence_issues(evidence: dict[str, Evidence]) -> list[dict[str, Any]]:
    """Validation issues for values the document does not support, or supports only weakly."""
    issues: list[dict[str, Any]] = []
    for name, found in evidence.items():
        if found.status == NOT_FOUND:
            issues.append(
                {
                    "field": name,
                    "rule": "value_not_in_document",
                    "message": "The value could not be found in the document text; check it against the original.",
                }
            )
        elif found.ambiguous_date:
            issues.append(
                {
                    "field": name,
                    "rule": "ambiguous_date",
                    "message": "The date can be read day-first or month-first; confirm which one the document uses.",
                }
            )
        elif found.weak:
            issues.append(
                {
                    "field": name,
                    "rule": "weak_evidence",
                    "message": "The value is a short number or a year and occurs in the text in other places too; confirm the location.",
                }
            )
    return issues
