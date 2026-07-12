#!/usr/bin/env python3
"""Acquire a resumable T0/T1 public-source GOV.UK metadata census."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.acquisition import AcquisitionError, SnapshotBuilder  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", help="immutable snapshot label, for example T0-20260711")
    parser.add_argument("--single-search-pass", action="store_true")
    parser.add_argument("--no-sitemap-stability-pass", action="store_true")
    parser.add_argument("--navigation-limit", type=int)
    parser.add_argument("--search-limit", type=int)
    parser.add_argument("--sitemap-shard-limit", type=int)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    if args.attempts < 1 or args.attempts > 5:
        parser.error("--attempts must be between 1 and 5")
    manifest = None
    for attempt in range(1, args.attempts + 1):
        builder = SnapshotBuilder(ROOT, args.label)
        try:
            manifest = builder.build(
                opposing_search=not args.single_search_pass,
                verify_sitemap=not args.no_sitemap_stability_pass,
                navigation_limit=args.navigation_limit,
                search_limit=args.search_limit,
                sitemap_shard_limit=args.sitemap_shard_limit,
            )
            break
        except (AcquisitionError, OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"acquisition attempt {attempt}/{args.attempts} failed closed: {exc}", file=sys.stderr)
            if attempt == args.attempts:
                return 1
            time.sleep(5 * attempt)
    if manifest is None:
        return 1
    print(json.dumps(manifest["reconciliation"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
