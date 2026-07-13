#!/usr/bin/env python3
"""Stage and transactionally promote an unsampled full-corpus machine release.

``stage`` writes a non-publishable full-corpus checkpoint after reconciling the
bundle, descriptor and closing census. ``promote`` derives every status boolean
from snapshot-bound evidence, regenerates the aim assessment inside the
transaction, and rolls every changed release-control file back if the final
publication validator reports any failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import append_activity  # noqa: E402
import build_aim_scorecard  # noqa: E402
import check_provenance  # noqa: E402
import check_release  # noqa: E402
import reproduce_release  # noqa: E402


DISALLOWED = ("fixture", "sample", "capacity", "development", "test")
ALL_GATES = check_release.RELEASE_FLAGS
MANIFEST_PATH = Path(check_release.MANIFEST_RELATIVE)
STATUS_PATH = Path(check_release.STATUS_RELATIVE)
CHECKPOINT_MARKER = "AFHF_GOVUK_OKF_FULL_CORPUS_CHECKPOINT_V1"
CLEAN_ROOM_TERMINAL_ID = "ACT-F2-CLEAN-ROOM-RC-TERMINAL-001"
ARTIFACT_DEFAULTS = {
    "aim_assessment": "release/aim-assessment.json",
    "browser_validation": "release/accessibility-browser.json",
    "bundle": "bundle",
    "checksums": "bundle/checksums.json",
    "citation_verification": "release/citation-verification.json",
    "clean_room_reproduction": "release/clean-room-reproduction.json",
    "descriptor": "bundle/okf-explorer.json",
    "evaluation": "evaluation/results/status.json",
    "full_repository_tests": "release/full-repository-tests.json",
    "provenance_validation": "release/provenance-validation.json",
    "question_contract": "questions/release-v2/verification-report.json",
    "reconciliation": None,
    "rights_privacy_audit": "release/rights-privacy-audit.json",
    "sbom": "release/sbom.cdx.json",
    "security_scan": "release/security-scan.json",
    "semantic_validation": "release/semantic-validation.json",
    "status": STATUS_PATH.as_posix(),
}


class PromotionError(ValueError):
    """Raised when staging or release promotion must fail closed."""


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PromotionError(f"{label} must be a JSON object: {path}")
    return value


def sha256(path: Path) -> str:
    return check_release._file_sha256(path)


def safe_relative(root: Path, value: str, label: str) -> tuple[Path, str]:
    try:
        path = check_release._resolve_relative(root, value, label)
    except check_release.ReleaseDocumentError as exc:
        raise PromotionError(str(exc)) from exc
    assert path is not None
    return path, Path(value).as_posix()


def validate_snapshot_id(snapshot: str) -> None:
    if not snapshot.strip() or any(marker in snapshot.casefold() for marker in DISALLOWED):
        raise PromotionError("full release snapshot rejects fixture, sample, capacity, development and test labels")


def load_bundle_contract(root: Path, bundle_relative: str) -> tuple[dict[str, Any], dict[str, Any]]:
    bundle, _ = safe_relative(root, bundle_relative, "bundle")
    descriptor = load_json(bundle / "okf-explorer.json", "Explorer descriptor")
    data_manifest_value = check_release._entrypoint_path(
        descriptor.get("entrypoints", {}).get("data_manifest")
    )
    if not data_manifest_value:
        raise PromotionError("Explorer descriptor has no data manifest")
    try:
        data_manifest_path = check_release._resolve_relative(bundle, data_manifest_value, "data manifest")
    except check_release.ReleaseDocumentError as exc:
        raise PromotionError(str(exc)) from exc
    assert data_manifest_path is not None
    return descriptor, load_json(data_manifest_path, "bundle data manifest")


def validate_reconciliation(
    reconciliation: dict[str, Any], descriptor: dict[str, Any], data_manifest: dict[str, Any], snapshot: str
) -> int:
    errors: list[str] = []
    if reconciliation.get("snapshot") != snapshot:
        errors.append("closing reconciliation snapshot differs")
    if reconciliation.get("sampled") is not False:
        errors.append("closing reconciliation is sampled")
    if reconciliation.get("unexplained_omissions") != 0:
        errors.append("closing reconciliation has unexplained omissions")
    dispositions = ("represented", "alias_of_represented", "redirect_only", "tombstone_only", "exceptioned")
    expected = reconciliation.get("expected_candidate_keys")
    if not isinstance(expected, int) or expected < 1:
        errors.append("closing reconciliation expected_candidate_keys is invalid")
    elif any(not isinstance(reconciliation.get(item), int) or reconciliation[item] < 0 for item in dispositions):
        errors.append("closing reconciliation disposition counts are invalid")
    elif sum(reconciliation[item] for item in dispositions) != expected:
        errors.append("closing reconciliation accounting identity does not hold")
    entity_counts = reconciliation.get("entity_class_counts")
    if not isinstance(entity_counts, dict) or not entity_counts or sum(entity_counts.values()) != expected:
        errors.append("closing reconciliation entity classes do not close")
    errors.extend(check_release._proof_errors(reconciliation))
    descriptor_counts = descriptor.get("counts")
    if not isinstance(descriptor_counts, dict) or descriptor_counts.get("records") != descriptor_counts.get("datasets"):
        errors.append("Explorer descriptor record counts are invalid")
        publication_records = -1
    else:
        publication_records = descriptor_counts["records"]
    if data_manifest.get("counts") != descriptor_counts or data_manifest.get("snapshot") != snapshot:
        errors.append("bundle data manifest counts or snapshot differ")
    if reconciliation.get("publication_records") != publication_records:
        errors.append("reconciliation and bundle publication record counts differ")
    if errors:
        raise PromotionError("; ".join(errors))
    return publication_records


def _replace_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.promotion-{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def transactional_update(
    paths: list[Path], initial: dict[Path, bytes], continuation: Callable[[], dict[Path, bytes]], validate: Callable[[], list[str]]
) -> None:
    backups = {path: path.read_bytes() if path.is_file() else None for path in paths}
    try:
        for path, payload in initial.items():
            _replace_bytes(path, payload)
        for path, payload in continuation().items():
            _replace_bytes(path, payload)
        errors = validate()
        if errors:
            raise PromotionError("final check_release failed: " + "; ".join(errors))
    except BaseException:
        for path in reversed(paths):
            previous = backups[path]
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                _replace_bytes(path, previous)
        raise


def artifact_paths(overrides: dict[str, str | None], reconciliation: str | None = None) -> dict[str, str | None]:
    result = dict(ARTIFACT_DEFAULTS)
    if reconciliation is not None:
        result["reconciliation"] = reconciliation
    for key, value in overrides.items():
        if value is not None:
            result[key] = value
    return result


def machine_candidate_status(snapshot_id: str) -> dict[str, Any]:
    return {
        **{gate: True for gate in ALL_GATES},
        "schema": "afhf-govuk-okf-release-status.v1",
        "release_id": snapshot_id,
        "status": "machine_release_candidate",
        "publication_ready": True,
        "completion_statement": check_release.MACHINE_MARKER,
        "machine_rc_complete": True,
        "full_evaluation_complete": False,
        "agent_evaluation_status": "completed",
        "human_evaluation_status": "not_authorised",
        "human_ui_of_choice_status": "not_yet_testable",
        "aims_assessed": True,
        "programme_complete": False,
        "promotion_finalized": False,
        "unexplained_omissions": 0,
        "reason": check_release.MACHINE_CANDIDATE_REASON,
    }


def stage_release(
    root: Path, *, snapshot: str, reconciliation_relative: str, bundle_relative: str = "bundle",
    source_relative: str, generated_at: str | None = None, compiler: str = "disk",
    overrides: dict[str, str | None] | None = None,
    validator: Callable[[Path, bool], list[str]] | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    validate_snapshot_id(snapshot)
    reconciliation_path, reconciliation_relative = safe_relative(root, reconciliation_relative, "reconciliation")
    reconciliation = load_json(reconciliation_path, "closing reconciliation")
    descriptor, data_manifest = load_bundle_contract(root, bundle_relative)
    publication_records = validate_reconciliation(reconciliation, descriptor, data_manifest, snapshot)
    artifacts = artifact_paths(overrides or {}, reconciliation_relative)
    artifacts["bundle"] = bundle_relative
    artifacts["descriptor"] = f"{bundle_relative.rstrip('/')}/okf-explorer.json"
    artifacts["checksums"] = f"{bundle_relative.rstrip('/')}/checksums.json"
    source_path, source_relative = safe_relative(root, source_relative, "frozen release source")
    if not source_path.exists():
        raise PromotionError(f"frozen release source is missing: {source_path}")
    if "tests/fixtures" in source_relative:
        raise PromotionError("fixture source cannot be staged for a full-corpus release")
    effective_generated_at = generated_at or data_manifest.get("generated_at")
    if not isinstance(effective_generated_at, str) or not effective_generated_at.strip():
        raise PromotionError("stage requires the deterministic bundle generated-at value")
    if compiler not in {"auto", "memory", "disk"}:
        raise PromotionError("stage compiler must be auto, memory or disk")
    reproduction = {
        "source": source_relative,
        "generated_at": effective_generated_at,
        "compiler": compiler,
        "source_binding": reproduce_release.source_binding(source_path, root),
    }
    gates = {gate: False for gate in ALL_GATES}
    gates["full_corpus_reconciled"] = True
    manifest = {
        "schema": "afhf-govuk-okf-release-manifest.v1",
        "release_id": snapshot,
        "release_kind": "full_corpus_checkpoint",
        "publication_ready": False,
        "snapshot": {"id": snapshot, "kind": "full_corpus", "sampled": False},
        "counts": {"publication_records": publication_records},
        "artifacts": artifacts,
        "gates": gates,
        "promotion_contract": {
            "schema": "afhf-govuk-okf-two-stage-promotion.v1",
            "stage": "full_corpus_checkpoint",
            "target_release_kind": "machine_release_candidate",
            "reproduction": reproduction,
        },
    }
    status = {
        **gates,
        "schema": "afhf-govuk-okf-release-status.v1",
        "release_id": snapshot,
        "status": "checkpoint",
        "publication_ready": False,
        "completion_statement": CHECKPOINT_MARKER,
        "machine_rc_complete": False,
        "full_evaluation_complete": False,
        "agent_evaluation_status": "not_started",
        "human_evaluation_status": "not_authorised",
        "human_ui_of_choice_status": "not_yet_testable",
        "aims_assessed": False,
        "programme_complete": False,
        "promotion_finalized": False,
        "unexplained_omissions": 0,
        "reason": "Full-corpus checkpoint staged for snapshot-bound clean-room and release evidence; it is not publishable.",
    }
    manifest_path, _ = safe_relative(root, MANIFEST_PATH.as_posix(), "release manifest")
    status_path, _ = safe_relative(root, STATUS_PATH.as_posix(), "release status")
    final_validator = validator or (lambda candidate_root, publication: check_release.validate_release(candidate_root, require_publication_ready=publication))
    transactional_update(
        [manifest_path, status_path],
        {manifest_path: canonical_bytes(manifest), status_path: canonical_bytes(status)},
        lambda: {},
        lambda: final_validator(root, False),
    )
    return {"manifest": manifest, "status": status}


def build_provenance_evidence(root: Path, snapshot: str, *, finalized: bool = False) -> dict[str, Any]:
    if root != ROOT.resolve():
        raise PromotionError("non-default roots require an injected provenance builder")
    try:
        document = check_provenance.build_validation_document(
            snapshot=snapshot,
            require_candidate=not finalized,
            require_release=finalized,
        )
    except check_provenance.ProvenanceError as exc:
        raise PromotionError(f"provenance validation failed: {exc}") from exc
    if document.get("provenance_validation_passed") is not True:
        raise PromotionError("provenance validation failed: " + "; ".join(document.get("validation_errors", [])))
    return document


def _command_result(command: list[str], root: Path) -> dict[str, Any]:
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
    output = result.stdout + result.stderr
    return {
        "command": command,
        "returncode": result.returncode,
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "output_tail": output[-2000:],
    }


def build_test_evidence(root: Path, snapshot: str) -> dict[str, Any]:
    before = check_release._tree_sha256(root)
    commands = [
        _command_result([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], root),
        _command_result(["npm", "test", "--prefix", "explorer"], root),
        _command_result(["npm", "test", "--prefix", "semantic"], root),
    ]
    if any(row["returncode"] != 0 for row in commands):
        raise PromotionError("full-repository tests failed")
    after = check_release._tree_sha256(root)
    if before != after:
        raise PromotionError("full-repository tests mutated code or test inputs")
    match = re.search(r"Ran\s+(\d+)\s+tests?", commands[0]["output_tail"])
    if not match:
        raise PromotionError("could not extract Python test count from full-repository test output")
    return {
        "schema": "afhf-govuk-okf-full-repository-tests.v1",
        "snapshot": snapshot,
        "scope": "full_repository",
        "passed": True,
        "tests_passed": True,
        "python_tests_run": int(match.group(1)),
        "commands": commands,
        "code_tree": {"paths": list(check_release.TEST_INPUT_PATHS), "sha256": after},
    }


def build_clean_room_evidence(
    root: Path,
    snapshot: str,
    staged_manifest: dict[str, Any],
    tests: dict[str, Any],
) -> dict[str, Any]:
    """Rebuild against the still-staged checkpoint and prospective RC contract."""

    if root != ROOT.resolve():
        raise PromotionError("non-default roots require an injected clean-room builder")
    contract = staged_manifest.get("promotion_contract")
    reproduction = contract.get("reproduction") if isinstance(contract, dict) else None
    if (
        not isinstance(contract, dict)
        or contract.get("schema") != "afhf-govuk-okf-two-stage-promotion.v1"
        or contract.get("stage") != "full_corpus_checkpoint"
        or contract.get("target_release_kind") != "machine_release_candidate"
        or not isinstance(reproduction, dict)
    ):
        raise PromotionError("staged checkpoint lacks a complete prospective clean-room contract")
    artifacts = staged_manifest["artifacts"]
    source, _ = safe_relative(root, str(reproduction.get("source") or ""), "frozen release source")
    bundle, _ = safe_relative(root, str(artifacts["bundle"]), "bundle")
    sbom, _ = safe_relative(root, str(artifacts["sbom"]), "sbom")
    evidence_output, _ = safe_relative(
        root, str(artifacts["clean_room_reproduction"]), "clean-room evidence"
    )
    tests_path, _ = safe_relative(root, str(artifacts["full_repository_tests"]), "full test evidence")
    arguments = argparse.Namespace(
        source=source,
        expected_bundle=bundle,
        sbom=sbom,
        evidence_output=evidence_output,
        declarations=root / "provenance/reproduction-declarations.json",
        activity_ledger=root / "provenance/activity-ledger.jsonl",
        release_manifest=root / MANIFEST_PATH,
        test_evidence=tests_path,
        generated_at=str(reproduction.get("generated_at") or ""),
        snapshot_id=snapshot,
        snapshot_kind="full_corpus",
        release_kind="machine_release_candidate",
        sampled=False,
        compiler=str(reproduction.get("compiler") or "disk"),
        timeout_seconds=21600,
    )
    evidence = reproduce_release.reproduce(arguments)
    errors = reproduce_release.validate_evidence(evidence, require_release=True)
    if errors:
        raise PromotionError("clean-room reproduction failed: " + "; ".join(errors))
    if evidence.get("test_evidence", {}).get("sha256") != hashlib.sha256(
        canonical_bytes(tests)
    ).hexdigest():
        raise PromotionError("clean-room reproduction did not bind the generated full-test evidence")
    return evidence


def build_clean_room_terminal(
    root: Path,
    snapshot: str,
    clean_room: dict[str, Any],
    tests: dict[str, Any],
    staged_manifest_sha: str,
    staged_status_sha: str,
    clean_path: Path,
    tests_path: Path,
    sbom_path: Path,
) -> dict[str, object]:
    """Build the terminal ledger row only after clean-room evidence exists."""

    recorded_at = clean_room.get("recorded_at")
    if not isinstance(recorded_at, str) or not recorded_at.strip():
        recorded_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )
    started_at = clean_room.get("started_at")
    if not isinstance(started_at, str) or not started_at.strip():
        started_at = recorded_at
    ended_at = clean_room.get("ended_at")
    if not isinstance(ended_at, str) or not ended_at.strip():
        ended_at = recorded_at
    return {
        "ledger_schema_version": "2.0",
        "activity_id": CLEAN_ROOM_TERMINAL_ID,
        "status": "completed",
        "work_class": "deterministic",
        "started_at": started_at,
        "ended_at": ended_at,
        "recorded_at": recorded_at,
        "commit": None,
        "agent": {
            "id": "CPython release promotion process",
            "role": "clean-room machine release-candidate verifier",
            "relationship": "deterministic_process",
        },
        "prompt": {
            "capture_status": "not_applicable",
            "objective": "",
            "reference": None,
            "sha256": None,
        },
        "model": None,
        "tool_calls": {
            "capture_status": "complete",
            "calls": [
                {
                    "tool": "CPython clean-room release verifier",
                    "command": "scripts/promote_release.py promote",
                    "purpose": "Run full tests and reproduce the frozen candidate in a zero-network temporary workspace.",
                    "call_count": len(clean_room.get("commands", [])),
                }
            ],
        },
        "source_snapshots": [snapshot],
        "outputs": [
            {
                "path": clean_path.relative_to(root).as_posix(),
                "state": "produced",
                "sha256": hashlib.sha256(canonical_bytes(clean_room)).hexdigest(),
            },
            {
                "path": tests_path.relative_to(root).as_posix(),
                "state": "produced",
                "sha256": hashlib.sha256(canonical_bytes(tests)).hexdigest(),
            },
            {
                "path": sbom_path.relative_to(root).as_posix(),
                "state": "produced",
                "sha256": sha256(sbom_path),
            },
        ],
        "validation": {
            "capture_status": "complete",
            "results": [
                "The full unsampled snapshot rebuilt exactly in an isolated temporary workspace.",
                "The reproduced bundle and CycloneDX SBOM matched byte for byte.",
                "Full-repository test evidence is hash-bound and passing.",
                f"Clean-room evidence is bound to staged manifest {staged_manifest_sha} and status {staged_status_sha}.",
                "The reproduction made zero official-source and external-model requests.",
            ],
        },
        "source_request_usage": {
            "status": "not_applicable",
            "attempts": "not_applicable",
            "budget_ledger": None,
            "observation_at": None,
            "included_in_model_cost": False,
            "evidence": "Clean-room reproduction consumed only frozen local inputs.",
        },
        "usage": {
            "external_paid_model": {
                "api_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_gbp": 0,
            },
            "product_session": {
                "input_tokens": 0,
                "output_tokens": 0,
                "marginal_cost_gbp": 0,
            },
        },
        "tokens": 0,
        "cost_gbp": 0,
        "external_paid_model_api_calls": 0,
    }


def append_clean_room_terminal(root: Path, entry: dict[str, object]) -> None:
    append_activity.append_entries(
        [entry],
        root / "provenance/activity-ledger.jsonl",
        root / "provenance/activity-ledger.schema.json",
        acquire_lock=False,
    )


def recover_prepared_clean_room(
    root: Path,
    snapshot: str,
    ledger_path: Path,
    clean_path: Path,
    tests_path: Path,
    sbom_path: Path,
    bundle_path: Path,
    promotion: dict[str, Any],
) -> dict[str, Any] | None:
    """Reuse an exact post-clean/pre-candidate crash checkpoint idempotently."""

    try:
        rows = [
            json.loads(line)
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise PromotionError(f"prepared clean-room recovery cannot read the ledger: {exc}") from exc
    terminals = [row for row in rows if row.get("activity_id") == CLEAN_ROOM_TERMINAL_ID]
    if not terminals:
        return None
    if len(terminals) != 1:
        raise PromotionError("prepared clean-room recovery found duplicate terminal activities")
    terminal = terminals[0]
    if terminal.get("status") != "completed" or terminal.get("source_snapshots") != [snapshot]:
        raise PromotionError("prepared clean-room terminal is not completed for the staged snapshot")
    try:
        clean_room = load_json(clean_path, "prepared clean-room evidence")
    except PromotionError as exc:
        raise PromotionError(f"prepared clean-room recovery failed: {exc}") from exc
    clean_errors = check_release._clean_room_errors(
        clean_room,
        root,
        sbom_path,
        promotion,
        bundle_path=bundle_path,
        test_evidence_path=tests_path,
    )
    if clean_errors:
        raise PromotionError(
            "prepared clean-room recovery evidence is inconsistent: " + "; ".join(clean_errors)
        )
    outputs = {
        row.get("path"): row.get("sha256")
        for row in terminal.get("outputs", [])
        if isinstance(row, dict)
    }
    expected = {
        clean_path.relative_to(root).as_posix(): sha256(clean_path),
        tests_path.relative_to(root).as_posix(): sha256(tests_path),
        sbom_path.relative_to(root).as_posix(): sha256(sbom_path),
    }
    if outputs != expected:
        raise PromotionError(
            "prepared clean-room recovery terminal hashes differ; preserve the ledger and repair the prepared artefacts before retrying"
        )
    return clean_room


def validate_pre_promotion(
    root: Path,
    manifest: dict[str, Any],
    provenance: dict[str, Any],
    tests: dict[str, Any],
    clean_room: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    snapshot = manifest["snapshot"]
    artifacts = manifest["artifacts"]
    promotion = manifest["promotion"]
    try:
        sbom_path = check_release._resolve_relative(root, artifacts.get("sbom"), "sbom")
        if sbom_path:
            errors.extend(check_release._sbom_errors(root, sbom_path))
        checksums = check_release._resolve_relative(root, artifacts.get("checksums"), "checksums")
        bundle = check_release._resolve_relative(root, artifacts.get("bundle"), "bundle")
        if checksums and bundle:
            errors.extend(check_release._checksum_errors(bundle, checksums))
        validators = (
            ("question_contract", lambda path, evidence: check_release._question_contract_errors(path, evidence, snapshot)),
            ("evaluation", lambda path, evidence: check_release._evaluation_errors(evidence, snapshot)),
            ("browser_validation", lambda path, evidence: check_release._browser_errors(evidence, snapshot)),
            ("security_scan", lambda path, evidence: check_release._security_errors(root, evidence, snapshot)),
        )
        for name, validator in validators:
            path = check_release._resolve_relative(root, artifacts.get(name), name)
            assert path is not None
            errors.extend(validator(path, check_release._load_json(path, name)))
        for name, pass_field, alternative in (
            ("semantic_validation", "semantic_validation_passed", "passed"),
            ("citation_verification", "citation_verification_passed", None),
            ("rights_privacy_audit", "rights_privacy_audit_passed", None),
        ):
            path = check_release._resolve_relative(root, artifacts.get(name), name)
            assert path is not None
            evidence = check_release._load_json(path, name)
            if evidence.get(pass_field) is not True and (alternative is None or evidence.get(alternative) is not True):
                errors.append(f"{name} evidence did not pass")
            if check_release._artifact_snapshot(evidence) != snapshot["id"]:
                errors.append(f"{name} snapshot differs")
        tests_path = check_release._resolve_relative(
            root, artifacts.get("full_repository_tests"), "full_repository_tests"
        )
        if clean_room.get("clean_room_reproduction_passed") is not True:
            errors.append("clean_room_reproduction evidence did not pass")
        if check_release._artifact_snapshot(clean_room) != snapshot["id"]:
            errors.append("clean_room_reproduction snapshot differs")
        if sbom_path:
            errors.extend(
                check_release._clean_room_errors(
                    clean_room,
                    root,
                    sbom_path,
                    promotion,
                    bundle_path=bundle,
                    test_evidence_path=tests_path,
                )
            )
        errors.extend(check_release._provenance_errors(root, provenance, snapshot))
        errors.extend(check_release._test_evidence_errors(root, tests, snapshot))
    except check_release.ReleaseDocumentError as exc:
        errors.append(str(exc))
    return errors


def promote_release(
    root: Path, *, overrides: dict[str, str | None] | None = None,
    provenance_builder: Callable[[Path, str], dict[str, Any]] = build_provenance_evidence,
    test_builder: Callable[[Path, str], dict[str, Any]] = build_test_evidence,
    clean_room_builder: Callable[
        [Path, str, dict[str, Any], dict[str, Any]], dict[str, Any]
    ] = build_clean_room_evidence,
    aim_renderer: Callable[[Path], dict[Path, str]] = build_aim_scorecard.render,
    validator: Callable[[Path, bool], list[str]] | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    manifest_path, _ = safe_relative(root, MANIFEST_PATH.as_posix(), "release manifest")
    status_path, _ = safe_relative(root, STATUS_PATH.as_posix(), "release status")
    current_manifest = load_json(manifest_path, "release manifest")
    current_status = load_json(status_path, "release status")
    snapshot = current_manifest.get("snapshot")
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("kind") != "full_corpus"
        or snapshot.get("sampled") is not False
    ):
        raise PromotionError("promote requires an unsampled full-corpus snapshot")
    snapshot_id = str(snapshot.get("id") or "")
    validate_snapshot_id(snapshot_id)
    recovery_mode = current_manifest.get("release_kind") == "machine_release_candidate"
    if recovery_mode:
        if overrides and any(value is not None for value in overrides.values()):
            raise PromotionError("partial-candidate recovery does not accept artifact overrides")
        promotion = current_manifest.get("promotion")
        if (
            current_manifest.get("publication_ready") is not True
            or not isinstance(promotion, dict)
            or promotion.get("schema") != "afhf-govuk-okf-two-stage-promotion.v1"
            or promotion.get("from") != "full_corpus_checkpoint"
            or promotion.get("finalized") is not False
            or not check_release._valid_sha256(promotion.get("staged_manifest_sha256"))
            or not check_release._valid_sha256(promotion.get("staged_status_sha256"))
            or not isinstance(promotion.get("reproduction"), dict)
        ):
            raise PromotionError("partial machine-candidate controls are not recoverable")
        staged_manifest_sha = promotion["staged_manifest_sha256"]
        staged_status_sha = promotion["staged_status_sha256"]
        final_manifest = current_manifest
        final_status = machine_candidate_status(snapshot_id)
        if current_status.get("status") == "checkpoint":
            if sha256(status_path) != staged_status_sha:
                raise PromotionError("partial-candidate staged status hash differs")
        elif current_status != final_status:
            raise PromotionError("partial-candidate status is neither the exact staged nor candidate document")
        artifacts = current_manifest.get("artifacts")
        if not isinstance(artifacts, dict):
            raise PromotionError("partial candidate has no artifact contract")
        staged_manifest = current_manifest
    else:
        staged_manifest = current_manifest
        staged_status = current_status
        if (
            staged_manifest.get("release_kind") != "full_corpus_checkpoint"
            or staged_manifest.get("publication_ready") is not False
            or staged_status.get("status") != "checkpoint"
            or staged_status.get("publication_ready") is not False
        ):
            raise PromotionError("promote requires a non-publishable staged full-corpus checkpoint")
        staged_manifest_sha = sha256(manifest_path)
        staged_status_sha = sha256(status_path)
        artifacts = artifact_paths(overrides or {})
        artifacts.update(
            {
                key: value
                for key, value in staged_manifest["artifacts"].items()
                if (overrides or {}).get(key) is None
            }
        )
        staged_contract = staged_manifest.get("promotion_contract")
        staged_reproduction = (
            staged_contract.get("reproduction") if isinstance(staged_contract, dict) else None
        )
        if not isinstance(staged_reproduction, dict):
            raise PromotionError("staged checkpoint lacks its immutable reproduction contract")
        promotion = {
            "schema": "afhf-govuk-okf-two-stage-promotion.v1",
            "from": "full_corpus_checkpoint",
            "staged_manifest_sha256": staged_manifest_sha,
            "staged_status_sha256": staged_status_sha,
            "reproduction": staged_reproduction,
            "finalized": False,
        }
        final_manifest = dict(staged_manifest)
        final_manifest.update(
            {
                "release_kind": "machine_release_candidate",
                "publication_ready": True,
                "artifacts": artifacts,
                "gates": {gate: True for gate in ALL_GATES},
                "promotion": promotion,
            }
        )
        final_manifest.pop("promotion_contract", None)
        final_status = machine_candidate_status(snapshot_id)
    reconciliation_path, _ = safe_relative(root, str(artifacts["reconciliation"]), "reconciliation")
    reconciliation = load_json(reconciliation_path, "closing reconciliation")
    descriptor, data_manifest = load_bundle_contract(root, str(artifacts["bundle"]))
    publication_records = validate_reconciliation(reconciliation, descriptor, data_manifest, snapshot_id)
    if final_manifest.get("counts", {}).get("publication_records") != publication_records:
        raise PromotionError("staged manifest publication count no longer matches bundle and reconciliation")
    provenance_path, _ = safe_relative(root, str(artifacts["provenance_validation"]), "provenance evidence")
    tests_path, _ = safe_relative(root, str(artifacts["full_repository_tests"]), "full test evidence")
    clean_room_path, _ = safe_relative(
        root, str(artifacts["clean_room_reproduction"]), "clean-room evidence"
    )
    sbom_path, _ = safe_relative(root, str(artifacts["sbom"]), "sbom")
    bundle_path, _ = safe_relative(root, str(artifacts["bundle"]), "bundle")
    ledger_path, _ = safe_relative(
        root, "provenance/activity-ledger.jsonl", "activity ledger"
    )
    ledger_schema_path, _ = safe_relative(
        root, "provenance/activity-ledger.schema.json", "activity-ledger schema"
    )
    if not ledger_path.is_file() or not ledger_schema_path.is_file():
        raise PromotionError("promotion requires regular activity-ledger and schema files")
    aim_path, _ = safe_relative(root, str(artifacts["aim_assessment"]), "aim assessment")
    report_path, _ = safe_relative(root, "reports/aim-scorecard.md", "aim scorecard")
    transaction_paths = [
        manifest_path,
        status_path,
        provenance_path,
        tests_path,
        clean_room_path,
        ledger_path,
        aim_path,
        report_path,
    ]
    final_validator = validator or (lambda candidate_root, publication: check_release.validate_release(candidate_root, require_publication_ready=publication))

    def continuation() -> dict[Path, bytes]:
        clean_room = prepared_clean_room
        if clean_room is None:
            clean_room = clean_room_builder(root, snapshot_id, staged_manifest, tests)
            clean_errors = check_release._clean_room_errors(
                clean_room,
                root,
                sbom_path,
                promotion,
                bundle_path=bundle_path,
                test_evidence_path=tests_path,
            )
            if clean_errors:
                raise PromotionError("clean-room evidence failed: " + "; ".join(clean_errors))
            _replace_bytes(clean_room_path, canonical_bytes(clean_room))
            terminal = build_clean_room_terminal(
                root,
                snapshot_id,
                clean_room,
                tests,
                staged_manifest_sha,
                staged_status_sha,
                clean_room_path,
                tests_path,
                sbom_path,
            )
            append_clean_room_terminal(root, terminal)
        provenance = provenance_builder(root, snapshot_id)
        preflight_errors = validate_pre_promotion(
            root, final_manifest, provenance, tests, clean_room
        )
        if preflight_errors:
            raise PromotionError("promotion evidence failed: " + "; ".join(preflight_errors))
        candidate_controls = {
            manifest_path: canonical_bytes(final_manifest),
            status_path: canonical_bytes(final_status),
            provenance_path: canonical_bytes(provenance),
        }
        for path, payload in candidate_controls.items():
            _replace_bytes(path, payload)
        rendered = aim_renderer(root)
        if aim_path not in rendered:
            raise PromotionError("aim renderer did not produce the manifest-declared assessment path")
        return {
            **{path: content.encode("utf-8") for path, content in rendered.items()},
        }

    with append_activity.ledger_lock(ledger_path):
        prepared_clean_room = recover_prepared_clean_room(
            root,
            snapshot_id,
            ledger_path,
            clean_room_path,
            tests_path,
            sbom_path,
            bundle_path,
            promotion,
        )
        if recovery_mode and prepared_clean_room is None:
            raise PromotionError(
                "partial-candidate recovery requires the exact prepared clean-room terminal and evidence"
            )
        tests = (
            load_json(tests_path, "prepared full test evidence")
            if prepared_clean_room is not None
            else test_builder(root, snapshot_id)
        )
        transactional_update(
            transaction_paths,
            {} if prepared_clean_room is not None else {tests_path: canonical_bytes(tests)},
            continuation,
            lambda: final_validator(root, True),
        )
    return {"manifest": final_manifest, "status": final_status}


def finalize_release(
    root: Path, *,
    provenance_builder: Callable[[Path, str], dict[str, Any]] | None = None,
    aim_renderer: Callable[[Path], dict[Path, str]] = build_aim_scorecard.render,
    validator: Callable[[Path, bool, bool], list[str]] | None = None,
) -> dict[str, Any]:
    """Finalize only after externally recorded publication/Pages/registry evidence."""

    root = root.resolve()
    ledger_path, _ = safe_relative(
        root, "provenance/activity-ledger.jsonl", "activity ledger"
    )
    ledger_schema_path, _ = safe_relative(
        root, "provenance/activity-ledger.schema.json", "activity-ledger schema"
    )
    if not ledger_path.is_file() or not ledger_schema_path.is_file():
        raise PromotionError("finalization requires regular activity-ledger and schema files")
    with append_activity.ledger_lock(ledger_path):
        return _finalize_release_locked(
            root,
            provenance_builder=provenance_builder,
            aim_renderer=aim_renderer,
            validator=validator,
        )


def _finalize_release_locked(
    root: Path, *,
    provenance_builder: Callable[[Path, str], dict[str, Any]] | None,
    aim_renderer: Callable[[Path], dict[Path, str]],
    validator: Callable[[Path, bool, bool], list[str]] | None,
) -> dict[str, Any]:
    """Finalize or recover a crash checkpoint while the ledger side lock is held."""

    manifest_path, _ = safe_relative(root, MANIFEST_PATH.as_posix(), "release manifest")
    status_path, _ = safe_relative(root, STATUS_PATH.as_posix(), "release status")
    manifest = load_json(manifest_path, "candidate release manifest")
    status = load_json(status_path, "candidate release status")
    promotion = manifest.get("promotion")
    snapshot_id = str(manifest.get("snapshot", {}).get("id") or "")
    candidate_status = machine_candidate_status(snapshot_id)
    final_reason = check_release.MACHINE_FINAL_REASON
    expected_final_status = dict(candidate_status)
    expected_final_status["promotion_finalized"] = True
    expected_final_status["reason"] = final_reason
    recovering = isinstance(promotion, dict) and promotion.get("finalized") is True
    if recovering:
        candidate_manifest = dict(manifest)
        candidate_promotion = dict(promotion)
        candidate_manifest_sha = candidate_promotion.pop("candidate_manifest_sha256", None)
        candidate_status_sha = candidate_promotion.pop("candidate_status_sha256", None)
        candidate_promotion["finalized"] = False
        candidate_manifest["promotion"] = candidate_promotion
        if (
            not check_release._valid_sha256(candidate_manifest_sha)
            or not check_release._valid_sha256(candidate_status_sha)
            or hashlib.sha256(canonical_bytes(candidate_manifest)).hexdigest()
            != candidate_manifest_sha
        ):
            raise PromotionError("partial finalization does not preserve the exact candidate manifest")
        if status == candidate_status:
            if hashlib.sha256(canonical_bytes(status)).hexdigest() != candidate_status_sha:
                raise PromotionError("partial finalization does not preserve the exact candidate status")
        elif status == expected_final_status:
            if hashlib.sha256(canonical_bytes(candidate_status)).hexdigest() != candidate_status_sha:
                raise PromotionError("partial finalization candidate-status hash cannot be reconstructed")
        else:
            raise PromotionError("partial finalization status is not recoverable")
        if status == expected_final_status:
            final_errors = check_release.validate_release(
                root, require_publication_ready=True, require_finalized=True
            )
            if not final_errors:
                return {"manifest": manifest, "status": status}
    if (
        manifest.get("release_kind") != "machine_release_candidate"
        or manifest.get("publication_ready") is not True
        or status.get("status") != "machine_release_candidate"
        or not isinstance(promotion, dict)
        or promotion.get("finalized") not in {False, True}
        or status.get("promotion_finalized") not in {False, True}
        or (not recovering and (promotion.get("finalized") is not False or status != candidate_status))
        or (recovering and status != candidate_status and status != expected_final_status)
    ):
        raise PromotionError("finalize requires an unfinalized, publication-ready machine release candidate")
    # Appending the external publication terminal necessarily makes the old
    # 10-of-11 candidate provenance stale. Build strict 11-of-11 provenance
    # first, then validate every final control together inside the transaction.
    if provenance_builder is None:
        provenance = build_provenance_evidence(root, snapshot_id, finalized=True)
    else:
        provenance = provenance_builder(root, snapshot_id)
    provenance_errors = check_release._provenance_errors(
        root, provenance, manifest["snapshot"], require_finalized=True
    )
    if provenance_errors:
        raise PromotionError("strict post-publication provenance failed: " + "; ".join(provenance_errors))
    if recovering:
        final_manifest = manifest
        final_status = expected_final_status
    else:
        final_manifest = dict(manifest)
        final_promotion = dict(promotion)
        final_promotion.update(
            {
                "finalized": True,
                "candidate_manifest_sha256": sha256(manifest_path),
                "candidate_status_sha256": sha256(status_path),
            }
        )
        final_manifest["promotion"] = final_promotion
        final_status = expected_final_status
    artifacts = manifest["artifacts"]
    provenance_path, _ = safe_relative(root, str(artifacts["provenance_validation"]), "provenance evidence")
    aim_path, _ = safe_relative(root, str(artifacts["aim_assessment"]), "aim assessment")
    report_path, _ = safe_relative(root, "reports/aim-scorecard.md", "aim scorecard")
    transaction_paths = [manifest_path, status_path, provenance_path, aim_path, report_path]
    final_validator = validator or (
        lambda candidate_root, publication, finalized: check_release.validate_release(
            candidate_root,
            require_publication_ready=publication,
            require_finalized=finalized,
        )
    )

    def continuation() -> dict[Path, bytes]:
        rendered = aim_renderer(root)
        if aim_path not in rendered:
            raise PromotionError("aim renderer did not produce the manifest-declared assessment path")
        return {path: content.encode("utf-8") for path, content in rendered.items()}

    transactional_update(
        transaction_paths,
        {
            manifest_path: canonical_bytes(final_manifest),
            status_path: canonical_bytes(final_status),
            provenance_path: canonical_bytes(provenance),
        },
        continuation,
        lambda: final_validator(root, True, True),
    )
    return {"manifest": final_manifest, "status": final_status}


def add_artifact_arguments(parser: argparse.ArgumentParser) -> None:
    for name in (
        "question_contract",
        "evaluation",
        "citation_verification",
        "semantic_validation",
        "rights_privacy_audit",
        "clean_room_reproduction",
        "browser_validation",
        "security_scan",
        "sbom",
    ):
        parser.add_argument("--" + name.replace("_", "-"), dest=name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    stage = subparsers.add_parser("stage", help="write a non-publishable full-corpus checkpoint")
    stage.add_argument("--snapshot", required=True)
    stage.add_argument("--reconciliation", required=True)
    stage.add_argument("--bundle", default="bundle")
    stage.add_argument("--source", required=True, help="frozen hydrated source records")
    stage.add_argument("--generated-at", help="deterministic bundle generation timestamp")
    stage.add_argument("--compiler", choices=("auto", "memory", "disk"), default="disk")
    add_artifact_arguments(stage)
    promote = subparsers.add_parser("promote", help="validate evidence and transactionally emit machine RC")
    add_artifact_arguments(promote)
    subparsers.add_parser(
        "finalize",
        help="require strict 11-of-11 post-publication provenance and finalize the machine RC",
    )
    args = parser.parse_args(argv)
    overrides = {
        name: getattr(args, name, None)
        for name in ARTIFACT_DEFAULTS
        if hasattr(args, name) and name not in {"bundle", "reconciliation"}
    }
    try:
        if args.command == "stage":
            result = stage_release(
                args.root,
                snapshot=args.snapshot,
                reconciliation_relative=args.reconciliation,
                bundle_relative=args.bundle,
                source_relative=args.source,
                generated_at=args.generated_at,
                compiler=args.compiler,
                overrides=overrides,
            )
            print(f"staged non-publishable full-corpus checkpoint: {result['manifest']['release_id']}")
        elif args.command == "promote":
            result = promote_release(args.root, overrides=overrides)
            print(f"promoted machine release candidate: {result['manifest']['release_id']}")
        else:
            result = finalize_release(args.root)
            print(f"finalized machine release candidate: {result['manifest']['release_id']}")
    except (PromotionError, OSError, KeyError, TypeError, check_release.ReleaseDocumentError) as exc:
        print(f"release promotion failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
