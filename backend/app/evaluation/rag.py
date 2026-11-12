"""Reproducible RAG evaluation: ``python -m app.evaluation.rag``.

Ingests the fictitious two-tenant corpus in ``eval_corpus/rag_questions.json`` through the real
:class:`~app.ingestion.pipeline.IngestionPipeline` (sanitise, mask, page-bounded chunks, in-memory
Qdrant), then asks every question through the production code path
(:func:`app.ingestion.answering.retrieve` and :func:`~app.ingestion.answering.answer`) and scores:

Retrieval (answerable questions; a hit is relevant when its ``(document, page)`` is in the ground truth)
  * ``recall@k``    -- share of questions with at least one relevant chunk in the top k
  * ``precision@k`` -- relevant chunks among the top k retrieved (mean over questions)
  * ``mrr``         -- mean reciprocal rank of the first relevant chunk (0 when none in the top k)
  * ``context_relevance`` -- retrieved chunks that come from a relevant *document* (any page)

Answers
  * ``fact_accuracy``      -- the answer quotes the expected fact
  * ``citation_accuracy``  -- the first citation is a relevant ``(document, page)``
  * ``citation_precision`` -- citations that point to a relevant ``(document, page)``
  * ``faithfulness``       -- every answer sentence appears verbatim in the chunk its citation names
  * ``false_abstention_rate`` -- answerable questions the system refused

Abstention and isolation
  * ``abstention_accuracy`` -- unanswerable and cross-tenant questions that abstained
  * ``cross_tenant_leaks``  -- retrieved chunks or citations from another tenant (must be 0)

The embedder is :class:`~app.ingestion.embeddings.HashingEmbedder`: deterministic, offline and
**lexical, not semantic**. The numbers therefore measure this pipeline with that embedder; they are a
regression baseline, not a statement about semantic retrieval. Run with ``--embedder local`` (needs
``fastembed`` and the model files) to measure a semantic embedder on the same questions.
"""

import argparse
import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.ingestion.answering import answer, retrieve
from app.ingestion.docxgen import docx_bytes
from app.ingestion.embeddings import Embedder, HashingEmbedder
from app.ingestion.entities import RuleEntityDetector
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.vectorstore import QdrantStore

CORPUS = Path(__file__).resolve().parents[2] / "eval_corpus" / "rag_questions.json"
_NS = uuid.UUID("5b8f4c1a-2d3e-4f60-8a71-9c0b1d2e3f40")
KS = (1, 3, 5)
# The extended corpus (rag_questions_extended.json, ADR-005) adds identifier, table, long-document,
# contradiction (answerable, and the conflict must be reported) and injection (answerable, the malicious
# document must never be cited) questions.
EXTENDED = Path(__file__).resolve().parents[2] / "eval_corpus" / "rag_questions_extended.json"
ANSWERABLE = {"factual", "paraphrase", "identifier", "table", "long_document", "contradiction", "injection"}
MUST_ABSTAIN = {"unanswerable", "cross_tenant"}


@dataclass
class QuestionResult:
    tenant: str
    category: str
    question: str
    relevant: list[tuple[str, int]]
    retrieved: list[tuple[str, int, str]]  # (document, page, tenant) best first
    first_relevant_rank: int | None
    answer: str
    abstained: bool
    citations: list[tuple[str, int]]
    fact_ok: bool
    faithful: bool
    leaks: int
    notes: list[str] = field(default_factory=list)
    conflicts: int = 0
    expect_conflict: bool = False
    cited_forbidden: int = 0


def load(path: Path = CORPUS) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def tenant_ids(corpus: dict[str, Any]) -> dict[str, str]:
    return {key: str(uuid.uuid5(_NS, key)) for key in corpus["tenants"]}


def document_id(tenant: str, file: str) -> str:
    return str(uuid.uuid5(_NS, f"{tenant}/{file}"))


def build_pipeline(embedder: Embedder | None = None) -> IngestionPipeline:
    from qdrant_client import QdrantClient

    store = QdrantStore(QdrantClient(":memory:"), "rag_eval")
    return IngestionPipeline(
        store, embedder or HashingEmbedder(256), masker_extra=[RuleEntityDetector()], max_chars=500, overlap=60,
    )


