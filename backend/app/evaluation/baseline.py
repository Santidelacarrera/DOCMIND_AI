"""Offline, rule-based extractor used as the reproducible baseline.

It implements the same ``LLMProvider`` interface as the real providers, so the exact
same pipeline, validation, review and export code runs around it. It needs no API key
and is fully deterministic, which makes the committed metrics reproducible anywhere.

It is deliberately *not* a stand-in for an LLM: it understands English and Spanish
labels and a handful of date/amount formats. Its misses (German labels, free-form
sentences, OCR-damaged digits) are exactly what an LLM provider is expected to improve
on -- compare with ``python -m app.evaluation --provider openai``.
"""

import re
from datetime import date
from typing import Any

MONTHS = {
    m: i
    for i, names in enumerate(
        [
            "january enero", "february febrero", "march marzo", "april abril", "may mayo", "june junio",
            "july julio", "august agosto", "september septiembre", "october octubre",
            "november noviembre", "december diciembre",
        ],
        start=1,
    )
    for m in names.split()
}
CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}
NUM_WORDS = {"twelve": 12, "twenty-four": 24, "thirty-six": 36, "six": 6, "eighteen": 18, "forty-eight": 48}

# Label aliases per output field (matched case-insensitively at the start of a line).
LABELS: dict[str, list[str]] = {
    "invoice_number": ["invoice number", "invoice no.", "invoice #", "factura nº", "factura no.", "número de factura"],
    "invoice_date": ["invoice date", "fecha de emisión", "fecha", "issued", "date"],
    "customer_name": ["bill to", "customer", "cliente", "client"],
    "total_amount": [
        "total due", "total factura", "amount payable", "amount due", "total amount", "total a pagar", "grand total",
    ],
    "tax_amount": ["tax", "vat", "iva"],
    "applicant_name": ["applicant name", "full name", "name", "nombre"],
    "date_of_birth": ["date of birth", "dob", "birth date", "fecha de nacimiento"],
    "id_number": ["id number", "national id", "id no.", "dni"],
    "email": ["email", "e-mail", "correo"],
    "country": ["country of residence", "country", "país"],
}


def _line_value(text: str, labels: list[str]) -> tuple[str | None, bool]:
    """Value after the first matching ``label[ (...)]:`` line; second item = label matched exactly."""
    for label in sorted(labels, key=len, reverse=True):
        pattern = rf"^\s*{re.escape(label)}\s*(?:\([^)]*\))?\s*:\s*(.+?)\s*$"
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1), True
    return None, False


def parse_amount(raw: str) -> float | None:
    cleaned = re.sub(r"[^\d.,]", "", raw).rstrip(".,")
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        decimal = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
    elif "," in cleaned:
        decimal = "," if re.search(r",\d{1,2}$", cleaned) else ""
    else:
        decimal = "." if re.search(r"\.\d{1,2}$", cleaned) else ""
    if decimal:
        integer, _, fraction = cleaned.rpartition(decimal)
        digits = re.sub(r"[.,]", "", integer) + "." + fraction
    else:
        digits = re.sub(r"[.,]", "", cleaned)
    try:
        return round(float(digits), 2)
    except ValueError:
        return None


