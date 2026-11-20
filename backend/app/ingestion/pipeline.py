"""Orchestration: document bytes -> masked, tenant-bound vectors in the store."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.core import settings
from app.ingestion import chunking, docx, formats, pdf, pii, text, vectorstore
from app.ingestion.embeddings import EgressPolicy, Embedder
from app.ocr import TesseractProvider, needs_ocr, pages_needing_ocr
from app.processing import ProcessingError, extract_pdf_pages

logger = logging.getLogger("docmind.ingestion")


class IngestionRefused(Exception):
    """The document must not be indexed; ``code`` is stable and safe to surface."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class IngestionReport:
    """What happened -- counts and names only, never document content or PII values."""

    tenant_id: str
    document_id: str
    pages: int = 0
    format: str = "pdf"
    page_basis: str = "explicit"
    ocr_used: bool = False
    chunks: int = 0
    pii: dict[str, int] = field(default_factory=dict)
    suspicious_chunks: int = 0
    stripped: dict[str, list[str]] = field(default_factory=dict)  # what the sanitiser removed
    tables_detected: int = 0


@dataclass
class IngestionPipeline:
    store: vectorstore.VectorStore
    embedder: Embedder
    masker_extra: Sequence[pii.EntityDetector] = ()
    reject_active_content: bool = True
    max_chars: int = 1200
    overlap: int = 150
    egress: EgressPolicy | None = None

    def __post_init__(self) -> None:
        # Refuse an unauthorised external embedder now, before any document is read.
        (self.egress or EgressPolicy.from_settings(settings())).check(self.embedder)

    @classmethod
    def from_settings(cls, store: vectorstore.VectorStore, embedder: Embedder) -> "IngestionPipeline":
        cfg = settings()
        from app.ingestion.entities import build_detectors

        return cls(store, embedder, masker_extra=build_detectors(cfg),
                   reject_active_content=cfg.ingest_reject_active_content,
                   max_chars=cfg.ingest_chunk_chars, overlap=cfg.ingest_chunk_overlap)

    def _masker(self, tenant: str) -> pii.PiiMasker:
        key = settings().pii_masking_key
        return pii.PiiMasker(key=key.get_secret_value().encode() if key else None, tenant_id=tenant,
                             extra=self.masker_extra)

    def query_vector(self, *, tenant_id: str, question: str) -> list[float]:
        """Embed a search query. It is masked exactly like documents: a question such as
        "contracts of maria@x.com" must not send that address to an external embedder."""
        self.store.assert_model(self.embedder.name)  # never query one vector space against another
        return self.embedder.embed([self.mask_query(tenant_id=tenant_id, question=question)])[0]

    def mask_query(self, *, tenant_id: str, question: str) -> str:
        """The question masked as documents are: no PII reaches an embedder or the lexical index. With
        ``PII_MASKING_KEY`` placeholders are keyed per tenant, so a masked value in the question equals
        its placeholder in the indexed chunks; without it they are numbered and only the rest matches."""
        return self._masker(chunking.parse_tenant(tenant_id)).mask(question).text

    # ------------------------------------------------------------------ stages
    def _extract(self, clean: bytes, report: IngestionReport) -> list[str]:
        try:
            pages = extract_pdf_pages(clean)
        except ProcessingError as exc:
            raise IngestionRefused(exc.code) from None
        report.pages = len(pages)
        mixed_pages = pages_needing_ocr(pages)
        if needs_ocr("\n".join(pages), len(pages)):
            try:
                ocr = TesseractProvider().extract_pages(clean)
            except Exception:
                raise IngestionRefused("OCR_FAILED") from None
            pages = [ocr[i] if i < len(ocr) else "" for i in range(len(pages))]
            report.ocr_used = True
        elif mixed_pages and len(mixed_pages) < len(pages):
            try:
                ocr = TesseractProvider().extract_pages(clean, page_numbers=mixed_pages)
            except Exception:
                raise IngestionRefused("OCR_FAILED") from None
            for idx, page_no in enumerate(mixed_pages):
                if 1 <= page_no <= len(pages) and idx < len(ocr):
                    pages[page_no - 1] = ocr[idx]
            report.ocr_used = True
        pages = formats.strip_running_headers_footers(pages)
        report.tables_detected = formats.count_pdf_tables(pages)
        return pages

    def _detect_mime(self, content: bytes, source_name: str, mime_type: str | None) -> str:
        if mime_type:
            return mime_type
        if source_name:
            try:
                return formats.detect_format(source_name, content).mime_type
            except formats.UnsafeFormat:
                pass
        if docx.looks_like_docx(content):
            return docx.DOCX_MIME
        if content.startswith(b"%PDF-"):
            return "application/pdf"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return formats.PNG_MIME
        if content.startswith(b"\xff\xd8\xff"):
            return formats.JPEG_MIME
        return "application/pdf"

    def _prepare(
        self, content: bytes, report: IngestionReport, *, source_name: str = "", mime_type: str | None = None
    ) -> list[str]:
        """Steps 1-3: structural sanitisation and per-page text across all supported formats."""
        resolved_mime = self._detect_mime(content, source_name, mime_type)
        if resolved_mime == docx.DOCX_MIME:
            report.format = "docx"
            try:
                result = docx.extract_docx(content, reject_active_content=self.reject_active_content)
            except docx.UnsafeDocx as exc:
                raise IngestionRefused(exc.code) from None
            report.stripped = docx.describe(result.findings)
            report.page_basis, report.pages = result.page_basis, len(result.pages)
            return result.pages
        if resolved_mime != "application/pdf":
            cfg = settings()
            try:
                extracted = formats.extract_structured_document(
                    content,
                    mime_type=resolved_mime,
                    reject_active_content=self.reject_active_content,
                    max_chars=cfg.max_pdf_text_chars,
                    max_ocr_pages=cfg.max_ocr_pages,
                )
            except formats.UnsafeFormat as exc:
                raise IngestionRefused(exc.code) from None
            report.format = extracted.format_name
            report.page_basis = extracted.page_basis
            report.tables_detected = extracted.tables_detected
            report.stripped = extracted.stripped
            if extracted.ocr_images:
                try:
                    pages = TesseractProvider().extract_images(extracted.ocr_images)
                except Exception:
                    raise IngestionRefused("OCR_FAILED") from None
                report.ocr_used = True
                report.pages = len(pages)
                return pages
            report.pages = len(extracted.pages)
            return extracted.pages
        try:
            clean, findings = pdf.sanitize_pdf(content, reject_active_content=self.reject_active_content)
        except pdf.UnsafeDocument as exc:
            raise IngestionRefused(exc.code) from None
        report.stripped = {"active": sorted(findings.active), "passive": sorted(findings.passive),
                           "metadata_keys": findings.metadata_keys, "xmp": ["xmp"] if findings.has_xmp else []}
        return self._extract(clean, report)

    def ingest(
        self, *, tenant_id: str, document_id: str, content: bytes, source_name: str = "",
        project_id: str | None = None, mime_type: str | None = None,
    ) -> IngestionReport:
        tenant = chunking.parse_tenant(tenant_id)  # raises before any work is done
        document = chunking.parse_tenant(document_id)
        report = IngestionReport(tenant, document)

        # 1-3. sanitise (no active content, no metadata survives) and extract text per page
        pages = self._prepare(content, report, source_name=source_name, mime_type=mime_type)

        # 4. PII masking *before* chunking, so no chunk boundary can split an identifier
        masker = self._masker(tenant)
        masked_pages: list[str] = []
        for page in pages:
            result = masker.mask(page)
            masked_pages.append(result.text)
            for kind, n in result.counts.items():
                report.pii[kind] = report.pii.get(kind, 0) + n

        # 5. structure-aware chunking (preserves tables & section headings), bind to tenant
        windows = chunking.split_pages_structured(masked_pages, max_chars=self.max_chars, overlap=self.overlap)
        if not windows:
            raise IngestionRefused("NO_TEXT")
        per_chunk = [masker.residual(w.text) for w in windows]
        # 6. verify (fail closed): masked text must contain nothing the detector still recognises
        if any(per_chunk):
            raise IngestionRefused("PII_RESIDUAL_AFTER_MASKING")
        flags = [text.looks_like_injection(w.text) for w in windows]
        chunks = chunking.build_chunks(
            tenant_id=tenant, document_id=document, windows=windows,
            source_name=source_name[:255], page_basis=report.page_basis,
            pii_counts=[pii.placeholder_counts(w.text) for w in windows], suspicious=flags,
            project_id=project_id,
        )
        chunking.assert_single_tenant(chunks, tenant)
        report.chunks = len(chunks)
        report.suspicious_chunks = sum(flags)

        # 7-8. embed masked text (context-enriched with section_path when present), write with tenant key
        vectors = self.embedder.embed([c.embedding_text for c in chunks])
        points = [vectorstore.to_point(c, v, embedding_model=self.embedder.name) for c, v in zip(chunks, vectors, strict=True)]
        self.store.ensure_collection(self.embedder.dimension, model=self.embedder.name)
        self.store.delete_document(tenant, document)  # re-ingestion replaces, never duplicates
        self.store.upsert(tenant, points)
        logger.info("document ingested", extra={"tenant_id": tenant, "document_id": document,
                                                "chunks": report.chunks, "pii_kinds": sorted(report.pii)})
        return report
