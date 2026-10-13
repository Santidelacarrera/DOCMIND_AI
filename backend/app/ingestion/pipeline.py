"""Orchestration: PDF bytes -> masked, tenant-bound vectors in the store."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.core import settings
from app.ingestion import chunking, docx, pdf, pii, text, vectorstore
from app.ingestion.embeddings import EgressPolicy, Embedder
from app.ocr import TesseractProvider, needs_ocr
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
        tenant = chunking.parse_tenant(tenant_id)
        masked = self._masker(tenant).mask(question).text
        return self.embedder.embed([masked])[0]

    # ------------------------------------------------------------------ stages
    def _extract(self, clean: bytes, report: IngestionReport) -> list[str]:
        try:
            pages = extract_pdf_pages(clean)
        except ProcessingError as exc:
            raise IngestionRefused(exc.code) from None
        report.pages = len(pages)
        if needs_ocr("\n".join(pages), len(pages)):
            try:
                ocr = TesseractProvider().extract_pages(clean)
            except Exception:
                raise IngestionRefused("OCR_FAILED") from None
            pages = [ocr[i] if i < len(ocr) else "" for i in range(len(pages))]
            report.ocr_used = True
        return pages

    def _prepare(self, content: bytes, report: IngestionReport) -> list[str]:
        """Steps 1-3: structural sanitisation and text, for PDF and Word."""
        if docx.looks_like_docx(content):
            report.format = "docx"
            try:
                result = docx.extract_docx(content, reject_active_content=self.reject_active_content)
            except docx.UnsafeDocx as exc:
                raise IngestionRefused(exc.code) from None
            report.stripped = docx.describe(result.findings)
            report.page_basis, report.pages = result.page_basis, len(result.pages)
            return result.pages
        try:
            clean, findings = pdf.sanitize_pdf(content, reject_active_content=self.reject_active_content)
        except pdf.UnsafeDocument as exc:
            raise IngestionRefused(exc.code) from None
        report.stripped = {"active": sorted(findings.active), "passive": sorted(findings.passive),
                           "metadata_keys": findings.metadata_keys, "xmp": ["xmp"] if findings.has_xmp else []}
        return self._extract(clean, report)

    def ingest(self, *, tenant_id: str, document_id: str, content: bytes, source_name: str = "") -> IngestionReport:
        tenant = chunking.parse_tenant(tenant_id)  # raises before any work is done
        document = chunking.parse_tenant(document_id)
        report = IngestionReport(tenant, document)

        # 1-3. sanitise (no active content, no metadata survives) and extract text per page
        pages = self._prepare(content, report)

        # 4. PII masking *before* chunking, so no chunk boundary can split an identifier
        masker = self._masker(tenant)
        masked_pages: list[str] = []
        for page in pages:
            result = masker.mask(page)
            masked_pages.append(result.text)
            for kind, n in result.counts.items():
                report.pii[kind] = report.pii.get(kind, 0) + n

        # 5. chunk, bind to tenant
        windows = chunking.split_pages(masked_pages, max_chars=self.max_chars, overlap=self.overlap)
        if not windows:
            raise IngestionRefused("NO_TEXT")
        per_chunk = [masker.residual(w[0]) for w in windows]
        # 6. verify (fail closed): masked text must contain nothing the detector still recognises
        if any(per_chunk):
            raise IngestionRefused("PII_RESIDUAL_AFTER_MASKING")
        flags = [text.looks_like_injection(w[0]) for w in windows]
        chunks = chunking.build_chunks(
            tenant_id=tenant, document_id=document, windows=windows,
            source_name=source_name[:255], page_basis=report.page_basis,
            pii_counts=[pii.placeholder_counts(w[0]) for w in windows], suspicious=flags,
        )
        chunking.assert_single_tenant(chunks, tenant)
        report.chunks = len(chunks)
        report.suspicious_chunks = sum(flags)

        # 7-8. embed masked text only, write with the tenant key
        vectors = self.embedder.embed([c.text for c in chunks])
        points = [vectorstore.to_point(c, v, embedding_model=self.embedder.name) for c, v in zip(chunks, vectors, strict=True)]
        self.store.ensure_collection(self.embedder.dimension)
        self.store.delete_document(tenant, document)  # re-ingestion replaces, never duplicates
        self.store.upsert(tenant, points)
        logger.info("document ingested", extra={"tenant_id": tenant, "document_id": document,
                                                "chunks": report.chunks, "pii_kinds": sorted(report.pii)})
        return report
