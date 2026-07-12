#!/usr/bin/env python3
"""Verify a v2 question matrix in a separate, fail-closed process."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.question_matrix_v2_validator import verify  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--require-release", action="store_true")
    return parser.parse_args()


def write_atomic(path: Path, value: dict[str, object]) -> None:
    content_without_checksum = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output = dict(value)
    output["report_sha256"] = hashlib.sha256(content_without_checksum.encode("utf-8")).hexdigest()
    content = json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    args = parse_args()
    matrix = args.matrix.resolve()
    corpus = args.corpus.resolve()
    report_path = (args.report or (matrix / "verification-report.json")).resolve()
    report = verify(matrix, corpus)
    ledger = report.pop("_verification_ledger")
    ledger_path = report_path.with_name("verification-ledger.jsonl")
    ledger_content = "".join(
        json.dumps(item, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n" for item in ledger
    )
    write_atomic_text(ledger_path, ledger_content)
    report["verification_ledger"] = {
        "path": str(ledger_path),
        "sha256": hashlib.sha256(ledger_content.encode("utf-8")).hexdigest(),
        "count": len(ledger),
        "verified": sum(item["gold_verification_status"] == "verified" for item in ledger),
        "failed": sum(item["gold_verification_status"] == "failed" for item in ledger),
    }
    write_atomic(report_path, report)
    print(
        json.dumps(
            {
                "report": str(report_path),
                "machine_validations_passed": report["machine_validations_passed"],
                "question_contract_passed": report["question_contract_passed"],
                "validation_errors": report["counts"]["validation_errors"],
            },
            sort_keys=True,
        )
    )
    if not report["machine_validations_passed"]:
        return 1
    if args.require_release and not report["question_contract_passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
