#!/usr/bin/env python3
"""Project a verified immutable evaluation run into canonical release evidence."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.evaluation_projection import project_release_results  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="Completed immutable release run directory")
    parser.add_argument(
        "--questions",
        type=Path,
        default=ROOT / "questions" / "release-v2",
        help="Independently verified release-v2 question assets",
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        default=ROOT / "bundle",
        help="Exact release bundle evaluated by the immutable run",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "evaluation" / "results",
        help="Canonical release-evidence projection directory",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run = args.run if args.run.is_absolute() else ROOT / args.run
    lexical_run = Path(os.path.abspath(run))
    try:
        source_reference = lexical_run.relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise SystemExit("--run must be inside the repository for a reproducible source binding") from exc
    projection = project_release_results(
        run=lexical_run,
        questions=args.questions,
        bundle=args.bundle,
        output=args.output,
        source_reference=source_reference,
        repository_root=ROOT,
    )
    print(json.dumps(projection, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
