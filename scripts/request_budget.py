#!/usr/bin/env python3
"""Initialise and inspect the fail-closed official-source request ledger."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / ".tmp" / "request-budget" / "official-sources.count"
LAUNCH = ROOT / "governance" / "launch-manifest.yaml"
PREFLIGHT = ROOT / "research" / "source-preflight.json"


def ceiling() -> int:
    match = re.search(r"^\s*official_source_requests:\s*([0-9]+)\s*$", LAUNCH.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise ValueError("launch manifest has no numeric official_source_requests ceiling")
    return int(match.group(1))


def observations(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if "requested_url" in value and "status" in value:
            yield value
        for child in value.values():
            yield from observations(child)
    elif isinstance(value, list):
        for child in value:
            yield from observations(child)


def preflight_attempts() -> int:
    value = json.loads(PREFLIGHT.read_text(encoding="utf-8"))
    return sum(int(row.get("attempts") or row.get("acquisition_attempt") or 1) for row in observations(value))


def read_consumed() -> int:
    if not LEDGER.is_file():
        return 0
    return int(LEDGER.read_text(encoding="utf-8").strip() or "0")


def write_initial(value: int) -> None:
    if value < 0 or value > ceiling():
        raise ValueError("initial request count is outside the authorised ceiling")
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".official-sources.", dir=LEDGER.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(f"{value}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, LEDGER)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("initialise", "status"))
    args = parser.parse_args()
    authorised = ceiling()
    if args.action == "initialise":
        observed = preflight_attempts()
        current = read_consumed()
        if LEDGER.exists() and current != observed:
            raise SystemExit(
                f"request ledger already exists at {current}; refusing to reset it to preflight count {observed}"
            )
        if not LEDGER.exists():
            write_initial(observed)
    consumed = read_consumed()
    print(
        json.dumps(
            {
                "schema": "govuk-okf-request-budget.v1",
                "ledger": str(LEDGER.relative_to(ROOT)),
                "authorised_ceiling": authorised,
                "consumed_attempts": consumed,
                "remaining_attempts": authorised - consumed,
                "preflight_evidence": str(PREFLIGHT.relative_to(ROOT)),
                "preflight_attempts": preflight_attempts(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
