#!/usr/bin/env python3
"""Validate the active canonical Markdown tree against OKF core 0.3."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.okf_v03 import OKF_VERSION, parse_frontmatter, validate_okf_v03_bundle  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT / "bundle")
    args = parser.parse_args()
    errors = validate_okf_v03_bundle(args.bundle)
    if errors:
        print(f"OKF v{OKF_VERSION} validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    typed = 0
    for path in args.bundle.rglob("*.md"):
        if path.name in {"index.md", "log.md"}:
            continue
        metadata, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        if metadata is not None and isinstance(metadata.get("type"), str) and metadata["type"].strip():
            typed += 1
    print(f"OKF v{OKF_VERSION} validated: {typed} typed Markdown concepts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
