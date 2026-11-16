"""Document intelligence: classification, entity extraction, summaries, comparison and templates.

All deterministic routines preserve provenance (document_id, page, verbatim snippet) and never
fabricate missing values. When no evidence supports a summary or entity, they return empty or
abstain cleanly.
"""

import csv
import io
import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

from app import rut
from app.api_support import neutralize_formula
from app.ingestion.answering import ABSTENTION, terms
from app.ingestion.entities import RuleConfig, RuleEntityDetector
from app.ingestion.pii import es_dni_ok, phone_ok
from app.rag import conflicts as conflict_detection
from app.rag.safety import has_active_content
from app.rag.text import split_sentences

SCHEMA_TEMPLATES: dict[str, dict[str, Any]] = {
    "invoice": {
        "name": "Commercial Invoice",
        "description": "Standard B2B/B2C invoice fields with totals, currency and issuer.",
        "json_schema": {
            "type": "object",
            "properties": {
                "invoice_number": {"type": ["string", "null"]},
                "issue_date": {"type": ["string", "null"]},
                "due_date": {"type": ["string", "null"]},
                "issuer_name": {"type": ["string", "null"]},
                "customer_name": {"type": ["string", "null"]},
                "currency": {"type": ["string", "null"]},
                "subtotal": {"type": ["number", "null"]},
                "tax_amount": {"type": ["number", "null"]},
                "total_amount": {"type": ["number", "null"]},
            },
            "required": [
                "invoice_number", "issue_date", "due_date", "issuer_name",
                "customer_name", "currency", "subtotal", "tax_amount", "total_amount",
            ],
            "additionalProperties": False,
        },
        "prompt_instructions": "Extract invoice fields verbatim. Return null if a field is not present.",
    },
    "dte_cl": {
        "name": "DTE Chile (Factura / Boleta / NC)",
        "description": "Chilean electronic tax document with modulo-11 RUT validation.",
        "json_schema": {
            "type": "object",
            "properties": {
                "tipo_dte": {"type": ["string", "null"]},
                "folio": {"type": ["string", "null"]},
                "fecha_emision": {"type": ["string", "null"]},
                "emisor_nombre": {"type": ["string", "null"]},
                "emisor_rut": {"type": ["string", "null"], "format": "cl-rut"},
                "receptor_nombre": {"type": ["string", "null"]},
                "receptor_rut": {"type": ["string", "null"], "format": "cl-rut"},
                "monto_neto": {"type": ["number", "null"]},
                "iva": {"type": ["number", "null"]},
                "monto_total": {"type": ["number", "null"]},
            },
            "required": [
                "tipo_dte", "folio", "fecha_emision", "emisor_nombre", "emisor_rut",
                "receptor_nombre", "receptor_rut", "monto_neto", "iva", "monto_total",
            ],
            "additionalProperties": False,
        },
        "prompt_instructions": "Extract Chilean DTE fields. Validate RUT digits without inventing values.",
    },
    "contract": {
        "name": "Commercial Contract",
        "description": "Contract metadata, parties, effective dates, governing law and termination.",
        "json_schema": {
            "type": "object",
            "properties": {
                "contract_title": {"type": ["string", "null"]},
                "effective_date": {"type": ["string", "null"]},
                "expiration_date": {"type": ["string", "null"]},
                "counterparty": {"type": ["string", "null"]},
                "governing_law": {"type": ["string", "null"]},
                "contract_value": {"type": ["number", "null"]},
                "payment_terms": {"type": ["string", "null"]},
                "termination_notice_days": {"type": ["integer", "null"]},
            },
            "required": [
                "contract_title", "effective_date", "expiration_date", "counterparty",
                "governing_law", "contract_value", "payment_terms", "termination_notice_days",
            ],
            "additionalProperties": False,
        },
        "prompt_instructions": "Extract contract terms strictly from the document text; null if missing.",
    },
    "policy": {
        "name": "Insurance Policy",
        "description": "Policy number, insurer, coverage limits, deductible and dates.",
        "json_schema": {
            "type": "object",
            "properties": {
                "policy_number": {"type": ["string", "null"]},
                "insurer": {"type": ["string", "null"]},
                "policyholder": {"type": ["string", "null"]},
                "effective_date": {"type": ["string", "null"]},
                "expiration_date": {"type": ["string", "null"]},
                "coverage_limit": {"type": ["number", "null"]},
                "deductible": {"type": ["number", "null"]},
                "premium": {"type": ["number", "null"]},
            },
            "required": [
                "policy_number", "insurer", "policyholder", "effective_date",
                "expiration_date", "coverage_limit", "deductible", "premium",
            ],
            "additionalProperties": False,
        },
        "prompt_instructions": "Extract insurance policy limits and dates verbatim; null when absent.",
    },
    "receipt": {
        "name": "Receipt / Expense Voucher",
        "description": "Merchant, date, payment method, currency and total paid.",
        "json_schema": {
            "type": "object",
            "properties": {
                "receipt_number": {"type": ["string", "null"]},
                "date": {"type": ["string", "null"]},
                "merchant": {"type": ["string", "null"]},
                "payment_method": {"type": ["string", "null"]},
                "currency": {"type": ["string", "null"]},
                "total_amount": {"type": ["number", "null"]},
            },
            "required": ["receipt_number", "date", "merchant", "payment_method", "currency", "total_amount"],
            "additionalProperties": False,
        },
        "prompt_instructions": "Extract receipt fields strictly from the text; null if absent.",
    },
}

