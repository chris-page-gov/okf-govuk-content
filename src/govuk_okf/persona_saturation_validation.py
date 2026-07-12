"""Independent deterministic checks for persona/use-taxonomy saturation assets."""

from __future__ import annotations

import hashlib
import json
from itertools import combinations
from pathlib import Path
from typing import Any

EXPECTED_DIMENSIONS = (
    "actor",
    "goal",
    "journey_stage",
    "content_service_type",
    "relationship_need",
    "jurisdiction",
    "language",
    "accessibility_need",
    "device_context",
    "urgency_risk",
    "agent_involvement",
)
EXPECTED_CLASS_COUNTS = {
    "agent_system": 12,
    "business_organisation": 8,
    "professional_intermediary": 10,
    "public_life_event": 18,
}
EXPECTED_INITIAL_OVERLAYS = 16
EXPECTED_CURRENT_OVERLAYS = 17
EXPECTED_SCHEMA_FAMILIES = 83
EXPECTED_V2_OPERATIONS = 10
EXPECTED_V2_STORY_ROLES = 6
EXPECTED_V2_STORIES_PER_PERSONA = 6
EXPECTED_HIGH_RISK_TWAY = 5


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _check_record(record: dict[str, Any], location: str, errors: list[str]) -> None:
    material = dict(record)
    checksum = material.pop("checksum", None)
    expected = _sha256_bytes(_canonical(material).encode("utf-8"))
    if checksum != expected:
        errors.append(f"{location}: record checksum mismatch")


def _line_range_sha256(path: Path, value: str) -> str:
    start_text, end_text = value.split("-", 1)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    return _sha256_bytes("".join(lines[int(start_text) - 1 : int(end_text)]).encode("utf-8"))


