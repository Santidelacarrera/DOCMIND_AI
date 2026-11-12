"""Second-stage reranking with a cross-encoder (ported from ContextRAG_Engine ``retrieval/reranker.py``).

A bi-encoder (the embedder) scores question and passage separately; a cross-encoder reads them
together and scores relevance directly, which is better at telling the passage that *answers* from one
that merely shares the topic. It is also slower (one model pass per candidate), so it runs only on the
fused top ``RERANK_CANDIDATES``.

Implementation: ``fastembed.TextCrossEncoder`` (ONNX, no ``torch``; the ``embeddings`` extra DOCMIND
already has). Model files are read from ``EMBEDDING_CACHE_DIR`` and are never downloaded at request
time unless ``EMBEDDING_LOCAL_FILES_ONLY=false``.

Failure policy (DOCMIND's, deliberately different from ContextRAG's): a reranker that times out or fails
does **not** fail the question. The first-stage order is kept, the event is counted
(``docmind_rerank_total{outcome}``) and logged. Reranking only reorders candidates that already passed
every tenant, scope and liveness check, so falling back can lower ranking quality but never widens what
can be cited. ContextRAG fails the request instead; for an extractive answer that still carries its own
sufficient-evidence check, availability is the better trade.
"""

import logging
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Protocol

from app.metrics import RERANK_TOTAL

logger = logging.getLogger("docmind.rag")


class Reranker(Protocol):
    name: str

    def score(self, query: str, passages: Sequence[str]) -> list[float]: ...


class RerankerError(RuntimeError):
    pass


class CrossEncoderReranker:
    def __init__(self, model: str, *, cache_dir: str | None = None, local_files_only: bool = True) -> None:
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError:
            raise RerankerError("fastembed is not installed (pip install '.[embeddings]')") from None
        try:
            self._model = TextCrossEncoder(model_name=model, cache_dir=cache_dir, local_files_only=local_files_only)
        except Exception as exc:  # missing files with local_files_only, unknown model, corrupt download
            raise RerankerError(f"reranker model unavailable: {type(exc).__name__}") from None
        self.name = f"cross-encoder:{model}"

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        return [float(s) for s in self._model.rerank(query, list(passages))]


class BoundedReranker:
    """Hard timeout and validated output around any reranker (ContextRAG's ``BoundedReranker``).

    A timed-out call keeps running in its worker thread (Python cannot kill it), which is why the pool is
    small and bounded: a stuck model cannot pile up threads."""

    def __init__(self, inner: Reranker, timeout_s: float, *, max_workers: int = 2) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        self._inner, self._timeout = inner, timeout_s
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rerank")
        self.name = inner.name

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        future = self._pool.submit(self._inner.score, query, list(passages))
        try:
            scores = future.result(timeout=self._timeout)
        except FutureTimeout:
            future.cancel()
            raise RerankerError("timeout") from None
        except RerankerError:
            raise
        except Exception as exc:
            raise RerankerError(type(exc).__name__) from None
        if len(scores) != len(passages):
            raise RerankerError("score count mismatch")
        return list(scores)


def rerank(reranker: Reranker | None, query: str, hits: list[dict[str, Any]], *, candidates: int) -> list[dict[str, Any]]:
    """``hits`` reordered by cross-encoder score (``rerank`` added to each), the first ``candidates`` only;
    the rest keep their order after them. Any failure returns ``hits`` unchanged (see module docstring)."""
    if reranker is None or len(hits) < 2:
        return hits
    head, tail = hits[:candidates], hits[candidates:]
    try:
        scores = reranker.score(query, [str(h.get("text", "")) for h in head])
    except RerankerError as exc:
        RERANK_TOTAL.labels("failed").inc()
        logger.warning("rerank skipped", extra={"error": str(exc)[:80]})
        return hits
    RERANK_TOTAL.labels("reranked").inc()
    ordered = sorted(zip(head, scores, strict=True), key=lambda pair: -pair[1])
    return [{**hit, "rerank": round(score, 4)} for hit, score in ordered] + tail


_instance: Reranker | None = None
_instance_key: tuple[Any, ...] | None = None
_lock = threading.Lock()


def configured() -> Reranker | None:
    """The process's reranker from settings (``RERANK_MODEL``), built once; ``None`` when not configured.
    A model that cannot be loaded is reported once and reranking stays off (counted as ``unavailable``)."""
    global _instance, _instance_key
    from app.core import settings

    cfg = settings()
    if not cfg.rerank_model:
        return None
    key = (cfg.rerank_model, cfg.embedding_cache_dir, cfg.embedding_local_files_only, cfg.rerank_timeout_seconds)
    with _lock:
        if key != _instance_key:
            _instance_key = key
            try:
                inner = CrossEncoderReranker(
                    cfg.rerank_model, cache_dir=str(cfg.embedding_cache_dir) if cfg.embedding_cache_dir else None, local_files_only=cfg.embedding_local_files_only,
                )
                _instance = BoundedReranker(inner, cfg.rerank_timeout_seconds)
            except RerankerError as exc:
                RERANK_TOTAL.labels("unavailable").inc()
                logger.error("reranker unavailable, reranking is off", extra={"error": str(exc)[:120]})
                _instance = None
        return _instance
