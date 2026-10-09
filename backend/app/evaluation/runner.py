"""Run the reference corpus through the real API + worker and score the outcome.

Everything user-visible goes through the HTTP API exactly as a client would use it
(upload -> status -> extraction -> review -> approve -> export); only the Celery
transport is replaced by a direct in-process call to the task so the run is hermetic.

The simulated reviewer uses the reference annotation as its oracle. It demonstrates the
review workflow and shows how much of the remaining error a review pass removes; it does
**not** measure how fast or how accurate a real person is.
"""

import csv
import io
import json
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from app.evaluation import corpus as corpus_mod
from app.evaluation import metrics as m
from app.processing import estimate_cost, extraction_envelope

PASSWORD = "evaluation-password-123"


class Provider(Protocol):
    def extract(
        self, text: str, schema: dict[str, Any], prompt_instructions: str | None = None
    ) -> dict[str, Any]: ...


def tesseract_version() -> str | None:
    try:
        import pytesseract

        return str(pytesseract.get_tesseract_version())
    except Exception:
        return None


def _truth_text(entry: dict[str, Any]) -> list[str]:
    return ["\n".join(lines) for lines in entry["pages"]]


def _api(client: Any, method: str, url: str, headers: dict[str, str], **kwargs: Any) -> Any:
    return client.request(method, url, headers=headers, **kwargs)


def run_evaluation(
    client: Any,
    provider: Provider,
    *,
    corpus_dir: Path = corpus_mod.CORPUS_DIR,
    review: bool = True,
    provider_name: str = "baseline-rules",
) -> dict[str, Any]:
    from sqlalchemy import select

    from app import worker
    from app.db import SessionLocal
    from app.models import DocumentPage, ExtractionRun

    manifest = corpus_mod.load(corpus_dir)
    worker.llm = lambda: provider  # type: ignore[assignment,return-value]

    # --- tenant, project, one schema per document type -------------------------------
    email = f"eval-{uuid.uuid4().hex[:8]}@example.com"
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "organization_name": "Evaluation"},
    ).json()
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {registered['access_token']}"}
    org = registered["organization_id"]
    project = _api(client, "POST", f"/api/v1/projects?organization_id={org}&name=reference-corpus", headers).json()["id"]
    schema_ids = {}
    for doc_type, schema in manifest["schemas"].items():
        response = _api(
            client, "POST", f"/api/v1/schemas?organization_id={org}&name={doc_type}", headers, json=schema
        )
        schema_ids[doc_type] = response.json()["id"]

    results: list[dict[str, Any]] = []
    for entry in manifest["documents"]:
        results.append(
            _process_one(client, headers, org, project, schema_ids, entry, corpus_dir, SessionLocal,
                         DocumentPage, ExtractionRun, select, worker, provider)
        )

    report = _summarize(results, manifest, provider_name)
    if review:
        report["review_workflow"] = _review_pass(client, headers, org, results, manifest)
    return report


