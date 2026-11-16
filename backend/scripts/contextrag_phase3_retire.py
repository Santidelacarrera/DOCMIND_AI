"""Diagnostic, backup exporter and Phase 3 retirement verifier for ``contextrag_events``.

Follows ``docs/CONTEXTRAG_SYNC_RETIREMENT_PLAN.md``:
1. If ``contextrag_events`` still exists on an unmigrated database, inspects row counts by status
   and exports a deterministic JSON backup + SHA-256 checksum file before migration ``0013_drop_contextrag_events``.
2. If migration ``0013_drop_contextrag_events`` has already run, verifies ``table_exists=False`` and
   writes a retirement certificate JSON + SHA-256 when ``--backup-dir`` is supplied.
"""

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def inspect_and_backup(backup_dir: Path | None = None) -> dict[str, Any]:
    from sqlalchemy import inspect, text

    from app.db import SystemSessionLocal, engine

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    table_exists = "contextrag_events" in tables
    total = 0
    by_status: dict[str, int] = {}
    serialized: list[dict[str, Any]] = []

    if table_exists:
        with SystemSessionLocal() as db:
            total = int(db.scalar(text("SELECT count(*) FROM contextrag_events")) or 0)
            status_rows = db.execute(
                text("SELECT status, count(*) FROM contextrag_events GROUP BY status")
            ).all()
            by_status = {str(s): int(c) for s, c in status_rows}
            rows = db.execute(
                text(
                    "SELECT id, organization_id, document_id, kind, dedupe_key, status, attempts, "
                    "last_error, correlation_id, created_at, done_at "
                    "FROM contextrag_events ORDER BY created_at ASC"
                )
            ).mappings().all()
            serialized = [dict(r) for r in rows]

    backup_path_str: str | None = None
    sha256_hex: str | None = None
    if backup_dir is not None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = backup_dir / f"contextrag_events_backup_{stamp}.json"
        payload = json.dumps(
            {
                "exported_at": stamp,
                "table_exists": table_exists,
                "total_rows": total,
                "by_status": by_status,
                "events": serialized,
            },
            indent=2,
            default=str,
        ).encode("utf-8")
        target.write_bytes(payload)
        sha256_hex = hashlib.sha256(payload).hexdigest()
        (backup_dir / f"contextrag_events_backup_{stamp}.json.sha256").write_text(
            f"{sha256_hex}  {target.name}\n", encoding="utf-8"
        )
        backup_path_str = str(target)

    return {
        "table_exists": table_exists,
        "total_rows": total,
        "by_status": by_status,
        "backup_file": backup_path_str,
        "backup_sha256": sha256_hex,
        "phase3_drop_executed": not table_exists,
        "status": "PHASE3_COMPLETED_TABLE_DROPPED" if not table_exists else "BACKUP_EXPORTED_READY_FOR_0013",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-dir", type=Path, default=None, help="directory to write JSON backup + SHA-256")
    args = parser.parse_args()
    report = inspect_and_backup(args.backup_dir)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
