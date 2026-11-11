"""The shipped .env.example must load: the Docker quick start and CI's e2e job start from it.

Regression: an empty ``ASK_MIN_SEMANTIC_SIMILARITY=`` made Settings() fail, so the API container exited
on startup -- unit tests never read this file, only the real stack did.
"""

from pathlib import Path

from app.core import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def test_env_example_loads_into_settings() -> None:
    cfg = Settings(_env_file=ENV_EXAMPLE)  # type: ignore[call-arg]
    assert cfg.environment == "development"
    assert cfg.qdrant_url and cfg.embedding_provider == "hashing"


def test_an_empty_semantic_threshold_means_calibrated() -> None:
    assert Settings(_env_file=None, ask_min_semantic_similarity="").ask_min_semantic_similarity is None  # type: ignore[call-arg, arg-type]
    assert Settings(_env_file=None, ask_min_semantic_similarity="0.6").ask_min_semantic_similarity == 0.6  # type: ignore[call-arg, arg-type]
