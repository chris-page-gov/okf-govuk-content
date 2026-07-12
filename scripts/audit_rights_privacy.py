#!/usr/bin/env python3
"""Emit snapshot-bound GOV.UK bundle rights, privacy and fair-use evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.rights_audit import RightsAuditError, audit_release, write_audit  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--release-manifest", type=Path)
    parser.add_argument("--publication-manifest", type=Path)
    parser.add_argument(
        "--corpus-manifest",
        type=Path,
        action="append",
        default=[],
        help="final hydration/source manifest; repeat only for disjoint snapshot-bound manifests",
    )
    parser.add_argument("--review-ledger", type=Path)
    parser.add_argument("--review-packet", type=Path, help="optional hashed-only item-review work packet")
    parser.add_argument("--generated-at", help="deterministic ISO-8601 evidence time; defaults to publication time")
    parser.add_argument("--output", type=Path, default=Path("release/rights-privacy-audit.json"))
    parser.add_argument("--check", action="store_true", help="compare with checked-in evidence without writing")
    parser.add_argument(
        "--require-release",
        action="store_true",
        help="exit non-zero unless an unsampled, corpus-bound, fully reviewed release audit passes",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        result = audit_release(
            root,
            release_manifest_path=args.release_manifest,
            publication_manifest_path=args.publication_manifest,
            corpus_manifest_paths=args.corpus_manifest,
            review_ledger_path=args.review_ledger,
            generated_at=args.generated_at,
            review_packet_path=args.review_packet,
        )
        output = args.output if args.output.is_absolute() else root / args.output
        if args.check:
            try:
                existing = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RightsAuditError(f"cannot read checked rights/privacy evidence: {exc}") from exc
            if existing != result:
                raise RightsAuditError("checked rights/privacy evidence is stale")
        else:
            write_audit(output, result)
    except (RightsAuditError, OSError, ValueError) as exc:
        print(f"rights/privacy audit failed closed: {exc}", file=sys.stderr)
        return 1
    state = "passed" if result["rights_privacy_audit_passed"] else "checkpoint, not release-passing"
    print(
        f"rights/privacy audit: {state}; "
        f"items={result['scan']['classification_items']}; "
        f"triggers={result['classification']['item_review_triggered_items']}; "
        f"violations={result['retention_and_secret_findings']['finding_count']}"
    )
    if args.require_release and not result["rights_privacy_audit_passed"]:
        for item in result["remaining_release_blockers"]:
            print(f"- {item}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
