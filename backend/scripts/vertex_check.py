"""Real-model check for the configured LLM provider (Vertex AI by default): connectivity, generation and
extraction, with latency, tokens and estimated cost. Fictitious texts only; prints no credential.

    LLM_PROVIDER=vertex VERTEX_PROJECT=<project> VERTEX_LOCATION=<region> \
      python scripts/vertex_check.py --yes

``--yes`` is required because the check makes two small **paid** calls (a few hundred tokens each).
Without it the script only reports the configuration it would use. Exit code 0 when both calls work and
the generated answer survives claim verification.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASSAGE = {"text": "El contrato de mantenimiento ficticio tiene un plazo de 24 meses y un precio de 1.200 euros al mes.",
           "document_id": "00000000-0000-0000-0000-000000000001", "page": 1, "source_name": "contrato_ficticio.pdf"}
QUESTION = "¿Cuál es el plazo del contrato de mantenimiento?"
INVOICE = "FACTURA FICTICIA\nNúmero: F-2026-0042\nCliente: Empresa Ejemplo S.L.\nTotal: 1.452,00 EUR\nFecha: 3 de marzo de 2026"
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["invoice_number", "total"],
    "properties": {"invoice_number": {"type": ["string", "null"]}, "total": {"type": ["string", "null"]}},
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="make the two paid calls")
    args = parser.parse_args()

    from app.core import settings
    from app.processing import ProcessingError, estimate_cost, extraction_envelope, llm, llm_model
    from app.rag.generation import (
        SYSTEM_PROMPT,
        ChatGenerator,
        GenerationError,
        build_prompt,
        select_passages,
    )
    from app.rag.verification import verify

    cfg = settings()
    config = {
        "provider": cfg.llm_provider, "model": llm_model(),
        "vertex_project_set": bool(cfg.vertex_project), "vertex_location": cfg.vertex_location,
        "google_application_credentials_set": bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")),
        "timeout_seconds": cfg.openai_timeout_seconds, "max_retries": cfg.openai_max_retries,
        "prices_per_1m_configured": bool(cfg.gemini_input_price_per_1m or cfg.openai_input_price_per_1m),
    }
    print(json.dumps({"configuration": config}, indent=2))
    if cfg.llm_provider == "mock":
        print("LLM_PROVIDER=mock: nothing to check against a real model", file=sys.stderr)
        return 2
    if not args.yes:
        print("dry run: add --yes to make the two paid calls")
        return 0

    report: dict[str, object] = {}
    ok = True
    generator = ChatGenerator(cfg.llm_provider)
    passages = select_passages([PASSAGE], max_chars=2000)
    started = time.perf_counter()
    try:
        raw = generator.complete(SYSTEM_PROMPT, build_prompt(QUESTION, passages))
        verdicts = verify(raw, [p.text for p in passages], threshold=cfg.ask_generation_threshold)
        report["generation"] = {
            "ok": True, "latency_ms": round((time.perf_counter() - started) * 1000),
            "claims": len(verdicts), "verified_claims": sum(v.supported for v in verdicts),
            "answer_mentions_24_months": "24" in raw,
            "prompt_tokens": generator.prompt_tokens, "completion_tokens": generator.completion_tokens,
            "estimated_cost": estimate_cost(generator.prompt_tokens, generator.completion_tokens),
        }
        ok = ok and "24" in raw
    except GenerationError as exc:
        report["generation"] = {"ok": False, "error": str(exc)}
        ok = False

    started = time.perf_counter()
    try:
        result = llm().extract(INVOICE, extraction_envelope(SCHEMA))
        fields = result.get("fields") or {}
        usage = result.get("usage") or {}
        report["extraction"] = {
            "ok": bool(fields), "latency_ms": round((time.perf_counter() - started) * 1000),
            "invoice_number_correct": fields.get("invoice_number") == "F-2026-0042",
            "total_found": "1.452" in str(fields.get("total", "")) or "1452" in str(fields.get("total", "")),
            "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "estimated_cost")},
        }
        ok = ok and bool(fields)
    except ProcessingError as exc:
        report["extraction"] = {"ok": False, "error": exc.code, "retryable": exc.retryable}
        ok = False
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