_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("invoice", ("invoice", "factura", "boleta", "nota de credito", "nota de cr\u00e9dito", "folio", "iva", "subtotal", "bill to")),
    ("contract", ("agreement", "contract", "contrato", "clausula", "cl\u00e1usula", "governing law", "jurisdicci\u00f3n", "party", "partes", "arrendamiento")),
    ("policy", ("policy", "p\u00f3liza", "poliza", "coverage", "cobertura", "deductible", "deducible", "insurer", "aseguradora", "premium", "prima")),
    ("financial_report", ("balance sheet", "income statement", "estado financiero", "ebitda", "revenue", "ingresos", "budget", "presupuesto")),
    ("receipt", ("receipt", "comprobante", "recibo", "ticket", "pago recibido")),
    ("technical", ("specification", "architecture", "manual", "protocol", "api", "manual t\u00e9cnico", "especificaci\u00f3n")),
)

_DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{4}|"
    r"\d{1,2}\s+(?:de\s+)?(?:ene(?:ro)?|feb(?:rero)?|mar(?:zo)?|abr(?:il)?|may(?:o)?|jun(?:io)?|"
    r"jul(?:io)?|ago(?:sto)?|sep(?:tiembre)?|oct(?:ubre)?|nov(?:iembre)?|dic(?:iembre)?|"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(?:de\s+)?\d{4})\b",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(
    r"(?:USD|EUR|GBP|CLP|UF|MXN|ARS|PEN|\$|€|£)\s*-?\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?"
    r"|-?\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?\s*(?:USD|EUR|GBP|CLP|UF|MXN|ARS|PEN)\b",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<![\w.,-])(?:\+|00)?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,5}(?!\w)")
_DNI_RE = re.compile(r"\b(?:\d{8}|[XYZxyz]\d{7})[- ]?[A-Za-z]\b")


def list_schema_templates() -> list[dict[str, Any]]:
    return [{"id": k, **v} for k, v in SCHEMA_TEMPLATES.items()]


def classify_document(
    filename: str,
    pages_text: Sequence[str],
    *,
    format_name: str | None = None,
    ocr_used: bool = False,
    tables_detected: int = 0,
) -> tuple[str, list[str]]:
    """Deterministic classification and automatic tags based on filename, structure and text."""
    combined = f"{filename}\n" + "\n".join(pages_text[:5])
    lowered = combined.lower()
    scores: list[tuple[int, str]] = []
    for cat, keywords in _CATEGORY_RULES:
        hits = sum(1 for kw in keywords if kw in lowered)
        if hits:
            scores.append((hits, cat))
    scores.sort(reverse=True)
    if scores:
        category = scores[0][1]
    elif format_name in ("csv", "xlsx"):
        category = "spreadsheet"
    elif format_name == "pptx":
        category = "presentation"
    else:
        category = "general"

    tags: list[str] = [category]
    if format_name and format_name not in tags:
        tags.append(format_name)
    if ocr_used:
        tags.append("ocr")
    if tables_detected > 0:
        tags.append("tabular")
    if any(rut.is_valid(m.group(0)) for m in rut.PATTERN.finditer(combined)):
        tags.append("chile-rut")
    if _AMOUNT_RE.search(combined):
        tags.append("financial")
    return category, sorted(set(tags))[:12]


def _snippet(text: str, start: int, end: int, radius: int = 50) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return " ".join(text[left:right].split())


