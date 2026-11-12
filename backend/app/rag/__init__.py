"""Retrieval and verification capabilities brought in from ContextRAG_Engine (ADR-005).

Pure, dependency-free building blocks used by ``app.ingestion.answering``:

* :mod:`app.rag.bm25`      -- BM25Plus lexical scoring over a tenant's canonical chunks
* :mod:`app.rag.fusion`    -- reciprocal rank fusion of dense and lexical rankings
* :mod:`app.rag.lexical`   -- the per-tenant lexical corpus, loaded from the vector store with the tenant filter
* :mod:`app.rag.conflicts` -- possible disagreements (dates, figures) between cited sources

Every chunk they handle is a payload of the single vector collection: the same document, page and
chunk index that citations point to. There is no second chunker and no second document model.
"""
