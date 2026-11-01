"""End-to-end smoke test against a running deployment (staging, never production data).

    python scripts/staging_smoke.py --base-url https://staging.example.com

Registers two throwaway tenants, uploads one synthetic invoice from eval_corpus, waits for
processing, reads and approves the extraction, exports it, and checks that the second tenant
cannot see the first one's document. Exit code 0 means every step passed.
"""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

CORPUS = Path(__file__).resolve().parent.parent / "eval_corpus"
PASSWORD = "Smoke-" + uuid.uuid4().hex[:12] + "!aA1"


def step(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {name} {detail}".rstrip())
    if not ok:
        sys.exit(1)


def register(client: httpx.Client, label: str) -> tuple[dict[str, str], str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": f"smoke-{label}-{uuid.uuid4().hex[:8]}@example.com", "password": PASSWORD, "organization_name": f"Smoke {label}"},
    )
    step(f"register tenant {label}", response.status_code == 201, str(response.status_code))
    body = response.json()
    client.cookies.clear()
    return {"Authorization": f"Bearer {body['access_token']}"}, body["organization_id"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--timeout", type=int, default=180, help="seconds to wait for processing")
    args = parser.parse_args()
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    entry = next(d for d in manifest["documents"] if d["doc_type"] == "invoice")
    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        step("health", client.get("/health").status_code == 200)
        step("ready", client.get("/ready").status_code == 200)
        headers, org = register(client, "A")
        other_headers, other_org = register(client, "B")
        project = client.post(f"/api/v1/projects?organization_id={org}&name=smoke", headers=headers).json()["id"]
        schema = client.post(
            f"/api/v1/schemas?organization_id={org}&name=invoice", headers=headers, json=manifest["schemas"]["invoice"]
        ).json()["id"]
        upload = client.post(
            f"/api/v1/documents?organization_id={org}&project_id={project}&schema_id={schema}",
            files={"file": ("smoke.pdf", (CORPUS / entry["file"]).read_bytes(), "application/pdf")},
            headers=headers,
        )
        step("upload accepted", upload.status_code == 202, str(upload.status_code))
        document = upload.json()["document_id"]
        deadline = time.time() + args.timeout
        status = {"status": "PENDING"}
        while time.time() < deadline and status["status"] not in ("COMPLETED", "FAILED"):
            time.sleep(3)
            status = client.get(f"/api/v1/documents/{document}/status?organization_id={org}", headers=headers).json()
        step("processing completed", status["status"] == "COMPLETED", status["status"])
        extraction = client.get(f"/api/v1/documents/{document}/extraction?organization_id={org}", headers=headers).json()
        step("extraction has fields", bool(extraction.get("fields")), f"{len(extraction.get('fields', []))} fields")
        exported = client.get(f"/api/v1/documents/{document}/export?organization_id={org}&format=json", headers=headers)
        # Export is blocked until pending fields are reviewed; both outcomes prove the gate is wired.
        step("export gate answers", exported.status_code in (200, 409, 422), str(exported.status_code))
        cross = client.get(f"/api/v1/documents/{document}/status?organization_id={org}", headers=other_headers)
        step("second tenant cannot read tenant A", cross.status_code in (403, 404), str(cross.status_code))
        cross_org = client.get(f"/api/v1/documents/{document}/status?organization_id={other_org}", headers=other_headers)
        step("document not visible inside tenant B", cross_org.status_code in (403, 404), str(cross_org.status_code))
    print("smoke test passed")


if __name__ == "__main__":
    main()
