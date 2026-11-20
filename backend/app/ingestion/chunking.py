"""Tenant-bound, structure-aware chunking.

A :class:`Chunk` cannot be constructed without a valid ``tenant_id`` (the organization UUID)
and its id is derived from ``(tenant, document, index, content)``, so re-ingesting a document
is idempotent (same points are overwritten) and two tenants can never produce the same id.

Structure preservation:
* Markdown/delimited tables (lines with ``|`` or tabs) are NEVER flattened onto a single line;
  oversized tables split cleanly by rows while repeating their header rows.
* Section headings (Markdown ``#..####`` or legal/technical headers such as ``CLÁUSULA``,
  ``ARTÍCULO``, ``SECTION``, ``ARTICLE``) are tracked as ``section_path`` metadata on each chunk.
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
class StructuredWindow:
    text: str
    page_start: int
    page_end: int
    section_path: str = ""
    chunk_type: str = "paragraph"  # "paragraph" | "table"


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
    # The case (project) the document belongs to. None means "unknown": such a vector can never
    # match a project-scoped query, because the filter requires this exact value.
    project_id: str | None = None
    section_path: str = ""
    chunk_type: str = "paragraph"

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", parse_tenant(self.tenant_id))
        object.__setattr__(self, "document_id", parse_tenant(self.document_id))
        if self.project_id is not None:
            object.__setattr__(self, "project_id", parse_tenant(self.project_id))
        if not self.text.strip():
            raise ValueError("CHUNK_EMPTY")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()

    @property
    def point_id(self) -> str:
        return str(uuid.uuid5(_NAMESPACE, f"{self.tenant_id}:{self.document_id}:{self.index}:{self.content_hash}"))

    @property
    def embedding_text(self) -> str:
        """Context-enriched text for vector embedding without altering citable ``text``."""
        return f"[{self.section_path}]\n{self.text}" if self.section_path else self.text


_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_MD_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
_LEGAL_HEADING = re.compile(
    r"^(?:CL[ÁA]USULA|ART[ÍI]CULO|SECCI[ÓO]N|CAP[ÍI]TULO|ANEXO|SECTION|ARTICLE|CLAUSE|APPENDIX)"
    r"\s+[A-Z0-9IVXLCDM][^\n]{0,80}$",
    re.IGNORECASE,
)


def _is_table_block(paragraph: str) -> bool:
    """True when a paragraph is a Markdown pipe table or tab-delimited table (>= 2 structured rows)."""
    lines = [ln.strip() for ln in paragraph.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    pipe_rows = sum(1 for ln in lines if ln.count("|") >= 2)
    if pipe_rows >= 2 and pipe_rows >= len(lines) // 2:
        return True
    tab_rows = sum(1 for ln in lines if "\t" in ln)
    return tab_rows >= 2 and tab_rows >= len(lines) // 2


def _split_table_preserving_header(table_text: str, limit: int) -> list[str]:
    """Keep a table intact when it fits ``limit``; otherwise split by rows repeating its header."""
    lines = [ln.rstrip() for ln in table_text.splitlines() if ln.strip()]
    joined = "\n".join(lines)
    if len(joined) <= limit or len(lines) <= 2:
        return [joined]
    # Detect Markdown separator row (|---|---|) to keep both header + separator on every split
    has_md_sep = len(lines) >= 2 and bool(re.fullmatch(r"[\s|:-]+", lines[1])) and "-" in lines[1]
    header = lines[:2] if has_md_sep else lines[:1]
    body = lines[len(header) :]
    chunks: list[str] = []
    batch: list[str] = list(header)
    for row in body:
        candidate = "\n".join([*batch, row])
        if len(candidate) > limit and len(batch) > len(header):
            chunks.append("\n".join(batch))
            batch = [*header, row]
        else:
            batch.append(row)
    if len(batch) > len(header) or not chunks:
        chunks.append("\n".join(batch))
    return chunks


def _extract_heading(paragraph: str) -> tuple[int, str] | None:
    """Return ``(level, title)`` if a single-line paragraph is a section/clause heading."""
    lines = [ln.strip() for ln in paragraph.splitlines() if ln.strip()]
    if len(lines) != 1:
        return None
    line = lines[0]
    m = _MD_HEADING.match(line)
    if m:
        return len(m.group(1)), m.group(2).strip()[:120]
    if _LEGAL_HEADING.match(line) and not line.endswith("."):
        return 2, line.rstrip(":").strip()[:120]
    return None


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


def split_pages_structured(
    pages: Sequence[str], *, max_chars: int = 1200, overlap: int = 150
) -> list[StructuredWindow]:
    """Structure-aware page-bounded chunking preserving tables and section hierarchy.

    **A window never crosses a page**, so ``page_start == page_end`` and citations name one page.
    Tables are kept intact (with line breaks preserved) and emitted as dedicated ``table`` windows
    so they are never corrupted by whitespace flattening.
    """
    if max_chars < 200 or not 0 <= overlap < max_chars // 2:
        raise ValueError("CHUNK_PARAMETERS_INVALID")
    windows: list[StructuredWindow] = []
    heading_stack: list[tuple[int, str]] = []

    def current_section() -> str:
        return " / ".join(title for _, title in heading_stack)[:180]

    for number, page in enumerate(pages, start=1):
        paragraphs = [p for p in re.split(r"\n\s*\n", page) if p.strip()]
        current: list[str] = []
        fresh = 0  # units in ``current`` that are not just overlap carried from the previous window
        cur_section = current_section()

        def length(cur: list[str]) -> int:
            return sum(len(t) for t in cur) + 2 * max(0, len(cur) - 1)

        def emit(
            cur: list[str], *, section: str, keep_overlap: bool = True, page_number: int = number
        ) -> list[str]:
            text = "\n\n".join(cur)
            windows.append(StructuredWindow(text, page_number, page_number, section_path=section, chunk_type="paragraph"))
            if not keep_overlap or not overlap:
                return []
            tail = text[-overlap:].lstrip()
            return [tail] if " " in tail else []

        for paragraph in paragraphs:
            heading = _extract_heading(paragraph)
            if heading is not None:
                level, title = heading
                if fresh:
                    current = emit(current, section=cur_section, keep_overlap=False)
                    fresh = 0
                heading_stack = [(lvl, t) for lvl, t in heading_stack if lvl < level]
                heading_stack.append((level, title))
                cur_section = current_section()
                # Keep the heading text inside the section's first chunk so lexical search still hits it
                current.append(title)
                fresh += 1
                continue

            if _is_table_block(paragraph):
                if fresh:
                    current = emit(current, section=cur_section, keep_overlap=False)
                    fresh = 0
                for table_chunk in _split_table_preserving_header(paragraph, max_chars):
                    windows.append(
                        StructuredWindow(
                            table_chunk, number, number, section_path=cur_section, chunk_type="table"
                        )
                    )
                continue

            for unit in _pieces(" ".join(paragraph.split()), max_chars):
                if fresh and length(current) + 2 + len(unit) > max_chars:
                    current = emit(current, section=cur_section, keep_overlap=True)
                    fresh = 0
                current.append(unit)
                fresh += 1
        if fresh:
            emit(current, section=cur_section, keep_overlap=False)
    return windows


def split_pages(
    pages: Sequence[str], *, max_chars: int = 1200, overlap: int = 150
) -> list[tuple[str, int, int]]:
    """Backwards-compatible ``(text, first_page, last_page)`` view over :func:`split_pages_structured`."""
    return [(w.text, w.page_start, w.page_end) for w in split_pages_structured(pages, max_chars=max_chars, overlap=overlap)]


def build_chunks(
    *, tenant_id: str, document_id: str, windows: Sequence[tuple[str, int, int] | StructuredWindow],
    pii_counts: Sequence[dict[str, int]] | None = None, suspicious: Sequence[bool] | None = None,
    source_name: str = "", page_basis: str = "explicit", project_id: str | None = None,
) -> list[Chunk]:
    tenant = parse_tenant(tenant_id)
    out: list[Chunk] = []
    for i, win in enumerate(windows):
        if isinstance(win, StructuredWindow):
            text, a, b, sec, ctype = win.text, win.page_start, win.page_end, win.section_path, win.chunk_type
        else:
            text, a, b = win
            sec, ctype = "", "paragraph"
        out.append(
            Chunk(
                tenant_id=tenant, document_id=document_id, index=i, text=text, page_start=a, page_end=b,
                pii_counts=(pii_counts[i] if pii_counts else {}), suspicious=(suspicious[i] if suspicious else False),
                source_name=source_name, page_basis=page_basis, project_id=project_id,
                section_path=sec, chunk_type=ctype,
            )
        )
    return out


def assert_single_tenant(chunks: Sequence[Chunk], tenant_id: str) -> None:
    tenant = parse_tenant(tenant_id)
    if any(c.tenant_id != tenant for c in chunks):
        raise TenantError("TENANT_MISMATCH")
