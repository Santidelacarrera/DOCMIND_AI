"""Multi-format document inspection, sanitisation and page-oriented text extraction.

Supports:
* Plain text / Markdown (``.txt``, ``.md``)
* Delimited tables (``.csv``)
* Excel workbooks (``.xlsx`` -- streamed read-only, macros and external links rejected)
* PowerPoint decks (``.pptx`` -- XML parsed via defusedxml, slides + speaker notes + tables)
* Raster images (``.png``, ``.jpg``, ``.jpeg``, ``.webp``, ``.tif``, ``.tiff`` -- OCR via Tesseract)

PDF and DOCX keep their dedicated modules (``pdf.py``, ``docx.py``) and are routed through
:func:`detect_format` / :func:`extract_document`. All modules keep the same canonical output:
a list of page strings, explicit vs estimated ``page_basis``, sanitisation findings and optional
image frames for OCR.
"""

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

TXT_MIME = "text/plain"
MD_MIME = "text/markdown"
CSV_MIME = "text/csv"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PNG_MIME = "image/png"
JPEG_MIME = "image/jpeg"
WEBP_MIME = "image/webp"
TIFF_MIME = "image/tiff"

IMAGE_MIMES = frozenset({PNG_MIME, JPEG_MIME, WEBP_MIME, TIFF_MIME})
SUPPORTED_EXTENSIONS = frozenset({
    ".pdf", ".docx", ".txt", ".md", ".csv", ".xlsx", ".pptx",
    ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff",
})

ESTIMATED_PAGE_CHARS = 3_000
MAX_ZIP_ENTRIES = 4_000
MAX_ZIP_ENTRY_BYTES = 40 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 150 * 1024 * 1024
MAX_ZIP_RATIO = 200
MAX_CSV_ROWS = 50_000
MAX_CSV_COLS = 200
MAX_XLSX_ROWS_PER_PAGE = 80
MAX_IMAGE_PIXELS = 25_000_000

_BINARY_SIGNATURES = (
    b"%PDF-",
    b"PK\x03\x04",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF8",
    b"\x7fELF",
    b"\xd0\xcf\x11\xe0",
    b"MZ",
)
_DANGEROUS_REL_TYPES = ("attachedTemplate", "oleObject", "frame", "package", "externalLink")
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_DRAW_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")