# --------------------------------------------------------------------------------------
def _process_one(
    client: Any, headers: dict[str, str], org: str, project: str, schema_ids: dict[str, str],
    entry: dict[str, Any], corpus_dir: Path, SessionLocal: Any, DocumentPage: Any, ExtractionRun: Any,
    select: Any, worker: Any, provider: Provider,
) -> dict[str, Any]:
    content = (corpus_dir / entry["file"]).read_bytes()
    row: dict[str, Any] = {"id": entry["id"], "kind": entry["kind"], "doc_type": entry["doc_type"],
                           "bytes": len(content), "pages": len(entry["pages"]) or None}
    t0 = time.perf_counter()
    upload = client.post(
        f"/api/v1/documents?organization_id={org}&project_id={project}&schema_id={schema_ids[entry['doc_type']]}",
        files={"file": (f"{entry['id']}.pdf", content, "application/pdf")},
        headers=headers,
    )
    row["upload_seconds"] = time.perf_counter() - t0
    if upload.status_code != 202:
        row.update(outcome="rejected_at_upload", reject_code=upload.json().get("detail"),
                   http_status=upload.status_code)
        return row
    body = upload.json()
    row["document_id"] = body["document_id"]
    t1 = time.perf_counter()
    worker.process_document.run(body["job_id"])
    row["process_seconds"] = time.perf_counter() - t1
    status = client.get(f"/api/v1/documents/{body['document_id']}/status?organization_id={org}", headers=headers).json()
    row["job_status"] = status["status"]
    if status["status"] != "COMPLETED":
        row.update(outcome="failed_in_processing", reject_code=status["failure_code"])
        return row
    row["outcome"] = "completed"
    extraction = client.get(
        f"/api/v1/documents/{body['document_id']}/extraction?organization_id={org}", headers=headers
    ).json()
    row["extraction"] = extraction
    with SessionLocal() as db:
        pages = db.scalars(
            select(DocumentPage).where(DocumentPage.document_id == uuid.UUID(body["document_id"]))
            .order_by(DocumentPage.page_number)
        ).all()
        row["ocr_used"] = any(p.ocr_used for p in pages)
        row["pipeline_text"] = [p.text or "" for p in pages]
        run = db.scalar(select(ExtractionRun).where(ExtractionRun.document_id == uuid.UUID(body["document_id"])))
        row["tokens"] = (run.prompt_tokens, run.completion_tokens) if run else (None, None)
    # Same extractor, perfect text: isolates extraction errors from OCR/text-layer errors.
    truth_text = "\n".join(_truth_text(entry))
    envelope = extraction_envelope(None)
    del envelope
    schema = corpus_mod.SCHEMAS[entry["doc_type"]]
    oracle = provider.extract(truth_text, extraction_envelope(schema))
    row["oracle_fields"] = oracle["fields"]
    row["total_seconds"] = time.perf_counter() - t0
    return row


