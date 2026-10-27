"""Local load smoke test: concurrent uploads through the real API and worker, then wait for completion.

Not part of CI and not a capacity claim. It reports what was measured on the machine that ran it.

    # with the API on :8000, a Celery worker and Redis/PostgreSQL configured as in .env
    python scripts/load_smoke.py --documents 40 --concurrency 8

Synthetic one-page PDFs only; no real document is read or sent anywhere.
"""

import argparse
import io
import json
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
from pypdf import PdfWriter


def synthetic_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_metadata({"/LoadId": uuid.uuid4().hex})  # unique bytes: no duplicate short-circuit
    writer.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--documents", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()

    with httpx.Client(base_url=args.api, timeout=60) as client:
        email = f"load-{uuid.uuid4().hex[:8]}@example.com"
        password = "correct-horse-battery-staple"
        account = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "organization_name": "Load"},
        ).json()
        headers = {"Authorization": f"Bearer {account['access_token']}"}
        org = account["organization_id"]
        project = client.post(
            f"/api/v1/projects?organization_id={org}&name=load", headers=headers
        ).json()["id"]

    def upload(_: int) -> tuple[float, int, str | None]:
        with httpx.Client(base_url=args.api, timeout=120) as client:
            started = time.perf_counter()
            response = client.post(
                f"/api/v1/documents?organization_id={org}&project_id={project}",
                files={"file": ("load.pdf", synthetic_pdf(), "application/pdf")},
                headers=headers,
            )
            elapsed = time.perf_counter() - started
            body = (
                response.json()
                if response.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            return elapsed, response.status_code, body.get("document_id")

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(upload, range(args.documents)))
    upload_wall = time.perf_counter() - start
    latencies = [r[0] for r in results]
    accepted = [r[2] for r in results if r[1] in (200, 201, 202) and r[2]]
    rejected = [r[1] for r in results if r[1] not in (200, 201, 202)]

    pending = set(accepted)
    deadline = time.perf_counter() + args.timeout
    final: dict[str, str] = {}
    with httpx.Client(base_url=args.api, timeout=60) as client:
        while pending and time.perf_counter() < deadline:
            for doc in list(pending):
                status = client.get(
                    f"/api/v1/documents/{doc}/status?organization_id={org}", headers=headers
                ).json()["status"]
                if status in ("COMPLETED", "FAILED"):
                    final[doc] = status
                    pending.discard(doc)
            if pending:
                time.sleep(1.0)
    completed_wall = time.perf_counter() - start

    summary = {
        "documents": args.documents,
        "concurrency": args.concurrency,
        "upload_http_codes": {
            str(c): sum(1 for r in results if r[1] == c) for c in {r[1] for r in results}
        },
        "upload_latency_s": {
            "p50": round(statistics.median(latencies), 3),
            "p95": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 3),
            "max": round(max(latencies), 3),
        },
        "upload_wall_s": round(upload_wall, 2),
        "completed": sum(1 for s in final.values() if s == "COMPLETED"),
        "failed": sum(1 for s in final.values() if s == "FAILED"),
        "still_pending_at_timeout": len(pending),
        "end_to_end_wall_s": round(completed_wall, 2),
        "throughput_docs_per_min": round(len(final) / (completed_wall / 60), 2)
        if completed_wall
        else None,
        "rejected_codes": sorted(set(rejected)),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
