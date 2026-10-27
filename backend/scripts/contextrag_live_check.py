"""Live check: DOCMIND synchronises with a ContextRAG_Engine running as a separate HTTP process.

NOT part of CI. It needs (1) a DOCMIND API with the outbox (the e2e stack), (2) a worker that processes
uploads, and (3) a ContextRAG_Engine process reachable on --contextrag-url with the keys file used here.
Synthetic text only. The script enables sync in its own process, runs the real delivery code over
HTTP, stops ContextRAG for a failure drill, restarts it, and checks what is retrievable at each step.

    CONTEXTRAG_KEYS_FILE=/path/organization-keys.json  # written by this script's --write-keys
    python scripts/contextrag_live_check.py --docmind-api http://localhost:8000 \
        --contextrag-url http://127.0.0.1:8100 --keys-file /path/organization-keys.json \
        --contextrag-key admin-live --pause-contextrag "pkill -f 'uvicorn --factory contextrag'" \
        --resume-contextrag "contextrag-start-command &"
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docmind-api", default="http://localhost:8000")
    parser.add_argument("--contextrag-url", default="http://127.0.0.1:8100")
    parser.add_argument(
        "--keys-file", required=True, help="organization uuid -> ContextRAG admin key (JSON)"
    )
    parser.add_argument(
        "--contextrag-key", help="admin key of the tenant for this organization (plain text)"
    )
    parser.add_argument(
        "--pause-contextrag", help="shell command that stops ContextRAG for the failure drill"
    )
    parser.add_argument("--resume-contextrag", help="shell command that restarts ContextRAG")
    args = parser.parse_args()

    # Configure sync for this process only, before any app import reads the settings.
    os.environ["CONTEXTRAG_SYNC_ENABLED"] = "true"
    os.environ["CONTEXTRAG_URL"] = args.contextrag_url
    os.environ["CONTEXTRAG_KEYS_FILE"] = args.keys_file
    os.environ["CONTEXTRAG_ALLOWED_HOSTS"] = "127.0.0.1,localhost"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import contextrag_sync
    from app.core import settings
    from app.evaluation.pdfgen import digital_pdf

    results: dict[str, object] = {}
    api = httpx.Client(base_url=args.docmind_api, timeout=60)
    crag = httpx.Client(base_url=args.contextrag_url, timeout=30)

    email = f"live-{uuid.uuid4().hex[:8]}@example.com"
    password = "correct-horse-battery-staple"
    account = api.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "organization_name": "Live"},
    ).json()
    org = account["organization_id"]
    headers = {"Authorization": f"Bearer {account['access_token']}"}
    project = api.post(f"/api/v1/projects?organization_id={org}&name=live", headers=headers).json()[
        "id"
    ]
    Path(args.keys_file).write_text(
        json.dumps({org: args.contextrag_key or "admin-live"}, indent=2)
    )

    def upload(text: str) -> str:
        pdf = digital_pdf([[text]])
        response = api.post(
            f"/api/v1/documents?organization_id={org}&project_id={project}",
            files={"file": ("live.pdf", pdf, "application/pdf")},
            headers=headers,
        )
        response.raise_for_status()
        doc = response.json()["document_id"]
        for _ in range(120):
            status = api.get(
                f"/api/v1/documents/{doc}/status?organization_id={org}", headers=headers
            ).json()["status"]
            if status in ("COMPLETED", "FAILED"):
                break
            time.sleep(1)
        return doc

    def retrievable(question: str) -> str:
        response = crag.post(
            "/query",
            json={"question": question},
            headers={"X-API-Key": args.contextrag_key or "admin-live"},
        )
        return response.text

    # 1. create and retrieve
    doc = upload("The warranty period for the live check is 24 months from delivery.")
    summary = contextrag_sync.process_due()
    results["create"] = summary
    results["retrievable_after_create"] = "24 months" in retrievable(
        "How long is the warranty period for the live check?"
    )

    # 2. delete in DOCMIND, then deliver the retirement
    api.delete(f"/api/v1/documents/{doc}?organization_id={org}", headers=headers)
    results["delete"] = contextrag_sync.process_due(
        now=contextrag_sync.utcnow() + timedelta(seconds=5)
    )
    results["retrievable_after_delete"] = "24 months" in retrievable(
        "How long is the warranty period for the live check?"
    )

    # 3. failure drill: ContextRAG down while a document is delivered
    upload("The notice period for the drill is 30 days.")
    if args.pause_contextrag:
        os.system(args.pause_contextrag)
        time.sleep(2)
    results["during_outage"] = contextrag_sync.process_due(
        now=contextrag_sync.utcnow() + timedelta(seconds=10)
    )
    if args.resume_contextrag:
        os.system(args.resume_contextrag)
        time.sleep(3)
    results["after_restart"] = contextrag_sync.process_due(
        now=contextrag_sync.utcnow() + timedelta(hours=2)
    )
    results["retrievable_after_recovery"] = "30 days" in retrievable(
        "What is the notice period for the drill?"
    )
    events = api.get(f"/api/v1/contextrag/events?organization_id={org}", headers=headers).json()
    results["admin_view_counts"] = events["counts"]
    results["settings"] = {
        "content_mode": settings().contextrag_content_mode,
        "url": args.contextrag_url,
    }
    print(json.dumps(results, indent=2, default=str))
    ok = (
        results["retrievable_after_create"] is True
        and results["retrievable_after_delete"] is False
        and results["retrievable_after_recovery"] is True
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