def ingest(pipeline: IngestionPipeline, corpus: dict[str, Any], base_dir: Path | None = None) -> None:
    """Ingest the corpus. A document either lists its ``pages`` (generated as PDF or DOCX) or names a real
    file with ``path`` (relative to the corpus file, or absolute), for evaluating real documents."""
    from app.evaluation.pdfgen import digital_pdf

    tenants = tenant_ids(corpus)
    for doc in corpus["documents"]:
        if "path" in doc:
            file = Path(doc["path"])
            content = (file if file.is_absolute() else (base_dir or CORPUS.parent) / file).read_bytes()
        elif doc["file"].endswith(".docx"):
            content = docx_bytes(doc["pages"], author="Autor Ficticio")
        else:
            content = digital_pdf(doc["pages"])
        pipeline.ingest(
            tenant_id=tenants[doc["tenant"]], document_id=document_id(doc["tenant"], doc["file"]),
            content=content, source_name=doc["file"],
        )


def _evaluate_question(
    pipeline: IngestionPipeline, q: dict[str, Any], tenants: dict[str, str], top_k: int, own_docs: set[str],
    min_coverage: float = 0.5,
    min_similarity: float | None = None,
    mode: str | None = None,
    answer_mode: str = "extractive",
    reranker: Any = None,
) -> QuestionResult:
    tenant = tenants[q["tenant"]]
    relevant = [(d, int(p)) for d, p in q["relevant"]]
    hits = retrieve(pipeline, tenant_id=tenant, question=q["question"], top_k=top_k, mode=mode, reranker=reranker)
    retrieved = [(str(h.get("source_name")), int(h.get("page") or 0), str(h.get("tenant_id"))) for h in hits]
    first = next((i for i, (d, p, _) in enumerate(retrieved, start=1) if (d, p) in relevant), None)
    a = answer(pipeline, tenant_id=tenant, question=q["question"], top_k=top_k, min_coverage=min_coverage,
               min_similarity=min_similarity, mode=mode, answer_mode=answer_mode, reranker=reranker,
               generator=_offline_generator() if answer_mode == "generative" else None)
    citations = [(c.document, c.page) for c in a.citations]
    leaks = sum(t != tenant for _, _, t in retrieved) + sum(c.document_id not in own_docs for c in a.citations)
    # Faithfulness: every sentence of the answer is a verbatim quote of a chunk the answer cites.
    by_cite = {(str(h.get("source_name")), int(h.get("page") or 0)): str(h.get("text", "")) for h in hits}
    faithful = a.abstained or all(c.excerpt and c.excerpt in by_cite.get((c.document, c.page), "") for c in a.citations)
    fact_ok = bool(q["contains"]) and not a.abstained and q["contains"] in a.text
    if a.mode == "generative":
        # Generated sentences carry [n] markers; each cited excerpt must still be literal text of its chunk.
        faithful = a.abstained or all(c.excerpt in by_cite.get((c.document, c.page), "") for c in a.citations)
    forbidden = set(q.get("forbidden", []))
    return QuestionResult(
        tenant=q["tenant"], category=q["category"], question=q["question"], relevant=relevant,
        retrieved=[(d, p, t) for d, p, t in retrieved], first_relevant_rank=first, answer=a.text,
        abstained=a.abstained, citations=citations, fact_ok=fact_ok, faithful=faithful, leaks=leaks,
        conflicts=len(a.conflicts), expect_conflict=bool(q.get("expect_conflict")),
        cited_forbidden=sum(d in forbidden for d, _ in citations) + sum(d in forbidden for d, _, _ in retrieved),
    )


