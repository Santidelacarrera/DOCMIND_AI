"""Entity detectors for what structured patterns cannot see: people's names and physical
addresses (plus a tenant-supplied list of terms that must always be masked).

Two implementations behind the :class:`~app.ingestion.pii.EntityDetector` protocol:

* :class:`RuleEntityDetector` -- no dependencies, deterministic, configurable. It is
  *context-triggered*: a name is recognised after a label ("Nombre:", "Bill to:"), a title
  ("Sra.", "Dr."), a signing phrase ("firmado por"); an address by street-word + number
  patterns, postal-code + city lines and postcodes. It will miss a bare name in running text.
* :class:`SpacyEntityDetector` -- statistical NER (PERSON, optionally places) for running
  text. Higher recall on prose, but probabilistic: it can miss and it can over-mask.

Use both (``PII_DETECTORS=rules,spacy``): rules give a predictable floor, NER adds recall.
Bias is deliberately toward over-masking; organisations are *not* masked (company suffixes are
recognised and skipped), because a company name is not personal data.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.core import Settings
from app.ingestion.pii import EntityDetector, Span

PERSON, ADDRESS, TERM, LOCATION = "PERSON", "ADDRESS", "TERM", "LOCATION"

_UP = "A-ZÁÉÍÓÚÑÜÀÈÌÒÙÂÊÎÔÛÇ"
_LOW = "a-záéíóúñüàèìòùâêîôûç"
_WORD = rf"[{_UP}][{_LOW}]+(?:[-'’][{_UP}]?[{_LOW}]+)*"
_PARTICLE = r"(?:de|del|de la|de los|de las|la|las|los|van|von|der|den|da|di|dos|y|e|bin|al|el)"
# 1-4 capitalised words, optionally joined by lower-case particles ("María José Fernández de la Torre").
_NAME = rf"{_WORD}(?:[ \t]+(?:{_PARTICLE}[ \t]+)?{_WORD}){{0,3}}"

DEFAULT_NAME_LABELS = (
    "applicant name", "full name", "name", "first name", "last name", "surname", "nombre y apellidos",
    "nombre completo", "nombre", "apellidos", "customer", "client", "cliente", "bill to", "ship to",
    "contact", "contacto", "attn", "atn", "atención", "titular", "employee", "empleado", "patient",
    "paciente", "buyer", "seller", "comprador", "vendedor", "landlord", "tenant", "arrendador",
    "arrendatario", "representante", "firmante", "signatory", "beneficiary", "beneficiario",
)
DEFAULT_TITLES = (
    "Mr", "Mrs", "Ms", "Miss", "Mx", "Dr", "Prof", "Sr", "Sra", "Srta", "Dña", "Dna", "Don", "Doña", "Dra", "D", "Dª",
)
DEFAULT_SIGNING = (
    "signed by", "prepared by", "approved by", "reviewed by", "issued to", "firmado por", "preparado por",
    "aprobado por", "emitido a", "a la atención de", "atención de",
)
ORG_SUFFIXES = frozenset(
    ["inc", "corp", "corporation", "llc", "ltd", "limited", "plc", "gmbh", "ag", "bv", "nv", "sa", "sl", "slu", "sas", "srl", "spa", "llp", "lp", "co", "company", "group", "holdings", "partners", "foundation", "fundación", "university", "universidad", "hospital", "bank", "banco", "ayuntamiento", "associates", "asociados", "cooperativa"]
)
_STREET_ES = r"(?:C/|Calle|Carrer|Avda\.?|Avenida|Av\.|Plaza|Pza\.|Paseo|Pº|Camino|Ronda|Travesía|Urbanización|Glorieta)"
_STREET_EN = (
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way|Court|Ct|Place|Pl|Square|Sq|"
    r"Terrace|Highway|Hwy|Parkway|Pkwy)"
)


ORG_WORDS = frozenset(
    ["hotel", "restaurant", "restaurante", "hospital", "clinic", "clinica", "clínica", "university", "universidad", "school", "colegio", "ministry", "ministerio", "ayuntamiento", "institute", "instituto", "church", "iglesia", "pharmacy", "farmacia"]
)
# A legal-form suffix right after the captured words: "Hotel Las Palmas S.A.", "Acme Retail Ltd."
_SUFFIX_AFTER = re.compile(
    r"[ \t]*,?[ \t]*(?:S\.?[ \t]?[AL]\.?(?:U\.?)?|S\.?R\.?L\.?|B\.?V\.?|N\.?V\.?|A\.?G\.?|Ltd|Inc|LLC|LLP|GmbH|Corp|PLC)(?!\w)",
    re.IGNORECASE,
)


def _is_org(text: str, following: str = "") -> bool:
    words = [re.sub(r"[.,]", "", w).lower() for w in text.split()]
    if not words:
        return False
    if words[-1] in ORG_SUFFIXES or any(w in ORG_WORDS for w in words):
        return True
    return bool(_SUFFIX_AFTER.match(following))


@dataclass
class RuleConfig:
    name_labels: Sequence[str] = DEFAULT_NAME_LABELS
    titles: Sequence[str] = DEFAULT_TITLES
    signing_phrases: Sequence[str] = DEFAULT_SIGNING
    mask_addresses: bool = True
    mask_names: bool = True
    terms: Sequence[str] = field(default_factory=tuple)


class RuleEntityDetector:
    """Configurable, deterministic names / addresses / terms."""

    deterministic = True  # a re-scan of masked text is meaningful (used for residual verification)

    def __init__(self, config: RuleConfig | None = None) -> None:
        self.config = config or RuleConfig()
        labels = "|".join(sorted((re.escape(x) for x in self.config.name_labels), key=len, reverse=True))
        titles = "|".join(re.escape(x) for x in self.config.titles)
        signing = "|".join(sorted((re.escape(x) for x in self.config.signing_phrases), key=len, reverse=True))
        # (?m)^ anchors a label at a line start; "Name:" must not match inside "Company name:".
        # Only the keyword part is case-insensitive: the name itself must start with capitals.
        self._name = re.compile(_NAME)
        self._prefixes = [
            re.compile(rf"(?m)(?:^|[\n;|,.(:])[ \t]*(?i:{labels})[ \t]*[:\-–][ \t]*"),
            re.compile(rf"\b(?:{titles})\.?[ \t]+"),
            re.compile(rf"\b(?i:{signing})[ \t]+"),
        ]
        self._addresses = [
            # Spanish: "Calle del Puerto 14, 3º B, 46001 Valencia"
            re.compile(
                rf"\b{_STREET_ES}[ \t]+(?:[{_UP}{_LOW}0-9.'’-]+[ \t]+){{1,5}}?(?:n[ºo°.]?[ \t]*)?\d{{1,4}}[A-Za-z]?"
                rf"(?:,?[ \t]*\d{{1,2}}[ºo°ª]?[ \t]*[A-Za-z]?)?(?:,[ \t]*\d{{5}}[ \t]+{_WORD}(?:[ \t]+{_WORD})?)?"
            ),
            # English / US: "742 Evergreen Terrace, Springfield, IL 62704"
            re.compile(
                rf"\b\d{{1,5}}[ \t]+(?:{_WORD}[ \t]+){{1,4}}{_STREET_EN}\b\.?"
                rf"(?:,[ \t]*(?:{_WORD}[ \t]*)+,?[ \t]*[A-Z]{{2}}[ \t]+\d{{5}}(?:-\d{{4}})?)?"
            ),
            # "P.O. Box 123"
            re.compile(r"(?i)\bP\.?O\.?[ \t]+Box[ \t]+\d+\b"),
            # UK postcode
            re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?[ \t]?\d[A-Z]{2}\b"),
        ]
        # "46001 Valencia" only when introduced by a comma / line start / "CP" (avoids "Order 12345 Items")
        self._postal_city = re.compile(rf"(?:(?:^|\n|,)[ \t]*|\b(?:CP|C\.P\.|ZIP|Postal code)[:\s]+)(?P<n>\d{{5}}[ \t]+{_WORD}(?:[ \t]+{_WORD})?)")
        terms = sorted({t.strip() for t in self.config.terms if t.strip()}, key=len, reverse=True)
        self._terms = (
            re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in terms) + r")(?!\w)", re.IGNORECASE)
            if terms else None
        )

    def detect(self, text: str) -> list[Span]:
        spans: list[Span] = []
        if self.config.mask_names:
            for prefix in self._prefixes:
                pos = 0
                while (m := prefix.search(text, pos)) is not None:
                    # Resume at the trigger's separator (not past it): a following label such as
                    # "Contact: Applicant Name: Ana" must still find its own boundary character.
                    trailing = len(m.group(0)) - len(m.group(0).rstrip(" \t"))
                    pos = max(pos + 1, m.end() - trailing - 1)
                    name = self._name.match(text, m.end())
                    if name is None:
                        continue
                    after = text[name.end() : name.end() + 12]
                    # "Contact: Applicant Name: Ana" -- a captured field label is not a value.
                    if not _is_org(name.group(0), after) and not re.match(r"[ \t]*[:–]", after):
                        spans.append(Span(name.start(), name.end(), PERSON))
        if self.config.mask_addresses:
            for rx in self._addresses:
                spans.extend(Span(m.start(), m.end(), ADDRESS) for m in rx.finditer(text))
            spans.extend(Span(m.start("n"), m.end("n"), ADDRESS) for m in self._postal_city.finditer(text))
        if self._terms is not None:
            spans.extend(Span(m.start(), m.end(), TERM) for m in self._terms.finditer(text))
        return spans


class SpacyEntityDetector:
    """spaCy NER. ``labels`` maps spaCy labels to our kinds; organisations are never masked."""

    deterministic = False  # probabilistic: re-scanning masked text proves nothing

    def __init__(self, model: str = "en_core_web_sm", *, include_locations: bool = False, nlp: object = None) -> None:
        try:
            import spacy
        except ImportError:
            raise DetectorUnavailable("spacy is not installed (pip install spacy)") from None
        try:
            self._nlp = nlp or spacy.load(model, disable=["parser", "lemmatizer"])
        except OSError:
            raise DetectorUnavailable(
                f"spaCy model {model!r} is not installed (python -m spacy download {model})"
            ) from None
        self._labels = {"PERSON": PERSON, "PER": PERSON}
        if include_locations:
            self._labels.update({"GPE": LOCATION, "LOC": LOCATION, "FAC": LOCATION})

    def detect(self, text: str) -> list[Span]:
        out: list[Span] = []
        limit = int(getattr(self._nlp, "max_length", 1_000_000)) - 1000
        for start in range(0, len(text), limit):
            window = text[start : start + limit]
            for ent in self._nlp(window).ents:  # type: ignore[operator]
                kind = self._labels.get(ent.label_)
                if kind and not (kind == PERSON and _is_org(ent.text)):
                    out.append(Span(start + ent.start_char, start + ent.end_char, kind))
        return out


class DetectorUnavailable(RuntimeError):
    """A configured detector cannot start. Fatal on purpose: silently running without the
    detector an operator asked for would leak the very PII it was meant to catch."""


def load_terms(raw: str = "", path: Path | None = None) -> list[str]:
    terms = [t.strip() for t in raw.split(",") if t.strip()]
    if path is not None:
        terms += [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    return terms


def build_detectors(cfg: Settings) -> list[EntityDetector]:
    """Detectors named by ``PII_DETECTORS``; an unknown or unstartable one raises."""
    out: list[EntityDetector] = []
    for name in (n.strip().lower() for n in cfg.pii_detectors.split(",") if n.strip()):
        if name == "rules":
            out.append(RuleEntityDetector(RuleConfig(terms=load_terms(cfg.pii_extra_terms, cfg.pii_terms_file))))
        elif name == "spacy":
            out.append(SpacyEntityDetector(cfg.pii_spacy_model))
        else:
            raise DetectorUnavailable(f"unknown PII detector {name!r}")
    return out


def combine(detectors: Iterable[EntityDetector]) -> list[EntityDetector]:
    return list(detectors)
