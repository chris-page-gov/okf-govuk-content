#!/usr/bin/env python3
"""Validate checked-in repository policy and an optional GitHub API capture."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.repository_policy import validate_repository_policy  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--api-capture", type=Path)
    parser.add_argument("--publication-api-capture", type=Path)
    args = parser.parse_args()
    report = validate_repository_policy(args.root, args.api_capture, args.publication_api_capture)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