def extract_entities(pages_text: Sequence[str], *, limit_per_kind: int = 50) -> dict[str, list[dict[str, Any]]]:
    """Extract deterministic, verifiable entities (dates, amounts, IDs, emails, phones, names/addresses)."""
    detector = RuleEntityDetector(RuleConfig(mask_addresses=True, mask_names=True))
    out: dict[str, list[dict[str, Any]]] = {
        "dates": [],
        "amounts": [],
        "tax_ids": [],
        "emails": [],
        "phones": [],
        "names": [],
        "addresses": [],
    }
    seen: dict[str, set[tuple[str, int]]] = {k: set() for k in out}

    def add(kind: str, value: str, page: int, snippet: str) -> None:
        cleaned = " ".join(value.split()).strip()
        if not cleaned or has_active_content(cleaned):
            return
        if len(out[kind]) >= limit_per_kind:
            return
        key = (cleaned.casefold(), page)
        if key in seen[kind]:
            return
        seen[kind].add(key)
        out[kind].append({"value": cleaned, "page": page, "snippet": snippet[:200]})

    for page_num, text in enumerate(pages_text, start=1):
        if not text.strip():
            continue
        for m in _DATE_RE.finditer(text):
            add("dates", m.group(0), page_num, _snippet(text, m.start(), m.end()))
        for m in _AMOUNT_RE.finditer(text):
            add("amounts", m.group(0), page_num, _snippet(text, m.start(), m.end()))
        for m in rut.PATTERN.finditer(text):
            if rut.is_valid(m.group(0)):
                add("tax_ids", m.group(0), page_num, _snippet(text, m.start(), m.end()))
        for m in _DNI_RE.finditer(text):
            norm = re.sub(r"[- ]", "", m.group(0))
            if es_dni_ok(norm):
                add("tax_ids", m.group(0), page_num, _snippet(text, m.start(), m.end()))
        for m in _EMAIL_RE.finditer(text):
            add("emails", m.group(0), page_num, _snippet(text, m.start(), m.end()))
        for m in _PHONE_RE.finditer(text):
            if phone_ok(m.group(0)):
                add("phones", m.group(0), page_num, _snippet(text, m.start(), m.end()))
        for span in detector.detect(text):
            if span.kind == "PERSON":
                add("names", text[span.start:span.end], page_num, _snippet(text, span.start, span.end))
            elif span.kind == "ADDRESS":
                add("addresses", text[span.start:span.end], page_num, _snippet(text, span.start, span.end))
    return out


def summarize_documents(
    docs_pages: Sequence[tuple[str, str, Sequence[str]]],
    *,
    max_sentences: int = 5,
) -> dict[str, Any]:
    """Create a grounded extractive summary across one or more documents.

    ``docs_pages`` items are ``(document_id, filename, pages_text)``. Every sentence returned is
    verbatim from a real page and carries its citation; if no valid sentences exist, abstains.
    """
    candidates: list[tuple[str, str, int, str, set[str]]] = []
    doc_freq: Counter[str] = Counter()

    for doc_id, filename, pages in docs_pages:
        for page_num, page_text in enumerate(pages, start=1):
            for sentence, _, _ in split_sentences(page_text):
                clean = " ".join(sentence.split()).strip()
                if len(clean) < 20 or has_active_content(clean):
                    continue
                t = terms(clean)
                if not t:
                    continue
                candidates.append((doc_id, filename, page_num, clean, t))
                for tok in t:
                    doc_freq[tok] += 1

    if not candidates:
        return {"summary": ABSTENTION, "abstained": True, "bullet_points": [], "citations": []}

    n_sents = len(candidates)
    scored: list[tuple[float, int, str, str, int, str]] = []
    for idx, (doc_id, filename, page_num, clean, t) in enumerate(candidates):
        tfidf = sum(math.log(1 + n_sents / max(1, doc_freq[tok])) for tok in t) / math.sqrt(len(t))
        pos_bonus = 1.15 if idx < 3 or page_num == 1 else 1.0
        scored.append((tfidf * pos_bonus, -idx, doc_id, filename, page_num, clean))

    scored.sort(reverse=True)
    picked: list[tuple[int, str, str, int, str]] = []
    seen_text: set[str] = set()
    for _score, neg_idx, doc_id, filename, page_num, clean in scored:
        key = clean.casefold()
        if key in seen_text:
            continue
        seen_text.add(key)
        picked.append((-neg_idx, doc_id, filename, page_num, clean))
        if len(picked) >= max(1, min(max_sentences, 12)):
            break

    # Present in natural reading order.
    picked.sort(key=lambda item: item[0])
    bullet_points = [
        {"document_id": doc_id, "document": filename, "page": page_num, "text": clean}
        for _, doc_id, filename, page_num, clean in picked
    ]
    citations: list[dict[str, Any]] = []
    seen_cite: set[tuple[str, int]] = set()
    for _, doc_id, filename, page_num, clean in picked:
        if (doc_id, page_num) not in seen_cite:
            seen_cite.add((doc_id, page_num))
            citations.append(
                {
                    "document_id": doc_id,
                    "document": filename,
                    "page": page_num,
                    "label": f"{filename}, p.{page_num}",
                    "excerpt": clean[:300],
                }
            )
    return {
        "summary": " ".join(clean for _, _, _, _, clean in picked),
        "abstained": False,
        "bullet_points": bullet_points,
        "citations": citations,
    }


