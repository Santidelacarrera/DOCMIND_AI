"""Embedding providers and the data-egress policy around them.

Embedding text is a *disclosure*: with a hosted API the (already masked) chunk text leaves
this host. So:

* the default provider is **local** (ONNX via ``fastembed``) and may not download anything at
  runtime (``EMBEDDING_LOCAL_FILES_ONLY=true``) -- provision the model in an image/volume;
* an **external** provider is refused unless ``EMBEDDING_ALLOW_EXTERNAL=true`` *and* its
  host is in ``EMBEDDING_ALLOWED_HOSTS`` *and* it is reached over HTTPS -- checked when the
  embedder is built, before any text exists to send;
* nothing but masked text ever reaches ``embed`` (the pipeline masks and verifies first), and
  search queries get the same masking (:meth:`IngestionPipeline.query_vector`);
* ``hashing`` is a deterministic non-semantic stand-in for tests / development.
"""

import hashlib
import math
from collections.abc import Sequence
from typing import Any, Protocol
from urllib.parse import urlparse

from app.core import Settings


class EmbedderUnavailable(RuntimeError):
    """The configured embedder cannot start (missing model, library, key)."""


class EgressDenied(RuntimeError):
    """Sending text to this embedder would leave the trust boundary without authorisation."""


class Embedder(Protocol):
    name: str  # recorded in every payload: vectors from different models must not be mixed
    dimension: int
    external: bool  # True when text leaves this host

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Bag-of-words hashing. **Not semantic.** Tests and local development only."""

    external = False

    def __init__(self, dimension: int = 256) -> None:
        self.dimension = dimension
        self.name = f"hashing-{dimension}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            vec = [0.0] * self.dimension
            for word in t.lower().split():
                vec[int(hashlib.md5(word.encode(), usedforsecurity=False).hexdigest(), 16) % self.dimension] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


class LocalEmbedder:
    """On-host ONNX embeddings (``fastembed``). ``model`` may be injected (tests)."""

    external = False

    def __init__(self, model_name: str, *, cache_dir: str | None = None, local_files_only: bool = True,
                 model: Any = None) -> None:
        self.name = f"local:{model_name}"
        if model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError:
                raise EmbedderUnavailable("fastembed is not installed (pip install fastembed)") from None
            try:
                model = TextEmbedding(model_name, cache_dir=cache_dir, local_files_only=local_files_only)
            except Exception as exc:  # fastembed raises ValueError / ONNX errors for a missing model
                hint = "; pre-provision it or set EMBEDDING_LOCAL_FILES_ONLY=false" if local_files_only else ""
                raise EmbedderUnavailable(f"embedding model {model_name!r} cannot be loaded{hint}") from exc
        self._model = model
        self.dimension = len(self.embed(["dimension probe"])[0])

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(x) for x in v] for v in self._model.embed(list(texts))]


class OpenAIEmbedder:
    """OpenAI-compatible hosted embeddings. External: guarded by :class:`EgressPolicy`."""

    external = True

    def __init__(self, *, model: str, api_key: str, base_url: str, dimension: int | None = None,
                 client: Any = None, batch_size: int = 64) -> None:
        self.name = f"openai:{model}"
        self.base_url = base_url
        self._model, self._batch = model, batch_size
        if client is None:
            try:
                from openai import OpenAI
            except ImportError:
                raise EmbedderUnavailable("openai is not installed") from None
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=30, max_retries=2)
        self._client = client
        self.dimension = dimension or len(self.embed(["dimension probe"])[0])

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            response = self._client.embeddings.create(model=self._model, input=list(texts[i : i + self._batch]))
            out.extend([float(x) for x in item.embedding] for item in response.data)
        return out


class EgressPolicy:
    def __init__(self, *, allow_external: bool, allowed_hosts: Sequence[str]) -> None:
        self.allow_external = allow_external
        self.allowed_hosts = frozenset(h.strip().lower() for h in allowed_hosts if h.strip())

    @classmethod
    def from_settings(cls, cfg: Settings) -> "EgressPolicy":
        return cls(allow_external=cfg.embedding_allow_external, allowed_hosts=cfg.embedding_allowed_hosts.split(","))

    def check_url(self, url: str) -> None:
        parsed = urlparse(url)
        if not self.allow_external:
            raise EgressDenied("EMBEDDING_EXTERNAL_NOT_ALLOWED")
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in self.allowed_hosts:
            raise EgressDenied("EMBEDDING_HOST_NOT_ALLOWED")

    def check(self, embedder: Embedder) -> None:
        if getattr(embedder, "external", False):
            self.check_url(str(getattr(embedder, "base_url", "")))


def build_embedder(cfg: Settings) -> Embedder:
    production = cfg.environment.lower() in {"staging", "production"}
    provider = cfg.embedding_provider
    if provider == "hashing":
        if production:
            raise EmbedderUnavailable("EMBEDDING_PROVIDER=hashing is for development only")
        return HashingEmbedder()
    if provider == "local":
        return LocalEmbedder(
            cfg.embedding_model,
            cache_dir=str(cfg.embedding_cache_dir) if cfg.embedding_cache_dir else None,
            local_files_only=cfg.embedding_local_files_only,
        )
    if provider == "openai":
        EgressPolicy.from_settings(cfg).check_url(cfg.embedding_base_url)  # before any client exists
        if not cfg.openai_api_key:
            raise EmbedderUnavailable("OPENAI_API_KEY is required for EMBEDDING_PROVIDER=openai")
        return OpenAIEmbedder(model=cfg.embedding_model, api_key=cfg.openai_api_key, base_url=cfg.embedding_base_url)
    raise EmbedderUnavailable(f"unknown embedding provider {provider!r}")
