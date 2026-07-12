#!/usr/bin/env python3
"""Validate the complete static publication contract without network access."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.publication_validation import validate_bundle  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT / "bundle")
    args = parser.parse_args()
    result = validate_bundle(args.bundle)
    if not result.passed:
        print("publication validation failed:", file=sys.stderr)
        for error in result.errors:
            print(f"- {error}", file=sys.stderr)
        if result.error_count > len(result.errors):
            print(
                f"- ... {result.error_count - len(result.errors)} more",
                file=sys.stderr,
            )
        return 1
    print(
        f"publication validated: {result.datasets} datasets, "
        f"{result.resources} resources, {result.publishers} publishers, "
        f"{result.relationships} provenance-complete relationships, "
        f"{result.semantic_nodes} semantic node occurrences"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
