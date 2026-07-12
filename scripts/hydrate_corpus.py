#!/usr/bin/env python3
"""Hydrate and close a frozen GOV.UK census through public structured links."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.hydration import CorpusHydrator, HydrationError  # noqa: E402
from govuk_okf.closure_hydration import (  # noqa: E402
    DEFAULT_RENDERED_SCAN_LIMIT,
    CompleteCorpusHydrator,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", help="snapshot label used by the census, for example T0-20260712")
    parser.add_argument("--source", type=Path, help="enumerated source-record inventory")
    parser.add_argument("--reconciliation", type=Path, help="enumeration reconciliation JSON")
    parser.add_argument("--rate", type=float, default=8.0, help="Content API request ceiling")
    parser.add_argument("--rendered-rate", type=float, default=2.0, help="rendered www.gov.uk request ceiling")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--request-limit", type=int, help="bounded development run; never exportable as complete")
    parser.add_argument("--skip-rendered-gap", action="store_true", help="development only: omit transient rendered-link closure")
    parser.add_argument("--queue-ceiling", type=int, default=1_500_000)
    parser.add_argument(
        "--rendered-scan-limit",
        type=int,
        default=DEFAULT_RENDERED_SCAN_LIMIT,
        help=(
            "deterministic rendered-link sample ceiling "
            f"(default: {DEFAULT_RENDERED_SCAN_LIMIT})"
        ),
    )
    parser.add_argument("--no-export", action="store_true")
    args = parser.parse_args()
    reconciliation = args.reconciliation or ROOT / "corpus" / "reconciliation" / f"{args.label}.json"
    source = args.source
    if source is None:
        if not reconciliation.is_file():
            parser.error("--source is required when the enumeration reconciliation is unavailable")
        enumeration = json.loads(reconciliation.read_text(encoding="utf-8"))
        inventory_path = enumeration.get("inventory_path")
        if not isinstance(inventory_path, str):
            parser.error("enumeration reconciliation has no inventory_path")
        source = ROOT / inventory_path
    hydrator_class = CorpusHydrator if args.skip_rendered_gap else CompleteCorpusHydrator
    extra = (
        {}
        if args.skip_rendered_gap
        else {
            "max_queue_records": args.queue_ceiling,
            "max_rendered_requests": args.rendered_scan_limit,
            "rendered_requests_per_second": args.rendered_rate,
        }
    )
    hydrator = hydrator_class(
        ROOT,
        args.label,
        source,
        requests_per_second=args.rate,
        workers=args.workers,
        batch_size=args.batch_size,
        **extra,
    )
    try:
        progress = hydrator.run(request_limit=args.request_limit)
        print(json.dumps(progress, sort_keys=True))
        if progress["closed"] and not args.no_export and args.request_limit is None:
            result = hydrator.export(reconciliation if reconciliation.is_file() else None)
            print(json.dumps(result, sort_keys=True))
    except (HydrationError, OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        print(f"hydration failed closed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
