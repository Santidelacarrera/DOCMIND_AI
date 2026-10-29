"""Stamp ``project_id`` on vectors indexed before project-scoped queries existed.

Idempotent: safe to run again. It changes only vector payloads of live, indexed documents, reading
the project from the database. It does not re-embed, does not touch deleted documents and needs no
schema migration (``Document.project_id`` already exists).

    cd backend && python scripts/backfill_vector_projects.py

Requires QDRANT_URL and DATABASE_URL. Prints JSON counts. Exit status 1 only when a document failed to
stamp. ``unscoped_points_remaining`` also counts vectors of deleted documents still waiting in the
deletion outbox, so it can be non-zero right after a deletion; it is informational.
"""

import json
import sys

from sqlalchemy import select

from app import vector_index
from app.db import SessionLocal
from app.models import Document


def main() -> int:
    if not vector_index.enabled():
        print(json.dumps({"status": "skipped", "reason": "QDRANT_URL not set"}))
        return 0
    report = vector_index.backfill_project_ids()
    with SessionLocal() as db:
        orgs = set(db.scalars(select(Document.organization_id).where(
            Document.deleted_at.is_(None), Document.indexed_at.is_not(None))).all())
    remaining = {str(org): vector_index.unscoped_points(str(org)) for org in orgs}
    report["unscoped_points_remaining"] = sum(remaining.values())
    print(json.dumps(report))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
