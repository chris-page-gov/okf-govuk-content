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
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_aim_scorecard  # noqa: E402
import check_provenance  # noqa: E402
import check_release  # noqa: E402


DISALLOWED = ("fixture", "sample", "capacity", "development", "test")
ALL_GATES = check_release.RELEASE_FLAGS
MANIFEST_PATH = Path(check_release.MANIFEST_RELATIVE)
STATUS_PATH = Path(check_release.STATUS_RELATIVE)
CHECKPOINT_MARKER = "AFHF_GOVUK_OKF_FULL_CORPUS_CHECKPOINT_V1"
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
    data_manifest_value = descriptor.get("entrypoints", {}).get("data_manifest")
    if not isinstance(data_manifest_value, str):
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


def stage_release(
    root: Path, *, snapshot: str, reconciliation_relative: str, bundle_relative: str = "bundle",
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
    manifest_path = root / MANIFEST_PATH
    status_path = root / STATUS_PATH
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
        "python_tests_run": int(match.group(1)),
        "commands": commands,
        "code_tree": {"paths": list(check_release.TEST_INPUT_PATHS), "sha256": after},
    }


def validate_pre_promotion(
    root: Path, manifest: dict[str, Any], provenance: dict[str, Any], tests: dict[str, Any]
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
            ("clean_room_reproduction", "clean_room_reproduction_passed", None),
        ):
            path = check_release._resolve_relative(root, artifacts.get(name), name)
            assert path is not None
            evidence = check_release._load_json(path, name)
            if evidence.get(pass_field) is not True and (alternative is None or evidence.get(alternative) is not True):
                errors.append(f"{name} evidence did not pass")
            if check_release._artifact_snapshot(evidence) != snapshot["id"]:
                errors.append(f"{name} snapshot differs")
            if name == "clean_room_reproduction" and sbom_path:
                errors.extend(check_release._clean_room_errors(evidence, sbom_path, promotion))
        errors.extend(check_release._provenance_errors(root, provenance, snapshot))
        errors.extend(check_release._test_evidence_errors(root, tests, snapshot))
    except check_release.ReleaseDocumentError as exc:
        errors.append(str(exc))
    return errors


def promote_release(
    root: Path, *, overrides: dict[str, str | None] | None = None,
    provenance_builder: Callable[[Path, str], dict[str, Any]] = build_provenance_evidence,
    test_builder: Callable[[Path, str], dict[str, Any]] = build_test_evidence,
    aim_renderer: Callable[[Path], dict[Path, str]] = build_aim_scorecard.render,
    validator: Callable[[Path, bool], list[str]] | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    manifest_path = root / MANIFEST_PATH
    status_path = root / STATUS_PATH
    staged_manifest = load_json(manifest_path, "staged release manifest")
    staged_status = load_json(status_path, "staged release status")
    snapshot = staged_manifest.get("snapshot")
    if (
        staged_manifest.get("release_kind") != "full_corpus_checkpoint"
        or staged_manifest.get("publication_ready") is not False
        or staged_status.get("status") != "checkpoint"
        or staged_status.get("publication_ready") is not False
        or not isinstance(snapshot, dict)
        or snapshot.get("kind") != "full_corpus"
        or snapshot.get("sampled") is not False
    ):
        raise PromotionError("promote requires a non-publishable staged full-corpus checkpoint")
    snapshot_id = str(snapshot.get("id") or "")
    validate_snapshot_id(snapshot_id)
    staged_manifest_sha = sha256(manifest_path)
    staged_status_sha = sha256(status_path)
    artifacts = artifact_paths(overrides or {})
    artifacts.update({key: value for key, value in staged_manifest["artifacts"].items() if (overrides or {}).get(key) is None})
    reconciliation_path, _ = safe_relative(root, str(artifacts["reconciliation"]), "reconciliation")
    reconciliation = load_json(reconciliation_path, "closing reconciliation")
    descriptor, data_manifest = load_bundle_contract(root, str(artifacts["bundle"]))
    publication_records = validate_reconciliation(reconciliation, descriptor, data_manifest, snapshot_id)
    if staged_manifest.get("counts", {}).get("publication_records") != publication_records:
        raise PromotionError("staged manifest publication count no longer matches bundle and reconciliation")
    promotion = {
        "schema": "afhf-govuk-okf-two-stage-promotion.v1",
        "from": "full_corpus_checkpoint",
        "staged_manifest_sha256": staged_manifest_sha,
        "staged_status_sha256": staged_status_sha,
        "finalized": False,
    }
    provenance = provenance_builder(root, snapshot_id)
    tests = test_builder(root, snapshot_id)
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
    final_status = {
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
        "reason": "Every machine release-candidate gate passed; human research remains not authorised and UI of choice remains not yet testable.",
    }
    preflight_errors = validate_pre_promotion(root, final_manifest, provenance, tests)
    if preflight_errors:
        raise PromotionError("promotion evidence failed: " + "; ".join(preflight_errors))

    provenance_path, _ = safe_relative(root, str(artifacts["provenance_validation"]), "provenance evidence")
    tests_path, _ = safe_relative(root, str(artifacts["full_repository_tests"]), "full test evidence")
    aim_path, _ = safe_relative(root, str(artifacts["aim_assessment"]), "aim assessment")
    report_path = root / "reports/aim-scorecard.md"
    transaction_paths = [manifest_path, status_path, provenance_path, tests_path, aim_path, report_path]
    final_validator = validator or (lambda candidate_root, publication: check_release.validate_release(candidate_root, require_publication_ready=publication))

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
            tests_path: canonical_bytes(tests),
        },
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
    manifest_path = root / MANIFEST_PATH
    status_path = root / STATUS_PATH
    manifest = load_json(manifest_path, "candidate release manifest")
    status = load_json(status_path, "candidate release status")
    promotion = manifest.get("promotion")
    if (
        manifest.get("release_kind") != "machine_release_candidate"
        or manifest.get("publication_ready") is not True
        or status.get("status") != "machine_release_candidate"
        or not isinstance(promotion, dict)
        or promotion.get("finalized") is not False
        or status.get("promotion_finalized") is not False
    ):
        raise PromotionError("finalize requires an unfinalized, publication-ready machine release candidate")
    candidate_errors = check_release.validate_release(root, require_publication_ready=True)
    if candidate_errors:
        raise PromotionError("candidate release no longer validates: " + "; ".join(candidate_errors))
    snapshot_id = manifest["snapshot"]["id"]
    if provenance_builder is None:
        provenance = build_provenance_evidence(root, snapshot_id, finalized=True)
    else:
        provenance = provenance_builder(root, snapshot_id)
    provenance_errors = check_release._provenance_errors(
        root, provenance, manifest["snapshot"], require_finalized=True
    )
    if provenance_errors:
        raise PromotionError("strict post-publication provenance failed: " + "; ".join(provenance_errors))
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
    final_status = dict(status)
    final_status["promotion_finalized"] = True
    final_status["reason"] = (
        "Machine release candidate finalized after the externally recorded publication, Pages and Explorer registry terminal event; human research remains not authorised."
    )
    artifacts = manifest["artifacts"]
    provenance_path, _ = safe_relative(root, str(artifacts["provenance_validation"]), "provenance evidence")
    aim_path, _ = safe_relative(root, str(artifacts["aim_assessment"]), "aim assessment")
    report_path = root / "reports/aim-scorecard.md"
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