def _summarize(results: list[dict[str, Any]], manifest: dict[str, Any], provider_name: str) -> dict[str, Any]:
    entries = {e["id"]: e for e in manifest["documents"]}
    completed = [r for r in results if r["outcome"] == "completed" and entries[r["id"]]["truth"]]

    per_doc: list[dict[str, m.Counts]] = []
    per_kind: dict[str, list[dict[str, m.Counts]]] = {}
    observations: list[m.Observation] = []
    attribution = {"correct": 0, "ocr_or_text_layer_error": 0, "extraction_error": 0}
    doc_rows = []
    for r in completed:
        truth = entries[r["id"]]["truth"]
        fields = {f["name"]: f for f in r["extraction"]["fields"]}
        predicted = {name: f["original_value"] for name, f in fields.items()}
        scores = m.score_document(truth, predicted)
        per_doc.append(scores)
        per_kind.setdefault(r["kind"] if not r["ocr_used"] else "scanned", []).append(scores)
        wrong_fields = []
        for name, expected in truth.items():
            f = fields.get(name)
            correct = m.values_match(expected, f["original_value"] if f else None)
            observations.append(
                m.Observation(r["id"], name, correct, f["confidence"] if f else None, bool(f and f["needs_review"]))
            )
            oracle_ok = m.values_match(expected, r["oracle_fields"].get(name))
            if correct:
                attribution["correct"] += 1
            elif oracle_ok:
                attribution["ocr_or_text_layer_error"] += 1
                wrong_fields.append(name)
            else:
                attribution["extraction_error"] += 1
                wrong_fields.append(name)
        doc_rows.append(
            {"id": r["id"], "kind": r["kind"], "ocr_used": r["ocr_used"],
             "fields_correct": sum(s.tp for s in scores.values() if s.fp == 0 and s.fn == 0),
             "fields_expected": sum(1 for v in truth.values() if m.normalize(v) is not None),
             "wrong_fields": wrong_fields,
             "requires_review": r["extraction"]["requires_review"]}
        )

    # --- text error rates (OCR for scans, text layer for digital PDFs) ----------------------
    text_rows = []
    for r in completed:
        reference = "\n".join(_truth_text(entries[r["id"]]))
        hypothesis = "\n".join(r["pipeline_text"])
        text_rows.append(
            {"id": r["id"], "source": "ocr" if r["ocr_used"] else "text_layer",
             "cer": round(m.cer(reference, hypothesis), 4), "wer": round(m.wer(reference, hypothesis), 4)}
        )
    ocr_rows = [t for t in text_rows if t["source"] == "ocr"]

    # --- rejection handling ----------------------------------------------------------------
    malformed = [r for r in results if r["kind"] == corpus_mod.MALFORMED]
    rejection_rows = []
    for r in malformed:
        expected = entries[r["id"]]["expect_reject"]
        got = r.get("reject_code")
        rejection_rows.append(
            {"id": r["id"], "expected": expected, "got": got, "stage": r["outcome"],
             "ok": r["outcome"] != "completed" and got in expected}
        )

    # --- prompt-injection documents ---------------------------------------------------------
    injection_rows = []
    for r in results:
        if r["kind"] != corpus_mod.ADVERSARIAL or r["outcome"] != "completed":
            continue
        truth = entries[r["id"]]["truth"]
        fields = {f["name"]: f["original_value"] for f in r["extraction"]["fields"]}
        injection_rows.append(
            {"id": r["id"],
             "unexpected_fields": sorted(set(fields) - set(truth)),
             "values_unchanged": all(m.values_match(v, fields.get(k)) for k, v in truth.items()),
             "flagged_for_review": r["extraction"]["requires_review"]}
        )

    aggregate_all = m.aggregate(per_doc)
    secs = [r["total_seconds"] for r in results if "total_seconds" in r]
    ocr_secs = [r["total_seconds"] for r in results if r.get("ocr_used")]
    digital_secs = [r["total_seconds"] for r in results if "total_seconds" in r and not r.get("ocr_used")]
    prompt = sum((r.get("tokens") or (0, 0))[0] or 0 for r in completed)
    completion = sum((r.get("tokens") or (0, 0))[1] or 0 for r in completed)
    return {
        "meta": {
            "provider": provider_name,
            "documents": len(results),
            "scored_documents": len(completed),
            "tesseract": tesseract_version(),
            "note": "Deterministic sections are reproducible for a given corpus, provider and OCR engine; "
                    "'timing' is machine-dependent.",
        },
        "extraction": {
            **aggregate_all,
            "by_kind": {kind: m.aggregate(docs)["micro"] for kind, docs in sorted(per_kind.items())},
            "documents": doc_rows,
        },
        "error_attribution": attribution,
        "text_quality": {
            "ocr_mean_cer": round(sum(t["cer"] for t in ocr_rows) / len(ocr_rows), 4) if ocr_rows else None,
            "ocr_mean_wer": round(sum(t["wer"] for t in ocr_rows) / len(ocr_rows), 4) if ocr_rows else None,
            "documents": text_rows,
        },
        "confidence": m.calibration(observations),
        "review_routing": m.review_routing(observations),
        "rejections": {
            "correct": sum(r["ok"] for r in rejection_rows), "total": len(rejection_rows), "documents": rejection_rows,
        },
        "prompt_injection": injection_rows,
        "timing": {
            "seconds_per_document_mean": round(sum(secs) / len(secs), 4) if secs else None,
            "seconds_per_document_p50": m.percentile(secs, 0.5),
            "seconds_per_document_p95": m.percentile(secs, 0.95),
            "digital_mean": round(sum(digital_secs) / len(digital_secs), 4) if digital_secs else None,
            "scanned_ocr_mean": round(sum(ocr_secs) / len(ocr_secs), 4) if ocr_secs else None,
        },
        "cost": {
            "prompt_tokens": prompt, "completion_tokens": completion,
            "per_document_usd_projected": round(
                (estimate_cost(prompt, completion) or 0.0) / max(1, len(completed)), 6
            ),
            "note": "Projected from token counts at the configured per-1M-token prices "
                    "(OPENAI_INPUT_PRICE_PER_1M / OPENAI_OUTPUT_PRICE_PER_1M); a rule-based "
                    "provider actually costs nothing. Token counts are character-based estimates "
                    "unless the provider reports usage.",
        },
        "_results": results,  # raw rows for the review pass; removed before writing
    }


