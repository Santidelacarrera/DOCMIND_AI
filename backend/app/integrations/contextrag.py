"""Hand processed documents to ContextRAG_Engine -- an optional, one-way integration.

DOCMIND_AI extracts and protects the document; ContextRAG_Engine retrieves and answers. This
module is the whole boundary between them, written against the API that ContextRAG exposes
today (``POST /ingest``, ``GET /documents``, ``DELETE /documents/{doc_id}``, ``X-API-Key`` bound
to one tenant; see docs/CONTEXTRAG_INTEGRATION.md for the verified contract and what is missing).

Design rules:

* **Optional.** Nothing imports this unless ``CONTEXTRAG_URL`` is set; tests and the rest of the
  application never need ContextRAG.
* **The credential decides the tenant.** ContextRAG binds each API key to one tenant, so the
  client needs a key *per DOCMIND organization* and never sends ``X-Tenant``. A missing key
  fails closed; one tenant's text can never be written under another tenant's key.
* **Page references survive.** ContextRAG's text endpoint has no page coordinates, so every page
  is its own ContextRAG document named ``<document_id>:p<NNNN>``. A fragment's ``doc_id`` therefore
  identifies document *and* page (``parse_doc_id``).
* **Masked before leaving.** Text is passed through :class:`~app.ingestion.pii.PiiMasker` unless
  masking is explicitly disabled by the operator.
* **Updates and deletions are exact.** Upserting replaces each page atomically on the ContextRAG
  side and retires pages that no longer exist; ``retire`` removes every page of a document. Both
  discover the pages from ContextRAG itself, so they work even after DOCMIND's row is gone.
* **No content in logs or errors.**
"""

import json
import logging
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingestion.pii import PiiMasker
from app.models import Document, DocumentPage, DocumentVersion

logger = logging.getLogger("docmind.contextrag")

CONTRACT_VERSION: Literal["docmind-contextrag/1"] = "docmind-contextrag/1"
MAX_PAGE_CHARS = 200_000
_DOC_ID = re.compile(r"^(?P<document>[0-9a-f-]{36}):p(?P<page>\d{4})$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class HandoffRefused(Exception):
    """The document is not in a state that may be shared; ``code`` is stable and content-free."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ContextRagUnavailable(Exception):
    """Network failure, 429 or 5xx: worth retrying later."""


class ContextRagRejected(Exception):
    """ContextRAG answered 4xx: retrying the same request will not help."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code


class PagePayload(BaseModel):
    page: int = Field(ge=1)
    page_basis: Literal["physical", "estimated"]
    text: str


class HandoffDocument(BaseModel):
    """Contract v1: everything ContextRAG needs, nothing it should not have."""

    contract: Literal["docmind-contextrag/1"] = CONTRACT_VERSION
    event: Literal["upsert"] = "upsert"
    tenant_id: str  # DOCMIND organization id; informational -- the API key is the authority
    document_id: str
    version: int = Field(ge=1)
    content_sha256: str  # of the original file; lets a consumer detect "same file, new version"
    source_name: str
    status: Literal["processed"] = "processed"
    correlation_id: str
    acl: list[str] = []
    pages: list[PagePayload]
    metadata: dict[str, Any] = {}


class RetireEvent(BaseModel):
    contract: Literal["docmind-contextrag/1"] = CONTRACT_VERSION
    event: Literal["delete"] = "delete"
    tenant_id: str
    document_id: str
    correlation_id: str
    reason: Literal["deleted", "expired", "superseded"] = "deleted"


def page_doc_id(document_id: str, page: int) -> str:
    return f"{document_id}:p{page:04d}"


def parse_doc_id(doc_id: str) -> tuple[str, int] | None:
    """``(document_id, page)`` for a ContextRAG ``doc_id`` written by this module, else ``None``."""
    match = _DOC_ID.match(doc_id)
    return (match["document"], int(match["page"])) if match else None


def new_correlation_id() -> str:
    return f"docmind-{uuid.uuid4()}"


# --------------------------------------------------------------------------- building the handoff
def build_handoff(
    db: Session,
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    correlation_id: str | None = None,
    acl: tuple[str, ...] = (),
    mask_pii: bool = True,
    masker: PiiMasker | None = None,
) -> HandoffDocument:
    """Assemble the contract from stored data. Authorization is part of the query: the
    document must belong to ``organization_id``, be live, and have finished processing."""
    document = db.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.organization_id == organization_id,
            Document.deleted_at.is_(None),
        )
    )
    if document is None:
        raise HandoffRefused("DOCUMENT_NOT_FOUND")
    pages = list(
        db.scalars(
            select(DocumentPage)
            .where(DocumentPage.document_id == document.id)
            .order_by(DocumentPage.page_number)
        )
    )
    if not pages:
        raise HandoffRefused(
            "DOCUMENT_NOT_PROCESSED"
        )  # no extracted text yet (or processing failed)
    version = db.scalar(
        select(DocumentVersion.version)
        .where(DocumentVersion.document_id == document.id)
        .order_by(DocumentVersion.version.desc())
        .limit(1)
    )
    docx = document.mime_type.endswith("wordprocessingml.document")
    masker = masker or PiiMasker(tenant_id=str(organization_id))
    payload_pages: list[PagePayload] = []
    masked_total = 0
    for page in pages:
        text = (page.text or "")[:MAX_PAGE_CHARS]  # a page without extracted text is sent as empty (and skipped)
        if mask_pii:
            masked = masker.mask(text)
            text, masked_total = masked.text, masked_total + sum(masked.counts.values())
        payload_pages.append(
            PagePayload(
                page=page.page_number, page_basis="estimated" if docx else "physical", text=text
            )
        )
    correlation = correlation_id or new_correlation_id()
    if not _REQUEST_ID.match(correlation):
        raise HandoffRefused("CORRELATION_ID_INVALID")
    return HandoffDocument(
        tenant_id=str(organization_id),
        document_id=str(document.id),
        version=version or 1,
        content_sha256=document.checksum,
        source_name=document.filename,
        correlation_id=correlation,
        acl=list(acl),
        pages=payload_pages,
        metadata={
            "mime_type": document.mime_type,
            "page_count": len(pages),
            "ocr_used": any(p.ocr_used for p in pages),
            "pii_masked": mask_pii,
            "pii_items_masked": masked_total,
        },
    )