def _offline_generator() -> Any:
    from app.rag.generation import ExtractiveGenerator

    return ExtractiveGenerator()


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def score(results: list[QuestionResult], ks: tuple[int, ...] = KS) -> dict[str, Any]:
    answerable = [r for r in results if r.category in ANSWERABLE]
    refusals = [r for r in results if r.category in MUST_ABSTAIN]
    metrics: dict[str, Any] = {}
    for k in ks:
        metrics[f"recall@{k}"] = _mean([float(r.first_relevant_rank is not None and r.first_relevant_rank <= k) for r in answerable])
        metrics[f"precision@{k}"] = _mean([
            sum((d, p) in r.relevant for d, p, _ in r.retrieved[:k]) / k for r in answerable
        ])
    metrics["mrr"] = _mean([1.0 / r.first_relevant_rank if r.first_relevant_rank else 0.0 for r in answerable])
    metrics["context_relevance"] = _mean([
        sum(d in {rd for rd, _ in r.relevant} for d, _, _ in r.retrieved) / len(r.retrieved)
        for r in answerable if r.retrieved
    ])
    answered = [r for r in answerable if not r.abstained]
    metrics["fact_accuracy"] = _mean([float(r.fact_ok) for r in answerable])
    metrics["citation_accuracy"] = _mean([float(bool(r.citations) and r.citations[0] in r.relevant) for r in answerable])
    metrics["citation_precision"] = _mean([
        sum(c in r.relevant for c in r.citations) / len(r.citations) for r in answered
    ])
    metrics["faithfulness"] = _mean([float(r.faithful) for r in answered])
    metrics["false_abstention_rate"] = _mean([float(r.abstained) for r in answerable])
    metrics["abstention_accuracy"] = _mean([float(r.abstained) for r in refusals])
    metrics["cross_tenant_leaks"] = sum(r.leaks for r in results)
    contradictions = [r for r in results if r.expect_conflict]
    if contradictions:
        metrics["conflict_detection"] = _mean([float(r.conflicts > 0) for r in contradictions])
    flagged = [r for r in results if r.category == "injection"]
    if flagged:
        metrics["injection_documents_used"] = sum(r.cited_forbidden for r in flagged)  # must be 0
    by_category: dict[str, dict[str, Any]] = {}
    for cat in sorted({r.category for r in results}):
        rows = [r for r in results if r.category == cat]
        ok = [r.abstained if cat in MUST_ABSTAIN else (r.fact_ok and bool(r.citations) and r.citations[0] in r.relevant) for r in rows]
        by_category[cat] = {"questions": len(rows), "correct": sum(ok)}
    return {
        "questions": len(results), "answerable": len(answerable), "must_abstain": len(refusals),
        "metrics": metrics, "by_category": by_category,
    }


def run(
    embedder: Embedder | None = None, *, top_k: int = max(KS), corpus_path: Path = CORPUS, min_coverage: float = 0.5,
    min_similarity: float | None = None, mode: str | None = None, answer_mode: str = "extractive",
    reranker: Any = None,
) -> dict[str, Any]:
    from app.core import settings

    mode = mode or settings().retrieval_mode
    corpus = load(corpus_path)
    pipeline = build_pipeline(embedder)
    ingest(pipeline, corpus, corpus_path.parent)
    tenants = tenant_ids(corpus)
    docs_of = {
        key: {document_id(key, d["file"]) for d in corpus["documents"] if d["tenant"] == key} for key in tenants
    }
    results = [_evaluate_question(pipeline, q, tenants, top_k, docs_of[q["tenant"]], min_coverage, min_similarity, mode, answer_mode, reranker) for q in corpus["questions"]]
    for r in results:
        own = {d["file"] for d in corpus["documents"] if d["tenant"] == r.tenant}
        if r.category in ANSWERABLE and not r.fact_ok:
            r.notes.append("retrieval_miss" if r.first_relevant_rank is None else ("abstained" if r.abstained else "wrong_sentence"))
        if r.expect_conflict and not r.conflicts:
            r.notes.append("conflict_not_reported")
        if r.cited_forbidden:
            r.notes.append("INJECTION_DOCUMENT_USED")
        if r.category in MUST_ABSTAIN and not r.abstained:
            r.notes.append("answered_without_evidence" if all(d in own for d, _ in r.citations) else "LEAK")
    report = score(results)
    report["embedder"] = pipeline.embedder.name
    report["min_coverage"] = min_coverage
    report["retrieval"] = mode
    report["answer_mode"] = answer_mode
    report["reranker"] = getattr(reranker, "name", None)
    report["top_k"] = top_k
    report["corpus"] = {"documents": len(corpus["documents"]), "version": corpus.get("version", 1)}
    report["results"] = [
        {"tenant": r.tenant, "category": r.category, "question": r.question,
         "expected": [f"{d}, p.{p}" for d, p in r.relevant], "first_relevant_rank": r.first_relevant_rank,
         "citations": [f"{d}, p.{p}" for d, p in r.citations], "abstained": r.abstained,
         "answer": r.answer, "notes": r.notes}
        for r in results
    ]
    return report


