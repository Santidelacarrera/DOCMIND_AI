"""Word (.docx) inspection and text extraction, standard library + defusedxml only.

A .docx is a ZIP of XML parts. Treat it as hostile input:

* **zip bombs** -- entry count, per-entry and total *uncompressed* size are capped before
  anything is read, and reads are bounded;
* **XML attacks** -- parsed with ``defusedxml`` (no DTDs, no entity expansion, no external
  entities);
* **active / outbound content** -- macros (``vbaProject.bin``, macro-enabled content types),
  embedded OLE objects / packages / ActiveX and external *template or object* relationships
  are rejected; plain external hyperlinks are ignored (never fetched);
* **hidden text** -- runs marked ``w:vanish`` and tracked deletions (``w:delText``) are *not*
  extracted: that is the classic place to hide instructions or data from a human reviewer.
  (Text hidden by colour or size is not detectable here; see docs/INGESTION.md.)

Pages: Word does not store pagination. A new page starts at an explicit page break, a
``pageBreakBefore`` paragraph, a next-page section break or a ``lastRenderedPageBreak`` marker
(written by Word when it last saved). With none of those the text is split into estimated pages
of ``ESTIMATED_PAGE_CHARS`` characters at paragraph boundaries and ``page_basis`` says so, so a
citation never claims precision it does not have.
"""

import io
import zipfile
from dataclasses import dataclass, field
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_ENTRIES = 2_000
MAX_ENTRY_BYTES = 40 * 1024 * 1024
MAX_TOTAL_BYTES = 120 * 1024 * 1024
MAX_RATIO = 200  # uncompressed / compressed, per entry
ESTIMATED_PAGE_CHARS = 3_000

_EMBEDDED_PREFIXES = ("word/embeddings/", "word/activeX/", "customXml/activeX/")
_DANGEROUS_REL_TYPES = ("attachedTemplate", "oleObject", "frame", "package", "externalLink")


class UnsafeDocx(ValueError):
    """Malformed, oversized, or carrying content the policy refuses. ``code`` is stable."""

    def __init__(self, code: str, findings: "DocxFindings | None" = None) -> None:
        super().__init__(code)
        self.code = code
        self.findings = findings


@dataclass
class DocxFindings:
    active: set[str] = field(default_factory=set)  # macro, embedded_object, external_template
    passive: set[str] = field(default_factory=set)  # external_hyperlink, hidden_text, tracked_deletions
    core_properties: bool = False  # author/title metadata part present (never extracted)


@dataclass
class DocxText:
    pages: list[str]
    page_basis: str  # "explicit" | "estimated"
    findings: DocxFindings


def looks_like_docx(content: bytes) -> bool:
    return content[:4] == b"PK\x03\x04"


def _open(content: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError, ValueError):
        raise UnsafeDocx("DOCUMENT_INVALID") from None
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise UnsafeDocx("DOCX_TOO_MANY_ENTRIES")
    total = 0
    for info in infos:
        total += info.file_size
        if info.file_size > MAX_ENTRY_BYTES or total > MAX_TOTAL_BYTES:
            raise UnsafeDocx("DOCX_TOO_LARGE_UNCOMPRESSED")
        if info.compress_size and info.file_size / info.compress_size > MAX_RATIO and info.file_size > 1_000_000:
            raise UnsafeDocx("DOCX_COMPRESSION_RATIO")
        if info.filename.startswith(("/", "..")) or ".." in info.filename.split("/"):
            raise UnsafeDocx("DOCX_PATH_TRAVERSAL")
        if info.flag_bits & 0x1:  # encrypted entry
            raise UnsafeDocx("DOCX_ENCRYPTED")
    return archive


def _read(archive: zipfile.ZipFile, name: str) -> bytes:
    with archive.open(name) as handle:
        data = handle.read(MAX_ENTRY_BYTES + 1)  # bounded even if the header lied about the size
    if len(data) > MAX_ENTRY_BYTES:
        raise UnsafeDocx("DOCX_TOO_LARGE_UNCOMPRESSED")
    return data


def _xml(archive: zipfile.ZipFile, name: str) -> Element:
    try:
        return SafeET.fromstring(_read(archive, name))
    except (DefusedXmlException, SafeET.ParseError):
        raise UnsafeDocx("DOCX_XML_INVALID") from None


