#!/usr/bin/env python3
"""Acquire, check or rebuild the bounded 69-record new-child demonstrator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.acquisition import AcquisitionError  # noqa: E402
from govuk_okf.new_child_demo import (  # noqa: E402
    DEFAULT_CONTRACT,
    NewChildDemoAcquirer,
    NewChildDemoError,
    rebuild_snapshot,
    validate_snapshot,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    commands = parser.add_subparsers(dest="command", required=True)

    acquire = commands.add_parser("acquire", help="perform one live bounded official-source acquisition")
    acquire.add_argument("snapshot_id")
    acquire.add_argument("--output", type=Path)
    acquire.add_argument(
        "--rate-state",
        type=Path,
        help="shared www.gov.uk timestamp ledger; defaults to this checkout's .tmp ledger",
    )
    acquire.add_argument(
        "--request-ledger",
        type=Path,
        help="programme-wide official-source attempt ledger, incremented separately from the local 500 cap",
    )

    check = commands.add_parser("check", help="verify hashes, scope and a byte-identical offline rebuild")
    check.add_argument("snapshot", type=Path)

    rebuild = commands.add_parser("rebuild", help="rebuild publication inputs without network access")
    rebuild.add_argument("snapshot", type=Path)
    rebuild.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "acquire":
            output = args.output or ROOT / "demo" / "snapshots" / args.snapshot_id
            result = NewChildDemoAcquirer(
                args.contract,
                rate_state_path=args.rate_state,
                request_ledger_path=args.request_ledger,
            ).acquire(args.snapshot_id, output)
        elif args.command == "check":
            result = validate_snapshot(args.snapshot)
        else:
            result = rebuild_snapshot(args.snapshot, args.output)
    except (AcquisitionError, NewChildDemoError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"new-child demonstrator failed closed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
