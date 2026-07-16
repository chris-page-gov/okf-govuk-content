#!/usr/bin/env python3
"""Query the local-only external GOV.UK Search extract database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.search_extracts import (  # noqa: E402
    SearchExtractError,
    query_extract_database,
)
from govuk_okf.storage import StoragePolicyError, load_storage_policy  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", help="extract-cache snapshot label")
    parser.add_argument("query", help="SQLite FTS5 query")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    try:
        path = load_storage_policy(ROOT).extract_database(args.label)
        if path is None or not path.is_file():
            raise SearchExtractError(f"extract database is unavailable for {args.label}")
        rows = query_extract_database(path, args.query, limit=args.limit)
    except (OSError, SearchExtractError, StoragePolicyError) as exc:
        print(f"extract query failed closed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"snapshot": args.label, "query": args.query, "results": rows}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