def parse_date(raw: str) -> tuple[str | None, bool]:
    """Return (ISO date, certain). Numeric day/month order is guessed (DD/MM), hence uncertain."""
    raw = raw.strip()
    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw):
        return raw, _valid(*map(int, m.groups()))
    if m := re.fullmatch(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", raw):
        day, month, year = map(int, m.groups())
        return (f"{year:04d}-{month:02d}-{day:02d}" if _valid(year, month, day) else None), False
    long_form = re.fullmatch(r"(\d{1,2})\s+([A-Za-zéñ]+)\s+(\d{4})", raw)
    if long_form and (named := MONTHS.get(long_form.group(2).lower())):
        return f"{int(long_form.group(3)):04d}-{named:02d}-{int(long_form.group(1)):02d}", True
    us_form = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", raw)
    if us_form and (named := MONTHS.get(us_form.group(1).lower())):
        return f"{int(us_form.group(3)):04d}-{named:02d}-{int(us_form.group(2)):02d}", True
    return None, False


def _valid(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


class RuleBasedProvider:
    """Deterministic ``LLMProvider`` for the invoice / contract / form schemas."""

    name = "baseline-rules"

    def extract(
        self, text: str, schema: dict[str, Any], prompt_instructions: str | None = None
    ) -> dict[str, Any]:
        wanted = list(schema["properties"]["fields"].get("properties", {}))
        fields: dict[str, Any] = {}
        confidence: dict[str, float] = {}
        for name in wanted:
            value, score = getattr(self, f"_{name}", self._missing)(text)
            fields[name] = value
            confidence[name] = score
        return {"fields": fields, "confidence": confidence}

    @staticmethod
    def _missing(_: str) -> tuple[Any, float]:
        return None, 0.0

    # ------------------------------------------------------------------ generic helpers
    @staticmethod
    def _text_field(text: str, name: str) -> tuple[str | None, float]:
        value, _ = _line_value(text, LABELS[name])
        return (value.strip(), 0.95) if value else (None, 0.0)

    # ------------------------------------------------------------------ invoice
    def _invoice_number(self, text: str) -> tuple[str | None, float]:
        return self._text_field(text, "invoice_number")

    def _invoice_date(self, text: str) -> tuple[str | None, float]:
        raw, _ = _line_value(text, LABELS["invoice_date"])
        if raw is None:
            return None, 0.0
        iso, certain = parse_date(raw)
        return (iso, 0.95 if certain else 0.6) if iso else (None, 0.0)

    def _vendor_name(self, text: str) -> tuple[str | None, float]:
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), None)
        return (first, 0.75) if first else (None, 0.0)

    def _customer_name(self, text: str) -> tuple[str | None, float]:
        return self._text_field(text, "customer_name")

    def _currency(self, text: str) -> tuple[str | None, float]:
        value, _ = _line_value(text, ["currency"])
        if value and re.fullmatch(r"[A-Za-z]{3}", value.strip()):
            return value.strip().upper(), 0.95
        if m := re.search(r"\((USD|EUR|GBP)\)", text):
            return m.group(1), 0.85
        if m := re.search(r"\b(USD|EUR|GBP)\b", text):
            return m.group(1), 0.8
        for symbol, code in CURRENCY_SYMBOLS.items():
            if symbol in text:
                return code, 0.85
        return None, 0.0

    def _amount(self, text: str, name: str) -> tuple[float | None, float]:
        raw, _ = _line_value(text, LABELS[name])
        if raw is None:
            return None, 0.0
        amount = parse_amount(raw)
        return (amount, 0.95) if amount is not None else (None, 0.0)

    def _total_amount(self, text: str) -> tuple[float | None, float]:
        return self._amount(text, "total_amount")

    def _tax_amount(self, text: str) -> tuple[float | None, float]:
        return self._amount(text, "tax_amount")

    # ------------------------------------------------------------------ contract
    def _effective_date(self, text: str) -> tuple[str | None, float]:
        if m := re.search(r"effective as of\s+([A-Za-z]+ \d{1,2}, \d{4})", text, re.IGNORECASE):
            iso, _ = parse_date(m.group(1))
            return (iso, 0.9) if iso else (None, 0.0)
        raw, _ = _line_value(text, ["date of effect", "effective date"])
        if raw:
            iso, certain = parse_date(raw)
            return (iso, 0.9 if certain else 0.6) if iso else (None, 0.0)
        return None, 0.0

    def _counterparty(self, text: str) -> tuple[str | None, float]:
        if m := re.search(r'and\s+(.+?)\s*\(\s*"?Client"?\s*\)', text.replace("\n", " ")):
            return m.group(1).strip(), 0.85
        value, _ = _line_value(text, ["supplier", "counterparty", "provider"])
        return (value, 0.6) if value else (None, 0.0)

    def _term_months(self, text: str) -> tuple[int | None, float]:
        if m := re.search(r"(?:term:|term of this agreement is|for a period of)\s*(\d+)\s*months", text, re.IGNORECASE):
            return int(m.group(1)), 0.9
        if m := re.search(r"(\d+)\s*months", text):
            return int(m.group(1)), 0.55
        return None, 0.0

    def _governing_law(self, text: str) -> tuple[str | None, float]:
        flat = text.replace("\n", " ")
        if m := re.search(r"governed by the laws of the\s+(.+?)\.(?:\s|$)", flat, re.IGNORECASE):
            return m.group(1).strip(), 0.9
        value, _ = _line_value(text, ["governing law"])
        return (value, 0.9) if value else (None, 0.0)

    def _total_fee(self, text: str) -> tuple[float | None, float]:
        flat = text.replace("\n", " ")
        if m := re.search(r"total fee of\s+(\S+)", flat, re.IGNORECASE):
            amount = parse_amount(m.group(1))
            if amount is not None:
                return amount, 0.9
        if m := re.search(r"(?:aggregate )?contract value is\s+(?:[A-Z]{3}\s+)?([\d.,]+)", flat, re.IGNORECASE):
            amount = parse_amount(m.group(1))
            if amount is not None:
                return amount, 0.8
        return None, 0.0

    # ------------------------------------------------------------------ form
    def _applicant_name(self, text: str) -> tuple[str | None, float]:
        return self._text_field(text, "applicant_name")

    def _date_of_birth(self, text: str) -> tuple[str | None, float]:
        raw, _ = _line_value(text, LABELS["date_of_birth"])
        if raw is None:
            return None, 0.0
        iso, certain = parse_date(raw)
        return (iso, 0.95 if certain else 0.6) if iso else (None, 0.0)

    def _id_number(self, text: str) -> tuple[str | None, float]:
        return self._text_field(text, "id_number")

    def _email(self, text: str) -> tuple[str | None, float]:
        value, _ = _line_value(text, LABELS["email"])
        if value and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            return value, 0.95
        if m := re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text):
            return m.group(0), 0.7
        return None, 0.0

    def _country(self, text: str) -> tuple[str | None, float]:
        return self._text_field(text, "country")
