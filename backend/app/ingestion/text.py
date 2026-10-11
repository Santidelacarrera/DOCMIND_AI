"""Text normalisation and prompt-injection heuristics."""

import re
import unicodedata

# Zero-width and bidi-control characters: invisible, used to hide text from reviewers and
# to split tokens so that pattern matchers (PII, injection) miss them.
_INVISIBLE = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD, 0x180E]
    + list(range(0x202A, 0x202F))
    + list(range(0x2066, 0x206A))
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_SPACES = re.compile(r"[ \t  -   　]+")
_BLANKS = re.compile(r"\n{3,}")


def normalise(text: str) -> str:
    """NFKC (folds full-width digits / letters), drop invisible and control characters,
    collapse runs of spaces and blank lines. Newlines are kept: they carry structure."""
    text = unicodedata.normalize("NFKC", text).translate(_INVISIBLE)
    text = _CONTROL.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", text).strip()


# Heuristic only -- an attacker can paraphrase. It exists to *flag* (never to rely on).
_INJECTION = re.compile(
    r"(ignore|disregard|forget|override)\W+(all\W+|any\W+|the\W+)?(previous|prior|above|earlier|system)\W+"
    r"(instructions?|prompts?|rules?|context)"
    r"|you\s+are\s+now\s+(in\s+)?(developer|dan|jailbreak|admin)\b"
    r"|(reveal|print|show|leak)\W+(your\W+|the\W+)?(system\W+)?(prompt|instructions|secrets?|api\W*keys?)"
    r"|\bnew\s+instructions?\s*:"
    r"|<\s*/?\s*(system|assistant|tool)\s*>"
    r"|\bact\s+as\s+(an?\s+)?(admin|root|system)\b",
    re.IGNORECASE,
)


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION.search(text))
