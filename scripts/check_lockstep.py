#!/usr/bin/env python3
"""Fail when controlling, generated or documentation surfaces drift apart."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from build_status_projections import (
    load_release_state,
    render as render_status_projections,
)
from build_aim_scorecard import render as render_aim_scorecard
from check_provenance import (
    ProvenanceError,
    build_validation_document as build_provenance_validation,
)

ROOT = Path(__file__).resolve().parents[1]

REQUIRED = [
    "AGENTS.md",
    "README.md",
    "CHANGELOG.md",
    "docs/architecture.md",
    "docs/implementation-status.md",
    "docs/reproducibility.md",
    "WHATS_ON_GOVUK_OKF.md",
    "planning/RUN_AFHF_GOVUK_OKF_UNATTENDED.md",
    "planning/AFHF_GOVUK_OKF_REQUIREMENTS_REGISTER.md",
    "planning/AFHF_GOVUK_OKF_BRIEF_TRACEABILITY.md",
    "planning/PLAN_SOURCE_PREFLIGHT.json",
    "governance/requirements.yaml",
    "governance/traceability.json",
    "governance/launch-manifest.yaml",
    "governance/implementation-status-source.json",
    "governance/aim-assessment-source.json",
    "governance/aim-assessment.schema.json",
    "governance/rights-review-ledger.json",
    "governance/requirements-status.json",
    "governance/traceability-status.json",
    "governance/task-status.json",
    "orchestration/dag.yaml",
    "orchestration/models.lock.yaml",
    "research/source-registry.yaml",
    "research/source-preflight.json",
    "research/source-constraints.json",
    "research/official-source-audit.md",
    "provenance/activity-ledger.jsonl",
    "provenance/activity-ledger.schema.json",
    "provenance/reproduction-declarations.json",
    "provenance/source-request-budget.json",
    "release/provenance-validation.json",
    "release/sbom.cdx.json",
    "release/clean-room-reproduction.json",
    "release/rights-privacy-audit.json",
    "release/aim-assessment.json",
    "reports/aim-scorecard.md",
    "scripts/promote_release.py",
]


def provenance_validation_flags(release_state: str) -> dict[str, bool]:
    if release_state == "checkpoint":
        return {"require_candidate": False, "require_release": False}
    if release_state == "candidate":
        return {"require_candidate": True, "require_release": False}
    if release_state == "release":
        return {"require_candidate": False, "require_release": True}
    raise ValueError(f"unsupported release state for provenance: {release_state}")


def load_json_document(path: Path, label: str, errors: list[str]) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{label}: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{label} must contain an object")
        return {}
    return value


def main() -> int:
    errors = [f"missing required lockstep file: {path}" for path in REQUIRED if not (ROOT / path).is_file()]
    release_control = None
    try:
        release_control = load_release_state(ROOT)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"release state: {exc}")
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

    plan_urls: list[str] = []
    plan_path = ROOT / "planning" / "AFHF_GOVUK_OKF_SOTA_RESEARCH_IMPLEMENTATION_PLAN.md"
    plan_preflight_path = ROOT / "planning" / "PLAN_SOURCE_PREFLIGHT.json"
    if plan_path.is_file() and plan_preflight_path.is_file():
        plan = plan_path.read_bytes()
        plan_urls = sorted(
            url.decode("utf-8") for url in set(re.findall(rb"\]\((https?://[^)]+)\)", plan))
        )
        plan_preflight = json.loads(plan_preflight_path.read_text(encoding="utf-8"))
        if plan_preflight.get("plan_sha256") != hashlib.sha256(plan).hexdigest():
            errors.append("plan-source preflight hash does not match the implementation plan")
        if [source.get("url") for source in plan_preflight.get("sources", [])] != plan_urls:
            errors.append("plan-source preflight URLs do not exactly match the implementation plan")

    preflight_path = ROOT / "research" / "source-preflight.json"
    if preflight_path.is_file():
        preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
        summary = preflight.get("summary", {})
        if summary.get("official_total") != 32 or summary.get("official_failed") != 0:
            errors.append("official-source preflight is not the accepted 32/32 result")
        if summary.get("plan_total") != 93:
            errors.append("plan-source preflight does not account for all 93 URLs")
        if summary.get("plan_failed") != 0 or summary.get("plan_ok") != 93:
            errors.append("the active plan-source preflight is not the accepted 93/93 result")
        if [source.get("requested_url") for source in preflight.get("plan_sources", [])] != plan_urls:
            errors.append("live plan-source results do not exactly match the implementation plan")
        history = preflight.get("plan_source_history", {})
        if history.get("preserved_original_result_count") != 93 or history.get("original_summary") != {
            "plan_failed": 1,
            "plan_ok": 92,
            "plan_total": 93,
        }:
            errors.append("the original 93-result plan preflight history is not preserved")
        superseded = history.get("superseded_results", [])
        if len(superseded) != 1 or "DH_KEY_TOO_SMALL" not in superseded[0].get("error", ""):
            errors.append("the superseded CMU strict-TLS failure is not preserved")
        else:
            superseded_by_id = {item["id"]: item for item in superseded}
            reconstructed = [
                superseded_by_id.get(item.get("id"), item)
                for item in preflight.get("plan_sources", [])
            ]
            original_digest = hashlib.sha256(
                json.dumps(
                    reconstructed,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if original_digest != history.get("original_results_sha256"):
                errors.append("the reconstructed original 93 plan results do not match their frozen digest")
        restrictions = history.get("access_restrictions", [])
        if not any(
            "researchgate.net" in item.get("url", "")
            and item.get("publication_record_result") == "HTTP 403 Forbidden"
            and item.get("exact_author_pdf_result") == "HTTP 403 Forbidden"
            for item in restrictions
        ):
            errors.append("the ResearchGate publication/PDF access restriction is not preserved")

    contracts = sorted((ROOT / "orchestration" / "task-contracts").glob("*.json"))
    dag = json.loads((ROOT / "orchestration" / "dag.yaml").read_text(encoding="utf-8"))
    if len(contracts) != len(dag["tasks"]):
        errors.append(f"task-contract count {len(contracts)} does not match DAG task count {len(dag['tasks'])}")

    try:
        status_projections = render_status_projections(ROOT)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        status_projections = {}
        errors.append(f"status projections: {exc}")
    for path, expected in status_projections.items():
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            errors.append(f"{path.relative_to(ROOT)} is missing or stale")
    for path, expected in render_aim_scorecard(ROOT).items():
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            errors.append(f"{path.relative_to(ROOT)} is missing or stale")

    requirement_status = load_json_document(
        ROOT / "governance" / "requirements-status.json",
        "requirements status",
        errors,
    )
    trace_status = load_json_document(
        ROOT / "governance" / "traceability-status.json",
        "traceability status",
        errors,
    )
    task_status = load_json_document(
        ROOT / "governance" / "task-status.json",
        "task status",
        errors,
    )
    if requirement_status.get("counts", {}).get("requirements") != 95:
        errors.append("requirements status does not cover all 95 requirements")
    if trace_status.get("counts", {}).get("clauses") != 21:
        errors.append("traceability status does not cover all 21 controlling clauses")
    if task_status.get("counts", {}).get("tasks") != len(contracts):
        errors.append("task status does not cover every task contract")
    if release_control is not None:
        for label, document in (
            ("requirements", requirement_status),
            ("traceability", trace_status),
            ("tasks", task_status),
        ):
            if document.get("release_state") != release_control["release_state"]:
                errors.append(f"{label} status does not match the checked release state")
            if document.get("release_kind") != release_control["release_kind"]:
                errors.append(f"{label} status does not match the checked release kind")
            if document.get("release_id") != release_control["release_id"]:
                errors.append(f"{label} status does not match the checked release ID")
            if document.get("publication_ready") is not release_control["publication_ready"]:
                errors.append(f"{label} status does not match publication readiness")
        if release_control["human_evaluation_status"] != "completed":
            requirement_rows = {
                row.get("requirement_id"): row for row in requirement_status.get("requirements", [])
            }
            task_rows = {row.get("task_id"): row for row in task_status.get("tasks", [])}
            if requirement_rows.get("REQ-077", {}).get("implementation_status") != "blocked":
                errors.append("REQ-077 is not blocked while human evaluation is unavailable")
            if task_rows.get("E3-01", {}).get("implementation_status") != "blocked":
                errors.append("E3-01 is not blocked while human evaluation is unavailable")

        try:
            provenance_flags = provenance_validation_flags(
                str(release_control["release_state"])
            )
            expected_provenance = build_provenance_validation(
                snapshot=release_control["release_id"],
                **provenance_flags,
            )
        except (ProvenanceError, ValueError) as exc:
            errors.extend(f"provenance: {line}" for line in str(exc).splitlines())
        else:
            if expected_provenance.get("provenance_validation_passed") is not True:
                validation_errors = expected_provenance.get("validation_errors", [])
                if validation_errors:
                    errors.extend(f"provenance: {line}" for line in validation_errors)
                else:
                    errors.append("state-aware provenance validation did not pass")
            if expected_provenance.get("external_paid_model_usage", {}).get("api_calls") != 0:
                errors.append("provenance ledger records external paid model calls despite zero authority")
            provenance_evidence_path = ROOT / "release" / "provenance-validation.json"
            if provenance_evidence_path.is_file():
                actual_provenance = json.loads(provenance_evidence_path.read_text(encoding="utf-8"))
                if actual_provenance != expected_provenance:
                    errors.append(
                        "release/provenance-validation.json is stale for the checked "
                        f"{release_control['release_state']} state"
                    )

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if "execution contract" not in changelog or "requirements" not in changelog:
        errors.append("CHANGELOG.md does not describe the controlling contract")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if "scripts/run_pipeline.py" in readme:
        errors.append("README.md references the removed run_pipeline.py command")
    for command in ("scripts/acquire_corpus.py", "scripts/hydrate_corpus.py", "scripts/check_release.py"):
        if command not in (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8"):
            errors.append(f"architecture documentation omits operational command {command}")

    if errors:
        print("lockstep validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    assert release_control is not None
    print(
        f"lockstep validated: 95 requirements, 21 clauses, 11 gates, "
        f"{len(contracts)} task contracts; state={release_control['release_state']}, "
        f"passed={requirement_status['counts']['passed']}, "
        f"accepted={task_status['counts']['accepted']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
