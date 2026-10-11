"""PII detection and masking.

Scope, stated plainly: this detects **structured** identifiers (e-mail, phone, IBAN, card
numbers, national IDs, IPs, credentials) using patterns *plus checksum validation*, which
keeps false positives low. It does **not** recognise free-text names, addresses or
organisations; plug an NER model in through :class:`EntityDetector` (Presidio, spaCy, a
hosted service) -- its spans go through exactly the same masking and verification.

Masking is irreversible by design: no mapping from placeholder to value is kept.
"""

import hashlib
import hmac
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.ingestion.text import normalise


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    kind: str


class EntityDetector(Protocol):
    """Extension point for ML / NER detectors. Offsets are into the *normalised* text."""

    def detect(self, text: str) -> list[Span]: ...


# --------------------------------------------------------------------------- validators
def luhn_ok(number: str) -> bool:
    digits = [int(c) for c in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def iban_ok(value: str) -> bool:
    iban = re.sub(r"\s", "", value).upper()
    if not 15 <= len(iban) <= 34 or not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", iban):
        return False
    rearranged = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"


def es_dni_ok(value: str) -> bool:
    v = value.upper()
    if v[0] in "XYZ":  # NIE
        v = str("XYZ".index(v[0])) + v[1:]
    return len(v) == 9 and v[:8].isdigit() and _DNI_LETTERS[int(v[:8]) % 23] == v[8]


def us_ssn_ok(value: str) -> bool:
    area, group, serial = value.split("-")
    return area not in {"000", "666"} and not area.startswith("9") and group != "00" and serial != "0000"


def phone_ok(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return 9 <= len(digits) <= 15 and len(set(digits)) > 2


def ipv4_ok(value: str) -> bool:
    return all(0 <= int(p) <= 255 for p in value.split("."))


# --------------------------------------------------------------------------- patterns
@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: re.Pattern[str]
    validate: Callable[[str], bool] | None = None
    priority: int = 0  # higher wins when spans overlap and have the same length


RULES: tuple[Rule, ...] = (
    Rule("SECRET", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}"
                              r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
                              r"|xox[abprs]-[A-Za-z0-9-]{10,})\b"), priority=9),
    Rule("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"), priority=8),
    Rule("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b"), iban_ok, 7),
    Rule("CREDIT_CARD", re.compile(r"(?<![\w-])(?:\d[ -]?){13,19}(?![\w-])"), luhn_ok, 6),
    Rule("ES_NATIONAL_ID", re.compile(r"\b(?:\d{8}|[XYZxyz]\d{7})[- ]?[A-Za-z]\b"),
         lambda v: es_dni_ok(re.sub(r"[- ]", "", v)), 6),
    Rule("US_SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), us_ssn_ok, 6),
    Rule("IP_ADDRESS", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), ipv4_ok, 4),
    Rule("PHONE", re.compile(r"(?<![\w.,-])(?:\+|00)?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,5}(?!\w)(?![.,-]\d)"), phone_ok, 3),
)


def detect(text: str, extra: Sequence[EntityDetector] = ()) -> list[Span]:
    """All PII spans in ``text`` (already normalised), overlaps resolved: longer span wins,
    then rule priority, then earlier start."""
    candidates: list[tuple[Span, int]] = []
    for rule in RULES:
        for m in rule.pattern.finditer(text):
            value = m.group(0).strip()
            if rule.validate is None or rule.validate(value):
                end = m.start() + len(m.group(0).rstrip())
                candidates.append((Span(m.start(), end, rule.kind), rule.priority))
    for detector in extra:
        candidates.extend((s, 5) for s in detector.detect(text))
    candidates.sort(key=lambda c: (-(c[0].end - c[0].start), -c[1], c[0].start))
    chosen: list[Span] = []
    for span, _ in candidates:
        if all(span.end <= o.start or span.start >= o.end for o in chosen):
            chosen.append(span)
    return sorted(chosen, key=lambda s: s.start)


# --------------------------------------------------------------------------- masking
@dataclass(frozen=True)
class MaskResult:
    text: str
    counts: dict[str, int]  # PII kind -> occurrences masked; never the values


_PLACEHOLDER = re.compile(r"\[([A-Z_]+?)(?:_\d+|:[0-9a-f]{8})\]")


def placeholder_counts(masked_text: str) -> dict[str, int]:
    """How many placeholders of each kind a (masked) chunk contains."""
    return dict(Counter(m.group(1) for m in _PLACEHOLDER.finditer(masked_text)))


class PiiMasker:
    """Replace each PII span by a placeholder.

    * ``key=None`` -> ``[EMAIL_1]``: numbered per document, the same value always gets the
      same number inside one call.
    * ``key`` set  -> ``[EMAIL:3fa9c1d2]``: keyed HMAC pseudonym, stable across documents of
      the **same tenant** (so repeated mentions still co-occur in retrieval) but different
      per tenant and not reversible without the key.
    """

    def __init__(self, *, key: bytes | None = None, tenant_id: str = "", extra: Sequence[EntityDetector] = ()) -> None:
        self._key = hmac.new(key, tenant_id.encode(), hashlib.sha256).digest() if key else None
        self._extra = tuple(extra)

    def _token(self, kind: str, value: str, seen: dict[tuple[str, str], int]) -> str:
        canonical = re.sub(r"[\s.-]", "", value).casefold()
        if self._key is not None:
            digest = hmac.new(self._key, f"{kind}:{canonical}".encode(), hashlib.sha256).hexdigest()[:8]
            return f"[{kind}:{digest}]"
        index = seen.setdefault((kind, canonical), sum(1 for k, _ in seen if k == kind) + 1)
        return f"[{kind}_{index}]"

    def mask(self, text: str) -> MaskResult:
        text = normalise(text)  # detection must see what a model would see
        spans = detect(text, self._extra)
        out: list[str] = []
        cursor = 0
        seen: dict[tuple[str, str], int] = {}
        counts: Counter[str] = Counter()
        for span in spans:
            out.append(text[cursor : span.start])
            out.append(self._token(span.kind, text[span.start : span.end], seen))
            counts[span.kind] += 1
            cursor = span.end
        out.append(text[cursor:])
        return MaskResult("".join(out), dict(counts))

    def residual(self, masked_text: str) -> dict[str, int]:
        """Re-scan already-masked text. Anything found is a masking failure (fail closed)."""
        return dict(Counter(s.kind for s in detect(masked_text, self._extra)))
