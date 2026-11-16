"""Documented aliases: resolve names declared inside retrieved documents.

Ported from ContextRAG_Engine ``verification/aliases.py`` (ADR-005).

The entity gate abstains when a question names something the evidence never mentions. That
would falsely abstain when the documents themselves declare an alias ("Industrias Valdivia S.A.
(en adelante, 'IVSA')"). This module reads explicit declarations out of retrieved text and
resolves a term against them conservatively (exact normalised match, unambiguous only).
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

_UPPER = "A-Z\u00c1\u00c9\u00cd\u00d3\u00da\u00d1\u00dc"
_NAME = (
    rf"[{_UPPER}][\w&.\-]*(?:\s+(?:(?:de|del|la|las|los|y|e|&|and|of|the)\s+)?"
    rf"[{_UPPER}0-9][\w&.\-]*){{0,5}}"
)
_Q = r"[\u00ab\"'\u201c\u2018]?"
_QE = r"[\u00bb\"'\u201d\u2019]?"
_LEAD = r"(?:(?:el|la|los|las|the)\s+)?"
_MARKERS = (
    r"(?:tambi[\u00e9e]n\s+(?:conocid[oa]|llamad[oa]|denominad[oa])\s+(?:como\s+)?|"
    r"also\s+(?:known|called)\s+(?:as\s+)?|"
    r"a\.k\.a\.?\s+|alias\s+|en\s+adelante,?\s+|en\s+lo\s+sucesivo,?\s+|"
    r"hereinafter,?\s+|o\s+simplemente\s+)"
)
_DECL = re.compile(rf"({_NAME})\s*[,(]\s*{_MARKERS}{_LEAD}{_Q}({_NAME}){_QE}", re.UNICODE)
_ARTICLE = re.compile(r"^(?:el|la|los|las|the)\s+", re.IGNORECASE)
_STRIP_CHARS = "\u00ab\u00bb\"\u201c\u201d'\u2018\u2019.,;:()"


@dataclass(frozen=True)
class AliasDecl:
    canonical: str
    alias: str


@dataclass(frozen=True)
class Resolution:
    term: str
    outcome: str  # "alias" | "ambiguous" | "unknown"
    canonicals: tuple[str, ...] = ()


def normalise(name: str) -> str:
    text = unicodedata.normalize("NFC", name).strip().strip(_STRIP_CHARS).casefold()
    return _ARTICLE.sub("", re.sub(r"\s+", " ", text))


def extract_alias_declarations(text: str) -> list[AliasDecl]:
    found: list[AliasDecl] = []
    for m in _DECL.finditer(text):
        canonical = _ARTICLE.sub("", m.group(1).strip(" ,-"))
        alias = _ARTICLE.sub("", m.group(2).strip(" ,-"))
        decl = AliasDecl(canonical, alias)
        if normalise(decl.canonical) != normalise(decl.alias) and decl not in found:
            found.append(decl)
    return found


def resolve(term: str, declarations: Iterable[AliasDecl]) -> Resolution:
    key = normalise(term)
    entities: dict[str, str] = {}
    for d in declarations:
        if normalise(d.alias) == key:
            entities.setdefault(normalise(d.canonical), d.canonical)
    if not entities:
        return Resolution(term, "unknown")
    return Resolution(term, "alias" if len(entities) == 1 else "ambiguous", tuple(entities.values()))
