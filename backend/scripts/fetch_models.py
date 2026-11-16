"""Provision the local ONNX models once, with disk-space and SHA-256 integrity checks.

    EMBEDDING_CACHE_DIR=/opt/models EMBEDDING_LOCAL_FILES_ONLY=false python scripts/fetch_models.py

Downloads ``EMBEDDING_MODEL`` (when ``EMBEDDING_PROVIDER=local``) and ``RERANK_MODEL`` (when set) into
``EMBEDDING_CACHE_DIR``, verifies free disk space before downloading, removes incomplete ``.tmp``/``.part``
artifacts, loads each model with ``local_files_only=True``, and writes/verifies ``models_manifest.json``
with SHA-256 checksums of all ONNX artifacts.
"""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _clean_partial_downloads(cache_path: Path) -> int:
    removed = 0
    for ext in ("*.tmp", "*.part", "*.incomplete"):
        for p in cache_path.rglob(ext):
            if p.is_file():
                p.unlink(missing_ok=True)
                removed += 1
    return removed


def _build_manifest(cache_path: Path) -> dict[str, dict[str, object]]:
    entries: dict[str, dict[str, object]] = {}
    for p in sorted(cache_path.rglob("*.onnx")):
        if p.is_file():
            rel = p.relative_to(cache_path).as_posix()
            entries[rel] = {"size_bytes": p.stat().st_size, "sha256": _sha256_file(p)}
    return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-free-mb", type=int, default=500, help="minimum free disk space in MB")
    parser.add_argument("--verify-only", action="store_true", help="verify existing manifest and offline load only")
    args = parser.parse_args()

    from app.core import settings
    from app.ingestion.embeddings import EmbedderUnavailable, LocalEmbedder
    from app.rag.rerank import CrossEncoderReranker, RerankerError

    cfg = settings()
    if cfg.embedding_cache_dir is None:
        print("EMBEDDING_CACHE_DIR is not set: models would go to a temporary directory", file=sys.stderr)
        return 2
    cache_path = Path(cfg.embedding_cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    cache = str(cache_path)

    free_mb = shutil.disk_usage(cache_path).free // (1024 * 1024)
    if free_mb < args.min_free_mb:
        print(f"[FAIL] insufficient disk space in {cache}: {free_mb} MB < {args.min_free_mb} MB", file=sys.stderr)
        return 3

    cleaned = _clean_partial_downloads(cache_path)
    if cleaned:
        print(f"[info] cleaned {cleaned} partial download file(s)")

    wanted: list[tuple[str, str]] = []
    if cfg.embedding_provider == "local":
        wanted.append(("embedding", cfg.embedding_model))
    if cfg.rerank_model:
        wanted.append(("rerank", cfg.rerank_model))
    if not wanted:
        print("nothing to fetch: EMBEDDING_PROVIDER is not local and RERANK_MODEL is empty")
        return 0

    manifest_file = cache_path / "models_manifest.json"
    if args.verify_only and manifest_file.exists():
        saved = json.loads(manifest_file.read_text(encoding="utf-8")).get("files", {})
        current = _build_manifest(cache_path)
        if saved != current:
            print("[FAIL] ONNX checksum mismatch against models_manifest.json", file=sys.stderr)
            return 1

    failed = False
    modes = (True,) if args.verify_only else (cfg.embedding_local_files_only, True)
    for kind, model in wanted:
        for offline in modes:
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

    if not failed:
        files = _build_manifest(cache_path)
        manifest_file.write_text(
            json.dumps({"models": dict(wanted), "files": files}, indent=2),
            encoding="utf-8",
        )
        print(f"[ok] manifest written to {manifest_file} ({len(files)} ONNX artifact(s), free={free_mb} MB)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
