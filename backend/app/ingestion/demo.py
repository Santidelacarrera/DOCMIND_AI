"""Offline demo with fictitious documents: ``python -m app.ingestion.demo``.

Builds a few synthetic PDF and Word documents for two fictitious companies, ingests them
(sanitise -> mask PII -> page-bounded tenant chunks -> in-memory Qdrant), then answers
questions and shows, for every answer, the document and page it came from. Nothing leaves the
machine: no network, no model download, no account, no cost.

Names, e-mail addresses, the IBAN (a published example number) and every fact below are made up.
"""

import argparse
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ingestion.answering import Answer, answer
from app.ingestion.docxgen import docx_bytes
from app.ingestion.embeddings import HashingEmbedder
from app.ingestion.entities import RuleEntityDetector
from app.ingestion.pipeline import IngestionPipeline, IngestionReport
from app.ingestion.vectorstore import QdrantStore

_NS = uuid.UUID("0f1e2d3c-4b5a-4968-8776-655443322110")
TENANTS = {
    "Aurora Demo S.L. (ficticia)": str(uuid.uuid5(_NS, "aurora")),
    "Orion Demo Ltd (ficticia)": str(uuid.uuid5(_NS, "orion")),
}
AURORA, ORION = TENANTS


@dataclass(frozen=True)
class DemoDoc:
    tenant: str
    filename: str
    pages: list[list[str]]


DOCS: list[DemoDoc] = [
    DemoDoc(AURORA, "politica_vacaciones.docx", [
        ["POLÍTICA DE VACACIONES", "Esta política describe cómo se gestionan las vacaciones del personal de Aurora Demo."],
        ["Los empleados tienen derecho a 23 días laborables de vacaciones al año.",
         "Las solicitudes se envían con 15 días de antelación a rrhh@aurora-demo.example.test.",
         "Responsable de recursos humanos: Nombre: Elena Ejemplo Prueba."],
        ["Se pueden trasladar hasta 5 días de vacaciones al año siguiente, antes del 31 de marzo."],
    ]),
    DemoDoc(AURORA, "contrato_servicios.pdf", [
        ["CONTRATO DE SERVICIOS", "Contrato entre Aurora Demo S.L. y su cliente ficticio.",
         "Firmado por Mario Ejemplo Prueba el día de la firma."],
        ["La duración del contrato de servicios es de 24 meses a partir del 1 de marzo de 2026."],
        ["El precio total del contrato es de 48.000 euros, pagaderos trimestralmente.",
         "IBAN de pago: ES91 2100 0418 4502 0005 1332."],
        ["Cualquiera de las partes puede resolver el contrato con 60 días de preaviso por escrito."],
    ]),
    DemoDoc(AURORA, "manual_seguridad.pdf", [
        ["MANUAL DE SEGURIDAD", "Las contraseñas deben tener al menos 14 caracteres y cambiarse cada 90 días."],
        ["Los incidentes de seguridad se notifican al equipo de respuesta en un máximo de 24 horas."],
    ]),
    DemoDoc(ORION, "politica_vacaciones.pdf", [
        ["VACATION POLICY", "Los empleados tienen derecho a 30 días laborables de vacaciones al año."],
    ]),
]


@dataclass(frozen=True)
class DemoQuestion:
    tenant: str
    question: str
    document: str | None  # expected cited document, None = must abstain
    page: int | None
    contains: str = ""  # the fact the answer text must contain


QUESTIONS: list[DemoQuestion] = [
    DemoQuestion(AURORA, "¿Cuántos días de vacaciones tienen los empleados?", "politica_vacaciones.docx", 2, "23 días"),
    DemoQuestion(AURORA, "¿Con cuánta antelación se solicitan las vacaciones?", "politica_vacaciones.docx", 2, "15 días"),
    DemoQuestion(AURORA, "¿Cuántos días de vacaciones se pueden trasladar al año siguiente?", "politica_vacaciones.docx", 3, "5 días"),
    DemoQuestion(AURORA, "¿Cuál es la duración del contrato de servicios?", "contrato_servicios.pdf", 2, "24 meses"),
    DemoQuestion(AURORA, "¿Cuál es el precio total del contrato?", "contrato_servicios.pdf", 3, "48.000"),
    DemoQuestion(AURORA, "¿Con cuántos días de preaviso se puede resolver el contrato?", "contrato_servicios.pdf", 4, "60 días"),
    DemoQuestion(AURORA, "¿Cada cuántos días deben cambiarse las contraseñas?", "manual_seguridad.pdf", 1, "90 días"),
    DemoQuestion(AURORA, "¿En cuánto tiempo se notifican los incidentes de seguridad?", "manual_seguridad.pdf", 2, "24 horas"),
    DemoQuestion(ORION, "¿Cuántos días de vacaciones tienen los empleados?", "politica_vacaciones.pdf", 1, "30 días"),
    DemoQuestion(AURORA, "¿Cuál es el color favorito del gerente?", None, None),
]


