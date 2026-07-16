#!/usr/bin/env python3
"""Verify minimum-free-space and EXTSSD cache policy."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.storage import StoragePolicyError, load_storage_policy  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="create authorised cache/extract directories")
    args = parser.parse_args()
    try:
        policy = load_storage_policy(ROOT)
        if args.prepare:
            if policy.external_cache_root is None:
                raise StoragePolicyError("no authorised external cache is available")
            for relative in ("cache", "extracts"):
                path = policy.external_cache_root / relative
                path.mkdir(parents=True, exist_ok=True)
                if path.is_symlink() or not path.is_dir():
                    raise StoragePolicyError(f"unsafe external cache directory: {path}")
        result = policy.preflight()
    except (OSError, StoragePolicyError) as exc:
        print(f"storage preflight failed closed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
