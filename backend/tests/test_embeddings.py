"""Embedding providers and the data-egress policy."""

import shutil
import types
from typing import Any

import pytest

from app.core import Settings
from app.evaluation.pdfgen import digital_pdf
from app.ingestion.embeddings import (
    EgressDenied,
    EgressPolicy,
    EmbedderUnavailable,
    HashingEmbedder,
    LocalEmbedder,
    OpenAIEmbedder,
    build_embedder,
)
from app.ingestion.entities import RuleEntityDetector
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.vectorstore import QdrantStore

qdrant_client = pytest.importorskip("qdrant_client")
TENANT, DOC = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


def cfg(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


class RecordingClient:
    """Stands in for the OpenAI SDK and records every string that would leave the host."""

    def __init__(self, dimension: int = 8) -> None:
        self.sent: list[str] = []
        self.dimension = dimension
        self.embeddings = types.SimpleNamespace(create=self._create)

    def _create(self, *, model: str, input: list[str]) -> Any:
        self.sent.extend(input)
        data = [types.SimpleNamespace(embedding=[float(len(t) % 7 + i) for i in range(self.dimension)]) for t in input]
        return types.SimpleNamespace(data=data)


# ------------------------------------------------------------------------- egress policy
def test_external_embeddings_are_denied_by_default() -> None:
    with pytest.raises(EgressDenied, match="EMBEDDING_EXTERNAL_NOT_ALLOWED"):
        build_embedder(cfg(embedding_provider="openai", openai_api_key="sk-x"))


@pytest.mark.parametrize(("url", "code"), [
    ("https://evil.example.net/v1", "EMBEDDING_HOST_NOT_ALLOWED"),
    ("http://api.openai.com/v1", "EMBEDDING_HOST_NOT_ALLOWED"),  # plaintext
    ("https://api.openai.com.evil.test/v1", "EMBEDDING_HOST_NOT_ALLOWED"),  # look-alike host
])
def test_only_listed_https_hosts_are_reachable(url: str, code: str) -> None:
    policy = EgressPolicy(allow_external=True, allowed_hosts=["api.openai.com"])
    with pytest.raises(EgressDenied, match=code):
        policy.check_url(url)
    with pytest.raises(EgressDenied, match=code):
        build_embedder(cfg(embedding_provider="openai", openai_api_key="sk-x", embedding_allow_external=True,
                           embedding_base_url=url))


def test_an_unauthorised_external_embedder_is_refused_before_any_document_is_read() -> None:
    embedder = OpenAIEmbedder(model="m", api_key="k", base_url="https://api.openai.com/v1", client=RecordingClient())
    store = QdrantStore(qdrant_client.QdrantClient(":memory:"), "c")
    with pytest.raises(EgressDenied):
        IngestionPipeline(store, embedder, egress=EgressPolicy(allow_external=False, allowed_hosts=["api.openai.com"]))
    IngestionPipeline(store, embedder, egress=EgressPolicy(allow_external=True, allowed_hosts=["api.openai.com"]))
    IngestionPipeline(store, HashingEmbedder(), egress=EgressPolicy(allow_external=False, allowed_hosts=[]))  # local: fine


def test_only_masked_text_ever_reaches_an_external_embedder() -> None:
    client = RecordingClient()
    embedder = OpenAIEmbedder(model="m", api_key="k", base_url="https://api.openai.com/v1", client=client)
    store = QdrantStore(qdrant_client.QdrantClient(":memory:"), "c")
    pipeline = IngestionPipeline(store, embedder, masker_extra=[RuleEntityDetector()],
                                 egress=EgressPolicy(allow_external=True, allowed_hosts=["api.openai.com"]))
    client.sent.clear()  # forget the dimension probe
    pipeline.ingest(tenant_id=TENANT, document_id=DOC, content=digital_pdf([[
        "Applicant Name: María José Fernández", "Mail maria@example.org or +34 612 345 678.",
        "IBAN ES91 2100 0418 4502 0005 1332 for the pump assembly in Calle del Puerto 14.",
    ]]))
    assert client.sent, "nothing was embedded"
    outbound = "\n".join(client.sent)
    for secret in ("María", "Fernández", "maria@example.org", "612 345", "ES91", "Puerto 14"):
        assert secret not in outbound, secret
    assert "pump assembly" in outbound


def test_search_queries_are_masked_before_embedding() -> None:
    client = RecordingClient()
    embedder = OpenAIEmbedder(model="m", api_key="k", base_url="https://api.openai.com/v1", client=client)
    pipeline = IngestionPipeline(QdrantStore(qdrant_client.QdrantClient(":memory:"), "c"), embedder,
                                 masker_extra=[RuleEntityDetector()],
                                 egress=EgressPolicy(allow_external=True, allowed_hosts=["api.openai.com"]))
    client.sent.clear()
    vector = pipeline.query_vector(tenant_id=TENANT, question="contracts of maria@example.org signed by Ana Ruiz")
    assert len(vector) == 8
    assert client.sent == ["contracts of [EMAIL_1] signed by [PERSON_1]"]


def test_openai_embedder_batches_and_reports_its_name_and_dimension() -> None:
    client = RecordingClient(dimension=5)
    e = OpenAIEmbedder(model="text-embedding-3-small", api_key="k", base_url="https://api.openai.com/v1",
                       client=client, batch_size=2)
    assert (e.name, e.dimension, e.external) == ("openai:text-embedding-3-small", 5, True)
    assert len(e.embed(["a", "b", "c", "d", "e"])) == 5


# ------------------------------------------------------------------------- local / hashing
class FakeModel:
    def embed(self, texts: list[str]) -> Any:
        return ([float(len(t)), 1.0, 0.0] for t in texts)


def test_local_embedder_wraps_a_model_without_network() -> None:
    e = LocalEmbedder("my-model", model=FakeModel())
    assert (e.name, e.dimension, e.external) == ("local:my-model", 3, False)
    assert e.embed(["abcd"]) == [[4.0, 1.0, 0.0]]
    EgressPolicy(allow_external=False, allowed_hosts=[]).check(e)  # local needs no authorisation


def test_a_missing_local_model_never_triggers_a_download(tmp_path: Any) -> None:
    pytest.importorskip("fastembed")
    with pytest.raises(EmbedderUnavailable, match="cannot be loaded"):
        LocalEmbedder("BAAI/bge-small-en-v1.5", cache_dir=str(tmp_path), local_files_only=True)
    assert not list(tmp_path.rglob("*.onnx")) and not [p for p in tmp_path.iterdir() if p.is_dir() and any(p.iterdir())]
    shutil.rmtree(tmp_path, ignore_errors=True)


def test_hashing_is_development_only() -> None:
    assert isinstance(build_embedder(cfg(embedding_provider="hashing")), HashingEmbedder)
    with pytest.raises(EmbedderUnavailable):
        build_embedder(cfg(embedding_provider="hashing").model_copy(update={"environment": "production"}))


def test_vectors_of_a_different_model_cannot_be_mixed_into_a_collection() -> None:
    store = QdrantStore(qdrant_client.QdrantClient(":memory:"), "mixed")
    store.ensure_collection(64)
    store.ensure_collection(64)  # same model: fine
    with pytest.raises(ValueError, match="EMBEDDING_DIMENSION_MISMATCH"):
        store.ensure_collection(384)


def test_every_point_records_which_model_produced_its_vector() -> None:
    client = qdrant_client.QdrantClient(":memory:")
    IngestionPipeline(QdrantStore(client, "m"), HashingEmbedder(32), max_chars=400).ingest(
        tenant_id=TENANT, document_id=DOC, content=digital_pdf([["Hydraulic pump assembly ships in March."]])
    )
    points, _ = client.scroll("m", limit=10, with_payload=True)
    assert {p.payload["embedding_model"] for p in points} == {"hashing-32"}


# ------------------------------------------------------------------------- production settings
PROD: dict[str, Any] = {
    "environment": "production", "database_url": "postgresql://x", "redis_url": "redis://x", "jwt_secret": "s" * 40,
    "cookie_secure": True, "cors_origins": "https://app.example.com", "antivirus_provider": "clamav",
    "storage_provider": "s3", "s3_bucket": "b", "s3_region": "r", "aws_access_key_id": "a",
    "aws_secret_access_key": "b", "smtp_host": "smtp.example.com", "frontend_base_url": "https://app.example.com",
}


def test_production_settings_enforce_a_safe_vector_configuration() -> None:
    Settings(_env_file=None, **PROD)  # baseline is valid  # type: ignore[call-arg]
    ok = dict(PROD, qdrant_url="https://qdrant.internal:6333", qdrant_api_key="k")
    Settings(_env_file=None, **ok)  # type: ignore[call-arg]
    for bad, message in [
        (dict(ok, qdrant_url="http://qdrant.internal:6333"), "QDRANT_URL must be https"),
        (dict(ok, qdrant_api_key=None), "QDRANT_API_KEY"),
        (dict(ok, embedding_provider="hashing"), "development only"),
        (dict(ok, embedding_provider="openai", openai_api_key="sk"), "EMBEDDING_ALLOW_EXTERNAL"),
        (dict(ok, embedding_provider="openai", openai_api_key="sk", embedding_allow_external=True,
              embedding_allowed_hosts="other.example.com"), "EMBEDDING_ALLOWED_HOSTS"),
    ]:
        with pytest.raises(ValueError, match=message):
            Settings(_env_file=None, **bad)  # type: ignore[call-arg]
    Settings(_env_file=None, **dict(ok, embedding_provider="openai", openai_api_key="sk",  # type: ignore[call-arg]
                                    embedding_allow_external=True))