def compare_documents(
    docs_data: Sequence[dict[str, Any]],
    *,
    topic: str = "",
) -> dict[str, Any]:
    """Compare 2..5 documents: extracted field differences, textual conflicts and entity deltas.

    Each item in ``docs_data`` carries: ``document_id``, ``filename``, ``pages`` (list[str]),
    and ``fields`` (list of dicts: ``name``, ``value``, ``page``, ``snippet``).
    """
    field_names: list[str] = sorted({f["name"] for d in docs_data for f in d.get("fields", [])})
    field_comparisons: list[dict[str, Any]] = []
    differences_count = 0
    for fname in field_names:
        entries = []
        values_norm: set[str] = set()
        missing = 0
        for doc in docs_data:
            match = next((f for f in doc.get("fields", []) if f["name"] == fname), None)
            val = match["value"] if match else None
            if val is None or val == "":
                missing += 1
            else:
                values_norm.add(json.dumps(val, sort_keys=True, default=str))
            entries.append(
                {
                    "document_id": doc["document_id"],
                    "filename": doc["filename"],
                    "value": val,
                    "page": match.get("page") if match else None,
                    "snippet": match.get("snippet") if match else None,
                }
            )
        if len(values_norm) > 1:
            status = "different"
            differences_count += 1
        elif missing and values_norm:
            status = "missing_in_some"
            differences_count += 1
        else:
            status = "identical"
        field_comparisons.append({"field": fname, "status": status, "values": entries})

    # Textual conflicts (dates, numbers, negations) across document pages using ContextRAG's detector.
    hits: list[dict[str, Any]] = []
    for doc in docs_data:
        for pno, ptext in enumerate(doc.get("pages", []), start=1):
            if ptext.strip():
                hits.append(
                    {
                        "document_id": doc["document_id"],
                        "source_name": doc["filename"],
                        "page": pno,
                        "text": ptext[:4000],
                    }
                )
    query_hint = topic.strip() or " ".join(field_names) or "contract amount date policy payment"
    conflicts = [c.to_dict() for c in conflict_detection.detect(query_hint, hits)]

    # Entity comparison summary per document
    entities_by_doc = {}
    for doc in docs_data:
        ents = extract_entities(doc.get("pages", []), limit_per_kind=20)
        entities_by_doc[doc["document_id"]] = {
            "filename": doc["filename"],
            "dates": [e["value"] for e in ents["dates"]],
            "amounts": [e["value"] for e in ents["amounts"]],
            "tax_ids": [e["value"] for e in ents["tax_ids"]],
        }

    return {
        "documents": [{"document_id": d["document_id"], "filename": d["filename"]} for d in docs_data],
        "summary": {
            "documents_compared": len(docs_data),
            "fields_compared": len(field_comparisons),
            "differences_found": differences_count,
            "conflicts_found": len(conflicts),
        },
        "field_comparisons": field_comparisons,
        "conflicts": conflicts,
        "entities_by_document": entities_by_doc,
    }


def export_comparison(report: dict[str, Any], fmt: str) -> tuple[bytes, str, str]:
    """Serialize a comparison report to json, csv or xlsx (with formula neutralization)."""
    if fmt == "json":
        return json.dumps(report, default=str).encode("utf-8"), "application/json", "comparison.json"

    docs = report["documents"]
    columns = ["field", "status", *[d["filename"] for d in docs]]
    rows: list[list[object]] = []
    for item in report["field_comparisons"]:
        by_doc = {v["document_id"]: v["value"] for v in item["values"]}
        row: list[object] = [neutralize_formula(item["field"]), item["status"]]
        for d in docs:
            val = by_doc.get(d["document_id"])
            row.append(neutralize_formula(json.dumps(val) if isinstance(val, (dict, list)) else val))
        rows.append(row)
    for conflict in report["conflicts"]:
        sources = conflict.get("sources", [])
        left = f"{sources[0]['document']} p.{sources[0]['page']}: {sources[0]['text']}" if len(sources) > 0 else ""
        right = f"{sources[1]['document']} p.{sources[1]['page']}: {sources[1]['text']}" if len(sources) > 1 else ""
        row = [neutralize_formula(f"conflict:{conflict.get('kind', '')}"), "conflict", neutralize_formula(left), neutralize_formula(right)]
        while len(row) < len(columns):
            row.append("")
        rows.append(row[: len(columns)])

    if fmt == "csv":
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([str(neutralize_formula(c)) for c in columns])
        writer.writerows(rows)
        return out.getvalue().encode("utf-8"), "text/csv", "comparison.csv"

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Comparison"
    ws.append([str(neutralize_formula(c)) for c in columns])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return (
        buf.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "comparison.xlsx",
    )
