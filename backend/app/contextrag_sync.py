"""Reliable, observable delivery of DOCMIND documents to ContextRAG_Engine.

Transactional outbox: every DOCMIND change that affects what ContextRAG may retrieve writes a
``contextrag_events`` row in the *same* transaction (processing completed, deleted, expired, purged).
A Celery beat task delivers pending events later. Guarantees, and how they are enforced:

* **Nothing is lost** if the process dies between the state change and the delivery: the row exists.
* **Idempotent enqueueing**: ``dedupe_key`` is unique and inserts use ``ON CONFLICT DO NOTHING``.
  An upsert key includes a digest of the stored page text, so re-processing with changed content
  creates a new event, while an unchanged re-run creates nothing.
* **Idempotent delivery**: ContextRAG replaces documents by ``doc_id``; a repeated upsert is a no-op
  for unchanged pages, and a repeated retire finds nothing left to remove.
* **No resurrection**: an upsert is rebuilt from the database when it is delivered. If the document was
  deleted, or its content changed (a newer event exists), the event is cancelled and nothing is sent.
* **A successful HTTP call is not treated as success for deletion**: a retire is only ``done`` once
  ContextRAG lists no page of the document any more.
* **Recovery**: events stuck in ``processing`` after a worker restart are released after a timeout;
  claims are fenced by ``started_at`` so a stale worker cannot finish someone else's claim.
* **Visibility**: status, attempts and the last *error code* (never a message with content) are
  readable by organization administrators through ``GET /api/v1/contextrag/events``.
"""

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.core import settings
from app.db import SessionLocal
from app.ingestion.pii import PiiMasker
from app.integrations.contextrag import (
    ContextRagClient,
    ContextRagRejected,
    ContextRagUnavailable,
    HandoffRefused,
    RetireEvent,
    build_handoff,
    load_tenant_keys,
    new_correlation_id,
)
from app.models import ContextRagEvent, Document, DocumentPage, DocumentVersion, utcnow

logger = logging.getLogger("docmind.contextrag_sync")

UPSERT, RETIRE = "upsert", "retire"
PENDING, PROCESSING, DONE, FAILED, CANCELLED = (
    "pending",
    "processing",
    "done",
    "failed",
    "cancelled",
)

STALE_AFTER = timedelta(minutes=15)  # a claim older than this belongs to a dead worker
MAX_BACKOFF = timedelta(hours=1)
FAILED_RETRY = timedelta(hours=6)  # slow retry for events that exhausted their attempts


def enabled() -> bool:
    cfg = settings()
    return bool(cfg.contextrag_sync_enabled and cfg.contextrag_url and cfg.contextrag_keys_file)


def build_client() -> ContextRagClient:
    """The only place credentials are read. Tests replace it; production builds it from settings."""
    cfg = settings()
    if not cfg.contextrag_url or not cfg.contextrag_keys_file:
        raise ContextRagRejected(0, "NOT_CONFIGURED")
    hosts = tuple(h.strip() for h in cfg.contextrag_allowed_hosts.split(",") if h.strip())
    return ContextRagClient(
        cfg.contextrag_url, load_tenant_keys(cfg.contextrag_keys_file), allowed_hosts=hosts
    )


# --------------------------------------------------------------------------- enqueueing (same transaction)
def _pages_digest(db: Session, document_id: uuid.UUID) -> str:
    """Digest of the stored page text. Identifies *content*, reveals nothing about it."""
    texts = db.scalars(
        select(DocumentPage.text)
        .where(DocumentPage.document_id == document_id)
        .order_by(DocumentPage.page_number)
    ).all()
    hasher = hashlib.sha256()
    for text in texts:
        hasher.update((text or "").encode())
        hasher.update(b"\x00")
    return hasher.hexdigest()[:32]


