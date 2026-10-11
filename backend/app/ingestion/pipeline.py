"""Orchestration: PDF bytes -> masked, tenant-bound vectors in the store."""

import hashlib
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.core import settings
from app.ingestion import chunking, pdf, pii, text, vectorstore
from app.ocr import TesseractProvider, needs_ocr
from app.processing import ProcessingError, extract_pdf_pages

logger = logging.getLogger("docmind.ingestion")


class Embedder(Protocol):
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministic bag-of-words hashing embedder. **Not semantic** -- for tests, local
    development and wiring checks only; production uses a real model behind ``Embedder``."""

    def __init__(self, dimension: int = 256) -> None:
        self.dimension = dimension

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            vec = [0.0] * self.dimension
            for word in t.lower().split():
                vec[int(hashlib.md5(word.encode(), usedforsecurity=False).hexdigest(), 16) % self.dimension] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


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

    @classmethod
    def from_settings(cls, store: vectorstore.VectorStore, embedder: Embedder) -> "IngestionPipeline":
        cfg = settings()
        return cls(store, embedder, reject_active_content=cfg.ingest_reject_active_content,
                   max_chars=cfg.ingest_chunk_chars, overlap=cfg.ingest_chunk_overlap)

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

    def ingest(self, *, tenant_id: str, document_id: str, content: bytes) -> IngestionReport:
        tenant = chunking.parse_tenant(tenant_id)  # raises before any work is done
        document = chunking.parse_tenant(document_id)
        report = IngestionReport(tenant, document)

        # 1. structural sanitisation: no active content, no metadata survives
        try:
            clean, findings = pdf.sanitize_pdf(content, reject_active_content=self.reject_active_content)
        except pdf.UnsafeDocument as exc:
            raise IngestionRefused(exc.code) from None
        report.stripped = {"active": sorted(findings.active), "passive": sorted(findings.passive),
                           "metadata_keys": findings.metadata_keys, "xmp": ["xmp"] if findings.has_xmp else []}

        # 2-3. text, normalised
        pages = self._extract(clean, report)

        # 4. PII masking *before* chunking, so no chunk boundary can split an identifier
        key = settings().pii_masking_key
        masker = pii.PiiMasker(key=key.get_secret_value().encode() if key else None, tenant_id=tenant,
                               extra=self.masker_extra)
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
            pii_counts=[pii.placeholder_counts(w[0]) for w in windows], suspicious=flags,
        )
        chunking.assert_single_tenant(chunks, tenant)
        report.chunks = len(chunks)
        report.suspicious_chunks = sum(flags)

        # 7-8. embed masked text only, write with the tenant key
        vectors = self.embedder.embed([c.text for c in chunks])
        points = [vectorstore.to_point(c, v) for c, v in zip(chunks, vectors, strict=True)]
        self.store.ensure_collection(self.embedder.dimension)
        self.store.delete_document(tenant, document)  # re-ingestion replaces, never duplicates
        self.store.upsert(tenant, points)
        logger.info("document ingested", extra={"tenant_id": tenant, "document_id": document,
                                                "chunks": report.chunks, "pii_kinds": sorted(report.pii)})
        return report
