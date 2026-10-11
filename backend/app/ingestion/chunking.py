"""Tenant-bound chunking.

A :class:`Chunk` cannot be constructed without a valid ``tenant_id`` (the organization UUID)
and its id is derived from ``(tenant, document, index, content)``, so re-ingesting a document
is idempotent (same points are overwritten) and two tenants can never produce the same id.
"""

import hashlib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

_NAMESPACE = uuid.UUID("6f1d2c1e-4a0b-4f6e-9c52-0d6b1e7a5c10")  # fixed: ids are stable across runs
SCHEMA_VERSION = 1


class TenantError(ValueError):
    """A chunk or batch is missing its tenant, or mixes tenants."""


def parse_tenant(value: object) -> str:
    """Canonical lower-case UUID string, or :class:`TenantError`."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise TenantError("TENANT_ID_INVALID") from None


@dataclass(frozen=True)
class Chunk:
    tenant_id: str
    document_id: str
    index: int
    text: str
    page_start: int
    page_end: int
    pii_counts: dict[str, int] = field(default_factory=dict)
    suspicious: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", parse_tenant(self.tenant_id))
        object.__setattr__(self, "document_id", parse_tenant(self.document_id))
        if not self.text.strip():
            raise ValueError("CHUNK_EMPTY")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()

    @property
    def point_id(self) -> str:
        return str(uuid.uuid5(_NAMESPACE, f"{self.tenant_id}:{self.document_id}:{self.index}:{self.content_hash}"))


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _pieces(paragraph: str, limit: int) -> list[str]:
    """A paragraph as units no longer than ``limit`` (sentence boundary, else hard split)."""
    if len(paragraph) <= limit:
        return [paragraph]
    out: list[str] = []
    current = ""
    for sentence in _SENTENCE.split(paragraph):
        while len(sentence) > limit:  # no boundary at all: split on whitespace / hard
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if current:
                out.append(current)
                current = ""
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > limit:
            out.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        out.append(current)
    return out


def split_pages(
    pages: Sequence[str], *, max_chars: int = 1200, overlap: int = 150
) -> list[tuple[str, int, int]]:
    """``(text, first_page, last_page)`` windows: at most ``max_chars`` characters of new content (plus the overlap).

    Paragraphs (blank-line separated) are the unit of packing, so a chunk rarely cuts a thought
    in half. The last ``overlap`` characters of a chunk are repeated at the start of the next
    one (when they fit) to keep context across the boundary.
    """
    if max_chars < 200 or not 0 <= overlap < max_chars // 2:
        raise ValueError("CHUNK_PARAMETERS_INVALID")
    units: list[tuple[str, int]] = []
    for number, page in enumerate(pages, start=1):
        for paragraph in re.split(r"\n\s*\n", page):
            paragraph = " ".join(paragraph.split())
            if paragraph:
                units.extend((piece, number) for piece in _pieces(paragraph, max_chars))

    windows: list[tuple[str, int, int]] = []
    current: list[tuple[str, int]] = []  # (text, page); may begin with an overlap tail
    fresh = 0  # units in ``current`` that are not just the overlap carried from the last window

    def length() -> int:
        return sum(len(t) for t, _ in current) + 2 * max(0, len(current) - 1)

    def emit() -> None:
        nonlocal current, fresh
        text = "\n\n".join(t for t, _ in current)
        windows.append((text, current[0][1], current[-1][1]))
        tail = text[-overlap:].lstrip() if overlap else ""
        current = [(tail, current[-1][1])] if " " in tail else []
        fresh = 0

    for unit, unit_page in units:
        if fresh and length() + 2 + len(unit) > max_chars:
            emit()
        current.append((unit, unit_page))
        fresh += 1
    if fresh:
        emit()
    return windows


def build_chunks(
    *, tenant_id: str, document_id: str, windows: Sequence[tuple[str, int, int]],
    pii_counts: Sequence[dict[str, int]] | None = None, suspicious: Sequence[bool] | None = None,
) -> list[Chunk]:
    tenant = parse_tenant(tenant_id)
    return [
        Chunk(
            tenant_id=tenant, document_id=document_id, index=i, text=text, page_start=a, page_end=b,
            pii_counts=(pii_counts[i] if pii_counts else {}), suspicious=(suspicious[i] if suspicious else False),
        )
        for i, (text, a, b) in enumerate(windows)
    ]


def assert_single_tenant(chunks: Sequence[Chunk], tenant_id: str) -> None:
    tenant = parse_tenant(tenant_id)
    if any(c.tenant_id != tenant for c in chunks):
        raise TenantError("TENANT_MISMATCH")