def validate(root: Path) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    evidence = _read_json(root / "personas" / "evidence.json")
    evidence_ids = {item["evidence_id"] for item in evidence["references"]}
    overlays = _read_json(root / "personas" / "overlays" / "catalogue.json")
    overlay_records = overlays["overlays"]
    overlay_ids = {item["overlay_id"] for item in overlay_records}
    if overlays.get("initial_overlay_count") != EXPECTED_INITIAL_OVERLAYS:
        errors.append("overlay catalogue: initial count is not 16")
    if overlays.get("current_overlay_count") != EXPECTED_CURRENT_OVERLAYS or len(overlay_ids) != EXPECTED_CURRENT_OVERLAYS:
        errors.append("overlay catalogue: current count is not 17")
    if "privacy-sensitive-context" not in overlay_ids:
        errors.append("overlay catalogue: privacy-sensitive-context missing")
    for overlay in overlay_records:
        if overlay.get("evidence_status") != "research_hypothesis_not_human_validated":
            errors.append(f"overlay {overlay.get('overlay_id')}: invalid evidence status")
        if not overlay.get("evidence_ids") or not set(overlay["evidence_ids"]) <= evidence_ids:
            errors.append(f"overlay {overlay.get('overlay_id')}: missing or unknown evidence")

    profiles = [_read_json(path) for path in sorted((root / "personas" / "profiles").glob("*.json"))]
    persona_ids = {profile["persona_id"] for profile in profiles}
    class_counts: dict[str, int] = {}
    for profile in profiles:
        class_counts[profile["archetype_class"]] = class_counts.get(profile["archetype_class"], 0) + 1
        _check_record(profile, profile["persona_id"], errors)
        if profile.get("evidence_status") != "research_hypothesis_not_human_validated":
            errors.append(f"{profile['persona_id']}: human validation status overstated")
        if not set(profile.get("evidence_ids", [])) <= evidence_ids:
            errors.append(f"{profile['persona_id']}: unknown persona evidence")
        if not set(profile.get("overlay_evidence_ids", [])) <= evidence_ids:
            errors.append(f"{profile['persona_id']}: unknown overlay evidence")
        if not set(profile.get("overlay_ids", [])) <= overlay_ids:
            errors.append(f"{profile['persona_id']}: unknown overlay")
        for field in (
            "jobs_to_be_done",
            "capabilities_and_context",
            "constraints",
            "trust_and_provenance_needs",
            "success_criteria",
            "failure_harms",
            "exclusion_notes",
            "linked_story_contract",
        ):
            if not profile.get(field):
                errors.append(f"{profile['persona_id']}: missing {field}")
    if len(profiles) != 48 or len(persona_ids) != 48:
        errors.append("persona profiles: expected 48 unique personas")
    if class_counts != EXPECTED_CLASS_COUNTS:
        errors.append(f"persona class counts: {class_counts!r}")

    coverage_rows = _read_jsonl(root / "personas" / "coverage-matrix.jsonl")
    if len(coverage_rows) != 48 or {row["persona_id"] for row in coverage_rows} != persona_ids:
        errors.append("coverage rows: expected exactly one row per primary persona")
    for row in coverage_rows:
        _check_record(row, row.get("coverage_row_id", "coverage-row"), errors)
        dimensions = row.get("dimension_values", {})
        if set(dimensions) != set(EXPECTED_DIMENSIONS):
            errors.append(f"{row.get('persona_id')}: dimension set mismatch")
        if any(not dimensions.get(dimension) for dimension in EXPECTED_DIMENSIONS):
            errors.append(f"{row.get('persona_id')}: empty dimension value")
        if row.get("evidence_status") != "research_hypothesis_not_human_validated":
            errors.append(f"{row.get('persona_id')}: matrix human status overstated")
        contract = row.get("release_story_contract", {})
        if contract.get("stories_per_persona") != EXPECTED_V2_STORIES_PER_PERSONA:
            errors.append(f"{row.get('persona_id')}: v2 story contract drift")
        if contract.get("status") != "contract_verified_final_snapshot_regeneration_pending":
            errors.append(f"{row.get('persona_id')}: final-snapshot regeneration boundary missing")

    matrix = _read_json(root / "personas" / "coverage-matrix.json")
    if tuple(matrix.get("required_dimensions", [])) != EXPECTED_DIMENSIONS:
        errors.append("coverage matrix: required dimensions mismatch")
    if matrix.get("unexplained_machine_dimension_gaps") != []:
        errors.append("coverage matrix: unexplained machine dimension gaps remain")
    if matrix.get("counts", {}).get("content_schema_families") != EXPECTED_SCHEMA_FAMILIES:
        errors.append("coverage matrix: not all 83 pinned schema families are mapped")
    covered_dimensions = {cell["dimension"] for cell in matrix.get("cells", []) if cell.get("persona_count", 0) > 0}
    if covered_dimensions != set(EXPECTED_DIMENSIONS):
        errors.append("coverage matrix: not every dimension has a covered cell")

    story_coverage = _read_json(root / "stories" / "coverage.json")
    if story_coverage.get("content_schema_family_count") != EXPECTED_SCHEMA_FAMILIES:
        errors.append("story coverage: schema count drift")
    if story_coverage.get("unmapped_content_schema_families") != []:
        errors.append("story coverage: schema families remain unmapped")

    overlay_array = _read_json(root / "personas" / "overlay-covering-array.json")
    pair_rows = [row for row in overlay_array.get("rows", []) if row.get("strength") == 2]
    triple_rows = [row for row in overlay_array.get("rows", []) if row.get("strength") == 3]
    expected_pairs = {tuple(pair) for pair in combinations(sorted(overlay_ids), 2)}
    actual_pairs = {tuple(row["overlay_ids"]) for row in pair_rows}
    if actual_pairs != expected_pairs:
        errors.append("overlay array: exhaustive pair set mismatch")
    if len(pair_rows) != EXPECTED_CURRENT_OVERLAYS * (EXPECTED_CURRENT_OVERLAYS - 1) // 2:
        errors.append("overlay array: pair count mismatch")
    if len(triple_rows) != EXPECTED_HIGH_RISK_TWAY:
        errors.append("overlay array: high-risk t-way count mismatch")
    for row in pair_rows + triple_rows:
        _check_record(row, row.get("scenario_id", "overlay-scenario"), errors)
        if row.get("assigned_persona_id") not in persona_ids:
            errors.append(f"{row.get('scenario_id')}: unknown persona")
        if row.get("evidence_status") != "research_hypothesis_not_human_validated":
            errors.append(f"{row.get('scenario_id')}: human validation status overstated")
        if row.get("question_binding_status") != "pending_final_snapshot_question_v2_regeneration":
            errors.append(f"{row.get('scenario_id')}: final-snapshot question boundary missing")

    challenge_paths = sorted((root / "personas" / "challenges").glob("*.json"))
    challenges = [_read_json(path) for path in challenge_paths]
    if len(challenges) < 2:
        errors.append("challenge ledger: fewer than two passes")
    for path, challenge in zip(challenge_paths, challenges):
        _check_record(challenge, path.relative_to(root).as_posix(), errors)
        if challenge.get("independence") != "method_and_input_partition_independent_only":
            errors.append(f"{path.name}: independence boundary missing")
        for artifact in challenge.get("input_artifacts", []):
            artifact_path = artifact.get("path")
            if artifact_path and artifact.get("sha256"):
                if _sha256_file(root / artifact_path) != artifact["sha256"]:
                    errors.append(f"{path.name}: input hash mismatch for {artifact_path}")
            if artifact_path and artifact.get("line_range_sha256"):
                if _line_range_sha256(root / artifact_path, artifact["line_range"]) != artifact["line_range_sha256"]:
                    errors.append(f"{path.name}: line-range hash mismatch for {artifact_path}")
    ordered_challenges = sorted(challenges, key=lambda item: item["sequence"])
    if [item["sequence"] for item in ordered_challenges] != list(range(1, len(ordered_challenges) + 1)):
        errors.append("challenge ledger: sequence is not contiguous")
    if len(ordered_challenges) >= 2 and any(item.get("novel_fraction", 1.0) >= 0.01 for item in ordered_challenges[-2:]):
        errors.append("challenge ledger: last two passes do not meet the below-one-percent rule")
    if len(ordered_challenges) >= 2 and any(item.get("findings") for item in ordered_challenges[-2:]):
        errors.append("challenge ledger: last two passes contain unresolved findings")

    saturation = _read_json(root / "personas" / "saturation.json")
    _check_record(saturation, "personas/saturation.json", errors)
    if saturation.get("machine_applicable_gate_status") != "passed":
        errors.append("saturation: machine-applicable gate is not passed")
    if saturation.get("human_validation_status") != "not_authorised_not_run":
        errors.append("saturation: human-validation boundary is not explicit")
    if saturation.get("human_ui_preference_status") != "not_yet_testable":
        errors.append("saturation: preference status is not not_yet_testable")
    if saturation.get("coverage_matrix", {}).get("sha256") != _sha256_file(root / "personas" / "coverage-matrix.json"):
        errors.append("saturation: coverage-matrix hash mismatch")
    if saturation.get("overlay_covering_array", {}).get("sha256") != _sha256_file(
        root / "personas" / "overlay-covering-array.json"
    ):
        errors.append("saturation: overlay-array hash mismatch")
    for reference in saturation.get("challenge_passes", []):
        if _sha256_file(root / reference["path"]) != reference["sha256"]:
            errors.append(f"saturation: challenge hash mismatch for {reference['path']}")
    model_usage = saturation.get("model_usage") or {}
    if model_usage.get("deterministic_generation") != {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_gbp": 0.0,
    }:
        errors.append("saturation: deterministic generation usage is not zero")
    if (model_usage.get("design_assistance") or {}).get("accounting_status") != "recorded_unknown_not_zero":
        errors.append("saturation: unavailable design-assistance usage is not recorded honestly")

    checksum_lines = (root / "personas" / "checksums.txt").read_text(encoding="utf-8").splitlines()
    for line in checksum_lines:
        digest, relative = line.split("  ", 1)
        if _sha256_file(root / relative) != digest:
            errors.append(f"personas/checksums.txt: mismatch for {relative}")
    manifest = _read_json(root / "personas" / "manifest.json")
    manifest_material = []
    for item in manifest.get("files", []):
        path = root / item["path"]
        digest = _sha256_file(path)
        if digest != item["sha256"] or path.stat().st_size != item["bytes"]:
            errors.append(f"personas/manifest.json: mismatch for {item['path']}")
        manifest_material.append((item["path"], digest))
    root_material = "".join(f"{path}\0{digest}\n" for path, digest in manifest_material)
    if _sha256_bytes(root_material.encode("utf-8")) != manifest.get("root_sha256"):
        errors.append("personas/manifest.json: root hash mismatch")

    return {
        "schema_version": 1,
        "validator": "independent-persona-saturation-validator-v1",
        "machine_validations_passed": not errors,
        "counts": {
            "primary_personas": len(profiles),
            "initial_overlays": EXPECTED_INITIAL_OVERLAYS,
            "current_overlays": len(overlay_ids),
            "coverage_dimensions": len(EXPECTED_DIMENSIONS),
            "content_schema_families": story_coverage.get("content_schema_family_count"),
            "overlay_pair_scenarios": len(pair_rows),
            "high_risk_tway_scenarios": len(triple_rows),
            "challenge_passes": len(challenges),
            "validation_errors": len(errors),
        },
        "human_validation_status": "not_authorised_not_run",
        "human_ui_preference_status": "not_yet_testable",
        "final_snapshot_question_regeneration": "required",
        "errors": errors,
    }