# --------------------------------------------------------------------------------------
def _reason(cause: str, expected: Any, predicted: Any) -> str:
    if m.values_match(expected, predicted):
        return "confirmed"
    if cause == "ocr":
        return "ocr_misread"
    if m.normalize(predicted) is None:
        return "missing_value"
    if m.normalize(expected) is None:
        return "hallucinated_value"
    return "wrong_value"


def _review_pass(
    client: Any, headers: dict[str, str], org: str, results: list[dict[str, Any]], manifest: dict[str, Any]
) -> dict[str, Any]:
    entries = {e["id"]: e for e in manifest["documents"]}
    queue = client.get(f"/api/v1/review/queue?organization_id={org}", headers=headers).json()
    queued_ids = {q["document_id"] for q in queue}
    corrections: list[dict[str, Any]] = []
    before = after = total = 0
    approved = blocked = 0
    exports: list[tuple[str, int]] = []
    for r in results:
        if r["outcome"] != "completed" or not entries[r["id"]]["truth"]:
            continue
        truth = entries[r["id"]]["truth"]
        extraction = r["extraction"]
        cause = "ocr" if r["ocr_used"] else "extraction"
        for f in extraction["fields"]:
            if f["name"] not in truth or not f["needs_review"]:
                continue
            expected = truth[f["name"]]
            reason = _reason(cause, expected, f["original_value"])
            response = client.patch(
                f"/api/v1/extraction-fields/{f['id']}?organization_id={org}&reason={reason}",
                content=json.dumps(expected), headers={**headers, "Content-Type": "application/json"},
            )
            assert response.status_code == 200, response.text
            corrections.append(
                {"document": r["id"], "field": f["name"], "model_value": f["original_value"],
                 "corrected_value": expected, "confidence": f["confidence"], "reason": reason,
                 "changed": response.json()["changed"]}
            )
        approve = client.post(
            f"/api/v1/documents/{r['document_id']}/review/approve?organization_id={org}", headers=headers
        )
        approved += approve.status_code == 200
        blocked += approve.status_code == 409
        t = time.perf_counter()
        exported = client.get(
            f"/api/v1/documents/{r['document_id']}/export?organization_id={org}&format=json", headers=headers
        ).json()
        csv_text = client.get(
            f"/api/v1/documents/{r['document_id']}/export?organization_id={org}&format=csv", headers=headers
        ).text
        exports.append((r["id"], len(list(csv.reader(io.StringIO(csv_text)))) - 1))
        r["export_seconds"] = time.perf_counter() - t
        final = {f["name"]: f["value"] for f in exported["fields"]}
        original = {f["name"]: f["original_value"] for f in exported["fields"]}
        for name, expected in truth.items():
            if m.normalize(expected) is None and name not in final:
                continue
            total += 1
            before += m.values_match(expected, original.get(name))
            after += m.values_match(expected, final.get(name))
    changed = [c for c in corrections if c["changed"]]
    reasons: dict[str, int] = {}
    for c in corrections:
        reasons[c["reason"]] = reasons.get(c["reason"], 0) + 1
    return {
        "documents_in_review_queue": len(queued_ids),
        "fields_reviewed": len(corrections),
        "fields_corrected": len(changed),
        "fields_confirmed_unchanged": len(corrections) - len(changed),
        "approved": approved,
        "approval_blocked": blocked,
        "accuracy_before_review": round(before / total, 4) if total else None,
        "accuracy_after_review": round(after / total, 4) if total else None,
        "reasons": dict(sorted(reasons.items())),
        "corrections": corrections,
        "exported_rows": dict(exports),
        "reviewer": "simulated (reference annotation used as oracle)",
    }


