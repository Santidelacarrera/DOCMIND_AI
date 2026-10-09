"""``python -m app.evaluation`` -- run the reference corpus and write a report."""

import argparse
import os
import sys
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.evaluation", description=__doc__)
    parser.add_argument("--provider", choices=["baseline", "openai"], default="baseline",
                        help="baseline = offline rule-based extractor (reproducible, no key); "
                             "openai = real model (needs OPENAI_API_KEY, costs money)")
    parser.add_argument("--output-dir", type=Path, default=Path("eval_reports"))
    parser.add_argument("--no-review", action="store_true", help="skip the simulated review/correction pass")
    parser.add_argument("--update-expected", action="store_true",
                        help="rewrite eval_corpus/baseline_expected.json from this run (baseline only)")
    parser.add_argument("--min-f1", type=float, default=None, help="exit 1 if micro F1 is below this")
    args = parser.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="docmind-eval-"))
    os.environ.update(
        ENVIRONMENT="development", DATABASE_URL=f"sqlite:///{(work / 'eval.db').as_posix()}",
        LOCAL_STORAGE_PATH=str(work / "uploads"), STORAGE_PROVIDER="local", ANTIVIRUS_PROVIDER="disabled",
        JWT_SECRET="evaluation-secret-evaluation-secret-123", COOKIE_SECURE="false",
        LLM_PROVIDER="openai" if args.provider == "openai" else "mock",
        FREE_PAGES_PER_MONTH="100000",
        **{k: "100000" for k in ("RATE_LIMIT_LOGIN", "RATE_LIMIT_REGISTER", "RATE_LIMIT_UPLOAD", "RATE_LIMIT_API")},
    )
    from fastapi.testclient import TestClient

    from app import main as api
    from app import rate_limit
    from app.db import Base, engine
    from app.evaluation import runner
    from app.evaluation.baseline import RuleBasedProvider
    from app.processing import OpenAIProvider

    class _NoRedis:  # the limiter is irrelevant here; avoid needing a Redis server
        @classmethod
        def from_url(cls, *_: object, **__: object) -> "_NoRedis":
            return cls()

        def pipeline(self, transaction: bool = True) -> "_NoRedis":
            return self

        def incr(self, key: str) -> None: ...
        def expire(self, *a: object, **k: object) -> None: ...
        def execute(self) -> list[int]:
            return [1]

    rate_limit.Redis = _NoRedis  # type: ignore[misc,assignment]
    api.process_document.delay = lambda job_id: None  # type: ignore[method-assign,assignment]
    from app import worker

    worker.dispatch_webhooks.delay = lambda *a, **k: None  # type: ignore[method-assign,assignment]
    Base.metadata.create_all(engine)
    provider = OpenAIProvider() if args.provider == "openai" else RuleBasedProvider()
    name = "openai" if args.provider == "openai" else "baseline-rules"
    with TestClient(api.app) as client:
        report = runner.run_evaluation(client, provider, review=not args.no_review, provider_name=name)
    runner.write_reports(report, args.output_dir, f"report-{args.provider}")
    micro = report["extraction"]["micro"]
    if args.update_expected:
        import json

        from app.evaluation.corpus import CORPUS_DIR

        (CORPUS_DIR / "baseline_expected.json").write_text(
            json.dumps(runner.expected_from(report), indent=2) + "\n", encoding="utf-8"
        )
        print("updated eval_corpus/baseline_expected.json")
    print(runner.to_markdown(runner.strip_internal(report)))
    print(f"\nreports written to {args.output_dir}/report-{args.provider}.(json|md)")
    if args.min_f1 is not None and (micro["f1"] or 0.0) < args.min_f1:
        print(f"micro F1 {micro['f1']} is below the required {args.min_f1}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
