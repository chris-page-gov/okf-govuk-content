#!/usr/bin/env python3
"""Validate the canonical Markdown tree against the OKF v0.2 contract."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.okf_v02 import OKF_VERSION, validate_okf_v02_bundle  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT / "bundle")
    args = parser.parse_args()
    errors = validate_okf_v02_bundle(args.bundle)
    if errors:
        print(f"OKF v{OKF_VERSION} validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    concepts = sum(
        1
        for path in args.bundle.rglob("*.md")
        if path.name not in {"index.md", "log.md"}
    )
    print(f"OKF v{OKF_VERSION} validated: {concepts} canonical concepts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
