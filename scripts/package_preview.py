#!/usr/bin/env python3
"""Package or verify the bounded demonstrator Pages preview."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.preview_publication import (  # noqa: E402
    PreviewPublicationError,
    check_preview_package,
    package_preview,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", type=Path, metavar="PREVIEW_DIRECTORY")
    parser.add_argument("--expected-commit")
    parser.add_argument("--bundle", type=Path, default=ROOT / "bundle")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--commit")
    args = parser.parse_args()
    if args.check:
        if args.output or args.commit:
            parser.error("--check cannot be combined with --output or --commit")
        errors = check_preview_package(args.check, expected_commit=args.expected_commit)
        if errors:
            print("preview package validation failed:", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1
        print(f"bounded demonstrator preview package passed: {args.check}")
        return 0
    if args.expected_commit:
        parser.error("--expected-commit requires --check")
    if not args.output or not args.commit:
        parser.error("packaging requires --output and --commit")
    try:
        manifest = package_preview(
            repository_root=ROOT,
            bundle=args.bundle,
            output=args.output,
            commit=args.commit,
        )
    except (OSError, PreviewPublicationError) as exc:
        print(f"preview packaging failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
