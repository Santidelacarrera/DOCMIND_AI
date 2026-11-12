"""BM25Plus over an in-memory inverted index (ported from ContextRAG_Engine ``retrieval/bm25.py``).

Same formula and defaults as ``rank_bm25.BM25Plus`` (k1=1.5, b=0.75, delta=1). BM25Plus instead of
Okapi because Okapi's IDF turns negative for a term present in more than half of the documents, which
breaks small tenants (a handful of contracts that all say "contrato").

The index is built once from a list of token lists and is immutable: adding or removing a chunk means
building a new one (``app.rag.lexical`` does that when its cached corpus expires or is invalidated).
"""

import math
from collections import Counter
from collections.abc import Sequence


class Bm25Index:
    def __init__(self, documents: Sequence[Sequence[str]], *, k1: float = 1.5, b: float = 0.75, delta: float = 1.0) -> None:
        self.k1, self.b, self.delta = k1, b, delta
        self.size = len(documents)
        self._length = [len(d) for d in documents]
        self._avg = (sum(self._length) / self.size) if self.size else 0.0
        self._postings: dict[str, dict[int, int]] = {}
        for position, tokens in enumerate(documents):
            for term, frequency in Counter(tokens).items():
                self._postings.setdefault(term, {})[position] = frequency

    def search(self, query: Sequence[str], limit: int) -> list[tuple[int, float]]:
        """``(document position, score)`` best first, only documents sharing at least one query term.

        Ties are broken by position so the ranking is deterministic.
        """
        if not self.size or not query or limit <= 0:
            return []
        avg = self._avg or 1.0
        scores: dict[int, float] = {}
        offset = 0.0  # delta * idf for every query term, as rank_bm25 adds it
        for term, query_frequency in Counter(query).items():
            postings = self._postings.get(term)
            if not postings:
                continue
            idf = math.log((self.size + 1) / len(postings))
            offset += query_frequency * self.delta * idf
            for position, tf in postings.items():
                norm = self.k1 * (1 - self.b + self.b * self._length[position] / avg) + tf
                scores[position] = scores.get(position, 0.0) + query_frequency * idf * (tf * (self.k1 + 1) / norm)
        ranked = sorted(((p, s + offset) for p, s in scores.items()), key=lambda item: (-item[1], item[0]))
        return ranked[:limit]
