#!/usr/bin/env python3
"""Verify that an annotated release tag matches the version and protected main."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.release_ref import validate_release_ref  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--main-ref", default="origin/main")
    args = parser.parse_args()
    report = validate_release_ref(
        ROOT,
        tag=args.tag,
        expected_commit=args.commit,
        main_ref=args.main_ref,
        verify_signature=True,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
