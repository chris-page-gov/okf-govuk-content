#!/usr/bin/env python3
"""Close a fully hydrated T0 against an authoritative T1 re-enumeration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.closing import ClosingDelta, ClosingError  # noqa: E402


def _reconciled_path(reconciliation: Path, fields: tuple[str, ...]) -> Path | None:
    if not reconciliation.is_file():
        return None
    try:
        value = json.loads(reconciliation.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    for field in fields:
        candidate = value.get(field)
        if isinstance(candidate, str) and candidate:
            return (ROOT / candidate).resolve()
    return None


def _default_t0_records(label: str, reconciliation: Path) -> Path:
    reconciled = _reconciled_path(
        reconciliation, ("hydrated_records_path", "hydrated_records_manifest")
    )
    if reconciled is not None:
        return reconciled
    root = ROOT / "corpus" / "records" / label
    legacy = root / "source-records.jsonl.gz"
    return root if (root / "manifest.json").is_file() else legacy


def _default_inventory(label: str, reconciliation: Path) -> Path:
    reconciled = _reconciled_path(
        reconciliation, ("inventory_path", "inventory_manifest", "hydrated_records_path")
    )
    if reconciled is not None:
        return reconciled
    sharded = ROOT / "corpus" / "inventory" / label
    legacy = ROOT / "corpus" / "inventory" / f"{label}-source-records.jsonl.gz"
    return sharded if sharded.is_dir() else legacy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("t0_label", help="fully hydrated opening snapshot label")
    parser.add_argument("t1_label", help="authoritative closing enumeration label")
    parser.add_argument("--label", help="closing output label; defaults to <T1>-closed")
    parser.add_argument(
        "--t0-enumeration",
        type=Path,
        help="T0 enumerator record file, shard manifest, or directory",
    )
    parser.add_argument(
        "--t0-hydrated",
        type=Path,
        help="T0 hydrated record file, shard manifest, or directory",
    )
    parser.add_argument(
        "--t1-enumeration",
        type=Path,
        help="T1 enumerator record file, shard manifest, or directory",
    )
    parser.add_argument("--t0-reconciliation", type=Path)
    parser.add_argument("--t1-reconciliation", type=Path)
    parser.add_argument("--rate", type=float, default=8.0, help="Content API requests per second")
    parser.add_argument(
        "--www-rate",
        type=float,
        default=2.0,
        help="robots and public www.gov.uk closing probes per second",
    )
    parser.add_argument(
        "--official-request-ceiling",
        type=int,
        default=1_000_000,
        help="hard ceiling including bounded retry attempts",
    )
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument(
        "--work-limit",
        type=int,
        help="bounded resumability test; an open run cannot be exported",
    )
    parser.add_argument("--no-export", action="store_true")
    args = parser.parse_args()

    t0_reconciliation = args.t0_reconciliation or (
        ROOT / "corpus" / "reconciliation" / f"{args.t0_label}-hydrated.json"
    )
    t1_reconciliation = args.t1_reconciliation or (
        ROOT / "corpus" / "reconciliation" / f"{args.t1_label}.json"
    )
    t0_enumeration_reconciliation = ROOT / "corpus" / "reconciliation" / f"{args.t0_label}.json"
    closing = ClosingDelta(
        ROOT,
        args.t0_label,
        args.t1_label,
        args.t0_enumeration
        or _default_inventory(args.t0_label, t0_enumeration_reconciliation),
        args.t0_hydrated or _default_t0_records(args.t0_label, t0_reconciliation),
        args.t1_enumeration or _default_inventory(args.t1_label, t1_reconciliation),
        t0_reconciliation,
        t1_reconciliation,
        closing_label=args.label,
        requests_per_second=args.rate,
        www_requests_per_second=args.www_rate,
        official_request_ceiling=args.official_request_ceiling,
        workers=args.workers,
        batch_size=args.batch_size,
    )
    try:
        progress = closing.run(work_limit=args.work_limit)
        print(json.dumps(progress, sort_keys=True))
        if progress["closed"] and not args.no_export and args.work_limit is None:
            result = closing.export()
            print(json.dumps(result, sort_keys=True))
    except (ClosingError, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"closing delta failed closed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