def to_markdown(report: dict[str, Any]) -> str:
    m = report["metrics"]
    lines = [
        "# RAG evaluation report", "",
        (f"Embedder `{report['embedder']}` (lexical hashing unless stated otherwise), retrieval={report.get('retrieval', 'dense')}, top_k={report['top_k']}, min_coverage={report['min_coverage']}, "
        f"{report['corpus']['documents']} fictitious documents in 2 tenants, {report['questions']} questions "
        f"({report['answerable']} answerable, {report['must_abstain']} that must abstain)."), "",
        "Reproduce: `cd backend && python -m app.evaluation.rag --output-dir eval_reports`.", "",
        "## Metrics", "", "| Metric | Value |", "|---|---:|",
    ]
    lines += [f"| `{k}` | {v} |" for k, v in m.items()]
    lines += ["", "## By category", "", "| Category | Correct | Questions |", "|---|---:|---:|"]
    lines += [f"| {c} | {v['correct']} | {v['questions']} |" for c, v in report["by_category"].items()]
    lines += ["", "## Failures", ""]
    failures = [r for r in report["results"] if r["notes"]]
    if not failures:
        lines.append("None.")
    for r in failures:
        lines.append(
            f"* **{r['tenant']} / {r['category']}** — {r['question']} -> {', '.join(r['notes'])}; "
            f"expected {', '.join(r['expected']) or 'abstention'}, cited {', '.join(r['citations']) or '—'}"
        )
    lines += ["", "## Reading these numbers", "",
              ("* The hashing embedder matches words, not meaning: paraphrases with no shared terms are expected to miss. "
              "That is a limitation of the offline default, measured here on purpose."),
              ("* `answer()` is extractive: it quotes masked sentences and never generates text, so `faithfulness` "
              "checks that each quoted excerpt is literally in the cited chunk."),
              "* `cross_tenant_leaks` counts any retrieved chunk or citation from another organization; the gate requires 0."]
    return "\n".join(lines) + "\n"


def _embedder(name: str, model: str | None = None) -> Embedder:
    if name == "hashing":
        return HashingEmbedder(256)
    from app.core import settings
    from app.ingestion.embeddings import LocalEmbedder

    cfg = settings()
    return LocalEmbedder(model or cfg.embedding_model, local_files_only=cfg.embedding_local_files_only)


def _reranker(model: str | None) -> Any:
    if not model:
        return None
    from app.core import settings
    from app.rag.rerank import CrossEncoderReranker

    cfg = settings()
    return CrossEncoderReranker(model, cache_dir=str(cfg.embedding_cache_dir) if cfg.embedding_cache_dir else None,
                                local_files_only=cfg.embedding_local_files_only)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.evaluation.rag", description="RAG retrieval and citation evaluation")
    parser.add_argument("--embedder", choices=["hashing", "local"], default="hashing")
    parser.add_argument("--model", default=None, help="fastembed model for --embedder local (default: EMBEDDING_MODEL)")
    parser.add_argument("--min-coverage", type=float, default=0.5, help="sufficient-evidence threshold (ADR-002)")
    parser.add_argument("--min-similarity", type=float, default=None,
                        help="semantic evidence threshold (default: calibrated for the model, ADR-003)")
    parser.add_argument("--retrieval", choices=["dense", "hybrid"], default=None,
                        help="retrieval mode (default: RETRIEVAL_MODE, ADR-005)")
    parser.add_argument("--corpus", default="base",
                        help="base: rag_questions.json; extended: identifiers, tables, contradictions, injection "
                             "(ADR-005); or the path of a corpus JSON (real documents: docs/REAL_DOCUMENT_EVALUATION.md)")
    parser.add_argument("--answer-mode", choices=["extractive", "generative"], default="extractive",
                        help="generative uses the offline extractive generator: it measures verification, not a model")
    parser.add_argument("--rerank-model", default=None,
                        help="fastembed cross-encoder to rerank with (e.g. BAAI/bge-reranker-v2-m3-int8); default: none")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--min-recall-at-5", type=float, default=None, help="fail (exit 1) below this recall@5")
    parser.add_argument("--min-citation-accuracy", type=float, default=None)
    parser.add_argument("--min-abstention-accuracy", type=float, default=None)
    args = parser.parse_args(argv)
    report = run(_embedder(args.embedder, args.model), min_coverage=args.min_coverage,
                 min_similarity=args.min_similarity, mode=args.retrieval, answer_mode=args.answer_mode,
                 corpus_path={"base": CORPUS, "extended": EXTENDED}.get(args.corpus) or Path(args.corpus),
                 reranker=_reranker(args.rerank_model))
    text = to_markdown(report)
    print(text)
    if args.output_dir is not None:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "rag-report.md").write_text(text, encoding="utf-8")
        (args.output_dir / "rag-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    m = report["metrics"]
    failed = [
        name for name, value, floor in (
            ("recall@5", m["recall@5"], args.min_recall_at_5),
            ("citation_accuracy", m["citation_accuracy"], args.min_citation_accuracy),
            ("abstention_accuracy", m["abstention_accuracy"], args.min_abstention_accuracy),
        ) if floor is not None and value < floor
    ]
    if m["cross_tenant_leaks"]:
        failed.append("cross_tenant_leaks")
    if m.get("injection_documents_used"):
        failed.append("injection_documents_used")
    if failed:
        print(f"RAG quality gate failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
