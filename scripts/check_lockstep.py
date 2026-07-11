#!/usr/bin/env python3
"""Fail when controlling, generated or documentation surfaces drift apart."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED = [
    "AGENTS.md",
    "README.md",
    "CHANGELOG.md",
    "WHATS_ON_GOVUK_OKF.md",
    "planning/RUN_AFHF_GOVUK_OKF_UNATTENDED.md",
    "planning/AFHF_GOVUK_OKF_REQUIREMENTS_REGISTER.md",
    "planning/AFHF_GOVUK_OKF_BRIEF_TRACEABILITY.md",
    "planning/PLAN_SOURCE_PREFLIGHT.json",
    "governance/requirements.yaml",
    "governance/traceability.json",
    "governance/launch-manifest.yaml",
    "orchestration/dag.yaml",
    "orchestration/models.lock.yaml",
]


def main() -> int:
    errors = [f"missing required lockstep file: {path}" for path in REQUIRED if not (ROOT / path).is_file()]
    requirements_path = ROOT / "governance" / "requirements.yaml"
    trace_path = ROOT / "governance" / "traceability.json"
    if requirements_path.is_file():
        document = json.loads(requirements_path.read_text(encoding="utf-8"))
        if document.get("counts") != {"acceptance_gates": 11, "requirements": 95}:
            errors.append("requirements projection does not contain 95 requirements and 11 gates")
    if trace_path.is_file():
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        if trace.get("counts", {}).get("requirements") != 95:
            errors.append("traceability projection has the wrong requirement denominator")

    contracts = sorted((ROOT / "orchestration" / "task-contracts").glob("*.json"))
    dag = json.loads((ROOT / "orchestration" / "dag.yaml").read_text(encoding="utf-8"))
    if len(contracts) != len(dag["tasks"]):
        errors.append(f"task-contract count {len(contracts)} does not match DAG task count {len(dag['tasks'])}")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if "execution contract" not in changelog or "requirements" not in changelog:
        errors.append("CHANGELOG.md does not describe the controlling contract")

    if errors:
        print("lockstep validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"lockstep validated: 95 requirements, 11 gates, {len(contracts)} task contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