def _insert_if_absent(db: Session, values: dict[str, Any]) -> None:
    dialect = db.get_bind().dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    db.execute(
        insert(ContextRagEvent)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
    )
    # An event that gave up earlier is reopened when the change is requested again (e.g. after the
    # operator fixed the key); a done or cancelled event is never reopened.
    db.execute(
        update(ContextRagEvent)
        .where(ContextRagEvent.dedupe_key == values["dedupe_key"], ContextRagEvent.status == FAILED)
        .values(status=PENDING, attempts=0, next_attempt_at=utcnow(), last_error=None)
    )


def enqueue_upsert(db: Session, organization_id: uuid.UUID, document_id: uuid.UUID) -> None:
    """Record that the document's current content should be in ContextRAG. Call in the state-changing transaction."""
    version = (
        db.scalar(
            select(DocumentVersion.version)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version.desc())
            .limit(1)
        )
        or 1
    )
    digest = _pages_digest(db, document_id)
    now = utcnow()
    _insert_if_absent(
        db,
        {
            "id": uuid.uuid4(),
            "organization_id": organization_id,
            "document_id": document_id,
            "kind": UPSERT,
            "dedupe_key": f"upsert:{document_id}:v{version}:{digest}",
            "status": PENDING,
            "attempts": 0,
            "next_attempt_at": now,
            "correlation_id": new_correlation_id(),
            "created_at": now,
            "updated_at": now,
        },
    )


def enqueue_retire(db: Session, organization_id: uuid.UUID, document_id: uuid.UUID) -> None:
    """Record that every page of the document must leave ContextRAG. Idempotent; call in the same transaction."""
    now = utcnow()
    _insert_if_absent(
        db,
        {
            "id": uuid.uuid4(),
            "organization_id": organization_id,
            "document_id": document_id,
            "kind": RETIRE,
            "dedupe_key": f"retire:{document_id}",
            "status": PENDING,
            "attempts": 0,
            "next_attempt_at": now,
            "correlation_id": new_correlation_id(),
            "created_at": now,
            "updated_at": now,
        },
    )


# --------------------------------------------------------------------------- delivery
@dataclass(frozen=True)
class _Claim:
    id: uuid.UUID
    kind: str
    organization_id: uuid.UUID
    document_id: uuid.UUID
    dedupe_key: str
    correlation_id: str
    attempts: int
    token: str


def _release_stale(db: Session, now: datetime) -> int:
    result = cast(
        CursorResult[Any],
        db.execute(
            update(ContextRagEvent)
            .where(
                ContextRagEvent.status == PROCESSING, ContextRagEvent.started_at < now - STALE_AFTER
            )
            .values(
                status=PENDING, next_attempt_at=now, last_error="WORKER_RESTART", claim_token=None
            )
        ),
    )
    return int(result.rowcount or 0)


