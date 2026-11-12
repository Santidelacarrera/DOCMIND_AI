"""Local load smoke test for question answering: concurrent /ask and conversation questions.

Not part of CI and not a capacity claim: it reports latency percentiles measured on the machine that
ran it, against a running stack (API, worker, Qdrant). Fictitious documents only.

    python scripts/ask_load_smoke.py --api http://localhost:8000 --questions 200 --concurrency 8

Uploads one fictitious policy, waits until it is answerable, then sends ``--questions`` questions with
``--concurrency`` threads (half answerable, half not) and checks every answer: answerable ones must cite
page 2, the others must abstain. With ``--conversations`` the same load goes through one conversation
per thread (needs CONVERSATION_ENCRYPTION_KEY on the server).
"""

import argparse
import json
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.evaluation.pdfgen import digital_pdf

PAGES = [["POLITICA DE VACACIONES", "Documento ficticio de carga."],
         ["Los empleados tienen derecho a 23 dias laborables de vacaciones al ano."]]
ANSWERABLE = "Cuantos dias de vacaciones tienen los empleados?"
UNANSWERABLE = "Cual es el color favorito del gerente?"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--questions", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--conversations", action="store_true")
    args = parser.parse_args()
    client = httpx.Client(base_url=args.api, timeout=60)
    password = "Load-" + uuid.uuid4().hex[:12] + "!aA1"
    account = client.post("/api/v1/auth/register", json={
        "email": f"askload-{uuid.uuid4().hex[:8]}@example.com", "password": password, "organization_name": "ask-load",
    }).json()
    headers, org = {"Authorization": f"Bearer {account['access_token']}"}, account["organization_id"]
    project = client.post(f"/api/v1/projects?organization_id={org}&name=load", headers=headers).json()["id"]
    upload = client.post(f"/api/v1/documents?organization_id={org}&project_id={project}",
                         files={"file": ("politica_carga.pdf", digital_pdf(PAGES), "application/pdf")}, headers=headers)
    upload.raise_for_status()
    started = time.perf_counter()
    while client.post(f"/api/v1/ask?organization_id={org}", headers=headers,
                      json={"question": ANSWERABLE}).json().get("abstained", True):
        if time.perf_counter() - started > 180:
            print("document never became answerable", file=sys.stderr)
            return 1
        time.sleep(1)
    ready_seconds = time.perf_counter() - started

    conversations: list[str] = []
    if args.conversations:
        for _ in range(args.concurrency):
            created = client.post(f"/api/v1/conversations?organization_id={org}", headers=headers, json={"project_id": project})
            created.raise_for_status()
            conversations.append(created.json()["id"])

    def one(i: int) -> tuple[float, bool, int]:
        question = ANSWERABLE if i % 2 == 0 else UNANSWERABLE
        path = (f"/api/v1/conversations/{conversations[i % len(conversations)]}/ask?organization_id={org}"
                if conversations else f"/api/v1/ask?organization_id={org}")
        with httpx.Client(base_url=args.api, timeout=60) as c:
            t0 = time.perf_counter()
            for _ in range(5):  # a conversation turn may collide with another thread (409): retry
                response = c.post(path, headers=headers, json={"question": question})
                if response.status_code != 409:
                    break
            elapsed = time.perf_counter() - t0
        if response.status_code != 200:
            return elapsed, False, response.status_code
        body = response.json()
        ok = (not body["abstained"] and body["citations"][0]["page"] == 2) if i % 2 == 0 else body["abstained"]
        return elapsed, ok, 200

    t0 = time.perf_counter()
    with ThreadPoolExecutor(args.concurrency) as pool:
        results = list(pool.map(one, range(args.questions)))
    wall = time.perf_counter() - t0
    latencies = sorted(r[0] for r in results)
    report = {
        "questions": args.questions, "concurrency": args.concurrency, "via": "conversations" if conversations else "/ask",
        "seconds_until_answerable": round(ready_seconds, 2), "wall_seconds": round(wall, 2),
        "throughput_per_second": round(args.questions / wall, 2),
        "p50_ms": round(statistics.median(latencies) * 1000), "p95_ms": round(latencies[int(len(latencies) * 0.95) - 1] * 1000),
        "max_ms": round(latencies[-1] * 1000), "wrong_answers": sum(not r[1] for r in results),
        "http_errors": sorted({r[2] for r in results if r[2] != 200}),
    }
    print(json.dumps(report, indent=2))
    return 0 if report["wrong_answers"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