def expected_from(report: dict[str, Any]) -> dict[str, Any]:
    """Regression thresholds derived from a run: OCR-free numbers exactly, the rest with a margin."""
    def down(x: float | None, margin: float = 0.02) -> float:
        return round(max(0.0, (x or 0.0) - margin), 2)

    routing, text = report["review_routing"], report["text_quality"]
    return {
        "provider": report["meta"]["provider"],
        "tesseract": report["meta"]["tesseract"],
        "by_kind": {k: v for k, v in report["extraction"]["by_kind"].items() if k != "scanned"},
        "floors": {
            "micro_f1": down(report["extraction"]["micro"]["f1"]),
            "micro_precision": down(report["extraction"]["micro"]["precision"]),
            "error_capture_rate": down(routing["error_capture_rate"], 0.05),
            "auto_accept_accuracy": down(routing["auto_accept_accuracy"], 0.03),
            "max_silent_errors": len(routing["silent_errors"]) + 1,
            "max_ocr_mean_cer": round((text["ocr_mean_cer"] or 0.0) * 1.3 + 0.01, 2),
        },
        "rejections_total": report["rejections"]["total"],
    }


def strip_internal(report: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in report.items() if not k.startswith("_")}


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _p(*parts: str) -> str:
    """Join string fragments into one paragraph (explicit, so no implicit concatenation)."""
    return "".join(parts)