def _claim_due(db: Session, now: datetime, limit: int) -> list[_Claim]:
    busy_documents = select(ContextRagEvent.document_id).where(ContextRagEvent.status == PROCESSING)
    rows = db.scalars(
        select(ContextRagEvent)
        .where(
            ContextRagEvent.status.in_([PENDING, FAILED]),
            ContextRagEvent.next_attempt_at <= now,
            ContextRagEvent.document_id.not_in(busy_documents),  # one event per document at a time
        )
        .order_by(ContextRagEvent.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    claims: list[_Claim] = []
    seen: set[uuid.UUID] = set()
    for row in rows:
        if row.document_id in seen:  # two due events for one document: deliver the older first
            continue
        seen.add(row.document_id)
        token = uuid.uuid4().hex
        row.status = PROCESSING
        row.started_at = now
        row.claim_token = token
        claims.append(
            _Claim(
                row.id,
                row.kind,
                row.organization_id,
                row.document_id,
                row.dedupe_key,
                row.correlation_id,
                row.attempts,
                token,
            )
        )
    return claims


def _verify_retired(client: ContextRagClient, claim: _Claim) -> None:
    remaining = client.pages_of(
        str(claim.organization_id), str(claim.document_id), claim.correlation_id
    )
    if remaining:
        raise ContextRagUnavailable("VERIFY_FAILED")


def _deliver(claim: _Claim, client_factory: Any) -> tuple[str, str | None]:
    """Returns (outcome, error_code). outcome: done | cancelled | retry | failed. No database transaction is open."""
    if claim.kind == RETIRE:
        client = client_factory()
        client.retire(
            RetireEvent(
                tenant_id=str(claim.organization_id),
                document_id=str(claim.document_id),
                correlation_id=claim.correlation_id,
                reason="deleted",
            )
        )
        _verify_retired(client, claim)  # an HTTP 200 is not proof: check what is still retrievable
        return DONE, None
    # Upsert: rebuild from the database *now*. A deleted document or superseded content is never sent.
    with SessionLocal() as db:
        document = db.get(Document, claim.document_id)
        if document is None or document.deleted_at is not None:
            return CANCELLED, "DOCUMENT_DELETED"
        if claim.dedupe_key.split(":")[-1] != _pages_digest(db, claim.document_id):
            return CANCELLED, "SUPERSEDED"  # content changed: a newer upsert event exists
        cfg = settings()
        try:
            handoff = build_handoff(
                db,
                claim.organization_id,
                claim.document_id,
                correlation_id=claim.correlation_id,
                mask_pii=cfg.contextrag_content_mode == "masked",
                masker=PiiMasker(tenant_id=str(claim.organization_id)),
            )
        except HandoffRefused as exc:
            return CANCELLED, exc.code
    client_factory().upsert(handoff)
    return DONE, None


def _finalize(
    claim: _Claim, outcome: str, error: str | None, now: datetime, max_attempts: int
) -> None:
    with SessionLocal.begin() as db:
        row = db.get(ContextRagEvent, claim.id)
        # Fencing: only the claim holding the current token may finish the event.
        if row is None or row.status != PROCESSING or row.claim_token != claim.token:
            return
        row.updated_at = now
        row.claim_token = None
        if outcome == DONE:
            row.status, row.done_at, row.last_error = DONE, now, None
        elif outcome == CANCELLED:
            row.status, row.last_error, row.done_at = CANCELLED, error, now
        else:
            attempts = claim.attempts + 1
            row.attempts = attempts
            row.last_error = error
            if outcome == FAILED or attempts >= max_attempts:
                row.status = FAILED
                row.next_attempt_at = now + FAILED_RETRY
            else:
                row.status = PENDING
                row.next_attempt_at = now + min(
                    MAX_BACKOFF, timedelta(minutes=2 ** min(attempts, 6))
                )
    logger.info(
        "contextrag event finished",
        extra={
            "event_id": str(claim.id),
            "kind": claim.kind,
            "outcome": outcome,
            "error_code": error,
        },
    )


def _release_unstarted(claims: list[_Claim], now: datetime) -> int:
    """Give back claims that were never attempted: no attempt is counted, nothing waits for the stale timeout."""
    if not claims:
        return 0
    with SessionLocal.begin() as db:
        released = 0
        for claim in claims:
            row = db.get(ContextRagEvent, claim.id)
            if row is not None and row.status == PROCESSING and row.claim_token == claim.token:
                row.status, row.claim_token, row.started_at = PENDING, None, None
                row.next_attempt_at = now
                released += 1
        return released


def process_due(
    *,
    now: datetime | None = None,
    limit: int = 50,
    client_factory: Any = None,
    max_rounds: int = 10,
    budget_seconds: float = 120.0,
    clock: Any = time.monotonic,
) -> dict[str, int]:
    """Deliver due events. Safe to run concurrently and after restarts.

    Several events for one document are delivered in order within one pass: a superseded upsert is
    cancelled and the newer one follows immediately, instead of waiting for the next beat tick.

    A pass has a wall-clock budget. Celery kills a task that exceeds ``processing_timeout_seconds``
    (300 s by default), and each event may wait up to the HTTP timeout on a slow peer, so an unbounded
    pass could be killed mid-way on every tick and never make progress. When the budget runs out, the
    claims that were not attempted are returned to pending without counting an attempt."""
    now = now or utcnow()
    result = {"done": 0, "cancelled": 0, "retried": 0, "failed": 0, "released": 0, "deferred": 0}
    if not enabled():
        return result
    factory = client_factory or build_client
    deadline = clock() + budget_seconds
    with SessionLocal.begin() as db:
        result["released"] = _release_stale(db, now)
    for _ in range(max_rounds):
        if clock() >= deadline:
            break
        with SessionLocal.begin() as db:
            claims = _claim_due(db, now, limit)
        if not claims:
            break
        for index, claim in enumerate(claims):
            if clock() >= deadline:
                result["deferred"] += _release_unstarted(claims[index:], now)
                return result
            try:
                outcome, error = _deliver(claim, factory)
            except ContextRagUnavailable as exc:
                outcome, error = "retry", str(exc)[:80]
            except ContextRagRejected as exc:
                # 4xx or a missing key: retrying the same request will not help until a person fixes it.
                outcome, error = (
                    FAILED,
                    exc.code
                    if exc.code in ("NO_KEY_FOR_TENANT", "NOT_CONFIGURED", "BAD_RESPONSE")
                    else f"HTTP_{exc.status}",
                )
            except Exception as exc:  # one event must never stop the batch; only the type is kept
                logger.exception("contextrag delivery error", extra={"event_id": str(claim.id)})
                outcome, error = "retry", type(exc).__name__[:80]
            _finalize(claim, outcome, error, now, settings().contextrag_max_attempts)
            key = {DONE: "done", CANCELLED: "cancelled", "retry": "retried", FAILED: "failed"}[
                outcome
            ]
            result[key] += 1
    return result


def reconcile(*, limit: int = 500) -> dict[str, int]:
    """Find inconsistencies: live documents whose current content has no upsert event, and deleted
    documents with no retire event (e.g. changes made before the outbox existed). Enqueues the missing
    events; delivery is left to ``process_due``."""
    created = {"upsert": 0, "retire": 0}
    with SessionLocal.begin() as db:
        live = db.execute(
            select(Document.id, Document.organization_id)
            .where(Document.deleted_at.is_(None))
            .order_by(Document.created_at.desc())
            .limit(limit)
        ).all()
        for document_id, org_id in live:
            has_pages = db.scalar(
                select(func.count())
                .select_from(DocumentPage)
                .where(DocumentPage.document_id == document_id)
            )
            if not has_pages:
                continue
            before = _count(db, document_id, UPSERT)
            enqueue_upsert(db, org_id, document_id)
            created["upsert"] += _count(db, document_id, UPSERT) - before
        deleted = db.execute(
            select(Document.id, Document.organization_id)
            .where(Document.deleted_at.is_not(None))
            .order_by(Document.deleted_at.desc())
            .limit(limit)
        ).all()
        for document_id, org_id in deleted:
            before = _count(db, document_id, RETIRE)
            enqueue_retire(db, org_id, document_id)
            created["retire"] += _count(db, document_id, RETIRE) - before
    return created


def _count(db: Session, document_id: uuid.UUID, kind: str) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(ContextRagEvent)
            .where(ContextRagEvent.document_id == document_id, ContextRagEvent.kind == kind)
        )
        or 0
    )


