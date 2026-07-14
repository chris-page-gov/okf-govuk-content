#!/usr/bin/env python3
"""Validate and emit checkpoint, candidate or final release provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "provenance" / "activity-ledger.jsonl"
SCHEMA = ROOT / "provenance" / "activity-ledger.schema.json"
DECLARATIONS = ROOT / "provenance" / "reproduction-declarations.json"
REQUEST_SNAPSHOT = ROOT / "provenance" / "source-request-budget.json"
LIVE_REQUEST_LEDGER = ROOT / ".tmp" / "request-budget" / "official-sources.count"
LAUNCH = ROOT / "governance" / "launch-manifest.yaml"
MODEL_LOCK = ROOT / "orchestration" / "models.lock.yaml"
RELEASE_STATUS = ROOT / "release" / "status.json"
DEFAULT_OUTPUT = ROOT / "release" / "provenance-validation.json"
DISALLOWED_RELEASE_SNAPSHOT_MARKERS = ("fixture", "sample", "capacity", "development", "test")
REPORT_SCHEMA = "afhf-govuk-okf-provenance-validation.v1"
PUBLICATION_TERMINAL_ACTIVITY_ID = "ACT-F2-PUBLICATION-REGISTRY-TERMINAL-001"
PRE_RELEASE_CITATION_TERMINAL_ACTIVITY_ID = "ACT-F2-CITATION-REVIEWS-TERMINAL-001"
RELEASE_CITATION_TERMINAL_ACTIVITY_ID = (
    "ACT-F2-RELEASE-SNAPSHOT-CITATION-REVIEWS-TERMINAL-001"
)
PRE_RELEASE_SECURITY_TERMINAL_ACTIVITY_ID = "ACT-D2-SECURITY-SCAN-TERMINAL-001"
RELEASE_SECURITY_TERMINAL_ACTIVITY_ID = (
    "ACT-D2-RELEASE-SNAPSHOT-SECURITY-SCAN-TERMINAL-001"
)
REQUIRED_FINAL_TERMINAL_ACTIVITY_IDS = frozenset(
    {
        "ACT-B1-T0-20260712-TERMINAL-001",
        "ACT-D1-T0-HYDRATION-TERMINAL-001",
        "ACT-E1-T1-RECONCILIATION-TERMINAL-001",
        "ACT-C1-RELEASE-V2-TERMINAL-001",
        "ACT-E2-AUTOMATED-EVALUATION-TERMINAL-001",
        RELEASE_CITATION_TERMINAL_ACTIVITY_ID,
        "ACT-D1-SHARD-CONTRACT-AUDIT-TERMINAL-001",
        RELEASE_SECURITY_TERMINAL_ACTIVITY_ID,
        "ACT-F2-CLEAN-ROOM-RC-TERMINAL-001",
        PUBLICATION_TERMINAL_ACTIVITY_ID,
        "ACT-F2-SOURCE-REQUEST-BUDGET-TERMINAL-001",
    }
)
RELEASE_SNAPSHOT_BOUND_TERMINAL_ACTIVITY_IDS = frozenset(
    {
        "ACT-E1-T1-RECONCILIATION-TERMINAL-001",
        "ACT-C1-RELEASE-V2-TERMINAL-001",
        "ACT-E2-AUTOMATED-EVALUATION-TERMINAL-001",
        RELEASE_CITATION_TERMINAL_ACTIVITY_ID,
        "ACT-D1-SHARD-CONTRACT-AUDIT-TERMINAL-001",
        RELEASE_SECURITY_TERMINAL_ACTIVITY_ID,
        "ACT-F2-CLEAN-ROOM-RC-TERMINAL-001",
        PUBLICATION_TERMINAL_ACTIVITY_ID,
        "ACT-F2-SOURCE-REQUEST-BUDGET-TERMINAL-001",
    }
)
REQUIRED_OUTPUT_PATHS_BY_TERMINAL = {
    RELEASE_CITATION_TERMINAL_ACTIVITY_ID: frozenset(
        {
            "release/citation-verification.json",
            "reports/citation-verification.md",
            "provenance/citation-request-aggregate.json",
        }
    ),
    RELEASE_SECURITY_TERMINAL_ACTIVITY_ID: frozenset(
        {"release/security-scan.json", "reports/security.md"}
    ),
}

LEGACY_REQUIRED = {
    "activity_id",
    "exact_model_version",
    "tokens",
    "cost_gbp",
    "external_paid_model_api_calls",
}
REQUIRED_FALLBACKS = {
    "FALLBACK-ACM-RRF-001",
    "FALLBACK-CITATION-PIROLLI-001",
    "FALLBACK-TNA-OGL-001",
    "FALLBACK-OPENAI-BROWSECOMP-001",
}
REQUIRED_RESTRICTIONS = {
    "SRC-CONSTRAINT-005",
    "SRC-CONSTRAINT-006",
    "SRC-CONSTRAINT-007",
    "SRC-CONSTRAINT-008",
    "SRC-CONSTRAINT-009",
}


class ProvenanceError(ValueError):
    """Raised when provenance evidence is invalid."""


def _sha256_line(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProvenanceError(f"{label} must be an object: {path}")
    return value


def load_ledger(path: Path = LEDGER) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    raw_lines: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            raise ProvenanceError(f"activity ledger has a blank row at line {number}")
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProvenanceError(f"invalid activity ledger row {number}: {exc}") from exc
        if not isinstance(row, dict):
            raise ProvenanceError(f"activity ledger row {number} is not an object")
        rows.append(row)
        raw_lines.append(raw)
    if not rows:
        raise ProvenanceError("activity ledger is empty")
    return rows, raw_lines


def _resolve_artifact_path(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ProvenanceError(f"{label} must be a non-empty repository-relative path")
    reference = PurePosixPath(value)
    if reference.is_absolute() or ".." in reference.parts or reference.as_posix() != value:
        raise ProvenanceError(f"{label} is not a safe canonical repository-relative path: {value}")
    lexical_root = root.absolute()
    if lexical_root.is_symlink():
        raise ProvenanceError(f"{label} artifact root cannot be a symbolic link")
    resolved_root = lexical_root.resolve()
    lexical_candidate = lexical_root
    for part in reference.parts:
        lexical_candidate /= part
        if lexical_candidate.is_symlink():
            raise ProvenanceError(
                f"{label} contains a symbolic-link component: {value}"
            )
    candidate = lexical_candidate.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ProvenanceError(f"{label} escapes the repository root: {value}") from exc
    return candidate


def validate_ledger(
    path: Path = LEDGER,
    schema_path: Path = SCHEMA,
    artifact_root: Path = ROOT,
) -> dict[str, Any]:
    rows, raw_lines = load_ledger(path)
    schema = _read_json(schema_path, "activity schema")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors: list[str] = []
    ids: set[str] = set()
    v2_started = False
    model_assisted = 0
    deterministic = 0
    unavailable_tokens = 0
    unavailable_cost = 0
    external_calls = 0
    external_input_tokens = 0
    external_output_tokens = 0
    external_cost_gbp = 0.0
    product_known_input_tokens = 0
    product_known_output_tokens = 0
    product_known_cost_gbp = 0.0
    product_unavailable_input = 0
    product_unavailable_output = 0
    product_unavailable_cost = 0
    exact_model_versions: set[str] = set()
    unavailable_exact_model_versions = 0
    superseded_by: dict[str, str] = {}

    for index, (row, raw) in enumerate(zip(rows, raw_lines, strict=True)):
        number = index + 1
        activity_id = row.get("activity_id")
        if not isinstance(activity_id, str) or not activity_id:
            errors.append(f"row {number} has no activity_id")
        elif activity_id in ids:
            errors.append(f"row {number} duplicates activity_id {activity_id}")
        else:
            ids.add(activity_id)

        is_v2 = row.get("ledger_schema_version") == "2.0"
        if not is_v2:
            if v2_started:
                errors.append(f"legacy row {number} occurs after the v2 hash chain started")
            if not LEGACY_REQUIRED <= set(row):
                errors.append(f"legacy row {number} lacks required usage fields")
            calls = row.get("external_paid_model_api_calls")
            if not isinstance(calls, int) or isinstance(calls, bool) or calls < 0:
                errors.append(f"legacy row {number} has invalid external paid call count")
            else:
                external_calls += calls
            if not isinstance(row.get("tokens"), int):
                unavailable_tokens += 1
            if not isinstance(row.get("cost_gbp"), (int, float)) or isinstance(row.get("cost_gbp"), bool):
                unavailable_cost += 1
            if row.get("model_family") is None:
                deterministic += 1
            else:
                model_assisted += 1
                version = row.get("exact_model_version")
                if isinstance(version, str) and version not in {
                    "unavailable",
                    "unavailable_to_session",
                    "unavailable_in_product_session",
                }:
                    exact_model_versions.add(version)
                else:
                    unavailable_exact_model_versions += 1
                product_unavailable_input += 1
                product_unavailable_output += 1
                if isinstance(row.get("cost_gbp"), (int, float)) and not isinstance(row.get("cost_gbp"), bool):
                    product_known_cost_gbp += float(row["cost_gbp"])
                else:
                    product_unavailable_cost += 1
            continue

        v2_started = True
        for failure in sorted(validator.iter_errors(row), key=lambda item: list(item.absolute_path)):
            location = "/".join(str(part) for part in failure.absolute_path) or "<root>"
            errors.append(f"row {number} {location}: {failure.message}")

        expected_previous = _sha256_line(raw_lines[index - 1]) if index else None
        if row.get("previous_entry_sha256") != expected_previous:
            errors.append(f"row {number} does not hash-chain to the exact previous JSONL row")

        work_class = row.get("work_class")
        if work_class == "deterministic":
            deterministic += 1
            if row.get("model") is not None:
                errors.append(f"row {number} deterministic work has a model object")
            if row.get("tokens") != 0 or row.get("cost_gbp") != 0:
                errors.append(f"row {number} deterministic work must record exact zero model tokens and cost")
            if row.get("prompt", {}).get("capture_status") != "not_applicable":
                errors.append(f"row {number} deterministic work must mark prompt not_applicable")
        elif work_class in {"model_assisted", "mixed"}:
            model_assisted += 1
            model = row.get("model")
            if model is None:
                errors.append(f"row {number} model-assisted work has no model object")
            elif isinstance(model, dict):
                version = model.get("exact_version")
                if isinstance(version, str) and version not in {
                    "unavailable",
                    "unavailable_to_session",
                    "unavailable_in_product_session",
                }:
                    exact_model_versions.add(version)
                else:
                    unavailable_exact_model_versions += 1
            if not isinstance(row.get("tokens"), int):
                unavailable_tokens += 1
            if not isinstance(row.get("cost_gbp"), (int, float)) or isinstance(row.get("cost_gbp"), bool):
                unavailable_cost += 1
            if row.get("prompt", {}).get("capture_status") == "not_applicable":
                errors.append(f"row {number} model-assisted work has no bounded prompt record")

        usage = row.get("usage", {})
        paid = usage.get("external_paid_model", {})
        calls = paid.get("api_calls")
        if isinstance(calls, int) and not isinstance(calls, bool):
            external_calls += calls
        for field, target in (
            ("input_tokens", "input"),
            ("output_tokens", "output"),
            ("cost_gbp", "cost"),
        ):
            value = paid.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if target == "input":
                    external_input_tokens += int(value)
                elif target == "output":
                    external_output_tokens += int(value)
                else:
                    external_cost_gbp += float(value)
        if row.get("external_paid_model_api_calls") != calls:
            errors.append(f"row {number} compatibility external call count disagrees with structured usage")
        if paid and any(paid.get(field) != 0 for field in ("api_calls", "input_tokens", "output_tokens", "cost_gbp")):
            errors.append(f"row {number} records paid-model use despite the zero paid-model authority")

        request_usage = row.get("source_request_usage", {})
        request_status = request_usage.get("status")
        attempts = request_usage.get("attempts")
        if request_usage.get("included_in_model_cost") is not False:
            errors.append(f"row {number} conflates source requests with model cost")
        if request_status in {"exact", "checkpoint"} and not isinstance(attempts, int):
            errors.append(f"row {number} {request_status} source requests need an integer attempt count")
        if request_status == "not_applicable" and attempts != "not_applicable":
            errors.append(f"row {number} not-applicable source requests use an inconsistent sentinel")

        if work_class in {"model_assisted", "mixed"}:
            product = usage.get("product_session", {})
            for field, target in (
                ("input_tokens", "input"),
                ("output_tokens", "output"),
                ("marginal_cost_gbp", "cost"),
            ):
                value = product.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if target == "input":
                        product_known_input_tokens += int(value)
                    elif target == "output":
                        product_known_output_tokens += int(value)
                    else:
                        product_known_cost_gbp += float(value)
                elif target == "input":
                    product_unavailable_input += 1
                elif target == "output":
                    product_unavailable_output += 1
                else:
                    product_unavailable_cost += 1

        supersedes = row.get("supersedes_activity_ids", [])
        if supersedes:
            if row.get("status") != "completed":
                errors.append(f"row {number} supersedes prior activities without being completed")
            for prior_id in supersedes:
                if prior_id == activity_id:
                    errors.append(f"row {number} cannot supersede itself")
                elif prior_id not in ids:
                    errors.append(f"row {number} supersedes an unknown or later activity: {prior_id}")
                elif prior_id in superseded_by:
                    errors.append(
                        f"row {number} supersedes {prior_id}, already superseded by {superseded_by[prior_id]}"
                    )
                else:
                    superseded_by[prior_id] = str(activity_id)

        for output in row.get("outputs", []):
            path_value = output.get("path")
            try:
                output_path = _resolve_artifact_path(
                    artifact_root, path_value, f"row {number} output path"
                )
            except ProvenanceError as exc:
                errors.append(str(exc))
                continue
            if output.get("state") != "pending":
                if not output_path.exists():
                    errors.append(f"row {number} output does not exist: {path_value}")
                elif output.get("sha256") is not None and not output_path.is_file():
                    errors.append(
                        f"row {number} hash-bound output is not a regular file: {path_value}"
                    )

        forbidden = {"chain_of_thought", "private_reasoning", "reasoning_trace"}
        if forbidden.intersection(row):
            errors.append(f"row {number} contains a prohibited private-reasoning field")

    if errors:
        raise ProvenanceError("\n".join(errors))
    return {
        "activities": len(rows),
        "legacy_rows": sum(row.get("ledger_schema_version") != "2.0" for row in rows),
        "hash_chained_v2_rows": sum(row.get("ledger_schema_version") == "2.0" for row in rows),
        "deterministic_activities": deterministic,
        "model_assisted_activities": model_assisted,
        "activities_with_unavailable_tokens": unavailable_tokens,
        "activities_with_unavailable_cost": unavailable_cost,
        "external_paid_model_api_calls": external_calls,
        "external_paid_model_input_tokens": external_input_tokens,
        "external_paid_model_output_tokens": external_output_tokens,
        "external_paid_model_cost_gbp": external_cost_gbp,
        "product_session_known_input_tokens": product_known_input_tokens,
        "product_session_known_output_tokens": product_known_output_tokens,
        "product_session_known_marginal_cost_gbp": product_known_cost_gbp,
        "product_session_activities_with_unavailable_input_tokens": product_unavailable_input,
        "product_session_activities_with_unavailable_output_tokens": product_unavailable_output,
        "product_session_activities_with_unavailable_cost": product_unavailable_cost,
        "exact_model_versions": sorted(exact_model_versions),
        "activities_with_unavailable_exact_model_version": unavailable_exact_model_versions,
        "superseded_activity_ids": sorted(superseded_by),
        "ledger_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "last_entry_sha256": _sha256_line(raw_lines[-1]),
    }


def validate_declarations(path: Path = DECLARATIONS) -> dict[str, Any]:
    document = _read_json(path, "reproduction declarations")
    fallbacks = document.get("fallbacks")
    if not isinstance(fallbacks, list):
        raise ProvenanceError("reproduction declarations fallbacks must be a list")
    fallback_ids = {row.get("id") for row in fallbacks if isinstance(row, dict)}
    missing_fallbacks = sorted(REQUIRED_FALLBACKS - fallback_ids)
    if missing_fallbacks:
        raise ProvenanceError(f"reproduction declarations omit fallbacks: {', '.join(missing_fallbacks)}")
    restrictions = document.get("source_access_restrictions")
    if not isinstance(restrictions, list):
        raise ProvenanceError("reproduction declarations source restrictions must be a list")
    restriction_ids = {row.get("constraint_id") for row in restrictions if isinstance(row, dict)}
    missing_restrictions = sorted(REQUIRED_RESTRICTIONS - restriction_ids)
    if missing_restrictions:
        raise ProvenanceError(f"reproduction declarations omit restrictions: {', '.join(missing_restrictions)}")
    model = document.get("model_and_cost", {})
    for field in ("external_paid_model_api_calls_for_reproduction", "external_paid_model_cost_gbp_for_reproduction"):
        if model.get(field) != 0:
            raise ProvenanceError(f"{field} must remain exact zero")
    final_entries = document.get("final_activity_entries_required")
    if not isinstance(final_entries, list) or len(final_entries) < 4:
        raise ProvenanceError("reproduction declarations omit terminal activity-entry requirements")
    final_events = " ".join(str(row.get("event", "")) for row in final_entries if isinstance(row, dict)).lower()
    for marker in ("t0", "t1", "evaluation", "security", "publication"):
        if marker not in final_events:
            raise ProvenanceError(f"terminal activity-entry requirements omit {marker}")
    terminal_ids = [row.get("terminal_activity_id") for row in final_entries if isinstance(row, dict)]
    if len(terminal_ids) != len(final_entries) or any(
        not isinstance(value, str) or not re.fullmatch(r"ACT-[A-Z0-9][A-Z0-9-]*", value)
        for value in terminal_ids
    ):
        raise ProvenanceError("every terminal activity requirement needs a valid terminal_activity_id")
    if len(set(terminal_ids)) != len(terminal_ids):
        raise ProvenanceError("terminal activity requirements contain duplicate terminal_activity_id values")
    observed_terminal_ids = frozenset(terminal_ids)
    if observed_terminal_ids != REQUIRED_FINAL_TERMINAL_ACTIVITY_IDS:
        missing = sorted(REQUIRED_FINAL_TERMINAL_ACTIVITY_IDS - observed_terminal_ids)
        unexpected = sorted(observed_terminal_ids - REQUIRED_FINAL_TERMINAL_ACTIVITY_IDS)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise ProvenanceError(
            "final terminal activity declaration set differs from the audited 11-event contract: "
            + "; ".join(details)
        )
    if any(not isinstance(row.get("must_bind_release_snapshot"), bool) for row in final_entries):
        raise ProvenanceError(
            "every final terminal activity requirement must explicitly declare "
            "must_bind_release_snapshot"
        )
    snapshot_bound_ids = frozenset(
        row["terminal_activity_id"]
        for row in final_entries
        if row["must_bind_release_snapshot"] is True
    )
    if snapshot_bound_ids != RELEASE_SNAPSHOT_BOUND_TERMINAL_ACTIVITY_IDS:
        raise ProvenanceError(
            "release-snapshot terminal binding set differs from the audited final-snapshot contract"
        )
    for row in final_entries:
        terminal_id = row["terminal_activity_id"]
        required_outputs = row.get("required_output_paths", [])
        if not isinstance(required_outputs, list) or any(
            not isinstance(value, str) for value in required_outputs
        ):
            raise ProvenanceError(f"{terminal_id} required_output_paths must be a string list")
        if len(set(required_outputs)) != len(required_outputs):
            raise ProvenanceError(f"{terminal_id} required_output_paths contains duplicates")
        for value in required_outputs:
            reference = PurePosixPath(value)
            if (
                not value
                or reference.is_absolute()
                or ".." in reference.parts
                or reference.as_posix() != value
            ):
                raise ProvenanceError(
                    f"{terminal_id} has an unsafe required output path: {value}"
                )
        expected_outputs = REQUIRED_OUTPUT_PATHS_BY_TERMINAL.get(terminal_id, frozenset())
        if frozenset(required_outputs) != expected_outputs:
            raise ProvenanceError(
                f"{terminal_id} required output binding differs from the audited contract"
            )
    citation_requirements = [
        row
        for row in final_entries
        if isinstance(row, dict)
        and row.get("terminal_activity_id") == RELEASE_CITATION_TERMINAL_ACTIVITY_ID
    ]
    if len(citation_requirements) != 1 or PRE_RELEASE_CITATION_TERMINAL_ACTIVITY_ID not in _required_supersedes(
        citation_requirements[0].get("must_supersede") if citation_requirements else None
    ):
        raise ProvenanceError(
            "release provenance must require one distinct release-snapshot citation terminal "
            "that supersedes the pre-release citation terminal"
        )
    security_requirements = [
        row
        for row in final_entries
        if isinstance(row, dict)
        and row.get("terminal_activity_id") == RELEASE_SECURITY_TERMINAL_ACTIVITY_ID
    ]
    if len(security_requirements) != 1 or PRE_RELEASE_SECURITY_TERMINAL_ACTIVITY_ID not in _required_supersedes(
        security_requirements[0].get("must_supersede") if security_requirements else None
    ):
        raise ProvenanceError(
            "release provenance must require one distinct release-snapshot security terminal "
            "that supersedes the pre-release security terminal"
        )
    return {
        "fallbacks": len(fallbacks),
        "source_access_restrictions": len(restrictions),
        "final_activity_entries_required": len(final_entries),
        "release_snapshot_bound_terminal_entries": len(snapshot_bound_ids),
        "output_bound_terminal_entries": len(REQUIRED_OUTPUT_PATHS_BY_TERMINAL),
    }


def validate_request_snapshot(
    path: Path = REQUEST_SNAPSHOT,
    live_request_ledger: Path | None = LIVE_REQUEST_LEDGER,
) -> dict[str, Any]:
    document = _read_json(path, "source request budget snapshot")
    required = {
        "schema",
        "recorded_at",
        "snapshot_id",
        "status",
        "authorised_ceiling",
        "consumed_attempts_at_observation",
        "remaining_attempts_at_observation",
        "preflight_attempts",
        "included_in_model_cost",
        "final_entries_required",
    }
    if not required <= set(document):
        raise ProvenanceError("source request budget snapshot lacks required fields")
    ceiling = document["authorised_ceiling"]
    consumed = document["consumed_attempts_at_observation"]
    remaining = document["remaining_attempts_at_observation"]
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (ceiling, consumed, remaining)):
        raise ProvenanceError("source request budget counts must be integers")
    if ceiling < 0 or consumed < 0 or consumed > ceiling or remaining < 0 or consumed + remaining != ceiling:
        raise ProvenanceError("source request budget arithmetic does not reconcile")
    snapshot_id = document["snapshot_id"]
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ProvenanceError("source request budget snapshot_id must be a non-empty string")
    if document["included_in_model_cost"] is not False:
        raise ProvenanceError("source request budget is incorrectly included in model cost")
    if document["status"] not in {"open_checkpoint", "final"}:
        raise ProvenanceError("source request budget status is invalid")
    live: int | None = None
    if live_request_ledger is not None and live_request_ledger.is_file():
        live = int(live_request_ledger.read_text(encoding="utf-8").strip() or "0")
        if live < consumed:
            raise ProvenanceError("live source request ledger moved below the committed checkpoint")
        if document["status"] == "final" and live != consumed:
            raise ProvenanceError("final source request snapshot does not equal the live ledger")
    return {
        "snapshot_id": snapshot_id,
        "authorised_ceiling": ceiling,
        "checkpoint_consumed": consumed,
        "consumed_attempts": consumed,
        "remaining_attempts": remaining,
        "status": document["status"],
        "final": document["status"] == "final",
        "live_consumed_attempts": live,
    }


def validate_lock_and_authority(
    launch_path: Path = LAUNCH,
    model_lock_path: Path = MODEL_LOCK,
) -> None:
    launch = launch_path.read_text(encoding="utf-8")
    model_lock = model_lock_path.read_text(encoding="utf-8")
    if not re.search(r"^\s*external_paid_model_cost_gbp:\s*0\s*$", launch, re.MULTILINE):
        raise ProvenanceError("launch manifest does not retain the zero paid-model cost ceiling")
    if not re.search(r"^\s*external_paid_model_tokens:\s*0\s*$", launch, re.MULTILINE):
        raise ProvenanceError("launch manifest does not retain the zero paid-model token ceiling")
    if "product_managed_not_exposed" not in launch:
        raise ProvenanceError("launch manifest does not distinguish the product-managed usage ceiling")
    if "external_model_api_calls: false" not in model_lock:
        raise ProvenanceError("model lock does not forbid external model API calls")
    for marker in ("exact_backend_version: unavailable_to_session", "parameters: unavailable_to_session"):
        if marker not in model_lock:
            raise ProvenanceError(f"model lock is missing honest unavailable marker: {marker}")


def validate_all(
    *,
    ledger_path: Path = LEDGER,
    schema_path: Path = SCHEMA,
    declarations_path: Path = DECLARATIONS,
    request_snapshot_path: Path = REQUEST_SNAPSHOT,
    live_request_ledger: Path | None = LIVE_REQUEST_LEDGER,
    launch_path: Path = LAUNCH,
    model_lock_path: Path = MODEL_LOCK,
    artifact_root: Path = ROOT,
) -> dict[str, Any]:
    validate_lock_and_authority(launch_path, model_lock_path)
    return {
        "ledger": validate_ledger(ledger_path, schema_path, artifact_root),
        "declarations": validate_declarations(declarations_path),
        "source_request_budget": validate_request_snapshot(request_snapshot_path, live_request_ledger),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _default_snapshot(status_path: Path = RELEASE_STATUS) -> str:
    status = _read_json(status_path, "release status")
    value = status.get("release_id")
    if not isinstance(value, str) or not value.strip():
        raise ProvenanceError("release status has no non-empty release_id")
    return value


def _release_snapshot_allowed(snapshot: str) -> bool:
    normalised = snapshot.strip().casefold()
    return bool(normalised) and not any(marker in normalised for marker in DISALLOWED_RELEASE_SNAPSHOT_MARKERS)


def _required_supersedes(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return []


def terminal_event_satisfaction(
    rows: list[dict[str, Any]],
    declarations: dict[str, Any],
    request_summary: dict[str, Any],
    snapshot: str,
    artifact_root: Path = ROOT,
) -> dict[str, Any]:
    by_id = {row.get("activity_id"): row for row in rows if isinstance(row.get("activity_id"), str)}
    required = declarations.get("final_activity_entries_required", [])
    events: list[dict[str, Any]] = []
    request_events = {
        "T0 census terminal disposition",
        "T0 hydration terminal disposition",
        "T1 census and closing reconciliation",
        "final release-snapshot citation independent semantic and joint-support reviews",
        "final source-request budget snapshot",
    }
    final_request_event = "final source-request budget snapshot"

    for declaration in required:
        event = declaration.get("event")
        terminal_id = declaration.get("terminal_activity_id")
        activity = by_id.get(terminal_id)
        problems: list[str] = []
        if activity is None:
            problems.append("terminal activity is missing")
        else:
            if activity.get("status") != "completed":
                problems.append("terminal activity is not completed")
            ended_at = activity.get("ended_at")
            if not isinstance(ended_at, str) or ended_at == "unavailable_to_session":
                problems.append("terminal activity has no exact completion timestamp")
            if activity.get("validation", {}).get("capture_status") != "complete":
                problems.append("terminal activity validation is not complete")
            if any(output.get("state") == "pending" for output in activity.get("outputs", [])):
                problems.append("terminal activity still has pending outputs")
            source_snapshots = activity.get("source_snapshots", [])
            if declaration.get("must_bind_release_snapshot") is True:
                if snapshot not in source_snapshots:
                    problems.append(
                        f"terminal activity is not bound to the exact release snapshot: {snapshot}"
                    )
                stale_snapshots = sorted(
                    value
                    for value in source_snapshots
                    if isinstance(value, str) and not _release_snapshot_allowed(value)
                )
                if stale_snapshots:
                    problems.append(
                        "terminal activity includes non-release fixture/sample source snapshots: "
                        + ", ".join(stale_snapshots)
                    )
            outputs_by_path: dict[str, list[dict[str, Any]]] = {}
            for output in activity.get("outputs", []):
                path_value = output.get("path")
                if isinstance(path_value, str):
                    outputs_by_path.setdefault(path_value, []).append(output)
            for required_path in declaration.get("required_output_paths", []):
                matches = outputs_by_path.get(required_path, [])
                if len(matches) != 1:
                    problems.append(
                        f"terminal activity does not bind exactly one required output: {required_path}"
                    )
                    continue
                output = matches[0]
                if output.get("state") == "pending":
                    problems.append(f"required terminal output remains pending: {required_path}")
                    continue
                expected_sha256 = output.get("sha256")
                if not isinstance(expected_sha256, str) or not re.fullmatch(
                    r"[a-f0-9]{64}", expected_sha256
                ):
                    problems.append(
                        f"required terminal output has no exact SHA-256 binding: {required_path}"
                    )
                    continue
                try:
                    output_path = _resolve_artifact_path(
                        artifact_root, required_path, "required terminal output path"
                    )
                except ProvenanceError as exc:
                    problems.append(str(exc))
                    continue
                if not output_path.is_file():
                    problems.append(f"required terminal output does not exist: {required_path}")
                elif _file_sha256(output_path) != expected_sha256:
                    problems.append(f"required terminal output hash differs: {required_path}")
            request_usage = activity.get("source_request_usage", {})
            if request_usage.get("status") == "pending_final":
                problems.append("terminal activity retains pending_final source-request usage")
            if event in request_events and request_usage.get("status") != "exact":
                problems.append("terminal activity does not record an exact source-request count")
            expected_supersedes = _required_supersedes(declaration.get("must_supersede"))
            actual_supersedes = set(activity.get("supersedes_activity_ids", []))
            missing_supersedes = sorted(set(expected_supersedes) - actual_supersedes)
            if missing_supersedes:
                problems.append(f"terminal activity does not supersede: {', '.join(missing_supersedes)}")
            if event == final_request_event and request_usage.get("attempts") != request_summary.get(
                "consumed_attempts"
            ):
                problems.append("terminal activity request count differs from the final shared counter")
        events.append(
            {
                "event": event,
                "terminal_activity_id": terminal_id,
                "found": activity is not None,
                "satisfied": not problems,
                "problems": problems,
                "required_evidence": declaration.get("required_evidence", []),
                "must_supersede": _required_supersedes(declaration.get("must_supersede")),
                "must_bind_release_snapshot": declaration.get("must_bind_release_snapshot"),
                "required_output_paths": declaration.get("required_output_paths", []),
            }
        )

    satisfied = sum(row["satisfied"] for row in events)
    return {
        "required": len(events),
        "satisfied": satisfied,
        "all_satisfied": bool(events) and satisfied == len(events),
        "events": events,
    }


def unresolved_activity_status(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    superseded = {
        prior_id
        for row in rows
        if row.get("status") == "completed"
        for prior_id in row.get("supersedes_activity_ids", [])
    }
    pending_final = sorted(
        str(row.get("activity_id"))
        for row in rows
        if row.get("ledger_schema_version") == "2.0"
        and row.get("source_request_usage", {}).get("status") == "pending_final"
        and row.get("activity_id") not in superseded
    )
    open_activities = sorted(
        str(row.get("activity_id"))
        for row in rows
        if row.get("ledger_schema_version") == "2.0"
        and row.get("status") == "in_progress"
        and row.get("activity_id") not in superseded
    )
    return {
        "unresolved_pending_final_activity_ids": pending_final,
        "unresolved_in_progress_activity_ids": open_activities,
        "superseded_activity_ids": sorted(superseded),
    }


def build_validation_document(
    *,
    snapshot: str,
    require_candidate: bool = False,
    require_release: bool = False,
    ledger_path: Path = LEDGER,
    schema_path: Path = SCHEMA,
    declarations_path: Path = DECLARATIONS,
    request_snapshot_path: Path = REQUEST_SNAPSHOT,
    live_request_ledger: Path | None = LIVE_REQUEST_LEDGER,
    launch_path: Path = LAUNCH,
    model_lock_path: Path = MODEL_LOCK,
    artifact_root: Path = ROOT,
) -> dict[str, Any]:
    if not isinstance(snapshot, str) or not snapshot.strip():
        raise ProvenanceError("snapshot must be a non-empty string")
    if require_candidate and require_release:
        raise ProvenanceError("require_candidate and require_release are mutually exclusive")

    structural_errors: list[str] = []
    ledger_summary: dict[str, Any] = {}
    declaration_summary: dict[str, Any] = {}
    request_summary: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    declarations: dict[str, Any] = {}

    try:
        validate_lock_and_authority(launch_path, model_lock_path)
    except (OSError, ProvenanceError) as exc:
        structural_errors.extend(str(exc).splitlines())
    try:
        ledger_summary = validate_ledger(ledger_path, schema_path, artifact_root)
        rows, _ = load_ledger(ledger_path)
    except (OSError, ProvenanceError) as exc:
        structural_errors.extend(str(exc).splitlines())
    try:
        declaration_summary = validate_declarations(declarations_path)
        declarations = _read_json(declarations_path, "reproduction declarations")
    except (OSError, ProvenanceError) as exc:
        structural_errors.extend(str(exc).splitlines())
    try:
        request_summary = validate_request_snapshot(request_snapshot_path, live_request_ledger)
    except (OSError, ProvenanceError, ValueError) as exc:
        structural_errors.extend(str(exc).splitlines())

    if rows and declarations and request_summary:
        terminals = terminal_event_satisfaction(
            rows, declarations, request_summary, snapshot, artifact_root
        )
        unresolved = unresolved_activity_status(rows)
    else:
        terminals = {"required": 0, "satisfied": 0, "all_satisfied": False, "events": []}
        unresolved = {
            "unresolved_pending_final_activity_ids": [],
            "unresolved_in_progress_activity_ids": [],
            "superseded_activity_ids": [],
        }

    common_promotion_blockers: list[str] = []
    if not _release_snapshot_allowed(snapshot):
        common_promotion_blockers.append(f"non-release snapshot label: {snapshot}")
    if request_summary:
        if request_summary.get("status") != "final":
            common_promotion_blockers.append("source request budget remains an open checkpoint")
        if request_summary.get("consumed_attempts", 0) > request_summary.get("authorised_ceiling", -1):
            common_promotion_blockers.append("source request count exceeds the authorised ceiling")
        if request_summary.get("snapshot_id") != snapshot:
            common_promotion_blockers.append(
                "source request budget snapshot does not match the requested release snapshot"
            )
    else:
        common_promotion_blockers.append("final source request ceiling and count are unavailable")
    if unresolved["unresolved_pending_final_activity_ids"]:
        common_promotion_blockers.append(
            "unresolved pending_final activities: "
            + ", ".join(unresolved["unresolved_pending_final_activity_ids"])
        )
    if unresolved["unresolved_in_progress_activity_ids"]:
        common_promotion_blockers.append(
            "unresolved in-progress activities: "
            + ", ".join(unresolved["unresolved_in_progress_activity_ids"])
        )
    release_unsatisfied = [row for row in terminals["events"] if not row["satisfied"]]
    candidate_events = [
        row
        for row in terminals["events"]
        if row["terminal_activity_id"] != PUBLICATION_TERMINAL_ACTIVITY_ID
    ]
    candidate_unsatisfied = [row for row in candidate_events if not row["satisfied"]]
    candidate_terminals = {
        "required": len(candidate_events),
        "satisfied": len(candidate_events) - len(candidate_unsatisfied),
        "all_satisfied": bool(candidate_events) and not candidate_unsatisfied,
        "excluded_post_publication_terminal_activity_id": PUBLICATION_TERMINAL_ACTIVITY_ID,
    }
    terminals["candidate_required"] = candidate_terminals["required"]
    terminals["candidate_satisfied"] = candidate_terminals["satisfied"]
    terminals["candidate_all_satisfied"] = candidate_terminals["all_satisfied"]
    terminals["pending_post_publication_terminal_activity_id"] = (
        None
        if not release_unsatisfied
        else PUBLICATION_TERMINAL_ACTIVITY_ID
        if len(release_unsatisfied) == 1
        and release_unsatisfied[0]["terminal_activity_id"] == PUBLICATION_TERMINAL_ACTIVITY_ID
        else None
    )
    candidate_blockers = list(common_promotion_blockers)
    if candidate_unsatisfied:
        candidate_blockers.append(
            "candidate terminal activities are unsatisfied: "
            + ", ".join(row["terminal_activity_id"] for row in candidate_unsatisfied)
        )
    release_blockers = list(common_promotion_blockers)
    if release_unsatisfied:
        release_blockers.append(
            "required terminal activities are unsatisfied: "
            + ", ".join(row["terminal_activity_id"] for row in release_unsatisfied)
        )

    external_usage = {
        "api_calls": ledger_summary.get("external_paid_model_api_calls"),
        "input_tokens": ledger_summary.get("external_paid_model_input_tokens"),
        "output_tokens": ledger_summary.get("external_paid_model_output_tokens"),
        "cost_gbp": ledger_summary.get("external_paid_model_cost_gbp"),
        "totals_complete": bool(ledger_summary),
    }
    model_usage = {
        "deterministic_activities": ledger_summary.get("deterministic_activities"),
        "model_assisted_activities": ledger_summary.get("model_assisted_activities"),
        "exact_model_versions": ledger_summary.get("exact_model_versions", []),
        "activities_with_unavailable_exact_model_version": ledger_summary.get(
            "activities_with_unavailable_exact_model_version"
        ),
        "product_session": {
            "known_input_tokens": ledger_summary.get("product_session_known_input_tokens"),
            "known_output_tokens": ledger_summary.get("product_session_known_output_tokens"),
            "known_marginal_cost_gbp": ledger_summary.get("product_session_known_marginal_cost_gbp"),
            "activities_with_unavailable_input_tokens": ledger_summary.get(
                "product_session_activities_with_unavailable_input_tokens"
            ),
            "activities_with_unavailable_output_tokens": ledger_summary.get(
                "product_session_activities_with_unavailable_output_tokens"
            ),
            "activities_with_unavailable_cost": ledger_summary.get(
                "product_session_activities_with_unavailable_cost"
            ),
            "totals_complete": bool(ledger_summary)
            and ledger_summary.get("product_session_activities_with_unavailable_input_tokens") == 0
            and ledger_summary.get("product_session_activities_with_unavailable_output_tokens") == 0
            and ledger_summary.get("product_session_activities_with_unavailable_cost") == 0,
        },
    }
    fallback_ids = sorted(
        row.get("id")
        for row in declarations.get("fallbacks", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    )
    restriction_ids = sorted(
        row.get("constraint_id")
        for row in declarations.get("source_access_restrictions", [])
        if isinstance(row, dict) and isinstance(row.get("constraint_id"), str)
    )
    source_budget = {
        "snapshot_id": request_summary.get("snapshot_id"),
        "status": request_summary.get("status"),
        "final_shared_request_ceiling": request_summary.get("authorised_ceiling"),
        "final_shared_request_count": (
            request_summary.get("consumed_attempts") if request_summary.get("status") == "final" else None
        ),
        "shared_request_count_at_observation": request_summary.get("consumed_attempts"),
        "remaining_attempts": request_summary.get("remaining_attempts"),
        "included_in_model_cost": False,
    }
    validation_mode = "release" if require_release else "candidate" if require_candidate else "checkpoint"
    selected_blockers = release_blockers if require_release else candidate_blockers if require_candidate else []
    validation_errors = structural_errors + selected_blockers
    passed = not validation_errors
    candidate_satisfied = not structural_errors and not candidate_blockers
    release_satisfied = not structural_errors and not release_blockers
    publication_event = next(
        (
            row
            for row in terminals["events"]
            if row["terminal_activity_id"] == PUBLICATION_TERMINAL_ACTIVITY_ID
        ),
        None,
    )
    report = {
        "schema": REPORT_SCHEMA,
        "snapshot": snapshot,
        "validation_tier": validation_mode,
        "validation_mode": validation_mode,
        "candidate_mode": require_candidate,
        "release_mode": require_release,
        "provenance_validation_passed": passed,
        "candidate_requirements_satisfied": candidate_satisfied,
        "release_requirements_satisfied": release_satisfied,
        "publication_workflow_status": (
            "completed" if publication_event and publication_event["satisfied"] else "pending_post_publication"
        ),
        "validation_errors": validation_errors,
        "candidate_blockers": candidate_blockers,
        "release_blockers": release_blockers,
        "hash_chain": {
            "passed": bool(ledger_summary),
            "ledger_sha256": ledger_summary.get("ledger_sha256"),
            "last_entry_sha256": ledger_summary.get("last_entry_sha256"),
            "legacy_rows": ledger_summary.get("legacy_rows"),
            "hash_chained_v2_rows": ledger_summary.get("hash_chained_v2_rows"),
            "superseded_activity_ids": ledger_summary.get("superseded_activity_ids", []),
        },
        "activity_counts": {
            "total": ledger_summary.get("activities"),
            "deterministic": ledger_summary.get("deterministic_activities"),
            "model_assisted_or_mixed": ledger_summary.get("model_assisted_activities"),
        },
        "model_usage": model_usage,
        "external_paid_model_usage": external_usage,
        "source_request_budget": source_budget,
        "fallbacks": {"count": len(fallback_ids), "ids": fallback_ids},
        "source_access_restrictions": {"count": len(restriction_ids), "ids": restriction_ids},
        "required_terminal_events": terminals,
        "candidate_terminal_events": candidate_terminals,
        "unresolved_activity_status": unresolved,
        "validated_contract_counts": declaration_summary,
        "inputs": {
            "activity_ledger_sha256": _file_sha256(ledger_path) if ledger_path.is_file() else None,
            "activity_schema_sha256": _file_sha256(schema_path) if schema_path.is_file() else None,
            "declarations_sha256": _file_sha256(declarations_path) if declarations_path.is_file() else None,
            "source_request_budget_sha256": (
                _file_sha256(request_snapshot_path) if request_snapshot_path.is_file() else None
            ),
            "validator_sha256": _file_sha256(Path(__file__)) if Path(__file__).is_file() else None,
            "launch_manifest_sha256": _file_sha256(launch_path) if launch_path.is_file() else None,
            "model_lock_sha256": _file_sha256(model_lock_path) if model_lock_path.is_file() else None,
        },
    }
    return report


def write_validation_document(path: Path, document: dict[str, Any]) -> None:
    payload = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--snapshot")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--require-candidate", action="store_true")
    mode.add_argument("--require-release", action="store_true")
    args = parser.parse_args()
    try:
        snapshot = args.snapshot or _default_snapshot()
        document = build_validation_document(
            snapshot=snapshot,
            require_candidate=args.require_candidate,
            require_release=args.require_release,
        )
    except ProvenanceError as exc:
        print("provenance validation failed:")
        for error in str(exc).splitlines():
            print(f"- {error}")
        return 1
    output = args.output if args.output.is_absolute() else (Path.cwd() / args.output)
    write_validation_document(output.resolve(), document)
    print(json.dumps(document, sort_keys=True))
    return 0 if document["provenance_validation_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
