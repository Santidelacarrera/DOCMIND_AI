"""Provision the local ONNX models once, so the services read them without network at run time.

    EMBEDDING_CACHE_DIR=/opt/models EMBEDDING_LOCAL_FILES_ONLY=false python scripts/fetch_models.py

Downloads ``EMBEDDING_MODEL`` (when ``EMBEDDING_PROVIDER=local``) and ``RERANK_MODEL`` (when set) into
``EMBEDDING_CACHE_DIR``, then loads each again with ``local_files_only=True`` to prove the services will
find them offline. Exit code 0 only if every configured model loads offline.

With Docker Compose (production overlay, ``models`` volume):

    docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm \
      -e EMBEDDING_LOCAL_FILES_ONLY=false -v docmind_models:/opt/models:rw api python scripts/fetch_models.py

Model files are public (Hugging Face); no credential is involved. Run it again after changing a model.
"""

import sys
from pathlib import Path


def main() -> int:
    from app.core import settings
    from app.ingestion.embeddings import EmbedderUnavailable, LocalEmbedder
    from app.rag.rerank import CrossEncoderReranker, RerankerError

    cfg = settings()
    if cfg.embedding_cache_dir is None:
        print("EMBEDDING_CACHE_DIR is not set: models would go to a temporary directory", file=sys.stderr)
        return 2
    cache = str(Path(cfg.embedding_cache_dir))
    wanted: list[tuple[str, str]] = []
    if cfg.embedding_provider == "local":
        wanted.append(("embedding", cfg.embedding_model))
    if cfg.rerank_model:
        wanted.append(("rerank", cfg.rerank_model))
    if not wanted:
        print("nothing to fetch: EMBEDDING_PROVIDER is not local and RERANK_MODEL is empty")
        return 0
    failed = False
    for kind, model in wanted:
        for offline in (cfg.embedding_local_files_only, True):  # fetch (if allowed), then prove offline load
            try:
                if kind == "embedding":
                    LocalEmbedder(model, cache_dir=cache, local_files_only=offline)
                else:
                    CrossEncoderReranker(model, cache_dir=cache, local_files_only=offline)
            except (EmbedderUnavailable, RerankerError) as exc:
                print(f"[FAIL] {kind} {model} (offline={offline}): {exc}", file=sys.stderr)
                failed = True
                break
        else:
            print(f"[ok] {kind} {model} available offline in {cache}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