def inspect_docx(content: bytes) -> tuple[zipfile.ZipFile, DocxFindings]:
    archive = _open(content)
    names = set(archive.namelist())
    if "[Content_Types].xml" not in names or "word/document.xml" not in names:
        raise UnsafeDocx("DOCUMENT_INVALID")
    found = DocxFindings(core_properties="docProps/core.xml" in names)
    content_types = _read(archive, "[Content_Types].xml").lower()
    if any(n.lower().endswith("vbaproject.bin") for n in names) or b"macroenabled" in content_types:
        found.active.add("macro")
    if any(n.startswith(_EMBEDDED_PREFIXES) for n in names):
        found.active.add("embedded_object")
    for name in names:
        if name.endswith(".rels"):
            for rel in _xml(archive, name).iter(f"{REL}Relationship"):
                kind = rel.get("Type", "").rsplit("/", 1)[-1]
                if rel.get("TargetMode") == "External":
                    if kind in _DANGEROUS_REL_TYPES:
                        found.active.add("external_template" if kind == "attachedTemplate" else "external_object")
                    elif kind == "hyperlink":
                        found.passive.add("external_hyperlink")
    return archive, found


def _is_hidden(run_props: Element | None) -> bool:
    if run_props is None:
        return False
    vanish = run_props.find(f"{W}vanish")
    return vanish is not None and vanish.get(f"{W}val", "true") not in ("0", "false", "off")


def _run_text(run: Element, found: DocxFindings) -> tuple[str, bool]:
    """Visible text of a run and whether it ends a page (rendered or explicit break)."""
    if _is_hidden(run.find(f"{W}rPr")):
        found.passive.add("hidden_text")
        return "", False
    parts: list[str] = []
    page_break = False
    for node in run:
        tag = node.tag
        if tag == f"{W}t":
            parts.append(node.text or "")
        elif tag == f"{W}tab":
            parts.append("\t")
        elif tag in (f"{W}br", f"{W}cr"):
            if node.get(f"{W}type") == "page":
                page_break = True
            else:
                parts.append("\n")
        elif tag == f"{W}delText":
            found.passive.add("tracked_deletions")
    return "".join(parts), page_break


def extract_docx(content: bytes, *, reject_active_content: bool = True) -> DocxText:
    archive, found = inspect_docx(content)
    if reject_active_content and found.active:
        raise UnsafeDocx("ACTIVE_CONTENT_REJECTED", found)
    body = _xml(archive, "word/document.xml").find(f"{W}body")
    if body is None:
        raise UnsafeDocx("DOCUMENT_INVALID", found)

    pages: list[list[str]] = [[]]
    explicit = False

    def new_page() -> None:
        nonlocal explicit
        explicit = True
        if pages[-1]:
            pages.append([])

    def paragraph(p: Element) -> str:
        props = p.find(f"{W}pPr")
        if props is not None and props.find(f"{W}pageBreakBefore") is not None:
            new_page()
        text_parts: list[str] = []
        for node in p.iter():
            if node.tag == f"{W}lastRenderedPageBreak":
                new_page()
            elif node.tag == f"{W}r":
                text, brk = _run_text(node, found)
                text_parts.append(text)
                if brk:
                    pages[-1].append("".join(text_parts))
                    text_parts.clear()
                    new_page()
        if props is not None:
            sect = props.find(f"{W}sectPr")
            kind = sect.find(f"{W}type") if sect is not None else None
            starts_next = sect is not None and (kind is None or kind.get(f"{W}val") in ("nextPage", None))
            if starts_next:
                pages[-1].append("".join(text_parts))
                text_parts.clear()
                new_page()
        return "".join(text_parts)

    def table(t: Element) -> list[str]:
        rows = []
        for tr in t.iter(f"{W}tr"):
            cells = []
            for tc in tr.findall(f"{W}tc"):
                cells.append(" ".join(x for x in (paragraph(p).strip() for p in tc.iter(f"{W}p")) if x))
            rows.append(" | ".join(cells))
        return rows

    for child in body:
        if child.tag == f"{W}p":
            line = paragraph(child)
            if line.strip() or pages[-1] and not line:
                pages[-1].append(line)
        elif child.tag == f"{W}tbl":
            pages[-1].extend(table(child))

    texts = ["\n".join(lines).strip() for lines in pages]
    texts = [t for t in texts if t]
    if not texts:
        raise UnsafeDocx("NO_TEXT", found)
    if explicit or sum(len(t) for t in texts) <= ESTIMATED_PAGE_CHARS:
        return DocxText(texts, "explicit", found)
    return DocxText(_estimated_pages("\n".join(texts)), "estimated", found)


def _estimated_pages(text: str) -> list[str]:
    pages: list[str] = []
    current = ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > ESTIMATED_PAGE_CHARS:
            pages.append(current.strip())
            current = ""
        current += line + "\n"
    if current.strip():
        pages.append(current.strip())
    return pages


def describe(findings: DocxFindings) -> dict[str, Any]:
    return {"active": sorted(findings.active), "passive": sorted(findings.passive),
            "metadata": ["core_properties"] if findings.core_properties else []}
