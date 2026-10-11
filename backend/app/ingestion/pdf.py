"""PDF inspection and sanitisation.

``inspect_pdf`` reports what a PDF carries beyond its visible text; ``sanitize_pdf`` rebuilds
the file from page content only, dropping everything an attacker or a careless author can
hide in the container: document JavaScript, open / additional actions, launch and URI
actions, embedded files, XFA forms, annotations (incl. file attachments) and the entire
metadata (Info dictionary and XMP).
"""

import io
from dataclasses import dataclass, field
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError
from pypdf.generic import ArrayObject, DictionaryObject, NameObject

# Findings that make a document unacceptable when ``reject_active_content`` is on.
ACTIVE = frozenset({"javascript", "launch_action", "embedded_file", "xfa"})


class UnsafeDocument(ValueError):
    """The PDF is malformed, encrypted or contains active content the policy refuses."""

    def __init__(self, code: str, findings: "PdfFindings | None" = None) -> None:
        super().__init__(code)
        self.code = code
        self.findings = findings


@dataclass
class PdfFindings:
    pages: int = 0
    active: set[str] = field(default_factory=set)  # subset of ACTIVE
    passive: set[str] = field(default_factory=set)  # open_action, uri_action, annotations
    metadata_keys: list[str] = field(default_factory=list)  # names only, never values
    has_xmp: bool = False

    @property
    def is_active(self) -> bool:
        return bool(self.active)


def _resolve(node: Any) -> Any:
    """Follow an indirect reference (``dict.get`` returns them unresolved)."""
    return node.get_object() if hasattr(node, "get_object") else node


def _walk_actions(node: Any, found: PdfFindings, depth: int = 0) -> None:
    """Look at an /A or /OpenAction (and its /Next chain) for dangerous action types."""
    node = _resolve(node)
    if depth > 8 or not isinstance(node, DictionaryObject):
        return
    kind = str(node.get("/S", ""))
    if kind == "/JavaScript":
        found.active.add("javascript")
    elif kind in {"/Launch", "/GoToE", "/ImportData", "/SubmitForm"}:
        found.active.add("launch_action")
    elif kind == "/URI":
        found.passive.add("uri_action")
    nxt = _resolve(node.get("/Next"))
    for item in nxt if isinstance(nxt, ArrayObject) else [nxt]:
        _walk_actions(item, found, depth + 1)


def inspect_pdf(content: bytes) -> PdfFindings:
    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise UnsafeDocument("PDF_ENCRYPTED")
        found = PdfFindings(pages=len(reader.pages))
        root: Any = reader.trailer["/Root"]
        names = _resolve(root.get("/Names"))
        if isinstance(names, DictionaryObject):
            if "/JavaScript" in names:
                found.active.add("javascript")
            if "/EmbeddedFiles" in names:
                found.active.add("embedded_file")
        if "/AA" in root:
            found.passive.add("additional_actions")
            triggers = _resolve(root.get("/AA"))
            for trigger in triggers.values() if isinstance(triggers, DictionaryObject) else []:
                _walk_actions(trigger, found)
        if "/OpenAction" in root:
            found.passive.add("open_action")
            _walk_actions(root["/OpenAction"].get_object(), found)
        acro = _resolve(root.get("/AcroForm"))
        if isinstance(acro, DictionaryObject) and "/XFA" in acro:
            found.active.add("xfa")
        for page in reader.pages:
            if "/AA" in page:
                found.passive.add("additional_actions")
                triggers = _resolve(page.get("/AA"))
                for trigger in triggers.values() if isinstance(triggers, DictionaryObject) else []:
                    _walk_actions(trigger, found)
            for annot in _resolve(page.get("/Annots")) or []:
                annot = _resolve(annot)
                if not isinstance(annot, DictionaryObject):
                    continue
                found.passive.add("annotations")
                if str(annot.get("/Subtype")) == "/FileAttachment":
                    found.active.add("embedded_file")
                _walk_actions(annot.get("/A"), found)
        info = _resolve(reader.trailer.get("/Info"))
        if isinstance(info, DictionaryObject):
            found.metadata_keys = sorted(str(k) for k in info)
        found.has_xmp = "/Metadata" in root
        return found
    except UnsafeDocument:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError, AttributeError, RecursionError, OSError):
        raise UnsafeDocument("DOCUMENT_INVALID") from None


def sanitize_pdf(content: bytes, *, reject_active_content: bool = True) -> tuple[bytes, PdfFindings]:
    """Return a PDF rebuilt from page content only, plus what the original contained."""
    findings = inspect_pdf(content)
    if findings.pages < 1:
        raise UnsafeDocument("DOCUMENT_INVALID", findings)
    if reject_active_content and findings.is_active:
        raise UnsafeDocument("ACTIVE_CONTENT_REJECTED", findings)
    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
        writer = PdfWriter()
        for page in reader.pages:
            new = writer.add_page(page)
            for key in ("/Annots", "/AA", "/Metadata", "/PieceInfo", "/Thumb"):
                if key in new:
                    del new[NameObject(key)]
        writer.add_metadata({})  # empty Info dictionary: no author, producer, dates, titles
        out = io.BytesIO()
        writer.write(out)
    except (PyPdfError, ValueError, KeyError, TypeError, AttributeError, OSError):
        raise UnsafeDocument("DOCUMENT_INVALID", findings) from None
    return out.getvalue(), findings