def status_summary(db: Session, organization_id: uuid.UUID, limit: int = 20) -> dict[str, Any]:
    """Counts by status and the most recent problems, for administrators. No document content."""
    counts = dict(
        db.execute(
            select(ContextRagEvent.status, func.count())
            .where(ContextRagEvent.organization_id == organization_id)
            .group_by(ContextRagEvent.status)
        ).all()
    )
    recent = db.scalars(
        select(ContextRagEvent)
        .where(
            ContextRagEvent.organization_id == organization_id,
            ContextRagEvent.status.in_([FAILED, PENDING]),
        )
        .order_by(ContextRagEvent.updated_at.desc())
        .limit(limit)
    ).all()
    return {
        "enabled": enabled(),
        "counts": {k: int(v) for k, v in counts.items()},
        "problems": [
            {
                "event_id": str(e.id),
                "kind": e.kind,
                "document_id": str(e.document_id),
                "status": e.status,
                "attempts": e.attempts,
                "last_error": e.last_error,
                "next_attempt_at": e.next_attempt_at,
            }
            for e in recent
        ],
    }


# --------------------------------------------------------------------------- administrative inspection and cancellation
class OutboxAdminError(Exception):
    """A refused administrative action. ``code`` is stable and content-free."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


CANCELLED_BY_ADMIN = "CANCELLED_BY_ADMIN"


def list_events(
    db: Session, organization_id: uuid.UUID, status: str | None, limit: int
) -> list[dict[str, Any]]:
    """Identifiers, status and error codes only. Never document text."""
    query = select(ContextRagEvent).where(ContextRagEvent.organization_id == organization_id)
    if status is not None:
        query = query.where(ContextRagEvent.status == status)
    rows = db.scalars(query.order_by(ContextRagEvent.updated_at.desc()).limit(limit)).all()
    return [
        {
            "event_id": str(e.id),
            "kind": e.kind,
            "document_id": str(e.document_id),
            "status": e.status,
            "attempts": e.attempts,
            "last_error": e.last_error,
            "next_attempt_at": e.next_attempt_at,
            "updated_at": e.updated_at,
            "cancellable": e.kind == UPSERT and e.status in (FAILED, PENDING),
        }
        for e in rows
    ]


def cancel_upsert(
    db: Session, organization_id: uuid.UUID, event_id: uuid.UUID, *, confirm: str, reason: str
) -> dict[str, Any]:
    """Cancel one failed or pending **upsert** of this organization. The caller has already checked admin rights.

    * Deletions (``retire``) are never cancellable: cancelling one would leave a deleted document retrievable.
    * ``processing`` and final events are refused; the update is conditional, so a claim taken at the same
      moment wins and the cancellation is refused instead of racing it.
    * ``confirm`` must equal the event id, so a stray request with a copied identifier does nothing.
    """
    if confirm != str(event_id):
        raise OutboxAdminError("CONFIRMATION_MISMATCH", 400)
    cleaned = reason.strip()
    if len(cleaned) < 10:
        raise OutboxAdminError("REASON_REQUIRED", 422)
    event = db.scalar(
        select(ContextRagEvent).where(
            ContextRagEvent.id == event_id, ContextRagEvent.organization_id == organization_id
        )
    )
    if event is None:  # another organization's identifier looks exactly like a missing one
        raise OutboxAdminError("EVENT_NOT_FOUND", 404)
    if event.kind != UPSERT:
        raise OutboxAdminError("RETIREMENT_CANNOT_BE_CANCELLED", 409)
    if event.status not in (FAILED, PENDING):
        raise OutboxAdminError("EVENT_NOT_CANCELLABLE", 409)
    previous = {"status": event.status, "attempts": event.attempts, "last_error": event.last_error}
    now = utcnow()
    result = cast(
        CursorResult[Any],
        db.execute(
            update(ContextRagEvent)
            .where(
                ContextRagEvent.id == event_id,
                ContextRagEvent.organization_id == organization_id,
                ContextRagEvent.kind == UPSERT,
                ContextRagEvent.status.in_([FAILED, PENDING]),
            )
            .values(
                status=CANCELLED,
                last_error=CANCELLED_BY_ADMIN,
                done_at=now,
                claim_token=None,
                updated_at=now,
            ),
        ),
    )
    if int(result.rowcount or 0) != 1:  # changed under us (a claim or another cancellation)
        raise OutboxAdminError("EVENT_NOT_CANCELLABLE", 409)
    return {
        "event_id": str(event_id),
        "previous": previous,
        "reason": cleaned,
        "document_id": str(event.document_id),
    }
