#!/usr/bin/env python3
"""Build or byte-check the deterministic GOV.UK OKF publication."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.publication import synchronize  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=ROOT
        / "demo"
        / "snapshots"
        / "NEW-CHILD-20260715"
        / "publication"
        / "source-records.jsonl",
    )
    parser.add_argument("--output", type=Path, default=ROOT / "bundle")
    parser.add_argument("--generated-at", default="2026-07-15T06:25:17Z")
    parser.add_argument("--snapshot-id", default="NEW-CHILD-20260715")
    parser.add_argument(
        "--compiler",
        choices=("auto", "memory", "disk"),
        default="auto",
        help="auto uses the bounded disk compiler for gzip, directories and large files",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    errors = synchronize(
        args.source,
        args.output,
        args.generated_at,
        args.snapshot_id,
        check=args.check,
        compiler=args.compiler,
    )
    if errors:
        print("publication check failed:", file=sys.stderr)
        for error in errors[:100]:
            print(f"- {error}", file=sys.stderr)
        if len(errors) > 100:
            print(f"- ... {len(errors) - 100} more", file=sys.stderr)
        return 1
    print("publication is synchronized" if args.check else f"built {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
