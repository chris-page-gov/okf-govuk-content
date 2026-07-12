#!/usr/bin/env python3
"""Collect, fetch, and verify the released citation corpus."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.citations import CitationError, cli  # noqa: E402


if __name__ == "__main__":
    try:
        raise SystemExit(cli())
    except CitationError as exc:
        print(f"citation verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
