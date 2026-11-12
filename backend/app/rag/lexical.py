"""The lexical half of hybrid retrieval: BM25 over a tenant's canonical chunks.

The corpus is the tenant's chunk payloads in the vector collection (``VectorStore.scroll_chunks``,
tenant condition inside the Qdrant filter, suspicious chunks excluded), so a lexical hit is the very
same chunk -- document, page, chunk index, masked text -- that dense search and citations use.

Cache: one corpus per (store, tenant) per process, rebuilt after ``ttl`` seconds or after
:func:`invalidate` (called when this process indexes or deletes a document). Another process may hold
a corpus up to ``ttl`` seconds old. That is safe in one direction and documented in the other:

* a deleted document still in a stale corpus is dropped by the caller's ``is_live`` check and by the
  scope checks every hit goes through (``answering._retrieve``), so it is never cited;
* a newly indexed document is found by dense search at once and by lexical search within ``ttl``.

A tenant with more than ``max_chunks`` chunks is not loaded at all: lexical search is skipped for it
(metric ``docmind_retrieval_lexical_total{outcome="too_large"}``) and retrieval stays dense only.
"""

import threading
import time
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.metrics import RETRIEVAL_LEXICAL_TOTAL
from app.rag.bm25 import Bm25Index


@dataclass(frozen=True)
class _Corpus:
    chunks: list[dict[str, Any]]
    index: Bm25Index
    loaded_at: float


_cache: "weakref.WeakKeyDictionary[Any, dict[str, _Corpus | None]]" = weakref.WeakKeyDictionary()
_lock = threading.Lock()


def invalidate(tenant_id: str | None = None) -> None:
    """Forget cached corpora: of one tenant, or of every tenant when ``tenant_id`` is None."""
    with _lock:
        for per_store in _cache.values():
            if tenant_id is None:
                per_store.clear()
            else:
                per_store.pop(tenant_id, None)


def _corpus(store: Any, tenant: str, tokenize: Callable[[str], list[str]], ttl: float, max_chunks: int) -> _Corpus | None:
    now = time.monotonic()
    with _lock:
        per_store = _cache.setdefault(store, {})
        if tenant in per_store:
            cached = per_store[tenant]
            if cached is None or now - cached.loaded_at < ttl:
                RETRIEVAL_LEXICAL_TOTAL.labels("cached" if cached else "too_large").inc()
                return cached
    chunks = store.scroll_chunks(tenant, limit=max_chunks)
    corpus = None if chunks is None else _Corpus(chunks, Bm25Index([tokenize(str(c.get("text", ""))) for c in chunks]), now)
    RETRIEVAL_LEXICAL_TOTAL.labels("loaded" if corpus else "too_large").inc()
    with _lock:
        _cache.setdefault(store, {})[tenant] = corpus
    return corpus


def search(
    store: Any,
    tenant: str,
    query_tokens: Sequence[str],
    tokenize: Callable[[str], list[str]],
    *,
    limit: int,
    ttl: float,
    max_chunks: int,
    project_id: str | None = None,
    project_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Chunk payloads best first (``bm25`` score added), limited to the given scope.

    Scope rules are those of dense search: ``project_id`` one case, ``project_ids`` a list (empty means
    nothing), neither the whole tenant. A chunk without a project never matches a scoped query.
    """
    if limit <= 0 or not query_tokens or (project_ids is not None and not project_ids):
        return []
    corpus = _corpus(store, tenant, tokenize, ttl, max_chunks)
    if corpus is None:
        return []
    allowed = set(project_ids) if project_ids is not None else None
    out: list[dict[str, Any]] = []
    for position, score in corpus.index.search(query_tokens, corpus.index.size):
        chunk = corpus.chunks[position]
        if chunk.get("tenant_id") != tenant:
            continue
        if project_id is not None and chunk.get("project_id") != project_id:
            continue
        if allowed is not None and chunk.get("project_id") not in allowed:
            continue
        out.append({**chunk, "bm25": round(score, 4)})
        if len(out) == limit:
            break
    return out
