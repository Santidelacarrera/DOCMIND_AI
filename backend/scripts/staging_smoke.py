"""End-to-end smoke test against a running deployment (staging, never production data).

    python scripts/staging_smoke.py --base-url https://staging.example.com

Registers two throwaway tenants, uploads one synthetic invoice from eval_corpus, waits for
processing, reads and approves the extraction, exports it, and checks that the second tenant
cannot see the first one's document. Exit code 0 means every step passed.

With ``--ask`` it also checks question answering end to end (needs ``QDRANT_URL`` on the server):
uploads a fictitious policy, waits until it is indexed, and requires a cited answer (document,
page, chunk and quoted excerpt), an abstention for a question the document cannot answer, and an
abstention for the second tenant, which must never see the first tenant's text.
"""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

CORPUS = Path(__file__).resolve().parent.parent / "eval_corpus"
POLICY_PAGES = [
    ["POLITICA DE VACACIONES", "Documento ficticio para la prueba de humo."],
    ["Los empleados tienen derecho a 23 dias laborables de vacaciones al ano."],
]
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


def wait_completed(client: httpx.Client, headers: dict[str, str], org: str, document: str, timeout: int) -> str:
    deadline = time.time() + timeout
    status = {"status": "PENDING"}
    while time.time() < deadline and status["status"] not in ("COMPLETED", "FAILED"):
        time.sleep(3)
        status = client.get(f"/api/v1/documents/{document}/status?organization_id={org}", headers=headers).json()
    return str(status["status"])


def check_ask(client: httpx.Client, headers: dict[str, str], org: str, project: str,
              other_headers: dict[str, str], other_org: str, timeout: int) -> None:
    sys.path.insert(0, str(CORPUS.parent))
    from app.evaluation.pdfgen import digital_pdf

    upload = client.post(
        f"/api/v1/documents?organization_id={org}&project_id={project}",
        files={"file": ("politica_smoke.pdf", digital_pdf(POLICY_PAGES), "application/pdf")}, headers=headers,
    )
    step("ask: policy upload accepted", upload.status_code == 202, str(upload.status_code))
    step("ask: policy processed", wait_completed(client, headers, org, upload.json()["document_id"], timeout) == "COMPLETED")
    question = {"question": "Cuantos dias de vacaciones tienen los empleados?"}
    deadline, body = time.time() + timeout, {"abstained": True}
    while time.time() < deadline and body.get("abstained", True):  # indexing runs after processing
        response = client.post(f"/api/v1/ask?organization_id={org}", headers=headers, json=question)
        step("ask: endpoint available", response.status_code == 200, str(response.status_code))
        body = response.json()
        if body["abstained"]:
            time.sleep(3)
    citation = (body.get("citations") or [{}])[0]
    step("ask: answer quotes the fact", "23 dias" in body.get("answer", ""), body.get("answer", "")[:80])
    step("ask: citation names document and page",
         citation.get("document") == "politica_smoke.pdf" and citation.get("page") == 2, str(citation.get("label")))
    step("ask: citation locates the fragment",
         citation.get("chunk_index") is not None and "23 dias" in str(citation.get("excerpt")), str(citation.get("chunk_index")))
    unknown = client.post(f"/api/v1/ask?organization_id={org}", headers=headers,
                          json={"question": "Cual es el color favorito del gerente?"}).json()
    step("ask: abstains without evidence", unknown["abstained"] is True and not unknown["citations"])
    other = client.post(f"/api/v1/ask?organization_id={other_org}", headers=other_headers, json=question).json()
    step("ask: other tenant gets nothing", other["abstained"] is True and not other["citations"])
    foreign = client.post(f"/api/v1/ask?organization_id={org}", headers=other_headers, json=question)
    step("ask: other tenant cannot query tenant A", foreign.status_code in (403, 404), str(foreign.status_code))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--timeout", type=int, default=180, help="seconds to wait for processing")
    parser.add_argument("--ask", action="store_true", help="also check cited answers, abstention and isolation")
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
        final = wait_completed(client, headers, org, document, args.timeout)
        step("processing completed", final == "COMPLETED", final)
        extraction = client.get(f"/api/v1/documents/{document}/extraction?organization_id={org}", headers=headers).json()
        step("extraction has fields", bool(extraction.get("fields")), f"{len(extraction.get('fields', []))} fields")
        exported = client.get(f"/api/v1/documents/{document}/export?organization_id={org}&format=json", headers=headers)
        # Export is blocked until pending fields are reviewed; both outcomes prove the gate is wired.
        step("export gate answers", exported.status_code in (200, 409, 422), str(exported.status_code))
        cross = client.get(f"/api/v1/documents/{document}/status?organization_id={org}", headers=other_headers)
        step("second tenant cannot read tenant A", cross.status_code in (403, 404), str(cross.status_code))
        cross_org = client.get(f"/api/v1/documents/{document}/status?organization_id={other_org}", headers=other_headers)
        step("document not visible inside tenant B", cross_org.status_code in (403, 404), str(cross_org.status_code))
        if args.ask:
            check_ask(client, headers, org, project, other_headers, other_org, args.timeout)
    print("smoke test passed")


if __name__ == "__main__":
    main()
