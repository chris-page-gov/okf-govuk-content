#!/usr/bin/env python3
"""Build or check honest requirement, traceability and task status projections."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

import check_provenance

ROOT = Path(__file__).resolve().parents[1]
SOURCE_RELATIVE = Path("governance/implementation-status-source.json")
REQUIREMENTS_RELATIVE = Path("governance/requirements.yaml")
TRACEABILITY_RELATIVE = Path("governance/traceability.json")
CONTRACTS_RELATIVE = Path("orchestration/task-contracts")
RELEASE_MANIFEST_RELATIVE = Path("release/manifest.yaml")
RELEASE_STATUS_RELATIVE = Path("release/status.json")
ACTIVITY_LEDGER_RELATIVE = Path("provenance/activity-ledger.jsonl")
ACTIVITY_SCHEMA_RELATIVE = Path("provenance/activity-ledger.schema.json")
DECLARATIONS_RELATIVE = Path("provenance/reproduction-declarations.json")
REQUEST_BUDGET_RELATIVE = Path("provenance/source-request-budget.json")
OUTPUT_RELATIVES = {
    "requirements": Path("governance/requirements-status.json"),
    "traceability": Path("governance/traceability-status.json"),
    "tasks": Path("governance/task-status.json"),
}
REQUIREMENT_STATUSES = {"passed", "produced", "in_progress", "blocked"}
TASK_STATUSES = {"accepted", "produced", "in_progress", "blocked"}
STATUS_RANK = {"passed": -1, "produced": 0, "in_progress": 1, "blocked": 2}
RANGE = re.compile(r"^REQ-(\d{3})\.\.REQ-(\d{3})$")
TERMINAL_ACTIVITY_ID = re.compile(r"^ACT-[A-Z0-9]+(?:-[A-Z0-9]+)*-TERMINAL-[0-9]{3}$")
POST_PUBLICATION_TERMINAL_ACTIVITY_ID = "ACT-F2-PUBLICATION-REGISTRY-TERMINAL-001"
MACHINE_MARKER = "AFHF_GOVUK_OKF_MACHINE_RELEASE_CANDIDATE_V1"
FULL_MARKER = "AFHF_GOVUK_OKF_RESEARCH_IMPLEMENTATION_COMPLETE_V1"
MACHINE_CANDIDATE_REASON = (
    "Every machine release-candidate gate passed; human research remains not authorised "
    "and UI of choice remains not yet testable."
)
MACHINE_FINAL_REASON = (
    "Machine release candidate finalized after the externally recorded publication, Pages "
    "and Explorer registry terminal event; human research remains not authorised."
)
RELEASE_FLAGS = (
    "aim_assessment_passed",
    "browser_validation_passed",
    "evaluation_passed",
    "full_corpus_reconciled",
    "full_repository_tests_passed",
    "provenance_validation_passed",
    "rights_privacy_audit_passed",
    "sbom_validation_passed",
    "security_scan_passed",
    "semantic_validation_passed",
    "question_contract_passed",
    "citation_verification_passed",
    "clean_room_reproduction_passed",
    "checksum_validation_passed",
)
PUBLICATION_READINESS_SOURCES = [
    RELEASE_MANIFEST_RELATIVE.as_posix(),
    RELEASE_STATUS_RELATIVE.as_posix(),
]
MILESTONE_BY_STATE = {
    ("fixture", "checkpoint"): "t0_census_closed",
    ("full_corpus_checkpoint", "checkpoint"): "full_corpus_checkpoint",
    ("machine_release_candidate", "candidate"): "machine_release_candidate",
    ("machine_release_candidate", "release"): "machine_release_finalized",
    ("full_programme", "release"): "full_programme_complete",
}


def valid_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def canonical_document_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def valid_reproduction_contract(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    source = value.get("source")
    reference = PurePosixPath(source) if isinstance(source, str) else None
    binding = value.get("source_binding")
    if (
        not source
        or reference is None
        or reference.is_absolute()
        or ".." in reference.parts
        or reference.as_posix() != source
        or not isinstance(value.get("generated_at"), str)
        or not value["generated_at"].strip()
        or value.get("compiler") not in {"auto", "memory", "disk"}
        or not isinstance(binding, dict)
        or binding.get("path") != source
        or binding.get("kind") not in {"file", "directory"}
        or not isinstance(binding.get("file_count"), int)
        or isinstance(binding.get("file_count"), bool)
        or binding["file_count"] < 1
        or not isinstance(binding.get("bytes"), int)
        or isinstance(binding.get("bytes"), bool)
        or binding["bytes"] < 0
        or not valid_sha256(binding.get("tree_sha256"))
    ):
        return False
    return binding.get("kind") != "file" or valid_sha256(binding.get("content_sha256"))


def validate_promotion_record(value: object, finalized: bool) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("publishable release lacks a two-stage promotion record")
    if (
        value.get("schema") != "afhf-govuk-okf-two-stage-promotion.v1"
        or value.get("from") != "full_corpus_checkpoint"
        or not valid_sha256(value.get("staged_manifest_sha256"))
        or not valid_sha256(value.get("staged_status_sha256"))
        or value.get("finalized") is not finalized
        or not valid_reproduction_contract(value.get("reproduction"))
    ):
        raise ValueError("publishable release has an incomplete two-stage promotion record")
    candidate_hashes = (
        valid_sha256(value.get("candidate_manifest_sha256"))
        and valid_sha256(value.get("candidate_status_sha256"))
    )
    if finalized and not candidate_hashes:
        raise ValueError("finalized promotion lacks exact candidate control hashes")
    if not finalized and (
        "candidate_manifest_sha256" in value or "candidate_status_sha256" in value
    ):
        raise ValueError("unfinalized promotion contains premature candidate control hashes")
    return value


def require_release_completion_markers(status: dict[str, Any], *, full_programme: bool) -> None:
    expected = {
        "machine_rc_complete": True,
        "agent_evaluation_status": "completed",
        "aims_assessed": True,
        "completion_statement": FULL_MARKER if full_programme else MACHINE_MARKER,
        "full_evaluation_complete": full_programme,
        "programme_complete": full_programme,
        "human_evaluation_status": "completed" if full_programme else "not_authorised",
        "unexplained_omissions": 0,
    }
    for field, value in expected.items():
        if status.get(field) != value:
            raise ValueError(f"release status completion marker differs: {field}")
    if full_programme:
        if status.get("human_ui_of_choice_status") in {None, "not_yet_testable"}:
            raise ValueError("full-programme status lacks completed human UI evidence")
    elif status.get("human_ui_of_choice_status") != "not_yet_testable":
        raise ValueError("machine candidate overstates human UI evidence")


def require_release_gates(manifest: dict[str, Any], status: dict[str, Any]) -> None:
    gates = manifest.get("gates")
    if not isinstance(gates, dict):
        raise ValueError("publishable release manifest has no gate record")
    for flag in RELEASE_FLAGS:
        if gates.get(flag) is not True or status.get(flag) is not True:
            raise ValueError(f"publishable release gate is not true: {flag}")


def load(path: Path, root: Path = ROOT) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        try:
            label = path.relative_to(root)
        except ValueError:
            label = path
        raise ValueError(f"{label} must contain an object")
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


def require_existing_evidence(groups: list[dict[str, Any]], root: Path = ROOT) -> None:
    for group in groups:
        for value in group.get("evidence", []):
            relative = Path(value)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe evidence path: {value}")
            if not (root / relative).exists():
                raise ValueError(f"status evidence does not exist: {value}")


def status_by_id(
    groups: list[dict[str, Any]],
    id_field: str,
    expected: set[str],
    allowed_statuses: set[str],
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for group in groups:
        identifiers = (
            expand_requirement_ids(group[id_field])
            if id_field == "requirement_ids"
            else group[id_field]
        )
        if group.get("status") not in allowed_statuses:
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


def classify_release_state(
    manifest: dict[str, Any], status: dict[str, Any]
) -> dict[str, Any]:
    """Classify the checked release controls without changing either document."""

    if manifest.get("schema") != "afhf-govuk-okf-release-manifest.v1":
        raise ValueError("release manifest schema is not recognised")
    if status.get("schema") != "afhf-govuk-okf-release-status.v1":
        raise ValueError("release status schema is not recognised")
    release_id = manifest.get("release_id")
    if not isinstance(release_id, str) or not release_id.strip():
        raise ValueError("release manifest has no non-empty release_id")
    if status.get("release_id") != release_id:
        raise ValueError("release manifest and status release IDs differ")
    snapshot = manifest.get("snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("id") != release_id:
        raise ValueError("release snapshot ID does not match release_id")
    manifest_ready = manifest.get("publication_ready")
    status_ready = status.get("publication_ready")
    if not isinstance(manifest_ready, bool) or status_ready is not manifest_ready:
        raise ValueError("release manifest and status publication readiness differ")

    release_kind = manifest.get("release_kind")
    status_kind = status.get("status")
    promotion = manifest.get("promotion")
    status_finalized = status.get("promotion_finalized")
    if release_kind == "fixture":
        if snapshot.get("kind") != "fixture" or snapshot.get("sampled") is not True:
            raise ValueError("fixture release does not have an exact sampled fixture snapshot")
        if status_kind != "checkpoint" or manifest_ready:
            raise ValueError("fixture release controls make a publication-ready claim")
        if promotion is not None:
            raise ValueError("fixture release manifest unexpectedly contains promotion state")
        if status_finalized not in {None, False}:
            raise ValueError("fixture release status is marked promotion-finalized")
        if status.get("programme_complete") is not False:
            raise ValueError("fixture release status is programme-complete")
        release_state = "checkpoint"
        finalized = False
    elif release_kind == "full_corpus_checkpoint":
        if snapshot.get("kind") != "full_corpus" or snapshot.get("sampled") is not False:
            raise ValueError("full-corpus checkpoint does not have an exact unsampled snapshot")
        if status_kind != "checkpoint" or manifest_ready:
            raise ValueError("checkpoint release controls make a publication-ready claim")
        if promotion is not None:
            raise ValueError("checkpoint release manifest unexpectedly contains promotion state")
        if status_finalized not in {None, False}:
            raise ValueError("checkpoint release status is marked promotion-finalized")
        if status.get("programme_complete") is not False:
            raise ValueError("checkpoint release status is programme-complete")
        contract = manifest.get("promotion_contract")
        if (
            not isinstance(contract, dict)
            or contract.get("schema") != "afhf-govuk-okf-two-stage-promotion.v1"
            or contract.get("stage") != "full_corpus_checkpoint"
            or contract.get("target_release_kind") != "machine_release_candidate"
            or not valid_reproduction_contract(contract.get("reproduction"))
        ):
            raise ValueError("full-corpus checkpoint lacks its immutable promotion contract")
        release_state = "checkpoint"
        finalized = False
    elif release_kind == "machine_release_candidate":
        if snapshot.get("kind") != "full_corpus" or snapshot.get("sampled") is not False:
            raise ValueError("machine candidate does not have an exact unsampled snapshot")
        if status_kind != "machine_release_candidate" or not manifest_ready:
            raise ValueError("machine candidate controls are not publication-ready and consistent")
        if not isinstance(promotion, dict) or not isinstance(promotion.get("finalized"), bool):
            raise ValueError("machine candidate manifest lacks explicit promotion finalization state")
        finalized = promotion["finalized"]
        promotion = validate_promotion_record(promotion, finalized)
        if status_finalized is not finalized:
            raise ValueError("manifest and status promotion finalization states differ")
        require_release_completion_markers(status, full_programme=False)
        require_release_gates(manifest, status)
        expected_reason = MACHINE_FINAL_REASON if finalized else MACHINE_CANDIDATE_REASON
        if status.get("reason") != expected_reason:
            raise ValueError("machine candidate status reason does not match finalization state")
        if finalized:
            candidate_manifest = copy.deepcopy(manifest)
            candidate_promotion = candidate_manifest["promotion"]
            candidate_promotion.pop("candidate_manifest_sha256")
            candidate_promotion.pop("candidate_status_sha256")
            candidate_promotion["finalized"] = False
            candidate_status = copy.deepcopy(status)
            candidate_status["promotion_finalized"] = False
            candidate_status["reason"] = MACHINE_CANDIDATE_REASON
            if canonical_document_sha256(candidate_manifest) != promotion["candidate_manifest_sha256"]:
                raise ValueError("finalized promotion candidate manifest hash cannot be reconstructed")
            if canonical_document_sha256(candidate_status) != promotion["candidate_status_sha256"]:
                raise ValueError("finalized promotion candidate status hash cannot be reconstructed")
        release_state = "release" if finalized else "candidate"
    elif release_kind == "full_programme":
        if snapshot.get("kind") != "full_corpus" or snapshot.get("sampled") is not False:
            raise ValueError("full-programme release does not have an exact unsampled snapshot")
        if status_kind != "full_programme" or not manifest_ready:
            raise ValueError("full-programme controls are not publication-ready and consistent")
        validate_promotion_record(promotion, True)
        if status_finalized is not True:
            raise ValueError("full-programme status is not promotion-finalized")
        require_release_completion_markers(status, full_programme=True)
        require_release_gates(manifest, status)
        release_state = "release"
        finalized = True
    else:
        raise ValueError(f"unsupported release kind: {release_kind}")

    return {
        "release_state": release_state,
        "release_kind": release_kind,
        "release_id": release_id,
        "publication_ready": manifest_ready,
        "promotion_finalized": finalized,
        "human_evaluation_status": status.get("human_evaluation_status"),
    }


def human_blocked_tasks(contracts: dict[str, dict[str, Any]]) -> set[str]:
    """Return every directly human-gated task and its transitive dependants."""

    blocked = {
        task_id
        for task_id, contract in contracts.items()
        if contract.get("human_gate") is not None
    }
    changed = True
    while changed:
        changed = False
        for task_id, contract in contracts.items():
            dependencies = contract.get("dependencies", [])
            if task_id not in blocked and any(item in blocked for item in dependencies):
                blocked.add(task_id)
                changed = True
    return blocked


def human_blocked_requirements(contracts: dict[str, dict[str, Any]]) -> set[str]:
    """Return requirements owned by directly human-gated task contracts."""

    blocked: set[str] = set()
    for contract in contracts.values():
        if contract.get("human_gate") is not None:
            blocked.update(expand_requirement_ids(contract.get("requirement_ids", [])))
    return blocked


def load_activity_ledger(root: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    path = root / ACTIVITY_LEDGER_RELATIVE
    try:
        check_provenance.validate_ledger(
            path,
            root / ACTIVITY_SCHEMA_RELATIVE,
            root,
        )
        rows, _ = check_provenance.load_ledger(path)
    except (OSError, check_provenance.ProvenanceError) as exc:
        raise ValueError(f"accepted task evidence has an invalid activity ledger: {exc}") from exc
    return rows, {row["activity_id"]: row for row in rows}


def exact_terminal_output_errors(activity: dict[str, Any], root: Path) -> list[str]:
    errors: list[str] = []
    outputs = activity.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        return ["terminal activity has no output evidence"]
    for output in outputs:
        if not isinstance(output, dict) or output.get("state") == "pending":
            errors.append("terminal activity retains a pending or malformed output")
            continue
        path_value = output.get("path")
        expected = output.get("sha256")
        if not isinstance(path_value, str) or not valid_sha256(expected):
            errors.append("terminal activity output lacks an exact path and SHA-256")
            continue
        try:
            output_path = check_provenance._resolve_artifact_path(
                root, path_value, "accepted task terminal output"
            )
        except check_provenance.ProvenanceError as exc:
            errors.append(str(exc))
            continue
        if not output_path.is_file() or digest(output_path) != expected:
            errors.append(f"terminal activity output hash differs: {path_value}")
    return errors


def terminal_declarations(
    root: Path, release: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    declarations_path = root / DECLARATIONS_RELATIVE
    try:
        check_provenance.validate_declarations(declarations_path)
        declarations = load(declarations_path, root)
        request_summary = check_provenance.validate_request_snapshot(
            root / REQUEST_BUDGET_RELATIVE,
            None,
        )
    except (OSError, ValueError, check_provenance.ProvenanceError) as exc:
        raise ValueError(f"accepted task terminal declarations are invalid: {exc}") from exc
    if request_summary.get("status") != "final" or request_summary.get("snapshot_id") != release["release_id"]:
        raise ValueError("publishable status requires a final source-request budget for its release ID")
    base = declarations.get("final_activity_entries_required")
    if not isinstance(base, list):
        raise ValueError("terminal declarations have no final activity contract")
    selected = [
        row
        for row in base
        if release["release_state"] == "release"
        or row.get("terminal_activity_id") != POST_PUBLICATION_TERMINAL_ACTIVITY_ID
    ]
    if release["release_kind"] == "full_programme":
        supplemental = declarations.get("full_programme_activity_entries_required")
        if not isinstance(supplemental, list) or len(supplemental) != 1:
            raise ValueError("full-programme release has no declared human terminal contract")
        human_terminal = supplemental[0]
        if (
            not isinstance(human_terminal, dict)
            or human_terminal.get("must_bind_release_snapshot") is not True
            or not isinstance(human_terminal.get("event"), str)
            or not human_terminal["event"].strip()
            or not isinstance(human_terminal.get("required_evidence"), list)
            or not human_terminal["required_evidence"]
            or not all(isinstance(item, str) and item.strip() for item in human_terminal["required_evidence"])
        ):
            raise ValueError("full-programme human terminal contract is incomplete")
        selected.extend(supplemental)
    indexed: dict[str, dict[str, Any]] = {}
    for row in selected:
        if not isinstance(row, dict) or not TERMINAL_ACTIVITY_ID.fullmatch(
            str(row.get("terminal_activity_id") or "")
        ):
            raise ValueError("release terminal declaration has an invalid activity ID")
        activity_id = row["terminal_activity_id"]
        if activity_id in indexed:
            raise ValueError(f"duplicate selected terminal declaration: {activity_id}")
        indexed[activity_id] = row
    return indexed, request_summary, base


def validate_release_dispositions(
    release: dict[str, Any],
    requirement_status: dict[str, dict[str, Any]],
    task_status: dict[str, dict[str, Any]],
    contracts: dict[str, dict[str, Any]],
    root: Path,
) -> None:
    requirement_counts = Counter(group["status"] for group in requirement_status.values())
    task_counts = Counter(group["status"] for group in task_status.values())
    if release["release_state"] == "checkpoint":
        if requirement_counts["passed"] != 0 or task_counts["accepted"] != 0:
            raise ValueError("checkpoint status cannot claim passed requirements or accepted tasks")
        return

    if requirement_counts["produced"] or requirement_counts["in_progress"]:
        raise ValueError("publishable release requirements must have only passed or blocked dispositions")
    if release["release_kind"] == "full_programme":
        if requirement_counts["passed"] != len(requirement_status):
            raise ValueError("full-programme release must pass every requirement")
        expected_blocked_requirements: set[str] = set()
        expected_blocked_tasks: set[str] = set()
    else:
        expected_blocked_requirements = human_blocked_requirements(contracts)
        expected_blocked_tasks = human_blocked_tasks(contracts)

    actual_blocked_requirements = {
        requirement_id
        for requirement_id, group in requirement_status.items()
        if group["status"] == "blocked"
    }
    if actual_blocked_requirements != expected_blocked_requirements:
        raise ValueError(
            "release requirement blockers differ from the directly human-gated contract: "
            f"expected {sorted(expected_blocked_requirements)}, "
            f"got {sorted(actual_blocked_requirements)}"
        )
    expected_passed_requirements = set(requirement_status) - expected_blocked_requirements
    actual_passed_requirements = {
        requirement_id
        for requirement_id, group in requirement_status.items()
        if group["status"] == "passed"
    }
    if actual_passed_requirements != expected_passed_requirements:
        raise ValueError("release passed requirements do not cover every non-human-gated requirement")

    actual_blocked_tasks = {
        task_id for task_id, group in task_status.items() if group["status"] == "blocked"
    }
    actual_accepted_tasks = {
        task_id for task_id, group in task_status.items() if group["status"] == "accepted"
    }
    if actual_blocked_tasks != expected_blocked_tasks:
        raise ValueError(
            "release task blockers differ from the human-gated dependency closure: "
            f"expected {sorted(expected_blocked_tasks)}, got {sorted(actual_blocked_tasks)}"
        )
    expected_accepted_tasks = set(contracts) - expected_blocked_tasks
    if actual_accepted_tasks != expected_accepted_tasks:
        raise ValueError(
            "release accepted tasks do not cover every unblocked task: "
            f"expected {sorted(expected_accepted_tasks)}, got {sorted(actual_accepted_tasks)}"
        )
    if task_counts["produced"] or task_counts["in_progress"]:
        raise ValueError("publishable release tasks must have only accepted or blocked dispositions")

    rows, activities = load_activity_ledger(root)
    declarations, request_summary, base_declarations = terminal_declarations(root, release)
    base_report = check_provenance.terminal_event_satisfaction(
        rows,
        {"final_activity_entries_required": base_declarations},
        request_summary,
        release["release_id"],
        root,
    )
    base_events = {
        row["terminal_activity_id"]: row for row in base_report.get("events", [])
    }
    for activity_id in declarations:
        if activity_id in base_events and not base_events[activity_id].get("satisfied"):
            problems = "; ".join(base_events[activity_id].get("problems", []))
            raise ValueError(f"declared terminal activity is unsatisfied: {activity_id}: {problems}")
    unresolved = check_provenance.unresolved_activity_status(rows)
    if unresolved["unresolved_pending_final_activity_ids"] or unresolved[
        "unresolved_in_progress_activity_ids"
    ]:
        raise ValueError("publishable status retains unresolved provenance activities")

    referenced_terminal_ids: set[str] = set()
    for task_id in sorted(actual_accepted_tasks):
        terminal_ids = task_status[task_id].get("terminal_activity_ids")
        if (
            not isinstance(terminal_ids, list)
            or not terminal_ids
            or not all(isinstance(item, str) for item in terminal_ids)
            or len(set(terminal_ids)) != len(terminal_ids)
        ):
            raise ValueError(f"accepted task {task_id} has no unique terminal activity IDs")
        for activity_id in terminal_ids:
            if not TERMINAL_ACTIVITY_ID.fullmatch(activity_id):
                raise ValueError(f"accepted task {task_id} has invalid terminal activity ID: {activity_id}")
            activity = activities.get(activity_id)
            if activity is None:
                raise ValueError(f"accepted task {task_id} terminal activity is absent: {activity_id}")
            if activity_id not in declarations:
                raise ValueError(f"accepted task {task_id} cites an undeclared terminal: {activity_id}")
            referenced_terminal_ids.add(activity_id)

    expected_terminal_ids = set(declarations)
    if referenced_terminal_ids != expected_terminal_ids:
        raise ValueError(
            "accepted task terminal coverage differs from the selected release contract: "
            f"expected {sorted(expected_terminal_ids)}, got {sorted(referenced_terminal_ids)}"
        )
    for activity_id, declaration in declarations.items():
        activity = activities.get(activity_id)
        if activity is None:
            raise ValueError(f"declared terminal activity is absent: {activity_id}")
        if activity.get("status") != "completed":
            raise ValueError(f"declared terminal activity is not completed: {activity_id}")
        ended_at = activity.get("ended_at")
        try:
            if not isinstance(ended_at, str) or ended_at == "unavailable_to_session":
                raise ValueError
            datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"declared terminal activity has no exact completion time: {activity_id}") from exc
        validation = activity.get("validation")
        if (
            not isinstance(validation, dict)
            or validation.get("capture_status") != "complete"
            or not isinstance(validation.get("results"), list)
            or not validation["results"]
        ):
            raise ValueError(f"declared terminal activity has incomplete validation: {activity_id}")
        request_status = activity.get("source_request_usage", {}).get("status")
        if request_status not in {"exact", "not_applicable"}:
            raise ValueError(f"declared terminal activity has unresolved request usage: {activity_id}")
        output_errors = exact_terminal_output_errors(activity, root)
        if output_errors:
            raise ValueError(f"declared terminal activity output is invalid: {activity_id}: {output_errors[0]}")
        snapshots = activity.get("source_snapshots")
        if declaration.get("must_bind_release_snapshot") is True and (
            not isinstance(snapshots, list) or release["release_id"] not in snapshots
        ):
            raise ValueError(
                f"declared terminal activity is not bound to {release['release_id']}: {activity_id}"
            )

    if release["release_kind"] == "full_programme":
        supplemental = check_provenance.terminal_event_satisfaction(
            rows,
            {
                "final_activity_entries_required": list(
                    load(root / DECLARATIONS_RELATIVE, root).get(
                        "full_programme_activity_entries_required", []
                    )
                )
            },
            request_summary,
            release["release_id"],
            root,
        )
        if not supplemental.get("all_satisfied"):
            raise ValueError("full-programme human terminal contract is not satisfied")


def load_release_state(root: Path = ROOT) -> dict[str, Any]:
    return classify_release_state(
        load(root / RELEASE_MANIFEST_RELATIVE, root),
        load(root / RELEASE_STATUS_RELATIVE, root),
    )


def render(root: Path = ROOT) -> dict[Path, str]:
    root = root.resolve()
    source_path = root / SOURCE_RELATIVE
    requirements_path = root / REQUIREMENTS_RELATIVE
    traceability_path = root / TRACEABILITY_RELATIVE
    contracts_path = root / CONTRACTS_RELATIVE
    outputs = {name: root / relative for name, relative in OUTPUT_RELATIVES.items()}
    source = load(source_path, root)
    requirements = load(requirements_path, root)
    traceability = load(traceability_path, root)
    release = load_release_state(root)
    expected_milestone = MILESTONE_BY_STATE.get(
        (release["release_kind"], release["release_state"])
    )
    if source.get("milestone") != expected_milestone:
        raise ValueError(
            "implementation status milestone differs from the authoritative release state: "
            f"expected {expected_milestone}, got {source.get('milestone')}"
        )
    if source.get("publication_readiness_source") != PUBLICATION_READINESS_SOURCES:
        raise ValueError(
            "implementation status publication_readiness_source must name the exact release controls"
        )
    if "publication_ready" in source:
        raise ValueError("implementation status source must not duplicate publication readiness")
    required_vocabulary = REQUIREMENT_STATUSES | TASK_STATUSES
    if not required_vocabulary.issubset(source.get("status_vocabulary", {})):
        missing = sorted(required_vocabulary - set(source.get("status_vocabulary", {})))
        raise ValueError(f"implementation status vocabulary is incomplete: {missing}")
    requirement_rows = requirements["requirements"]
    requirement_ids = {row["id"] for row in requirement_rows}
    contracts = {path.stem: load(path, root) for path in sorted(contracts_path.glob("*.json"))}
    require_existing_evidence(source["requirement_status_groups"], root)
    requirement_status = status_by_id(
        source["requirement_status_groups"],
        "requirement_ids",
        requirement_ids,
        REQUIREMENT_STATUSES,
    )
    task_status = status_by_id(
        source["task_status_groups"], "task_ids", set(contracts), TASK_STATUSES
    )
    directly_human_requirements = human_blocked_requirements(contracts)
    if release["human_evaluation_status"] != "completed":
        if release["release_state"] != "checkpoint":
            for requirement_id in directly_human_requirements:
                if requirement_status[requirement_id]["status"] != "blocked":
                    raise ValueError(
                        f"{requirement_id} must remain blocked until human evaluation is completed"
                    )
        if requirement_status["REQ-077"]["status"] != "blocked":
            raise ValueError("REQ-077 must remain blocked until human evaluation is completed")
        if task_status["E3-01"]["status"] != "blocked":
            raise ValueError("E3-01 must remain blocked until human evaluation is completed")
    elif release["release_kind"] == "full_programme":
        for requirement_id in directly_human_requirements:
            if requirement_status[requirement_id]["status"] != "passed":
                raise ValueError(f"{requirement_id} must pass for a full-programme release")
        if task_status["E3-01"]["status"] != "accepted":
            raise ValueError("E3-01 must be accepted for a full-programme release")
    validate_release_dispositions(
        release,
        requirement_status,
        task_status,
        contracts,
        root,
    )

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
        "generated_from": SOURCE_RELATIVE.as_posix(),
        "generated_from_sha256": digest(source_path),
        "requirements_contract_sha256": digest(requirements_path),
        "as_of": source["as_of"],
        "milestone": source["milestone"],
        "release_state": release["release_state"],
        "release_kind": release["release_kind"],
        "release_id": release["release_id"],
        "publication_ready": release["publication_ready"],
        "interpretation": (
            "accepted is the contract status; implementation passed is reserved for "
            "independently verified evidence at the declared milestone, while blocked "
            "human-only evidence remains visible"
        ),
        "counts": {
            "requirements": len(projected_requirements),
            "by_implementation_status": dict(sorted(requirement_counts.items())),
            "passed": requirement_counts["passed"],
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
        "generated_from": SOURCE_RELATIVE.as_posix(),
        "generated_from_sha256": digest(source_path),
        "traceability_contract_sha256": digest(traceability_path),
        "as_of": source["as_of"],
        "milestone": source["milestone"],
        "release_state": release["release_state"],
        "release_kind": release["release_kind"],
        "release_id": release["release_id"],
        "publication_ready": release["publication_ready"],
        "interpretation": (
            "Clause status is the least advanced mapped requirement status; mapping "
            "coverage does not prove implementation acceptance."
        ),
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
                "terminal_activity_ids": group.get("terminal_activity_ids", []),
                "contract_outputs": contract["output_artifacts"],
                "requirement_ids": contract["requirement_ids"],
            }
        )
    task_counts = Counter(row["implementation_status"] for row in projected_tasks)
    task_document = {
        "schema": "afhf-govuk-okf-task-status.v1",
        "generated_from": SOURCE_RELATIVE.as_posix(),
        "generated_from_sha256": digest(source_path),
        "as_of": source["as_of"],
        "milestone": source["milestone"],
        "release_state": release["release_state"],
        "release_kind": release["release_kind"],
        "release_id": release["release_id"],
        "publication_ready": release["publication_ready"],
        "interpretation": (
            "A produced task has an artefact foundation only; accepted is reserved for a "
            "satisfied promotion gate with terminal evidence, and human-only tasks remain "
            "blocked without authority."
        ),
        "counts": {
            "tasks": len(projected_tasks),
            "by_implementation_status": dict(sorted(task_counts.items())),
            "accepted": task_counts["accepted"],
        },
        "tasks": projected_tasks,
    }
    documents = {
        outputs["requirements"]: requirement_document,
        outputs["traceability"]: trace_document,
        outputs["tasks"]: task_document,
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
        documents = render(ROOT)
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
    requirement_document = json.loads(documents[ROOT / OUTPUT_RELATIVES["requirements"]])
    task_document = json.loads(documents[ROOT / OUTPUT_RELATIVES["tasks"]])
    action = "validated" if args.check else "wrote"
    print(
        f"{action} 95 requirement, 21 clause and 36 task statuses; "
        f"state={requirement_document['release_state']}, "
        f"passed={requirement_document['counts']['passed']}, "
        f"accepted={task_document['counts']['accepted']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
