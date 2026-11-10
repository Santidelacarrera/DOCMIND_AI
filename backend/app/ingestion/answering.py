"""Answers that always say where they came from.

``answer()`` is **extractive and local**: it retrieves the tenant's most relevant chunks,
picks the sentences that share the most terms with the question and returns them verbatim
(masked text), each tied to a :class:`Citation` -- document name, document id and page.
It uses no language model and makes no network call, so it costs nothing and cannot invent a
fact: the worst case is "not found".

Invariant (enforced in ``Answer.__post_init__`` and tested): an answer either has at least one
citation with a document and a page >= 1, or it is an explicit abstention with none. There is no
third state in which text is returned without a source.
"""

import math
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.ingestion.chunking import parse_tenant
from app.ingestion.pipeline import IngestionPipeline

ABSTENTION = "No encontré información sobre eso en los documentos disponibles."
_STOP = frozenset(
    ["a", "al", "algo", "algunos", "ante", "como", "con", "contra", "cual", "cuando", "de", "del", "desde", "donde", "el", "ella", "ellos", "en", "entre", "era", "es", "esa", "ese", "eso", "esta", "este", "esto", "fue", "ha", "hay", "la", "las", "le", "les", "lo", "los", "me", "mi", "mis", "muy", "no", "nos", "o", "para", "pero", "por", "que", "quien", "se", "ser", "si", "sin", "sobre", "su", "sus", "te", "todo", "un", "una", "uno", "unos", "y", "ya", "the", "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "has", "have", "how", "in", "is", "it", "its", "of", "on", "or", "that", "the", "their", "there", "this", "to", "was", "were", "what", "when", "where", "which", "who", "will", "with", "cuál", "cuáles", "cuánto", "cuántos", "cuántas", "qué", "quién"]
)
_WORD = re.compile(r"[\wáéíóúñü]+", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
EXCERPT_CHARS = 300  # bound on the quoted supporting sentence returned with each citation
# Semantic evidence thresholds, measured per model with app.evaluation.rag (ADR-003). Similarity
# scales differ between models: bge-small-en scores unrelated Spanish sentences above 0.5, so a
# threshold is only trusted for the model it was measured on. Others use the lexical rule.
CALIBRATED_SIMILARITY = {
    "local:sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 0.5,
}


@dataclass(frozen=True)
class Citation:
    document_id: str
    document: str  # display name of the source file
    page: int
    page_basis: str  # "explicit" or "estimated" (Word documents without page breaks)
    score: float
    # Locates the supporting fragment: the chunk's position in the document and the verbatim
    # (masked) sentence quoted from it. Optional so callers built before they existed still work.
    chunk_index: int | None = None
    excerpt: str = ""

    def __post_init__(self) -> None:
        if not self.document_id or not self.document or self.page < 1:
            raise ValueError("CITATION_INCOMPLETE")

    def label(self) -> str:
        approx = "~" if self.page_basis == "estimated" else ""
        return f"{self.document}, p.{approx}{self.page}"


@dataclass(frozen=True)
class Answer:
    question: str
    text: str
    citations: tuple[Citation, ...] = field(default_factory=tuple)
    abstained: bool = False

    def __post_init__(self) -> None:
        if self.abstained == bool(self.citations):
            raise ValueError("ANSWER_WITHOUT_SOURCE" if not self.abstained else "ABSTENTION_WITH_CITATIONS")


def _stem(word: str) -> str:
    """Crude prefix stem (es/en): ``solicitudes``/``solicitan``/``solicitar`` meet at ``solici``."""
    word = unicodedata.normalize("NFKD", word.lower())
    word = "".join(c for c in word if not unicodedata.combining(c))
    return word[:6]


def terms(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(text) if len(w) > 2 and w.lower() not in _STOP}


def _best_sentences(
    question: str, hits: Sequence[dict[str, Any]], limit: int = 2, min_coverage: float = 0.0,
) -> list[tuple[str, dict[str, Any]]]:
    """The sentence(s) that best match the question.

    Terms are weighted by rarity among the retrieved chunks (a word present in every chunk,
    like "vacaciones" in a vacation policy, says little about which sentence answers); a second
    sentence is added only if it is nearly as good as the first.

    **Sufficient evidence.** The best sentence must cover at least ``min_coverage`` of the
    question's total term weight, or nothing is returned and the caller abstains. A term found
    in no retrieved chunk keeps the highest weight, so a question about something the documents
    never mention cannot be "answered" by a sentence that only shares a generic word.
    """
    wanted = terms(question)
    per_chunk = [terms(str(h.get("text", ""))) for h in hits]
    weight = {t: math.log(1 + len(hits) / max(1, sum(t in c for c in per_chunk))) for t in wanted}
    total = sum(weight.values())

    def coverage(sentence: str) -> float:
        return sum(weight[t] for t in wanted & terms(sentence)) / total if total else 0.0

    scored: list[tuple[float, int, str, dict[str, Any]]] = []
    for rank, hit in enumerate(hits):
        for sentence in (s.strip() for s in _SENTENCE.split(str(hit.get("text", "")))):
            if len(sentence) <= 15:
                continue
            overlap = sum(weight[t] for t in wanted & terms(sentence))
            if overlap:
                scored.append((overlap + 0.1 * float(hit.get("score", 0.0)), -rank, sentence, hit))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    if not scored or total <= 0 or coverage(scored[0][2]) < min_coverage:
        return []
    return _pick(scored, limit, 0.75)


def _pick(
    scored: list[tuple[float, int, str, dict[str, Any]]], limit: int, keep: float,
) -> list[tuple[str, dict[str, Any]]]:
    """Best-first sentences; a further one only if it scores at least ``keep`` x the best."""
    picked: list[tuple[str, dict[str, Any]]] = []
    for score, _, sentence, hit in scored:
        if picked and score < keep * scored[0][0]:
            break
        if all(sentence != p[0] for p in picked):
            picked.append((sentence, hit))
        if len(picked) == limit:
            break
    return picked


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / norm if norm else 0.0


def _best_sentences_semantic(
    question: str, hits: Sequence[dict[str, Any]], question_vector: Sequence[float],
    embed: Callable[[list[str]], list[list[float]]], *, limit: int = 2,
    min_coverage: float = 0.5, min_similarity: float = 0.5,
) -> list[tuple[str, dict[str, Any]]]:
    """Like :func:`_best_sentences`, ranked by meaning (cosine with the question vector).

    Used only with a semantic embedder. Sufficient evidence: the best sentence covers at least
    ``min_coverage`` of the question's terms (the lexical rule) **or** is at least
    ``min_similarity`` similar in meaning, so a paraphrase with no shared words can be answered,
    while a sentence about something else still abstains (docs/decisions/ADR-003).
    """
    candidates = [
        (sentence, rank, hit)
        for rank, hit in enumerate(hits)
        for sentence in (s.strip() for s in _SENTENCE.split(str(hit.get("text", ""))))
        if len(sentence) > 15
    ]
    if not candidates:
        return []
    vectors = embed([sentence for sentence, _, _ in candidates])
    scored = sorted(
        ((_cosine(question_vector, v), -rank, sentence, hit) for (sentence, rank, hit), v in zip(candidates, vectors, strict=True)),
        key=lambda t: (t[0], t[1]), reverse=True,
    )
    best = scored[0]
    if best[0] < min_similarity and _coverage(question, hits, best[2]) < min_coverage:
        return []
    return _pick(scored, limit, 0.95)


def _coverage(question: str, hits: Sequence[dict[str, Any]], sentence: str) -> float:
    """Share of the question's rarity-weighted terms that ``sentence`` contains (see _best_sentences)."""
    wanted = terms(question)
    per_chunk = [terms(str(h.get("text", ""))) for h in hits]
    weight = {t: math.log(1 + len(hits) / max(1, sum(t in c for c in per_chunk))) for t in wanted}
    total = sum(weight.values())
    return sum(weight[t] for t in wanted & terms(sentence)) / total if total else 0.0


def answer(
    pipeline: IngestionPipeline,
    *,
    tenant_id: str,
    question: str,
    top_k: int = 4,
    min_score: float = 0.05,
    is_live: Callable[[str], bool] | None = None,
    project_id: str | None = None,
    project_ids: Sequence[str] | None = None,
    min_coverage: float = 0.5,
    min_similarity: float | None = None,
) -> Answer:
    """Answer ``question`` from this tenant's documents only.

    Scope, at most one of the two (neither = the whole tenant, which internal callers and the tests use;
    every endpoint passes one):

    * ``project_id``: one case. The vector query carries that condition.
    * ``project_ids``: the cases the caller may read. An empty list abstains without querying.

    Each hit is checked against the scope again, so a chunk without a project, or with another one,
    is never used. ``is_live(document_id)`` drops hits whose document was deleted but whose vectors are
    still waiting in the deletion outbox: a deleted document is never cited.

    ``min_coverage`` is the sufficient-evidence threshold (see :func:`_best_sentences`); the API
    passes ``ASK_MIN_EVIDENCE_COVERAGE``. 0 restores the old "any shared term" behaviour.
    With a semantic embedder (``embedder.semantic``) that has a threshold -- ``min_similarity``
    (``ASK_MIN_SEMANTIC_SIMILARITY``) or, when that is ``None``, its entry in
    :data:`CALIBRATED_SIMILARITY` -- sentences are ranked by meaning and that similarity is a second
    way to be sufficient evidence. Without a threshold the lexical rule alone applies.
    """
    question = question.strip()
    hits, vector = _retrieve(
        pipeline, tenant_id=tenant_id, question=question, top_k=top_k, min_score=min_score,
        is_live=is_live, project_id=project_id, project_ids=project_ids,
    )
    threshold = min_similarity if min_similarity is not None else CALIBRATED_SIMILARITY.get(
        str(getattr(pipeline.embedder, "name", ""))
    )
    if vector is not None and threshold is not None and getattr(pipeline.embedder, "semantic", False):
        picked = _best_sentences_semantic(
            question, hits, vector, pipeline.embedder.embed,
            min_coverage=min_coverage, min_similarity=threshold,
        )
    else:
        picked = _best_sentences(question, hits, min_coverage=min_coverage)
    if not picked:
        return Answer(question, ABSTENTION, (), True)
    citations: list[Citation] = []
    for sentence, hit in picked:
        citation = Citation(
            document_id=str(hit["document_id"]), document=str(hit.get("source_name") or hit["document_id"]),
            page=int(hit.get("page") or hit.get("page_start") or 0), page_basis=str(hit.get("page_basis", "explicit")),
            score=round(float(hit.get("score", 0.0)), 4),
            chunk_index=_chunk_index(hit), excerpt=sentence[:EXCERPT_CHARS],
        )
        if all((citation.document_id, citation.page) != (c.document_id, c.page) for c in citations):
            citations.append(citation)
    text = " ".join(sentence for sentence, _ in picked)
    return Answer(question, text, tuple(citations), False)


def _chunk_index(hit: dict[str, Any]) -> int | None:
    value = hit.get("chunk_index")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def retrieve(
    pipeline: IngestionPipeline,
    *,
    tenant_id: str,
    question: str,
    top_k: int = 4,
    min_score: float = 0.05,
    is_live: Callable[[str], bool] | None = None,
    project_id: str | None = None,
    project_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """The chunks ``answer()`` reads, best first, after every scope and liveness check.

    Exposed so the RAG evaluation (``app.evaluation.rag``) measures the exact production retrieval
    path instead of a copy of it. Same scope rules as :func:`answer`.
    """
    return _retrieve(
        pipeline, tenant_id=tenant_id, question=question, top_k=top_k, min_score=min_score,
        is_live=is_live, project_id=project_id, project_ids=project_ids,
    )[0]


def _retrieve(
    pipeline: IngestionPipeline,
    *,
    tenant_id: str,
    question: str,
    top_k: int,
    min_score: float,
    is_live: Callable[[str], bool] | None,
    project_id: str | None,
    project_ids: Sequence[str] | None,
) -> tuple[list[dict[str, Any]], list[float] | None]:
    """Hits plus the (masked) question vector, so ``answer()`` does not embed the question twice."""
    if project_id is not None and project_ids is not None:
        raise ValueError("ONE_SCOPE_ONLY")
    tenant = parse_tenant(tenant_id)
    project = parse_tenant(project_id) if project_id is not None else None
    allowed = {parse_tenant(p) for p in project_ids} if project_ids is not None else None
    question = question.strip()
    if not terms(question) or (allowed is not None and not allowed):
        return [], None
    vector = pipeline.query_vector(tenant_id=tenant, question=question)
    hits = [
        h for h in pipeline.store.search(
            tenant, vector, limit=top_k, project_id=project,
            project_ids=sorted(allowed) if allowed is not None else None,
        )
        if h.get("score", 0.0) >= min_score and h.get("tenant_id") == tenant
        and (project is None or h.get("project_id") == project)
        and (allowed is None or h.get("project_id") in allowed)
        and (is_live is None or is_live(str(h.get("document_id"))))
    ]
    return hits, vector