# --------------------------------------------------------------------------- the client
@dataclass(frozen=True)
class SyncResult:
    upserted: int
    unchanged: int
    skipped_empty: int
    retired: int


class ContextRagClient:
    """Thin, synchronous client. ``tenant_keys`` maps DOCMIND organization id -> ContextRAG admin key."""

    def __init__(
        self,
        base_url: str,
        tenant_keys: Mapping[str, str],
        *,
        allowed_hosts: tuple[str, ...] = (),
        client: httpx.Client | None = None,
        timeout: float = 15.0,
    ) -> None:
        parsed = urlparse(base_url)
        host = (parsed.hostname or "").lower()
        local = host in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme not in {"https", "http"} or not host:
            raise ValueError("CONTEXTRAG_URL_INVALID")
        if not local and (
            parsed.scheme != "https" or host not in {h.lower() for h in allowed_hosts}
        ):
            # Documents leave this system: plain HTTP or an unlisted host is refused.
            raise ValueError("CONTEXTRAG_HOST_NOT_ALLOWED")
        self._base = base_url.rstrip("/")
        self._keys = dict(tenant_keys)
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    # -- plumbing
    def _call(
        self, method: str, path: str, tenant_id: str, correlation_id: str, **kwargs: Any
    ) -> Any:
        key = self._keys.get(tenant_id)
        if not key:
            raise ContextRagRejected(
                0, "NO_KEY_FOR_TENANT"
            )  # fail closed: never fall back to another key
        headers = {"X-API-Key": key, "X-Request-Id": correlation_id}
        try:
            response = self._client.request(method, self._base + path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ContextRagUnavailable(type(exc).__name__) from None
        if response.status_code == 429 or response.status_code >= 500:
            raise ContextRagUnavailable(str(response.status_code))
        if not 200 <= response.status_code < 300:  # includes 3xx: redirects are never followed
            raise ContextRagRejected(response.status_code, "REJECTED")
        try:
            return response.json()
        except ValueError:
            raise ContextRagRejected(response.status_code, "BAD_RESPONSE") from None

    def health(self) -> bool:
        try:
            return self._client.get(self._base + "/healthz").status_code == 200
        except httpx.HTTPError:
            return False

    def pages_of(self, tenant_id: str, document_id: str, correlation_id: str) -> dict[int, str]:
        """ContextRAG doc ids of this document's pages, by page number (what is still retrievable)."""
        listing = self._call("GET", "/documents", tenant_id, correlation_id)
        found: dict[int, str] = {}
        for item in listing.get("documents", []):
            parsed = parse_doc_id(str(item.get("doc_id", "")))
            if parsed and parsed[0] == document_id:
                found[parsed[1]] = str(item["doc_id"])
        return found

    # -- events
    def upsert(self, handoff: HandoffDocument) -> SyncResult:
        """Write every page (ContextRAG replaces atomically and skips unchanged ones), then retire
        pages of a previous, longer version. Idempotent: safe to repeat after a failure."""
        tenant, cid = handoff.tenant_id, handoff.correlation_id
        upserted = unchanged = skipped = 0
        keep: set[int] = set()
        for page in handoff.pages:
            if not page.text.strip():
                skipped += 1  # ContextRAG refuses empty text; a blank page has nothing to retrieve
                continue
            body = {
                "documents": [
                    {
                        "doc_id": page_doc_id(handoff.document_id, page.page),
                        "text": page.text,
                        "source": f"{handoff.source_name} (v{handoff.version}, p.{page.page}"
                        + ("~" if page.page_basis == "estimated" else "")
                        + ")",
                        "kind": "text",
                        "acl": handoff.acl,
                    }
                ]
            }
            result = self._call("POST", "/ingest", tenant, cid, json=body)
            keep.add(page.page)
            if result["documents"][0].get("unchanged"):
                unchanged += 1
            else:
                upserted += 1
        retired = 0
        for number, doc_id in self.pages_of(tenant, handoff.document_id, cid).items():
            if number not in keep:
                self._call("DELETE", f"/documents/{doc_id}", tenant, cid)
                retired += 1
        logger.info(
            "contextrag upsert",
            extra={
                "document_id": handoff.document_id,
                "correlation_id": cid,
                "pages": len(keep),
                "retired": retired,
            },
        )
        return SyncResult(upserted, unchanged, skipped, retired)

    def retire(self, event: RetireEvent) -> int:
        """Remove every page of the document for the tenant. Returns how many pages were removed."""
        pages = self.pages_of(event.tenant_id, event.document_id, event.correlation_id)
        for doc_id in pages.values():
            self._call("DELETE", f"/documents/{doc_id}", event.tenant_id, event.correlation_id)
        logger.info(
            "contextrag retire",
            extra={
                "document_id": event.document_id,
                "correlation_id": event.correlation_id,
                "pages": len(pages),
            },
        )
        return len(pages)


def load_tenant_keys(path: str) -> dict[str, str]:
    """``{"<organization uuid>": "<contextrag admin key>"}`` from a file kept outside the repository."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return {str(k): str(v) for k, v in data.items()}
