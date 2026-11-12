"""Helpers shared by the API routers (moved out of app.main, unchanged)."""

import logging
import re
import unicodedata
import uuid
from urllib.parse import quote

from fastapi import (
    HTTPException,
    Query,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Document,
    User,
)
from app.security import (
    ApiPrincipal,
)

logger = logging.getLogger("docmind")


def audit(
    db: Session,
    action: str,
    target_type: str,
    target_id: str | None,
    actor: User | ApiPrincipal | None = None,
    org_id: uuid.UUID | None = None,
) -> None:
    db.add(
        AuditLog(
            organization_id=org_id,
            actor_id=actor.id if isinstance(actor, User) else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata_={"api_key_id": str(actor.key.id)} if isinstance(actor, ApiPrincipal) else {},
        )
    )


def organization_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(400, "Invalid organization id")


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ \-()\[\]]")


def sanitize_filename(raw: str | None) -> str:
    """Reduce a client-supplied name to a harmless display name (no paths, no control chars)."""
    name = unicodedata.normalize("NFKC", raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip().strip(".")
    return name[:255] or "document.pdf"


def content_disposition(filename: str, inline: bool = False) -> str:
    """RFC 6266 header value that cannot be broken out of by quotes or newlines."""
    ascii_name = _SAFE_NAME.sub("_", filename.encode("ascii", "ignore").decode()) or "document.pdf"
    disposition = "inline" if inline else "attachment"
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


_FORMULA_PREFIXES = ("=", "+", "-", "@", chr(9), chr(13), chr(10))


def neutralize_formula(value: object) -> object:
    """Prevent CSV/XLSX formula injection by forcing spreadsheet apps to treat text as text."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


Name = Query(min_length=1, max_length=160)
MAX_SCHEMA_BYTES = 64 * 1024
MAX_FIELD_VALUE_BYTES = 64 * 1024


def live_document(db: Session, org_id: uuid.UUID, document_id: uuid.UUID) -> Document:
    item = db.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.organization_id == org_id,
            Document.deleted_at.is_(None),
        )
    )
    if not item:
        raise HTTPException(404, "DOCUMENT_NOT_FOUND")
    return item
