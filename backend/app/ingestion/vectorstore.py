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

TENANT_KEY = "tenant_id"


@dataclass(frozen=True)
class Point:
    id: str
    vector: list[float]
    payload: dict[str, Any]


def to_point(chunk: Chunk, vector: Sequence[float]) -> Point:
    return Point(
        id=chunk.point_id,
        vector=list(vector),
        payload={
            TENANT_KEY: chunk.tenant_id,
            "document_id": chunk.document_id,
            "chunk_index": chunk.index,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "text": chunk.text,
            "content_hash": chunk.content_hash,
            "pii": chunk.pii_counts,
            "suspicious": chunk.suspicious,
            "schema_version": SCHEMA_VERSION,
        },
    )


class VectorStore(Protocol):
    def ensure_collection(self, dimension: int) -> None: ...
    def upsert(self, tenant_id: str, points: Sequence[Point]) -> None: ...
    def delete_document(self, tenant_id: str, document_id: str) -> None: ...
    def search(self, tenant_id: str, vector: Sequence[float], limit: int = 5, *, include_suspicious: bool = False) -> list[dict[str, Any]]: ...


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

    def ensure_collection(self, dimension: int) -> None:
        from qdrant_client import models

        if not self._c.collection_exists(self._collection):
            self._c.create_collection(
                self._collection,
                vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
            )
        self._c.create_payload_index(
            self._collection, TENANT_KEY,
            field_schema=models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD, is_tenant=True),
        )
        self._c.create_payload_index(self._collection, "document_id", field_schema=models.PayloadSchemaType.KEYWORD)

    @staticmethod
    def tenant_filter(tenant_id: str, *, document_id: str | None = None, include_suspicious: bool = True) -> Any:
        from qdrant_client import models

        must: list[Any] = [models.FieldCondition(key=TENANT_KEY, match=models.MatchValue(value=parse_tenant(tenant_id)))]
        if document_id:
            must.append(models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id)))
        must_not: list[Any] = [] if include_suspicious else [
            models.FieldCondition(key="suspicious", match=models.MatchValue(value=True))
        ]
        return models.Filter(must=must, must_not=must_not or None)

    def upsert(self, tenant_id: str, points: Sequence[Point]) -> None:
        from qdrant_client import models

        check_points(tenant_id, points)
        self._c.upsert(
            self._collection,
            points=[models.PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in points],
            wait=True,
        )

    def delete_document(self, tenant_id: str, document_id: str) -> None:
        from qdrant_client import models

        self._c.delete(
            self._collection,
            points_selector=models.FilterSelector(filter=self.tenant_filter(tenant_id, document_id=document_id)),
            wait=True,
        )

    def search(self, tenant_id: str, vector: Sequence[float], limit: int = 5, *, include_suspicious: bool = False) -> list[dict[str, Any]]:
        hits = self._c.query_points(
            self._collection, query=list(vector), limit=limit, with_payload=True,
            query_filter=self.tenant_filter(tenant_id, include_suspicious=include_suspicious),
        ).points
        return [{"score": h.score, **(h.payload or {})} for h in hits]


__all__ = ["Point", "QdrantStore", "VectorStore", "assert_single_tenant", "check_points", "to_point"]
