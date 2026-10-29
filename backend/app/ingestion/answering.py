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


@dataclass(frozen=True)
class Citation:
    document_id: str
    document: str  # display name of the source file
    page: int
    page_basis: str  # "explicit" or "estimated" (Word documents without page breaks)
    score: float

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


def _best_sentences(question: str, hits: Sequence[dict[str, Any]], limit: int = 2) -> list[tuple[str, dict[str, Any]]]:
    """The sentence(s) that best match the question.

    Terms are weighted by rarity among the retrieved chunks (a word present in every chunk,
    like "vacaciones" in a vacation policy, says little about which sentence answers); a second
    sentence is added only if it is nearly as good as the first.
    """
    wanted = terms(question)
    per_chunk = [terms(str(h.get("text", ""))) for h in hits]
    weight = {t: math.log(1 + len(hits) / max(1, sum(t in c for c in per_chunk))) for t in wanted}
    scored: list[tuple[float, int, str, dict[str, Any]]] = []
    for rank, hit in enumerate(hits):
        for sentence in (s.strip() for s in _SENTENCE.split(str(hit.get("text", "")))):
            if len(sentence) <= 15:
                continue
            overlap = sum(weight[t] for t in wanted & terms(sentence))
            if overlap:
                scored.append((overlap + 0.1 * float(hit.get("score", 0.0)), -rank, sentence, hit))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    picked: list[tuple[str, dict[str, Any]]] = []
    for score, _, sentence, hit in scored:
        if picked and score < 0.75 * scored[0][0]:
            break
        if all(sentence != p[0] for p in picked):
            picked.append((sentence, hit))
        if len(picked) == limit:
            break
    return picked


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
) -> Answer:
    """Answer ``question`` from this tenant's documents only.

    Scope, at most one of the two (neither = the whole tenant, which internal callers and the tests use;
    every endpoint passes one):

    * ``project_id``: one case. The vector query carries that condition.
    * ``project_ids``: the cases the caller may read. An empty list abstains without querying.

    Each hit is checked against the scope again, so a chunk without a project, or with another one,
    is never used. ``is_live(document_id)`` drops hits whose document was deleted but whose vectors are
    still waiting in the deletion outbox: a deleted document is never cited.
    """
    if project_id is not None and project_ids is not None:
        raise ValueError("ONE_SCOPE_ONLY")
    tenant = parse_tenant(tenant_id)
    project = parse_tenant(project_id) if project_id is not None else None
    allowed = {parse_tenant(p) for p in project_ids} if project_ids is not None else None
    question = question.strip()
    if not terms(question) or (allowed is not None and not allowed):
        return Answer(question, ABSTENTION, (), True)
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
    picked = _best_sentences(question, hits)
    if not picked:
        return Answer(question, ABSTENTION, (), True)
    citations: list[Citation] = []
    for _, hit in picked:
        citation = Citation(
            document_id=str(hit["document_id"]), document=str(hit.get("source_name") or hit["document_id"]),
            page=int(hit.get("page") or hit.get("page_start") or 0), page_basis=str(hit.get("page_basis", "explicit")),
            score=round(float(hit.get("score", 0.0)), 4),
        )
        if all((citation.document_id, citation.page) != (c.document_id, c.page) for c in citations):
            citations.append(citation)
    text = " ".join(sentence for sentence, _ in picked)
    return Answer(question, text, tuple(citations), False)
