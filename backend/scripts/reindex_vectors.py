"""Rebuild the vector index from the stored documents, without calling the LLM.

    cd backend && python scripts/reindex_vectors.py [--organization-id <uuid>]

Use it after losing or replacing the Qdrant collection, or to move to another embedding model on a
new ``QDRANT_COLLECTION``. Extractions and reviews are not touched. Requires DATABASE_URL, QDRANT_URL
and the storage settings of the API. Prints JSON counts and the elapsed time; exit status 1 if any
document failed for a transient reason (run it again: it is idempotent).
"""

import argparse
import json
import sys
import time
import uuid

from app import vector_index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization-id", type=uuid.UUID, default=None, help="only this organization")
    args = parser.parse_args(argv)
    if not vector_index.enabled():
        print(json.dumps({"status": "skipped", "reason": "QDRANT_URL not set"}))
        return 0
    started = time.perf_counter()
    report = vector_index.reindex_all(organization_id=args.organization_id)
    print(json.dumps({**report, "seconds": round(time.perf_counter() - started, 1)}))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
