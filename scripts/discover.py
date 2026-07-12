#!/usr/bin/env python3
"""Read-only command-line search, fetch and traversal interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.discovery import DiscoveryError, DiscoveryIndex  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["search", "fetch", "traverse", "citation"])
    parser.add_argument("value")
    parser.add_argument("--bundle", type=Path, default=ROOT / "bundle")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--kind", choices=["dataset", "publisher", "resource"])
    parser.add_argument("--predicate", action="append", default=[])
    args = parser.parse_args()
    index = DiscoveryIndex(args.bundle)
    try:
        if args.operation == "search":
            result = index.search(args.value, limit=args.limit)
        elif args.operation == "fetch":
            result = index.fetch(args.value, kind=args.kind)
        elif args.operation == "traverse":
            result = index.traverse(
                args.value,
                kind=args.kind,
                predicates=set(args.predicate) or None,
                limit=args.limit,
            )
        else:
            result = index.citation(args.value, kind=args.kind)
    except DiscoveryError as exc:
        print(json.dumps({"error": "not_found", "message": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
