"""Vector store boundary: Qdrant with one shared collection and a tenant payload key.

Qdrant's recommended multitenancy layout is a single collection partitioned by a payload
field that is indexed with ``is_tenant=True`` (co-locates a tenant's vectors, keeps HNSW
fast). Isolation then depends on *every* read and delete carrying the tenant filter, so this
module makes it impossible to express them without one:

* :func:`tenant_filter` always includes the tenant condition;
* ``search`` / ``delete_document`` require a tenant argument and have no unfiltered variant;
* ``upsert`` refuses points whose payload tenant differs from the call's tenant.

Payloads carry masked text only.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.ingestion.chunking import SCHEMA_VERSION, Chunk, assert_single_tenant, parse_tenant
from app.rag import lexical

TENANT_KEY = "tenant_id"
PROJECT_KEY = "project_id"  # the case a chunk belongs to; project-scoped queries require it


class EmbeddingModelMismatch(ValueError):
    """The collection holds vectors of another embedding model. Two models of the same dimension
    (bge-small-en and multilingual MiniLM are both 384) would otherwise be mixed silently, and a
    query in one vector space against points of another returns plausible-looking nonsense."""

    code = "EMBEDDING_MODEL_MISMATCH"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True)
class Point:
    id: str
    vector: list[float]
    payload: dict[str, Any]


def to_point(chunk: Chunk, vector: Sequence[float], *, embedding_model: str = "") -> Point:
    return Point(
        id=chunk.point_id,
        vector=list(vector),
        payload={
            TENANT_KEY: chunk.tenant_id,
            PROJECT_KEY: chunk.project_id,
            "document_id": chunk.document_id,
            "chunk_index": chunk.index,
            "source_name": chunk.source_name,
            "page": chunk.page_start,  # chunks never cross a page: this is the page to cite
            "page_basis": chunk.page_basis,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "text": chunk.text,
            "content_hash": chunk.content_hash,
            "pii": chunk.pii_counts,
            "suspicious": chunk.suspicious,
            "embedding_model": embedding_model,
            "schema_version": SCHEMA_VERSION,
        },
    )


class VectorStore(Protocol):
    def ensure_collection(self, dimension: int, model: str = "") -> None: ...
    def assert_model(self, model: str) -> None: ...
    def upsert(self, tenant_id: str, points: Sequence[Point]) -> None: ...
    def delete_document(self, tenant_id: str, document_id: str) -> None: ...
    def search(
        self, tenant_id: str, vector: Sequence[float], limit: int = 5, *,
        include_suspicious: bool = False, project_id: str | None = None,
        project_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]: ...
    def set_document_project(self, tenant_id: str, document_id: str, project_id: str) -> None: ...
    def count_without_project(self, tenant_id: str) -> int: ...
    def scroll_chunks(self, tenant_id: str, *, limit: int) -> list[dict[str, Any]] | None: ...
    def vectors(self, tenant_id: str, point_ids: Sequence[str]) -> dict[str, list[float]]: ...


def check_points(tenant_id: str, points: Sequence[Point]) -> str:
    tenant = parse_tenant(tenant_id)
    if any(p.payload.get(TENANT_KEY) != tenant for p in points):
        raise ValueError("TENANT_MISMATCH")
    return tenant


class QdrantStore:
    """``qdrant-client`` adapter (``pip install qdrant-client``; works with a server URL or
    ``QdrantClient(":memory:")`` for tests)."""

    def __init__(self, client: Any, collection: str) -> None:
        self._c = client
        self._collection = collection
        self._model_ok: str | None = None

    def assert_model(self, model: str) -> None:
        """Refuse to mix models: any point produced by another model raises
        :class:`EmbeddingModelMismatch`. Checked once per store instance and model."""
        if not model or self._model_ok == model:
            return
        if self._c.collection_exists(self._collection):
            from qdrant_client import models

            other = models.Filter(must_not=[models.FieldCondition(key="embedding_model", match=models.MatchValue(value=model))])
            points, _ = self._c.scroll(self._collection, scroll_filter=other, limit=1, with_payload=False)
            if points:
                raise EmbeddingModelMismatch()
        self._model_ok = model

    def ensure_collection(self, dimension: int, model: str = "") -> None:
        from qdrant_client import models

        if not self._c.collection_exists(self._collection):
            self._c.create_collection(
                self._collection,
                vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
            )
        else:
            existing = self._c.get_collection(self._collection).config.params.vectors
            size = getattr(existing, "size", None)
            if size is not None and size != dimension:
                # A different embedding model: mixing vector spaces silently ruins retrieval.
                raise ValueError("EMBEDDING_DIMENSION_MISMATCH")
        self._c.create_payload_index(
            self._collection, TENANT_KEY,
            field_schema=models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD, is_tenant=True),
        )
        self._c.create_payload_index(self._collection, "document_id", field_schema=models.PayloadSchemaType.KEYWORD)
        self._c.create_payload_index(self._collection, PROJECT_KEY, field_schema=models.PayloadSchemaType.KEYWORD)
        self.assert_model(model)

    @staticmethod
    def tenant_filter(
        tenant_id: str, *, document_id: str | None = None, project_id: str | None = None,
        project_ids: Sequence[str] | None = None, include_suspicious: bool = True,
    ) -> Any:
        from qdrant_client import models

        must: list[Any] = [models.FieldCondition(key=TENANT_KEY, match=models.MatchValue(value=parse_tenant(tenant_id)))]
        if document_id:
            must.append(models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id)))
        if project_id:
            # A point without this key (legacy, or indexed without a project) cannot match.
            must.append(models.FieldCondition(key=PROJECT_KEY, match=models.MatchValue(value=parse_tenant(project_id))))
        if project_ids is not None:
            # An empty list is handled by the caller (nothing may match); MatchAny([]) is never sent.
            must.append(models.FieldCondition(
                key=PROJECT_KEY, match=models.MatchAny(any=[parse_tenant(p) for p in project_ids]),
            ))
        must_not: list[Any] = [] if include_suspicious else [
            models.FieldCondition(key="suspicious", match=models.MatchValue(value=True))
        ]
        return models.Filter(must=must, must_not=must_not or None)

    def upsert(self, tenant_id: str, points: Sequence[Point]) -> None:
        from qdrant_client import models

        tenant = check_points(tenant_id, points)
        self._c.upsert(
            self._collection,
            points=[models.PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in points],
            wait=True,
        )
        lexical.invalidate(tenant)  # this process's lexical corpus for the tenant is out of date

    def delete_document(self, tenant_id: str, document_id: str) -> None:
        from qdrant_client import models

        self._c.delete(
            self._collection,
            points_selector=models.FilterSelector(filter=self.tenant_filter(tenant_id, document_id=document_id)),
            wait=True,
        )
        lexical.invalidate(parse_tenant(tenant_id))

    def search(
        self, tenant_id: str, vector: Sequence[float], limit: int = 5, *,
        include_suspicious: bool = False, project_id: str | None = None,
        project_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        if project_ids is not None and not project_ids:
            return []  # the caller may read no project: nothing can match, so do not query at all
        # The project condition is part of the vector query itself, so Qdrant never scores or
        # returns another project's chunks; nothing is filtered afterwards.
        hits = self._c.query_points(
            self._collection, query=list(vector), limit=limit, with_payload=True,
            query_filter=self.tenant_filter(
                tenant_id, project_id=project_id, project_ids=project_ids, include_suspicious=include_suspicious,
            ),
        ).points
        return [{"score": h.score, **(h.payload or {})} for h in hits]

    def set_document_project(self, tenant_id: str, document_id: str, project_id: str) -> None:
        """Stamp the project on this document's existing points (no re-embedding). Idempotent."""
        from qdrant_client import models

        self._c.set_payload(
            self._collection,
            payload={PROJECT_KEY: parse_tenant(project_id)},
            points=models.FilterSelector(filter=self.tenant_filter(tenant_id, document_id=document_id)),
            wait=True,
        )
        lexical.invalidate(parse_tenant(tenant_id))

    def count_without_project(self, tenant_id: str) -> int:
        """Points of this tenant that carry no project (legacy or unverifiable): they are never returned
        by a project-scoped query. Used to verify a backfill."""
        from qdrant_client import models

        tenant_only = self.tenant_filter(tenant_id)
        unscoped = models.Filter(
            must=[*tenant_only.must, models.IsEmptyCondition(is_empty=models.PayloadField(key=PROJECT_KEY))]
        )
        return self._c.count(self._collection, count_filter=unscoped, exact=True).count

    def scroll_chunks(self, tenant_id: str, *, limit: int) -> list[dict[str, Any]] | None:
        """Every non-suspicious chunk payload of this tenant (``_point_id`` added), for lexical search.

        ``None`` when the tenant has more than ``limit`` chunks: the caller skips lexical search rather
        than hold an unbounded corpus in memory. The tenant condition is part of the Qdrant filter.
        """
        if not self._c.collection_exists(self._collection):
            return []
        query = self.tenant_filter(tenant_id, include_suspicious=False)
        out: list[dict[str, Any]] = []
        offset = None
        while True:
            points, offset = self._c.scroll(
                self._collection, scroll_filter=query, limit=min(1000, limit + 1), offset=offset,
                with_payload=True, with_vectors=False,
            )
            out.extend({**(p.payload or {}), "_point_id": str(p.id)} for p in points)
            if len(out) > limit:
                return None
            if offset is None:
                return out

    def vectors(self, tenant_id: str, point_ids: Sequence[str]) -> dict[str, list[float]]:
        """Stored vectors of these points, only those that belong to ``tenant_id``."""
        if not point_ids:
            return {}
        tenant = parse_tenant(tenant_id)
        points = self._c.retrieve(self._collection, ids=list(point_ids), with_payload=[TENANT_KEY], with_vectors=True)
        return {
            str(p.id): list(p.vector)  # type: ignore[arg-type]  # one unnamed vector per point
            for p in points
            if (p.payload or {}).get(TENANT_KEY) == tenant and p.vector is not None
        }


__all__ = ["EmbeddingModelMismatch", "Point", "QdrantStore", "VectorStore", "assert_single_tenant", "check_points", "to_point"]
