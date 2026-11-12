"""Possible disagreements between sources (ported from ContextRAG_Engine ``verification/conflicts.py``).

When two retrieved passages give different dates or figures for what is otherwise the same statement
("la audiencia es el 5 de mayo" / "la audiencia es el 9 de junio"), quoting one of them as *the*
answer hides that the documents disagree. This module finds those cases so the answer can say so.

**Not semantic.** It compares dates, figures and a negation flip between sentences that share most of
their words and name the same subject. It cannot read a revoked clause, a contradiction without figures
or a paraphrase. Every result is a *possible* conflict for a person to review; it never says which
source is right. The word-overlap thresholds are ContextRAG's and are uncalibrated for DOCMIND's corpus.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.rag.text import ends_with_break, sentence_breaks, split_at

MONTHS = {m: i for i, names in enumerate(
    ["enero january jan", "febrero february feb", "marzo march mar", "abril april apr", "mayo may", "junio june jun",
     "julio july jul", "agosto august aug", "septiembre setiembre september sep", "octubre october oct",
     "noviembre november nov", "diciembre december dec"], start=1) for m in names.split()}
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
_DATES = [
    (re.compile(rf"\b(\d{{1,2}})\s+(?:de\s+)?({_MONTH_RE})\.?(?:\s+(?:de\s+|del\s+)?(\d{{4}}))?\b", re.IGNORECASE), "dmy"),
    (re.compile(rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?(?:\s+(\d{{4}}))?\b", re.IGNORECASE), "mdy"),
    (re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b"), "slash"),
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
]
_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d+))?(?:\s*(millones|millón|mil)\b)?", re.IGNORECASE)
_WORD = re.compile(r"[^\W_][\w\-]*", re.UNICODE)
_STOP = frozenset(
    ["a", "an", "the", "of", "to", "in", "on", "for", "and", "or", "is", "are", "was", "were", "be", "been", "it", "its", "this", "that", "these", "those", "with", "as", "by", "at", "from", "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al", "y", "o", "en", "es", "son", "fue", "ser", "se", "su", "sus", "que", "con", "por", "para", "como", "lo"]
)
_NEGATIONS = frozenset(["no", "nunca", "jamás", "tampoco", "ni", "sin", "not", "never"])
_CONNECTORS = frozenset(["de", "del", "la", "las", "los", "of", "the"])
# Stricter than a loose match on purpose: "firmado el 5 de mayo" and "vence el 5 de mayo de 2029" share
# most words and are not a contradiction.
MIN_JACCARD, MIN_JACCARD_UNNAMED = 0.75, 0.85


@dataclass(frozen=True)
class SourceRef:
    document_id: str
    document: str
    page: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"document_id": self.document_id, "document": self.document, "page": self.page, "text": self.text[:300]}


@dataclass(frozen=True)
class Conflict:
    kind: str  # "value_mismatch" (dates or figures differ) or "negation" (same statement, one negated)
    score: float  # word overlap of the two sentences
    a: SourceRef
    b: SourceRef

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "score": round(self.score, 4), "needs_human_review": True,
                "method": "structured-values (dates, figures, negation; not semantic)",
                "sources": [self.a.to_dict(), self.b.to_dict()]}


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


def _dates(text: str) -> tuple[frozenset[str], str]:
    """ISO dates in ``text`` and the text without them (their digits are not counted again as figures)."""
    found: set[str] = set()
    for pattern, order in _DATES:
        def take(m: re.Match[str], order: str = order) -> str:
            g = m.groups()
            try:
                if order == "dmy":
                    day, month, year = int(g[0]), MONTHS[g[1].lower()], g[2] or "????"
                elif order == "mdy":
                    month, day, year = MONTHS[g[0].lower()], int(g[1]), g[2] or "????"
                elif order == "slash":
                    day, month, year = int(g[0]), int(g[1]), g[2]
                else:
                    year, month, day = g[0], int(g[1]), int(g[2])
            except (KeyError, ValueError):
                return m.group()
            found.add(f"{year}-{month:02d}-{day:02d}")
            return " "

        text = pattern.sub(take, text)
    return frozenset(found), text


def _numbers(text: str) -> frozenset[str]:
    out = set()
    for m in _NUMBER.finditer(text):
        whole, frac, scale = m.group(1).replace(".", ""), m.group(2), (m.group(3) or "").lower()
        value = float(f"{whole}.{frac}" if frac else whole) * {"millones": 1e6, "millón": 1e6, "mil": 1e3, "": 1}[scale]
        out.add(f"{value:g}")
    return frozenset(out)


def _words(text: str) -> frozenset[str]:
    return frozenset(t for t in _tokens(text) if not t.isdigit() and t not in MONTHS and t not in _STOP)


def _differ(a: frozenset[str], b: frozenset[str]) -> bool:
    """Each side has a value the other lacks; a superset is more detail, not a conflict."""
    return bool(a and b and a - b and b - a)


def named_phrases(text: str) -> list[str]:
    """Capitalised words that do not start a sentence, and identifiers with a digit, joined across
    connectors ("Juzgado de Letras"). Placeholders such as ``[PERSON_1]`` count as names."""
    phrases: list[str] = []
    run: list[str] = []
    pending: list[str] = []
    previous_end = 0

    def flush() -> None:
        if run and " ".join(run) not in phrases:
            phrases.append(" ".join(run))
        run.clear()
        pending.clear()

    for index, m in enumerate(_WORD.finditer(text)):
        word = m.group()
        initial = index == 0 or ends_with_break(text[previous_end:m.start()], ".!?:;")
        previous_end = m.end()
        lower = word.lower()
        identifier = any(c.isdigit() for c in word) and any(c.isalpha() for c in word)
        placeholder = text[max(0, m.start() - 1):m.start()] == "[" and word.isupper()
        named = lower not in _STOP and ((word[0].isupper() and not initial) or identifier or placeholder)
        if named:
            run.extend(pending)
            pending.clear()
            run.append(word)
        elif run and lower in _CONNECTORS:
            pending.append(word)
        else:
            flush()
    flush()
    return phrases


def _score(a: str, b: str) -> tuple[str, float] | None:
    dates_a, rest_a = _dates(a)
    dates_b, rest_b = _dates(b)
    values_differ = _differ(dates_a, dates_b) or _differ(_numbers(rest_a), _numbers(rest_b))
    negation_flip = bool(_NEGATIONS & set(_tokens(a))) != bool(_NEGATIONS & set(_tokens(b)))
    if not (values_differ or negation_flip):
        return None
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return None
    jaccard = len(wa & wb) / len(wa | wb)
    subject_a = {p.lower() for p in named_phrases(a)}
    if subject_a != {p.lower() for p in named_phrases(b)}:
        return None  # about different things ("expediente Alfa" and "expediente Bravo")
    if not values_differ:
        return ("negation", jaccard) if jaccard >= MIN_JACCARD else None
    return ("value_mismatch", jaccard) if jaccard >= (MIN_JACCARD if subject_a else MIN_JACCARD_UNNAMED) else None


_HEADING = re.compile(r"^(?:[A-ZÁÉÍÓÚÑÜ0-9][A-ZÁÉÍÓÚÑÜ0-9\-—.,:]*\s+){2,}(?=[A-ZÁÉÍÓÚÑÜ][a-záéíóúñü])")


def strip_heading(sentence: str) -> str:
    """Drop an all-caps heading that text extraction ran into the sentence ("ACTA DE MARZO La audiencia
    ..."): otherwise the heading's words read as the sentence's subject and hide a real disagreement."""
    return _HEADING.sub("", sentence, count=1)


def detect(question: str, hits: Sequence[dict[str, Any]], *, per_source: int = 3, max_pairs: int = 200) -> list[Conflict]:
    """Possible conflicts among the sentences of ``hits`` that share words with ``question``.

    Two sentences of the same document page are never compared (a page does not disagree with
    itself here). ``hits`` are retrieval payloads: ``text``, ``document_id``, ``source_name``, ``page``.
    """
    wanted = set(_tokens(question)) - _STOP
    refs: list[SourceRef] = []
    for hit in hits:
        scored = []
        for sentence in (strip_heading(s.strip()) for s in split_at(str(hit.get("text", "")), sentence_breaks(str(hit.get("text", "")), word_after=False))):
            overlap = len(wanted & set(_tokens(sentence)))
            if overlap and len(sentence) > 15:
                scored.append((overlap, sentence))
        page = int(hit.get("page") or hit.get("page_start") or 0)
        document_id = str(hit.get("document_id"))
        name = str(hit.get("source_name") or document_id)
        for _, sentence in sorted(scored, key=lambda item: -item[0])[:per_source]:
            refs.append(SourceRef(document_id, name, page, sentence))
    found: list[Conflict] = []
    pairs = 0
    for i, a in enumerate(refs):
        for b in refs[i + 1:]:
            if (a.document_id, a.page) == (b.document_id, b.page):
                continue
            pairs += 1
            if pairs > max_pairs:
                return found
            result = _score(a.text, b.text)
            if result:
                found.append(Conflict(result[0], result[1], a, b))
    return found
