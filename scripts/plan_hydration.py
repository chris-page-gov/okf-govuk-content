#!/usr/bin/env python3
"""Build a deterministic Content API selection manifest from a frozen inventory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.acquisition import write_jsonl_gzip_shards, write_text_atomic  # noqa: E402
from govuk_okf.hydration import read_source_records  # noqa: E402
from govuk_okf.hydration_policy import (  # noqa: E402
    hydration_decision,
    selection_manifest,
)
from govuk_okf.storage import StoragePolicyError, load_storage_policy  # noqa: E402
from govuk_okf.util import pretty_json  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", help="immutable snapshot label")
    parser.add_argument("--source", type=Path, help="source-record inventory manifest")
    parser.add_argument("--reconciliation", type=Path, help="enumeration reconciliation JSON")
    args = parser.parse_args()
    reconciliation = args.reconciliation or ROOT / "corpus" / "reconciliation" / f"{args.label}.json"
    source = args.source
    if source is None:
        if not reconciliation.is_file():
            parser.error("--source is required when reconciliation is unavailable")
        document = json.loads(reconciliation.read_text(encoding="utf-8"))
        inventory_path = document.get("inventory_path")
        if not isinstance(inventory_path, str):
            parser.error("enumeration reconciliation has no inventory_path")
        source = ROOT / inventory_path
    try:
        policy = load_storage_policy(ROOT)
        policy.preflight(reserve_bytes=52 * 1024 * 1024)

        def selected_records():
            for record in read_source_records(source):
                decision = hydration_decision(record)
                if decision.selected:
                    yield decision.record(record)

        output = write_jsonl_gzip_shards(
            ROOT / "corpus" / "inventory" / args.label,
            "hydration-selection",
            selected_records(),
        )
        summary = selection_manifest(read_source_records(source), args.label)
        summary["selected_candidate_manifest"] = (
            Path(output["root"]) / "index.json"
        ).relative_to(ROOT).as_posix()
        summary["selected_candidate_shards"] = [
            (Path(output["root"]) / row["path"]).relative_to(ROOT).as_posix()
            for row in output["shards"]
        ]
        summary["selected_candidate_canonical_sha256"] = output["canonical_sha256"]
        target = ROOT / "corpus" / "source-manifests" / args.label / "hydration-selection.json"
        write_text_atomic(target, pretty_json(summary))
    except (OSError, ValueError, StoragePolicyError) as exc:
        print(f"hydration planning failed closed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
