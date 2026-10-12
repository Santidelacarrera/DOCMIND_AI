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
    source_name: str = ""  # the document's display name, quoted in citations
    page_basis: str = "explicit"  # "explicit" (real pages) or "estimated" (Word without page breaks)

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
    """``(text, first_page, last_page)`` windows of at most ``max_chars`` characters of new content.

    **A window never crosses a page**, so ``first_page == last_page`` and a citation can name
    exactly one page. Within a page, paragraphs (blank-line separated) are the unit of packing,
    and the last ``overlap`` characters of a window are repeated at the start of the next one
    (same page only) to keep context across the boundary.
    """
    if max_chars < 200 or not 0 <= overlap < max_chars // 2:
        raise ValueError("CHUNK_PARAMETERS_INVALID")
    windows: list[tuple[str, int, int]] = []
    for number, page in enumerate(pages, start=1):
        units = [
            piece
            for paragraph in re.split(r"\n\s*\n", page)
            for piece in _pieces(" ".join(paragraph.split()), max_chars)
            if paragraph.strip()
        ]
        current: list[str] = []
        fresh = 0  # units in ``current`` that are not just the overlap carried from the last window

        def length(cur: list[str]) -> int:
            return sum(len(t) for t in cur) + 2 * max(0, len(cur) - 1)

        def emit(cur: list[str], page_number: int = number) -> list[str]:
            text = "\n\n".join(cur)
            windows.append((text, page_number, page_number))
            tail = text[-overlap:].lstrip() if overlap else ""
            return [tail] if " " in tail else []

        for unit in units:
            if fresh and length(current) + 2 + len(unit) > max_chars:
                current = emit(current)
                fresh = 0
            current.append(unit)
            fresh += 1
        if fresh:
            emit(current)
    return windows


def build_chunks(
    *, tenant_id: str, document_id: str, windows: Sequence[tuple[str, int, int]],
    pii_counts: Sequence[dict[str, int]] | None = None, suspicious: Sequence[bool] | None = None,
    source_name: str = "", page_basis: str = "explicit",
) -> list[Chunk]:
    tenant = parse_tenant(tenant_id)
    return [
        Chunk(
            tenant_id=tenant, document_id=document_id, index=i, text=text, page_start=a, page_end=b,
            pii_counts=(pii_counts[i] if pii_counts else {}), suspicious=(suspicious[i] if suspicious else False),
            source_name=source_name, page_basis=page_basis,
        )
        for i, (text, a, b) in enumerate(windows)
    ]


def assert_single_tenant(chunks: Sequence[Chunk], tenant_id: str) -> None:
    tenant = parse_tenant(tenant_id)
    if any(c.tenant_id != tenant for c in chunks):
        raise TenantError("TENANT_MISMATCH")
