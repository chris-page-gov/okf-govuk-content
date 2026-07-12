#!/usr/bin/env python3
"""Run the deterministic matched agent evaluation without network access."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.evaluation import (  # noqa: E402
    DEFAULT_TRACE_SHARD_RECORDS,
    MAX_TRACE_SHARD_RECORDS,
    run_evaluation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run all frozen v2 questions against the proposal, deterministic baselines, "
            "one-factor ablations and serialization controls."
        )
    )
    parser.add_argument("--questions", type=Path, required=True, help="Verified v2 question-matrix directory")
    parser.add_argument("--bundle", type=Path, default=ROOT / "bundle", help="Built static bundle directory")
    parser.add_argument("--output", type=Path, required=True, help="Immutable evaluation run directory")
    parser.add_argument("--run-id", help="Stable run identifier; defaults to the output directory name")
    parser.add_argument("--mode", choices=("fixture", "release"), default="fixture")
    parser.add_argument("--question-limit", type=int, help="Fixture-only bounded smoke-test limit")
    parser.add_argument(
        "--trace-shard-records",
        type=int,
        default=DEFAULT_TRACE_SHARD_RECORDS,
        help=f"Raw trace records per gzip JSONL shard (maximum {MAX_TRACE_SHARD_RECORDS})",
    )
    parser.add_argument("--resume", action="store_true", help="Resume matching incomplete state in the output directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.question_limit is not None and args.question_limit < 1:
        raise SystemExit("--question-limit must be positive")
    result = run_evaluation(
        questions=args.questions,
        bundle=args.bundle,
        output=args.output,
        run_id=args.run_id or args.output.name,
        mode=args.mode,
        question_limit=args.question_limit,
        trace_shard_records=args.trace_shard_records,
        resume=args.resume,
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "root_sha256": result["manifest"]["root_sha256"],
                "questions": result["status"]["questions"],
                "outcomes": result["status"]["outcomes"],
                "agent_evaluation_status": result["status"]["agent_evaluation_status"],
                "human_evaluation_status": result["status"]["human_evaluation_status"],
                "release_eligible": result["status"]["release_eligible"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
