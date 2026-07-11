#!/usr/bin/env python3
"""Import the controlling Markdown contract into deterministic projections."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.contract import ContractError, synchronize  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if generated projections differ")
    args = parser.parse_args()
    try:
        errors = synchronize(check=args.check)
    except (ContractError, OSError, ValueError) as exc:
        print(f"contract import failed: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("contract projections are synchronized" if args.check else "wrote contract projections")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