def to_markdown(report: dict[str, Any]) -> str:
    meta, ext = report["meta"], report["extraction"]
    pct = _pct
    micro = ext["micro"]
    lines = [
        f"# Evaluation report — `{meta['provider']}`",
        "",
        _p(f"{meta['scored_documents']} scored documents of {meta['documents']} ",
           f"(Tesseract {meta['tesseract'] or 'not available'})."),
        "",
        "## Field extraction (before any human review)",
        "",
        _p(f"Micro precision **{pct(micro['precision'])}**, recall **{pct(micro['recall'])}**, ",
           f"F1 **{pct(micro['f1'])}** (macro F1 {pct(ext['macro_f1'])})."),
        "",
        "| Field | TP | FP | FN | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, c in ext["per_field"].items():
        lines.append(_p(f"| {name} | {c['tp']} | {c['fp']} | {c['fn']} | ",
                        f"{pct(c['precision'])} | {pct(c['recall'])} | {pct(c['f1'])} |"))
    lines += ["", "| Document kind | Precision | Recall | F1 |", "|---|---:|---:|---:|"]
    for kind, c in ext["by_kind"].items():
        lines.append(f"| {kind} | {pct(c['precision'])} | {pct(c['recall'])} | {pct(c['f1'])} |")

    a = report["error_attribution"]
    lines += [
        "", "## Where errors come from", "",
        _p(f"Of {sum(a.values())} annotated fields: {a['correct']} correct, ",
           f"{a['ocr_or_text_layer_error']} wrong only because the text was damaged (OCR / text layer), ",
           f"{a['extraction_error']} wrong even on perfect text (extraction)."),
    ]
    t = report["text_quality"]
    if t["ocr_mean_cer"] is not None:
        lines += ["",
                  _p(f"OCR: mean character error rate **{pct(t['ocr_mean_cer'])}**, ",
                     f"word error rate **{pct(t['ocr_mean_wer'])}**."),
                  "", "| Document | Source | CER | WER |", "|---|---|---:|---:|"]
        lines += [f"| {d['id']} | {d['source']} | {pct(d['cer'])} | {pct(d['wer'])} |" for d in t["documents"]]

    c, rr = report["confidence"], report["review_routing"]
    lines += ["", "## Confidence and human review", "",
              f"Expected calibration error **{c['ece']}**.", "",
              "| Confidence | Fields | Mean confidence | Accuracy |", "|---|---:|---:|---:|"]
    for b in c["bins"]:
        mean = b["mean_confidence"] if b["n"] else "–"
        lines.append(f"| {b['range'][0]:.1f}–{b['range'][1]:.1f} | {b['n']} | {mean} | {pct(b['accuracy'])} |")
    lines += ["",
              _p(f"Routing to a person catches **{pct(rr['error_capture_rate'])}** of wrong fields at a ",
                 f"review load of **{pct(rr['review_load'])}** of all fields. ",
                 f"Accuracy without review {pct(rr['raw_accuracy'])}; ",
                 f"accuracy of the auto-accepted fields {pct(rr['auto_accept_accuracy'])}. ",
                 f"Wrong fields that were *not* flagged: {', '.join(rr['silent_errors']) or 'none'}.")]

    rej = report["rejections"]
    lines += ["", "## Invalid inputs", "",
              f"{rej['correct']}/{rej['total']} malformed files were rejected with the expected stable code.", "",
              "| File | Stage | Expected | Got | OK |", "|---|---|---|---|---|"]
    for d in rej["documents"]:
        lines.append(_p(f"| {d['id']} | {d['stage']} | {'/'.join(d['expected'])} | {d['got']} | ",
                        f"{'yes' if d['ok'] else 'NO'} |"))

    if report["prompt_injection"]:
        lines += ["", "## Embedded instructions", "",
                  "| Document | Unexpected fields | Values unchanged | Flagged for review |", "|---|---|---|---|"]
        for d in report["prompt_injection"]:
            lines.append(_p(f"| {d['id']} | {', '.join(d['unexpected_fields']) or 'none'} | ",
                            f"{d['values_unchanged']} | {d['flagged_for_review']} |"))

    wf = report.get("review_workflow")
    if wf:
        lines += ["", "## Review, correction and export", "",
                  _p(f"{wf['documents_in_review_queue']} documents entered the review queue; ",
                     f"{wf['fields_reviewed']} fields were reviewed ({wf['fields_corrected']} corrected, ",
                     f"{wf['fields_confirmed_unchanged']} confirmed). Field accuracy ",
                     f"{pct(wf['accuracy_before_review'])} → **{pct(wf['accuracy_after_review'])}** after review ",
                     f"(reviewer: {wf['reviewer']})."),
                  "", "| Document | Field | Model value | Corrected to | Confidence | Reason |",
                  "|---|---|---|---|---:|---|"]
        for corr in wf["corrections"]:
            if corr["changed"]:
                conf = "–" if corr["confidence"] is None else f"{corr['confidence']:.2f}"
                lines.append(_p(f"| {corr['document']} | {corr['field']} | `{corr['model_value']}` | ",
                                f"`{corr['corrected_value']}` | {conf} | {corr['reason']} |"))
        lines += ["", "Reasons: " + ", ".join(f"{k} ×{v}" for k, v in wf["reasons"].items())]

    tm, cost = report["timing"], report["cost"]
    lines += ["", "## Time and cost (machine-dependent)", "",
              _p(f"Mean **{tm['seconds_per_document_mean']} s** per document end to end ",
                 f"(p50 {tm['seconds_per_document_p50']} s, p95 {tm['seconds_per_document_p95']} s); ",
                 f"digital {tm['digital_mean']} s, scanned/OCR {tm['scanned_ocr_mean']} s."),
              "",
              _p(f"Approximate LLM cost per document: **${cost['per_document_usd_projected']}** ",
                 f"({cost['prompt_tokens']} prompt + {cost['completion_tokens']} completion tokens in total). ",
                 cost["note"])]
    return "\n".join(lines) + "\n"


def write_reports(report: dict[str, Any], directory: Path, stem: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    clean = strip_internal(report)
    (directory / f"{stem}.json").write_text(json.dumps(clean, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    (directory / f"{stem}.md").write_text(to_markdown(clean), encoding="utf-8")
