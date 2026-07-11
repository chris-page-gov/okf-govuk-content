"""Import and validate the controlling programme contract."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
BRIEF = ROOT / "WHATS_ON_GOVUK_OKF.md"
REGISTER = ROOT / "planning" / "AFHF_GOVUK_OKF_REQUIREMENTS_REGISTER.md"
TRACE = ROOT / "planning" / "AFHF_GOVUK_OKF_BRIEF_TRACEABILITY.md"
PLAN = ROOT / "planning" / "AFHF_GOVUK_OKF_SOTA_RESEARCH_IMPLEMENTATION_PLAN.md"
PREFLIGHT = ROOT / "planning" / "PLAN_SOURCE_PREFLIGHT.json"
REQUIREMENTS_OUTPUT = ROOT / "governance" / "requirements.yaml"
TRACE_OUTPUT = ROOT / "governance" / "traceability.json"

REQUIREMENT_RE = re.compile(r"^- \*\*(REQ-\d{3})\*\* (.+)$")
RANGE_RE = re.compile(r"REQ-(\d{3})(?:[–-]REQ-(\d{3}))?")
CLAUSE_RE = re.compile(r"^\| ((?:BRIEF|PROMPT|USER)-\d{3}) \|")


class ContractError(RuntimeError):
    """Raised when a controlling input or generated projection is invalid."""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expand_requirement_references(text: str) -> list[str]:
    found: list[str] = []
    for match in RANGE_RE.finditer(text):
        start = int(match.group(1))
        end = int(match.group(2) or match.group(1))
        if end < start:
            raise ContractError(f"reversed requirement range: {match.group(0)}")
        found.extend(f"REQ-{number:03d}" for number in range(start, end + 1))
    return sorted(set(found))


def parse_requirements() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    lines = REGISTER.read_text(encoding="utf-8").splitlines()
    requirements: list[dict[str, object]] = []
    section = ""
    for line in lines:
        if line.startswith("### "):
            section = line[4:].strip()
        match = REQUIREMENT_RE.match(line)
        if match:
            requirements.append(
                {
                    "id": match.group(1),
                    "section": section,
                    "text": match.group(2).strip(),
                    "status": "accepted",
                }
            )

    gates: list[dict[str, object]] = []
    in_gates = False
    for line in lines:
        if line == "## Acceptance gates":
            in_gates = True
            continue
        if in_gates and line.startswith("## "):
            break
        match = re.match(r"^(\d+)\. \*\*([^*]+):\*\* (.+)$", line)
        if in_gates and match:
            gates.append(
                {
                    "id": f"GATE-{int(match.group(1)):02d}",
                    "title": match.group(2),
                    "criterion": match.group(3),
                    "status": "pending",
                }
            )
    return requirements, gates


def parse_traceability() -> list[dict[str, object]]:
    clauses: list[dict[str, object]] = []
    for line in TRACE.read_text(encoding="utf-8").splitlines():
        match = CLAUSE_RE.match(line)
        if not match:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        clauses.append(
            {
                "clause_id": match.group(1),
                "cells": cells[1:-1],
                "requirement_ids": expand_requirement_references(cells[-1]),
            }
        )
    return clauses


def validate_inputs() -> None:
    brief_hash = sha256(BRIEF)
    trace_text = TRACE.read_text(encoding="utf-8")
    match = re.search(r"Controlling file SHA-256: `([0-9a-f]{64})`", trace_text)
    if not match or match.group(1) != brief_hash:
        raise ContractError("controlling brief hash does not match traceability record")

    preflight = json.loads(PREFLIGHT.read_text(encoding="utf-8"))
    if preflight.get("plan_sha256") != sha256(PLAN):
        raise ContractError("implementation plan hash does not match PLAN_SOURCE_PREFLIGHT.json")
    summary = preflight.get("summary", {})
    if summary.get("unique_urls") != len(preflight.get("sources", [])):
        raise ContractError("plan-source preflight source count is inconsistent")


def render() -> dict[Path, str]:
    validate_inputs()
    requirements, gates = parse_requirements()
    clauses = parse_traceability()
    if len(requirements) != 95:
        raise ContractError(f"expected 95 requirements, found {len(requirements)}")
    if len(gates) != 11:
        raise ContractError(f"expected 11 acceptance gates, found {len(gates)}")

    mapped = {req for clause in clauses for req in clause["requirement_ids"]}
    requirement_ids = {item["id"] for item in requirements}
    unknown = mapped - requirement_ids
    if unknown:
        raise ContractError(f"traceability references unknown requirements: {sorted(unknown)}")
    for requirement in requirements:
        requirement["clause_ids"] = [
            clause["clause_id"] for clause in clauses if requirement["id"] in clause["requirement_ids"]
        ]
        if not requirement["clause_ids"]:
            requirement["derived_control_rationale"] = (
                "Derived deterministic, security, reproducibility or statistical control under PROMPT-007/008."
            )

    requirement_document = {
        "schema_version": 1,
        "generated_from": REGISTER.relative_to(ROOT).as_posix(),
        "controlling_sha256": sha256(REGISTER),
        "requirements": requirements,
        "acceptance_gates": gates,
        "counts": {"requirements": len(requirements), "acceptance_gates": len(gates)},
    }
    trace_document = {
        "schema_version": 1,
        "generated_from": TRACE.relative_to(ROOT).as_posix(),
        "controlling_sha256": sha256(TRACE),
        "brief_sha256": sha256(BRIEF),
        "clauses": clauses,
        "counts": {
            "clauses": len(clauses),
            "mapped_requirements": len(mapped),
            "requirements": len(requirements),
        },
    }
    return {
        REQUIREMENTS_OUTPUT: json.dumps(requirement_document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        TRACE_OUTPUT: json.dumps(trace_document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    }


def synchronize(check: bool = False) -> list[str]:
    errors: list[str] = []
    for path, expected in render().items():
        if check:
            if not path.is_file():
                errors.append(f"{path.relative_to(ROOT)} is missing")
            elif path.read_text(encoding="utf-8") != expected:
                errors.append(f"{path.relative_to(ROOT)} is out of date")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8")
    return errors

