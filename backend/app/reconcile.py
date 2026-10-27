"""Find (and optionally remove) stored objects that no database row refers to.

Orphans appear when a crash falls between writing an object and committing its row, or when
object deletion fails during a purge. They are personal data nobody can reach through the
API, so retention would otherwise never remove them.

Safety rules -- a wrong database must not be able to wipe the bucket:

* only keys shaped like ``<org-uuid>/<uuid>.(pdf|docx)`` are candidates; anything else is
  reported as ``unrecognised_key`` and never touched;
* objects younger than ``storage_reconcile_grace_hours`` are kept (in-flight uploads);
* deletion is off unless ``STORAGE_RECONCILE_DELETE=true``, and is refused when it would remove
  more than ``storage_reconcile_max_delete_fraction`` of all objects;
* ``dry_run`` classifies and lists every decision and deletes nothing, whatever the configuration.

Reports identify objects by a fingerprint (a truncated SHA-256 of the key), so an operator report
does not repeat tenant identifiers.

Run from beat (``app.worker.reconcile_storage``) or ``python -m app.reconcile [--dry-run|--delete]``.
"""

import argparse
import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core import settings
from app.db import SessionLocal
from app.models import Document, DocumentVersion
from app.storage import storage

logger = logging.getLogger("docmind.reconcile")

_KEY = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f-]{36}\.(pdf|docx)$"
)


def _fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def reconcile(
    *,
    delete: bool | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    decisions_out: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Classify every stored object; remove orphans only when deletion is enabled and the safety rules pass.

    ``decisions_out`` receives one entry per object: ``{"object": fingerprint, "action", "reason"}`` with
    ``action`` one of ``keep``, ``would_delete``, ``deleted``.
    """
    cfg = settings()
    delete = cfg.storage_reconcile_delete if delete is None else delete
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=cfg.storage_reconcile_grace_hours)

    objects = list(storage.list_objects())
    with SessionLocal() as db:
        known = set(db.scalars(select(Document.storage_key))) | set(
            db.scalars(select(DocumentVersion.storage_key))
        )
        # Objects of deleted documents are removed at deletion time (docs/DATA_RETENTION.md), so only live
        # documents can be "rows without object". Deleted rows still protect their keys from being orphans.
        live_keys = set(
            db.scalars(select(Document.storage_key).where(Document.deleted_at.is_(None)))
        ) | set(
            db.scalars(
                select(DocumentVersion.storage_key)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(Document.deleted_at.is_(None))
            )
        )

    decisions: list[dict[str, str]] = []
    orphans: list[str] = []
    unrecognised = 0
    for key, modified in objects:
        if key in known:
            action, reason = "keep", "referenced"
        elif not _KEY.match(key):
            action, reason = "keep", "unrecognised_key"  # reported, never deleted
            unrecognised += 1
        elif modified > cutoff:
            action, reason = "keep", "within_grace_period"  # may be an upload still committing
        else:
            action, reason = "candidate", "orphan"
            orphans.append(key)
        decisions.append({"object": _fingerprint(key), "action": action, "reason": reason})

    present = {key for key, _ in objects}
    summary: dict[str, Any] = {
        "mode": "dry_run" if dry_run else ("delete" if delete else "report"),
        "objects": len(objects),
        "orphans": len(orphans),
        "unrecognised": unrecognised,
        "rows_without_object": len([k for k in live_keys if k not in present]),
        "would_delete": 0,
        "deleted": 0,
        "refused": None,
    }

    if orphans and (dry_run or delete):
        if len(orphans) > cfg.storage_reconcile_max_delete_fraction * len(objects):
            # Mass orphaning looks like a wrong database, not garbage: remove nothing.
            summary["refused"] = "TOO_MANY_ORPHANS"
            logger.error(
                "reconcile refused to delete: orphans exceed the safety fraction", extra=summary
            )
            _relabel(decisions, orphans, "keep", "refused_by_safety_fraction")
        elif dry_run:
            summary["would_delete"] = len(orphans)
            _relabel(decisions, orphans, "would_delete", "orphan")
        else:
            for key in orphans:
                try:
                    storage.delete(key)
                    summary["deleted"] += 1
                    _relabel(decisions, [key], "deleted", "orphan")
                except Exception:
                    logger.warning("orphan removal failed")  # no key in logs: it names a tenant
                    _relabel(decisions, [key], "keep", "delete_failed")
    else:
        _relabel(decisions, orphans, "keep", "orphan_not_deleted_deletion_disabled")

    if decisions_out is not None:
        decisions_out.extend(decisions)
    logger.info("storage reconciled", extra={k: v for k, v in summary.items() if k != "refused"})
    return summary


def _relabel(decisions: list[dict[str, str]], keys: list[str], action: str, reason: str) -> None:
    wanted = {_fingerprint(key) for key in keys}
    for entry in decisions:
        if entry["object"] in wanted:
            entry["action"], entry["reason"] = action, reason


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report, simulate or delete storage objects without a database row."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="classify and list decisions; delete nothing"
    )
    parser.add_argument(
        "--delete", action="store_true", help="remove orphans (still subject to the safety rules)"
    )
    parser.add_argument(
        "--decisions", help="write one entry per object to this JSON file (fingerprints only)"
    )
    args = parser.parse_args()
    decisions: list[dict[str, str]] = []
    summary = reconcile(
        delete=True if args.delete else None, dry_run=args.dry_run, decisions_out=decisions
    )
    if args.decisions:
        with open(args.decisions, "w", encoding="utf-8") as handle:
            json.dump({"summary": summary, "decisions": decisions}, handle, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
