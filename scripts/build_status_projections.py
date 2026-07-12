#!/usr/bin/env python3
"""Build or check honest requirement, traceability and task status projections."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "governance" / "implementation-status-source.json"
REQUIREMENTS = ROOT / "governance" / "requirements.yaml"
TRACEABILITY = ROOT / "governance" / "traceability.json"
CONTRACTS = ROOT / "orchestration" / "task-contracts"
OUTPUTS = {
    "requirements": ROOT / "governance" / "requirements-status.json",
    "traceability": ROOT / "governance" / "traceability-status.json",
    "tasks": ROOT / "governance" / "task-status.json",
}
STATUS_RANK = {"produced": 0, "in_progress": 1, "blocked": 2}
RANGE = re.compile(r"^REQ-(\d{3})\.\.REQ-(\d{3})$")


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.relative_to(ROOT)} must contain an object")
    return value


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expand_requirement_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        match = RANGE.fullmatch(value)
        if match:
            start, end = map(int, match.groups())
            if end < start:
                raise ValueError(f"reversed requirement range: {value}")
            result.extend(f"REQ-{number:03d}" for number in range(start, end + 1))
        elif re.fullmatch(r"REQ-\d{3}", value):
            result.append(value)
        else:
            raise ValueError(f"invalid requirement identifier: {value}")
    return result


def require_existing_evidence(groups: list[dict[str, Any]]) -> None:
    for group in groups:
        for value in group.get("evidence", []):
            relative = Path(value)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe evidence path: {value}")
            if not (ROOT / relative).exists():
                raise ValueError(f"status evidence does not exist: {value}")


def status_by_id(
    groups: list[dict[str, Any]],
    id_field: str,
    expected: set[str],
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for group in groups:
        identifiers = (
            expand_requirement_ids(group[id_field])
            if id_field == "requirement_ids"
            else group[id_field]
        )
        if group.get("status") not in STATUS_RANK:
            raise ValueError(f"invalid implementation status: {group.get('status')}")
        for identifier in identifiers:
            if identifier in indexed:
                raise ValueError(f"duplicate status assignment: {identifier}")
            indexed[identifier] = group
    missing = expected - set(indexed)
    unknown = set(indexed) - expected
    if missing or unknown:
        raise ValueError(f"status coverage differs; missing={sorted(missing)}, unknown={sorted(unknown)}")
    return indexed


def render() -> dict[Path, str]:
    source = load(SOURCE)
    requirements = load(REQUIREMENTS)
    traceability = load(TRACEABILITY)
    requirement_rows = requirements["requirements"]
    requirement_ids = {row["id"] for row in requirement_rows}
    contracts = {path.stem: load(path) for path in sorted(CONTRACTS.glob("*.json"))}
    require_existing_evidence(source["requirement_status_groups"])
    requirement_status = status_by_id(
        source["requirement_status_groups"], "requirement_ids", requirement_ids
    )
    task_status = status_by_id(source["task_status_groups"], "task_ids", set(contracts))

    projected_requirements = []
    for row in requirement_rows:
        group = requirement_status[row["id"]]
        projected_requirements.append(
            {
                "requirement_id": row["id"],
                "contract_status": row["status"],
                "implementation_status": group["status"],
                "artifact_tier": group["artifact_tier"],
                "evidence": group["evidence"],
                "qualification": group["qualification"],
                "clause_ids": row.get("clause_ids", []),
            }
        )
    requirement_counts = Counter(row["implementation_status"] for row in projected_requirements)
    requirement_document = {
        "schema": "afhf-govuk-okf-requirements-status.v1",
        "generated_from": SOURCE.relative_to(ROOT).as_posix(),
        "generated_from_sha256": digest(SOURCE),
        "requirements_contract_sha256": digest(REQUIREMENTS),
        "as_of": source["as_of"],
        "milestone": source["milestone"],
        "publication_ready": source["publication_ready"],
        "interpretation": "accepted is the contract status; produced is not passed or release-verified",
        "counts": {
            "requirements": len(projected_requirements),
            "by_implementation_status": dict(sorted(requirement_counts.items())),
            "passed": 0,
        },
        "requirements": projected_requirements,
    }

    projected_clauses = []
    for clause in traceability["clauses"]:
        statuses = [requirement_status[item]["status"] for item in clause["requirement_ids"]]
        overall = max(statuses, key=STATUS_RANK.__getitem__) if statuses else "in_progress"
        projected_clauses.append(
            {
                "clause_id": clause["clause_id"],
                "requirement_ids": clause["requirement_ids"],
                "implementation_status": overall,
                "requirement_statuses": dict(sorted(Counter(statuses).items())),
            }
        )
    clause_counts = Counter(row["implementation_status"] for row in projected_clauses)
    trace_document = {
        "schema": "afhf-govuk-okf-traceability-status.v1",
        "generated_from": SOURCE.relative_to(ROOT).as_posix(),
        "generated_from_sha256": digest(SOURCE),
        "traceability_contract_sha256": digest(TRACEABILITY),
        "as_of": source["as_of"],
        "milestone": source["milestone"],
        "interpretation": "Clause status is the least advanced mapped requirement status; mapping coverage does not prove implementation acceptance.",
        "counts": {
            "clauses": len(projected_clauses),
            "by_implementation_status": dict(sorted(clause_counts.items())),
        },
        "clauses": projected_clauses,
    }

    projected_tasks = []
    for task_id, contract in sorted(contracts.items()):
        group = task_status[task_id]
        projected_tasks.append(
            {
                "task_id": task_id,
                "objective": contract["objective"],
                "implementation_status": group["status"],
                "artifact_tier": group["artifact_tier"],
                "qualification": group["qualification"],
                "contract_outputs": contract["output_artifacts"],
                "requirement_ids": contract["requirement_ids"],
            }
        )
    task_counts = Counter(row["implementation_status"] for row in projected_tasks)
    task_document = {
        "schema": "afhf-govuk-okf-task-status.v1",
        "generated_from": SOURCE.relative_to(ROOT).as_posix(),
        "generated_from_sha256": digest(SOURCE),
        "as_of": source["as_of"],
        "milestone": source["milestone"],
        "interpretation": "A produced task has an artefact foundation only; the controller promotion gate has not accepted it unless separate run evidence says so.",
        "counts": {
            "tasks": len(projected_tasks),
            "by_implementation_status": dict(sorted(task_counts.items())),
            "accepted": 0,
        },
        "tasks": projected_tasks,
    }
    documents = {
        OUTPUTS["requirements"]: requirement_document,
        OUTPUTS["traceability"]: trace_document,
        OUTPUTS["tasks"]: task_document,
    }
    return {
        path: json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        for path, document in documents.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if checked-in projections differ")
    args = parser.parse_args()
    try:
        documents = render()
        errors = []
        for path, expected in documents.items():
            if args.check:
                if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                    errors.append(f"{path.relative_to(ROOT)} is missing or stale")
            else:
                path.write_text(expected, encoding="utf-8")
        if errors:
            print("status projection check failed:", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"status projection failed: {exc}", file=sys.stderr)
        return 1
    action = "validated" if args.check else "wrote"
    print(f"{action} 95 requirement, 21 clause and 36 task statuses; no release passes claimed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
