"""Reciprocal rank fusion (ported from ContextRAG_Engine ``retrieval/hybrid_search.py``).

score(d) = sum over rankings of 1 / (k + rank(d)), rank starting at 1. Scores of different retrievers
(cosine, BM25) are not comparable, ranks are; RRF needs no calibration between them.
"""

from collections.abc import Hashable, Sequence


def reciprocal_rank_fusion[K: Hashable](rankings: Sequence[Sequence[K]], k: int = 60) -> list[tuple[K, float]]:
    """Fused ``(key, score)`` best first. Exact ties keep the order of first appearance (dense first)."""
    score: dict[K, float] = {}
    first: dict[K, int] = {}
    seen = 0
    for ranking in rankings:
        for position, key in enumerate(ranking, start=1):
            if key not in first:
                first[key] = seen
                seen += 1
            score[key] = score.get(key, 0.0) + 1.0 / (k + position)
    return [(key, score[key]) for key in sorted(score, key=lambda key: (-score[key], first[key]))]