def build_files() -> dict[str, bytes]:
    from app.evaluation.pdfgen import digital_pdf

    files = {}
    for d in DOCS:
        files[f"{d.tenant}/{d.filename}"] = docx_bytes(d.pages, author="Autor Ficticio") if d.filename.endswith(".docx") else digital_pdf(d.pages)
    return files


def build_pipeline() -> IngestionPipeline:
    from qdrant_client import QdrantClient

    store = QdrantStore(QdrantClient(":memory:"), "demo")
    return IngestionPipeline(store, HashingEmbedder(256), masker_extra=[RuleEntityDetector()], max_chars=500, overlap=60)


def ingest_all(pipeline: IngestionPipeline) -> list[tuple[DemoDoc, IngestionReport]]:
    files = build_files()
    out = []
    for d in DOCS:
        report = pipeline.ingest(
            tenant_id=TENANTS[d.tenant], document_id=str(uuid.uuid5(_NS, f"{d.tenant}/{d.filename}")),
            content=files[f"{d.tenant}/{d.filename}"], source_name=d.filename,
        )
        out.append((d, report))
    return out


def run(output_dir: Path | None = None) -> dict[str, Any]:
    pipeline = build_pipeline()
    ingested = ingest_all(pipeline)
    rows: list[dict[str, Any]] = []
    for q in QUESTIONS:
        a: Answer = answer(pipeline, tenant_id=TENANTS[q.tenant], question=q.question)
        # Strict: the *first* citation is the expected document and page, and the fact is in the text.
        first = (a.citations[0].document, a.citations[0].page) if a.citations else None
        ok = a.abstained if q.document is None else (first == (q.document, q.page) and q.contains in a.text)
        rows.append({"tenant": q.tenant, "question": q.question, "answer": a.text, "citations": [c.label() for c in a.citations],
                     "expected": None if q.document is None else f"{q.document}, p.{q.page}", "ok": ok,
                     "abstained": a.abstained})
    result = {
        "documents": [{"tenant": d.tenant, "file": d.filename, "format": r.format, "pages": r.pages,
                       "chunks": r.chunks, "pii_masked": r.pii} for d, r in ingested],
        "questions": rows, "correct": sum(r["ok"] for r in rows), "total": len(rows),
    }
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "demo-report.md").write_text(to_markdown(result), encoding="utf-8")
        for name, content in build_files().items():
            target = output_dir / "documentos_ficticios" / name.replace("/", "__").replace(" ", "_").replace("(", "").replace(")", "")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    return result


def to_markdown(result: dict[str, Any]) -> str:
    lines = ["# Demo con documentos ficticios", "", "Nexus Security & AI Lab · procesamiento 100 % local · coste $0", "",
             "## Documentos ingeridos", "", "| Empresa ficticia | Archivo | Formato | Páginas | Fragmentos | PII enmascarada |", "|---|---|---|---:|---:|---|"]
    for d in result["documents"]:
        pii = ", ".join(f"{k}×{v}" for k, v in sorted(d["pii_masked"].items())) or "—"
        lines.append(f"| {d['tenant']} | {d['file']} | {d['format']} | {d['pages']} | {d['chunks']} | {pii} |")
    lines += ["", "## Preguntas y respuestas con fuente", ""]
    for r in result["questions"]:
        sources = "; ".join(r["citations"]) or "—"
        lines += [f"**{r['tenant']}** — {r['question']}", "", f"> {r['answer']}", "",
                  f"Fuente: {sources} · esperado: {r['expected'] or 'sin respuesta'} · {'✅' if r['ok'] else '❌'}", ""]
    lines.append(f"Citas correctas: **{result['correct']}/{result['total']}**")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ingestion.demo", description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=None, help="also write demo-report.md and the fictitious files here")
    args = parser.parse_args(argv)
    result = run(args.output_dir)
    print(to_markdown(result))
    return 0 if result["correct"] == result["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