class UnsafeFormat(ValueError):
    """Malformed, oversized, unsupported or active-content-bearing input. ``code`` is stable."""

    def __init__(self, code: str, findings: dict[str, list[str]] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.findings = findings or {}


@dataclass(frozen=True)
class FormatSpec:
    format_name: str  # pdf | docx | txt | md | csv | xlsx | pptx | image
    mime_type: str
    extension: str


@dataclass
class ExtractedContent:
    format_name: str
    mime_type: str
    pages: list[str]
    page_basis: str = "explicit"  # "explicit" | "estimated"
    tables_detected: int = 0
    stripped: dict[str, list[str]] = field(default_factory=dict)
    ocr_images: list[Any] = field(default_factory=list)


def detect_format(filename: str | None, content: bytes) -> FormatSpec:
    """Identify the file format by extension AND magic bytes, failing closed on mismatch."""
    name = (filename or "").strip().lower()
    dot = name.rfind(".")
    ext = name[dot:] if dot != -1 else ""

    if ext == ".pdf" and content.startswith(b"%PDF-"):
        return FormatSpec("pdf", "application/pdf", ".pdf")
    if ext == ".docx" and content[:4] == b"PK\x03\x04":
        return FormatSpec("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx")
    if ext == ".xlsx" and content[:4] == b"PK\x03\x04":
        return FormatSpec("xlsx", XLSX_MIME, ".xlsx")
    if ext == ".pptx" and content[:4] == b"PK\x03\x04":
        return FormatSpec("pptx", PPTX_MIME, ".pptx")
    if ext in (".txt", ".md"):
        _assert_plain_text_bytes(content)
        return FormatSpec("md" if ext == ".md" else "txt", MD_MIME if ext == ".md" else TXT_MIME, ext)
    if ext == ".csv":
        _assert_plain_text_bytes(content)
        return FormatSpec("csv", CSV_MIME, ".csv")
    if ext == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"):
        return FormatSpec("image", PNG_MIME, ".png")
    if ext in (".jpg", ".jpeg") and content.startswith(b"\xff\xd8\xff"):
        return FormatSpec("image", JPEG_MIME, ext)
    if ext == ".webp" and len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return FormatSpec("image", WEBP_MIME, ".webp")
    if ext in (".tif", ".tiff") and content[:4] in (b"II*\x00", b"MM\x00*"):
        return FormatSpec("image", TIFF_MIME, ext)
    raise UnsafeFormat("UNSUPPORTED_FILE_TYPE")


def format_from_mime(mime_type: str) -> str:
    if mime_type == "application/pdf":
        return "pdf"
    if mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return "docx"
    if mime_type in (TXT_MIME, MD_MIME):
        return "txt"
    if mime_type == CSV_MIME:
        return "csv"
    if mime_type == XLSX_MIME:
        return "xlsx"
    if mime_type == PPTX_MIME:
        return "pptx"
    if mime_type in IMAGE_MIMES:
        return "image"
    return "unknown"


def _assert_plain_text_bytes(content: bytes) -> None:
    if not content:
        raise UnsafeFormat("DOCUMENT_INVALID")
    head = content[:8192]
    if any(head.startswith(sig) for sig in _BINARY_SIGNATURES):
        raise UnsafeFormat("UNSUPPORTED_FILE_TYPE")
    if b"\x00" in head or b"\x00" in content:
        raise UnsafeFormat("DOCUMENT_INVALID")


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("cp1252", errors="replace")


def _split_estimated_pages(text: str, *, max_chars: int = ESTIMATED_PAGE_CHARS) -> tuple[list[str], str]:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        raise UnsafeFormat("NO_TEXT")
    if len(cleaned) <= max_chars:
        return [cleaned], "explicit"
    pages: list[str] = []
    current = ""
    for line in cleaned.split("\n"):
        if current and len(current) + len(line) + 1 > max_chars:
            pages.append(current.strip())
            current = ""
        while len(line) > max_chars:
            if current:
                pages.append(current.strip())
                current = ""
            pages.append(line[:max_chars].strip())
            line = line[max_chars:]
        current += line + "\n"
    if current.strip():
        pages.append(current.strip())
    return [p for p in pages if p], "estimated"


def extract_txt(content: bytes, *, mime_type: str = TXT_MIME, max_chars: int = 500_000) -> ExtractedContent:
    _assert_plain_text_bytes(content)
    decoded = _decode_text(content)[:max_chars]
    pages, basis = _split_estimated_pages(decoded)
    fmt = "md" if mime_type == MD_MIME else "txt"
    return ExtractedContent(fmt, mime_type, pages, page_basis=basis)


def extract_csv(content: bytes, *, max_chars: int = 500_000) -> ExtractedContent:
    _assert_plain_text_bytes(content)
    decoded = _decode_text(content)[:max_chars]
    sample = decoded[:4096]
    delimiter = ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        for candidate in (";", "\t", "|", ","):
            if candidate in sample:
                delimiter = candidate
                break

    reader = csv.reader(io.StringIO(decoded), delimiter=delimiter)
    rows: list[list[str]] = []
    for idx, row in enumerate(reader):
        if idx >= MAX_CSV_ROWS:
            break
        trimmed = [cell.strip() for cell in row[:MAX_CSV_COLS]]
        if any(trimmed):
            rows.append(trimmed)
    if not rows:
        raise UnsafeFormat("NO_TEXT")

    header = " | ".join(rows[0])
    body_rows = [" | ".join(r) for r in rows[1:]] or [header]
    pages: list[str] = []
    current_lines: list[str] = [header] if len(rows) > 1 else []
    current_len = len(header) + 1 if len(rows) > 1 else 0
    for line in body_rows:
        if len(current_lines) > 1 and (
            current_len + len(line) + 1 > ESTIMATED_PAGE_CHARS or len(current_lines) >= MAX_XLSX_ROWS_PER_PAGE + 1
        ):
            pages.append("\n".join(current_lines))
            current_lines = [header] if len(rows) > 1 else []
            current_len = len(header) + 1 if len(rows) > 1 else 0
        current_lines.append(line)
        current_len += len(line) + 1
    if current_lines:
        pages.append("\n".join(current_lines))
    basis = "explicit" if len(pages) == 1 else "estimated"
    return ExtractedContent("csv", CSV_MIME, pages, page_basis=basis, tables_detected=1)


def _open_safe_zip(content: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError, ValueError):
        raise UnsafeFormat("DOCUMENT_INVALID") from None
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise UnsafeFormat("OOXML_TOO_MANY_ENTRIES")
    total = 0
    for info in infos:
        total += info.file_size
        if info.file_size > MAX_ZIP_ENTRY_BYTES or total > MAX_ZIP_TOTAL_BYTES:
            raise UnsafeFormat("OOXML_TOO_LARGE_UNCOMPRESSED")
        if info.compress_size and info.file_size / info.compress_size > MAX_ZIP_RATIO and info.file_size > 1_000_000:
            raise UnsafeFormat("OOXML_COMPRESSION_RATIO")
        if info.filename.startswith(("/", "..")) or ".." in info.filename.split("/"):
            raise UnsafeFormat("OOXML_PATH_TRAVERSAL")
        if info.flag_bits & 0x1:
            raise UnsafeFormat("DOCUMENT_ENCRYPTED")
    return archive


def _read_zip_entry(archive: zipfile.ZipFile, name: str) -> bytes:
    with archive.open(name) as handle:
        data = handle.read(MAX_ZIP_ENTRY_BYTES + 1)
    if len(data) > MAX_ZIP_ENTRY_BYTES:
        raise UnsafeFormat("OOXML_TOO_LARGE_UNCOMPRESSED")
    return data


def _parse_zip_xml(archive: zipfile.ZipFile, name: str) -> Element:
    try:
        return SafeET.fromstring(_read_zip_entry(archive, name))
    except (DefusedXmlException, SafeET.ParseError):
        raise UnsafeFormat("OOXML_XML_INVALID") from None


def _inspect_ooxml(
    content: bytes,
    *,
    required_entry: str,
    embedded_prefixes: tuple[str, ...],
    reject_active_content: bool = True,
) -> tuple[zipfile.ZipFile, dict[str, list[str]]]:
    archive = _open_safe_zip(content)
    names = set(archive.namelist())
    if "[Content_Types].xml" not in names or not any(
        n == required_entry or n.startswith(required_entry) for n in names
    ):
        raise UnsafeFormat("DOCUMENT_INVALID")
    active: set[str] = set()
    passive: set[str] = set()
    content_types = _read_zip_entry(archive, "[Content_Types].xml").lower()
    if any(n.lower().endswith("vbaproject.bin") for n in names) or b"macroenabled" in content_types:
        active.add("macro")
    if any(n.startswith(embedded_prefixes) for n in names):
        active.add("embedded_object")
    for name in names:
        if name.endswith(".rels"):
            for rel in _parse_zip_xml(archive, name).iter(f"{_REL_NS}Relationship"):
                kind = rel.get("Type", "").rsplit("/", 1)[-1]
                if rel.get("TargetMode") == "External":
                    if kind in _DANGEROUS_REL_TYPES:
                        active.add("external_template" if kind == "attachedTemplate" else "external_object")
                    elif kind == "hyperlink":
                        passive.add("external_hyperlink")
    findings = {
        "active": sorted(active),
        "passive": sorted(passive),
        "metadata": ["core_properties"] if "docProps/core.xml" in names else [],
    }
    if reject_active_content and active:
        raise UnsafeFormat("ACTIVE_CONTENT_REJECTED", findings)
    return archive, findings


def extract_xlsx(
    content: bytes, *, reject_active_content: bool = True, max_chars: int = 500_000
) -> ExtractedContent:
    from openpyxl import load_workbook

    _archive, findings = _inspect_ooxml(
        content,
        required_entry="xl/workbook.xml",
        embedded_prefixes=("xl/embeddings/", "xl/activeX/", "customXml/activeX/"),
        reject_active_content=reject_active_content,
    )
    _archive.close()
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:
        raise UnsafeFormat("DOCUMENT_INVALID", findings) from None

    pages: list[str] = []
    tables_detected = 0
    total_chars = 0
    try:
        for sheet in wb.worksheets:
            title_line = f"[Sheet: {sheet.title}]"
            header_line: str | None = None
            chunk_lines: list[str] = [title_line]
            row_count = 0
            sheet_has_data = False
            for row in sheet.iter_rows(values_only=True):
                cells = [str(c).strip() if c is not None else "" for c in row[:MAX_CSV_COLS]]
                if not any(cells):
                    continue
                sheet_has_data = True
                line = " | ".join(cells)
                if header_line is None:
                    header_line = line
                if len(chunk_lines) > 2 and (
                    row_count >= MAX_XLSX_ROWS_PER_PAGE
                    or sum(len(x) + 1 for x in chunk_lines) + len(line) > ESTIMATED_PAGE_CHARS
                ):
                    page_text = "\n".join(chunk_lines)
                    pages.append(page_text)
                    total_chars += len(page_text)
                    if total_chars >= max_chars:
                        break
                    chunk_lines = [title_line, header_line]
                    row_count = 0
                chunk_lines.append(line)
                row_count += 1
            if sheet_has_data:
                tables_detected += 1
                if len(chunk_lines) > 1 and total_chars < max_chars:
                    page_text = "\n".join(chunk_lines)
                    pages.append(page_text)
                    total_chars += len(page_text)
            if total_chars >= max_chars:
                break
    finally:
        wb.close()

    if not pages:
        raise UnsafeFormat("NO_TEXT", findings)
    return ExtractedContent(
        "xlsx",
        XLSX_MIME,
        [p[:max_chars] for p in pages],
        page_basis="explicit" if len(pages) == tables_detected else "estimated",
        tables_detected=tables_detected,
        stripped=findings,
    )


def _pptx_paragraphs(node: Element) -> list[str]:
    lines: list[str] = []
    for p in node.iter(f"{_DRAW_NS}p"):
        runs = [t.text for t in p.iter(f"{_DRAW_NS}t") if t.text]
        line = "".join(runs).strip()
        if line:
            lines.append(line)
    return lines


def _pptx_tables(root: Element) -> tuple[list[str], int]:
    rows: list[str] = []
    table_count = 0
    for tbl in root.iter(f"{_DRAW_NS}tbl"):
        table_count += 1
        for tr in tbl.findall(f"{_DRAW_NS}tr"):
            cells = []
            for tc in tr.findall(f"{_DRAW_NS}tc"):
                cells.append(" ".join(_pptx_paragraphs(tc)))
            if any(cells):
                rows.append(" | ".join(cells))
    return rows, table_count


def extract_pptx(
    content: bytes, *, reject_active_content: bool = True, max_chars: int = 500_000
) -> ExtractedContent:
    archive, findings = _inspect_ooxml(
        content,
        required_entry="ppt/presentation.xml",
        embedded_prefixes=("ppt/embeddings/", "ppt/activeX/", "customXml/activeX/"),
        reject_active_content=reject_active_content,
    )
    try:
        names = set(archive.namelist())
        slides: list[tuple[int, str]] = []
        for name in names:
            m = _SLIDE_RE.match(name)
            if m:
                slides.append((int(m.group(1)), name))
        slides.sort(key=lambda item: item[0])
        if not slides:
            raise UnsafeFormat("DOCUMENT_INVALID", findings)

        pages: list[str] = []
        tables_detected = 0
        for slide_num, slide_path in slides:
            root = _parse_zip_xml(archive, slide_path)
            # Extract text excluding table cells first, then append tables cleanly.
            lines: list[str] = []
            for p in root.iter(f"{_DRAW_NS}p"):
                runs = [t.text for t in p.iter(f"{_DRAW_NS}t") if t.text]
                line = "".join(runs).strip()
                if line:
                    lines.append(line)
            table_lines, tbl_cnt = _pptx_tables(root)
            tables_detected += tbl_cnt
            for tl in table_lines:
                if tl not in lines:
                    lines.append(tl)

            # Check slide relationships for speaker notes.
            rels_path = f"ppt/slides/_rels/slide{slide_num}.xml.rels"
            if rels_path in names:
                for rel in _parse_zip_xml(archive, rels_path).iter(f"{_REL_NS}Relationship"):
                    target = rel.get("Target", "")
                    if "notesSlide" in target and rel.get("TargetMode") != "External":
                        resolved = "ppt/" + target.lstrip("./").replace("../", "")
                        if resolved in names:
                            notes_root = _parse_zip_xml(archive, resolved)
                            notes = _pptx_paragraphs(notes_root)
                            if notes:
                                lines.append("[Notes] " + " ".join(notes))

            slide_text = "\n".join(lines).strip()[:max_chars]
            if slide_text:
                pages.append(slide_text)
    finally:
        archive.close()

    if not pages:
        raise UnsafeFormat("NO_TEXT", findings)
    return ExtractedContent(
        "pptx",
        PPTX_MIME,
        pages,
        page_basis="explicit",
        tables_detected=tables_detected,
        stripped=findings,
    )


def load_image_frames(content: bytes, *, mime_type: str, max_pages: int = 25) -> list[Any]:
    """Validate and decode raster image bytes (PNG/JPEG/WEBP/TIFF) with decompression-bomb bounds."""
    from PIL import Image, ImageSequence, UnidentifiedImageError

    previous_max = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(io.BytesIO(content)) as probe:
            probe.verify()
        img = Image.open(io.BytesIO(content))
        frames: list[Any] = []
        for idx, frame in enumerate(ImageSequence.Iterator(img)):
            if idx >= max_pages:
                break
            # Convert to clean RGB/L copy (drops EXIF/ICC/metadata payloads).
            clean = frame.convert("RGB")
            frames.append(clean)
        if not frames:
            raise UnsafeFormat("DOCUMENT_INVALID")
        return frames
    except UnsafeFormat:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        raise UnsafeFormat("DOCUMENT_INVALID") from None
    finally:
        Image.MAX_IMAGE_PIXELS = previous_max


def extract_structured_document(
    content: bytes,
    *,
    mime_type: str,
    reject_active_content: bool = True,
    max_chars: int = 500_000,
    max_ocr_pages: int = 25,
) -> ExtractedContent:
    """Extract non-PDF/non-DOCX formats into canonical pages or image frames for OCR."""
    if mime_type in (TXT_MIME, MD_MIME):
        return extract_txt(content, mime_type=mime_type, max_chars=max_chars)
    if mime_type == CSV_MIME:
        return extract_csv(content, max_chars=max_chars)
    if mime_type == XLSX_MIME:
        return extract_xlsx(content, reject_active_content=reject_active_content, max_chars=max_chars)
    if mime_type == PPTX_MIME:
        return extract_pptx(content, reject_active_content=reject_active_content, max_chars=max_chars)
    if mime_type in IMAGE_MIMES:
        frames = load_image_frames(content, mime_type=mime_type, max_pages=max_ocr_pages)
        return ExtractedContent("image", mime_type, [""] * len(frames), page_basis="explicit", ocr_images=frames)
    raise UnsafeFormat("UNSUPPORTED_FILE_TYPE")


def count_pdf_tables(pages: list[str]) -> int:
    """Heuristic count of table-like multi-column blocks in extracted PDF text."""
    tables = 0
    for page in pages:
        in_table = 0
        for line in page.split("\n"):
            stripped = line.strip()
            if "|" in stripped or "\t" in line or re.search(r"\S {3,}\S", stripped):
                in_table += 1
                if in_table == 2:
                    tables += 1
            else:
                in_table = 0
    return tables


def strip_running_headers_footers(pages: list[str]) -> list[str]:
    """Remove identical running header/footer lines repeated across >= 3 pages of a PDF."""
    if len(pages) < 3:
        return pages
    first_lines: dict[str, int] = {}
    last_lines: dict[str, int] = {}
    for p in pages:
        lines = [ln.strip() for ln in p.split("\n") if ln.strip()]
        if not lines:
            continue
        first_lines[lines[0]] = first_lines.get(lines[0], 0) + 1
        if len(lines) > 1:
            last_lines[lines[-1]] = last_lines.get(lines[-1], 0) + 1
    threshold = max(3, len(pages) // 2 + 1)
    bad_first = {ln for ln, count in first_lines.items() if count >= threshold and len(ln) <= 120}
    bad_last = {ln for ln, count in last_lines.items() if count >= threshold and len(ln) <= 120}
    if not bad_first and not bad_last:
        return pages
    cleaned: list[str] = []
    for p in pages:
        lines = p.split("\n")
        non_empty_indices = [i for i, ln in enumerate(lines) if ln.strip()]
        if non_empty_indices and lines[non_empty_indices[0]].strip() in bad_first:
            lines[non_empty_indices[0]] = ""
        if len(non_empty_indices) > 1 and lines[non_empty_indices[-1]].strip() in bad_last:
            lines[non_empty_indices[-1]] = ""
        cleaned.append("\n".join(lines).strip())
    return cleaned
