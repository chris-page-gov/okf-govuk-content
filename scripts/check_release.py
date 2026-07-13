#!/usr/bin/env python3
"""Validate release evidence and fail closed before public deployment.

The default mode validates a checkpoint without pretending it is publishable.
``--publication-ready`` applies the complete machine-release-candidate gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.rights_audit import validate_audit_evidence  # noqa: E402

MANIFEST_RELATIVE = "release/manifest.yaml"
STATUS_RELATIVE = "release/status.json"
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
DISALLOWED_SNAPSHOT_MARKERS = ("fixture", "sample", "capacity", "development", "test")
MACHINE_MARKER = "AFHF_GOVUK_OKF_MACHINE_RELEASE_CANDIDATE_V1"
FULL_MARKER = "AFHF_GOVUK_OKF_RESEARCH_IMPLEMENTATION_COMPLETE_V1"
MACHINE_CANDIDATE_REASON = "Every machine release-candidate gate passed; human research remains not authorised and UI of choice remains not yet testable."
MACHINE_FINAL_REASON = "Machine release candidate finalized after the externally recorded publication, Pages and Explorer registry terminal event; human research remains not authorised."
HUMAN_STATUSES = {"not_authorised", "blocked", "not_yet_testable", "completed"}
AIM_STATUSES = {"fulfilled", "partly_fulfilled", "not_fulfilled", "not_yet_testable"}
TEST_INPUT_PATHS = (
    "pyproject.toml",
    "uv.lock",
    "src",
    "scripts",
    "tests",
    "semantic/package.json",
    "semantic/package-lock.json",
    "semantic/tests",
    "explorer/package.json",
    "explorer/src",
    "explorer/tests",
)
SECURITY_SCAN_INPUT_PATHS = (
    ".github",
    "orchestration",
    "pyproject.toml",
    "uv.lock",
    "src",
    "scripts",
    "tests",
    "semantic/package.json",
    "semantic/package-lock.json",
    "semantic/rdfc-equivalence.mjs",
    "semantic/rdfc-stream.mjs",
    "semantic/tests",
    "explorer/package.json",
    "explorer/src",
    "explorer/tests",
)
CLEAN_ROOM_INPUT_PATHS = (
    "LICENSE.md",
    "governance/launch-manifest.yaml",
    "orchestration/models.lock.yaml",
    "pyproject.toml",
    "provenance/activity-ledger.jsonl",
    "provenance/activity-ledger.schema.json",
    "provenance/reproduction-declarations.json",
    "provenance/source-request-budget.json",
    "release/provenance-validation.json",
    "release/manifest.yaml",
    "research/official-source-audit.md",
    "research/source-constraints.json",
    "src",
    "scripts/build_bundle.py",
    "scripts/build_checksums.py",
    "scripts/build_sbom.py",
    "scripts/check_publication.py",
    "scripts/check_provenance.py",
    "scripts/check_release.py",
    "scripts/reproduce_release.py",
    "semantic/README.md",
    "semantic/context",
    "semantic/crosswalks",
    "semantic/package-lock.json",
    "semantic/profile",
    "semantic/schemas",
    "semantic/shapes",
    "explorer/src",
    "uv.lock",
)
CLEAN_ROOM_MUTABLE_INPUT_PATHS = {
    "provenance/activity-ledger.jsonl",
    "release/provenance-validation.json",
    "release/manifest.yaml",
}


class ReleaseDocumentError(ValueError):
    """Raised when a release document cannot be loaded safely."""


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _entrypoint_path(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("path"), str):
        return value["path"]
    return ""


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReleaseDocumentError(f"missing {label}: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseDocumentError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseDocumentError(f"{label} must be a JSON object: {path}")
    return value


def _resolve_relative(root: Path, value: object, label: str, *, required: bool = True) -> Path | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReleaseDocumentError(f"{label} must be a non-empty repository-relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ReleaseDocumentError(f"unsafe {label}: {value}")
    resolved_root = root.resolve()
    candidate = resolved_root / relative
    cursor = resolved_root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ReleaseDocumentError(f"{label} cannot traverse a symlink: {value}")
    resolved = candidate.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ReleaseDocumentError(f"{label} escapes the repository: {value}")
    return resolved


def _checksum_errors(bundle: Path, manifest_path: Path) -> list[str]:
    errors: list[str] = []
    checksum = _load_json(manifest_path, "checksum manifest")
    if checksum.get("schema") != "okf-checksums.v1" or checksum.get("algorithm") != "sha256":
        errors.append("checksum manifest does not use okf-checksums.v1 with sha256")
        return errors
    rows = checksum.get("files")
    if not isinstance(rows, list):
        errors.append("checksum manifest files must be a list")
        return errors
    actual_files = [
        path
        for path in sorted(bundle.rglob("*"))
        if path.is_file() and path.name not in {"checksums.json", ".DS_Store"}
    ]
    expected_names = [path.relative_to(bundle).as_posix() for path in actual_files]
    row_names = [row.get("path") if isinstance(row, dict) else None for row in rows]
    if checksum.get("file_count") != len(rows):
        errors.append("checksum file_count does not equal the number of rows")
    if len(set(row_names)) != len(row_names):
        errors.append("checksum paths are not unique")
    if row_names != expected_names:
        errors.append("checksum manifest does not cover exactly the published bundle files")
        return errors
    for path, row in zip(actual_files, rows, strict=True):
        if not isinstance(row, dict):
            errors.append(f"invalid checksum row for {path.name}")
            continue
        payload = path.read_bytes()
        if row.get("bytes") != len(payload):
            errors.append(f"checksum byte count differs: {row.get('path')}")
        if row.get("sha256") != hashlib.sha256(payload).hexdigest():
            errors.append(f"checksum differs: {row.get('path')}")
    return errors


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _document_sha256(document: dict[str, Any]) -> str:
    payload = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _tree_sha256(root: Path, relative_paths: tuple[str, ...] = TEST_INPUT_PATHS) -> str:
    """Hash the exact code/test inputs covered by full-repository test evidence."""

    rows: list[bytes] = []
    for relative_value in relative_paths:
        path = root / relative_value
        candidates = [path] if path.is_file() else sorted(path.rglob("*")) if path.is_dir() else []
        for candidate in candidates:
            if not candidate.is_file() or candidate.name == ".DS_Store" or "__pycache__" in candidate.parts:
                continue
            relative = candidate.relative_to(root).as_posix()
            rows.append(f"{relative}\0{_file_sha256(candidate)}\n".encode("utf-8"))
    return hashlib.sha256(b"".join(rows)).hexdigest()


def _content_summary(path: Path, label: str) -> dict[str, Any]:
    ignored = {".DS_Store", "__pycache__", "node_modules"}

    def included(candidate: Path) -> bool:
        return (
            candidate.name not in ignored
            and not candidate.name.startswith("._")
            and candidate.suffix not in {".pyc", ".pyo"}
        )

    if path.is_symlink():
        raise ReleaseDocumentError(f"{label} cannot be a symlink: {path}")
    if path.is_file():
        candidates = [(Path(path.name), path)]
    elif path.is_dir():
        entries = sorted(path.rglob("*"))
        symlinks = [candidate for candidate in entries if candidate.is_symlink()]
        if symlinks:
            raise ReleaseDocumentError(
                f"{label} cannot contain symlinks: {symlinks[0].relative_to(path)}"
            )
        candidates = [
            (candidate.relative_to(path), candidate)
            for candidate in entries
            if candidate.is_file()
            and all(included(part) for part in candidate.relative_to(path).parents)
            and included(candidate)
        ]
    else:
        raise ReleaseDocumentError(f"{label} is missing: {path}")
    rows = [
        {
            "path": relative.as_posix(),
            "bytes": candidate.stat().st_size,
            "sha256": _file_sha256(candidate),
        }
        for relative, candidate in candidates
    ]
    canonical = json.dumps(
        rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return {
        "file_count": len(rows),
        "bytes": sum(row["bytes"] for row in rows),
        "tree_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _single_file_summary(name: str, size: int, content_sha256: str) -> dict[str, Any]:
    row = {"path": name, "bytes": size, "sha256": content_sha256}
    canonical = json.dumps(
        [row], ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return {
        "file_count": 1,
        "bytes": size,
        "tree_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _source_binding(path: Path, root: Path) -> dict[str, Any]:
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ReleaseDocumentError(f"frozen reproduction source escapes the repository: {path}") from exc
    kind = "file" if path.is_file() else "directory" if path.is_dir() else None
    if kind is None:
        raise ReleaseDocumentError(f"frozen reproduction source is missing: {path}")
    binding: dict[str, Any] = {
        "path": relative,
        "kind": kind,
        **_content_summary(path, "frozen reproduction source"),
    }
    if kind == "file":
        binding["content_sha256"] = _file_sha256(path)
    return binding


def _clean_room_input_errors(evidence: dict[str, Any], root: Path) -> list[str]:
    """Recompute immutable inputs copied into the clean-room workspace."""

    errors: list[str] = []
    inputs = evidence.get("inputs")
    components = inputs.get("components") if isinstance(inputs, dict) else None
    expected_paths = [*CLEAN_ROOM_INPUT_PATHS, "frozen_source"]
    if (
        not isinstance(inputs, dict)
        or inputs.get("schema") != "afhf-govuk-okf-reproduction-input-manifest.v1"
        or not isinstance(components, list)
        or inputs.get("component_count") != len(expected_paths)
    ):
        return ["clean-room input manifest is incomplete"]
    component_paths = [row.get("path") if isinstance(row, dict) else None for row in components]
    if component_paths != expected_paths:
        errors.append("clean-room input manifest does not cover the exact reproduction inputs")
        return errors
    canonical = json.dumps(
        components, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if inputs.get("tree_sha256") != hashlib.sha256(canonical).hexdigest():
        errors.append("clean-room input-manifest tree hash differs")
    for relative, row in zip(CLEAN_ROOM_INPUT_PATHS, components[:-1], strict=True):
        if relative in CLEAN_ROOM_MUTABLE_INPUT_PATHS:
            continue
        try:
            path = _resolve_relative(root, relative, "clean-room reproduction input")
            assert path is not None
            expected = {"path": relative, **_content_summary(path, "clean-room reproduction input")}
            if row != expected:
                errors.append(f"clean-room immutable input tree differs: {relative}")
        except (OSError, ValueError, ReleaseDocumentError) as exc:
            errors.append(str(exc))
    source_binding = evidence.get("source_binding")
    frozen_row = components[-1]
    if not isinstance(source_binding, dict) or frozen_row != {
        "path": "frozen_source",
        "source": evidence.get("source"),
        **{
            key: source_binding.get(key)
            for key in ("file_count", "bytes", "tree_sha256")
        },
    }:
        errors.append("clean-room frozen-source input row differs from its content binding")
    component_by_path = {
        row["path"]: row for row in components if isinstance(row, dict)
    }
    release_control = evidence.get("release_control")
    manifest_component = component_by_path.get("release/manifest.yaml")
    staged_manifest_sha = (
        release_control.get("manifest_sha256")
        if isinstance(release_control, dict)
        else None
    )
    if (
        not isinstance(manifest_component, dict)
        or not isinstance(manifest_component.get("bytes"), int)
        or not _valid_sha256(staged_manifest_sha)
        or manifest_component
        != {
            "path": "release/manifest.yaml",
            **_single_file_summary(
                "manifest.yaml", manifest_component["bytes"], staged_manifest_sha
            ),
        }
    ):
        errors.append("clean-room staged-manifest input row differs from its dedicated hash")
    return errors


def _sbom_errors(root: Path, sbom_path: Path) -> list[str]:
    errors: list[str] = []
    sbom = _load_json(sbom_path, "CycloneDX SBOM")
    if sbom.get("bomFormat") != "CycloneDX" or sbom.get("specVersion") != "1.6":
        errors.append("SBOM is not CycloneDX 1.6")
        return errors
    components = sbom.get("components")
    dependencies = sbom.get("dependencies")
    if not isinstance(components, list) or not components:
        errors.append("SBOM has no components")
        return errors
    if not isinstance(dependencies, list) or not dependencies:
        errors.append("SBOM has no dependency graph")
        return errors
    references = {
        row.get("bom-ref")
        for row in components
        if isinstance(row, dict) and isinstance(row.get("bom-ref"), str)
    }
    root_reference = sbom.get("metadata", {}).get("component", {}).get("bom-ref")
    if not isinstance(root_reference, str):
        errors.append("SBOM root component has no bom-ref")
    else:
        references.add(root_reference)
    dependency_references = set()
    for row in dependencies:
        if not isinstance(row, dict) or not isinstance(row.get("ref"), str):
            errors.append("SBOM dependency row is invalid")
            continue
        dependency_references.add(row["ref"])
        children = row.get("dependsOn")
        if not isinstance(children, list) or any(child not in references for child in children):
            errors.append(f"SBOM dependency has an unresolved component: {row.get('ref')}")
    if references and dependency_references != references:
        errors.append("SBOM dependency graph does not cover every component")
    properties = sbom.get("metadata", {}).get("properties")
    property_values = {
        row.get("name"): row.get("value")
        for row in properties or []
        if isinstance(row, dict)
    }
    locks = (
        ("govuk-okf:lock:uv.sha256", root / "uv.lock"),
        (
            "govuk-okf:lock:semantic-package-lock.sha256",
            root / "semantic" / "package-lock.json",
        ),
        ("govuk-okf:input:pyproject.sha256", root / "pyproject.toml"),
    )
    for name, path in locks:
        if not path.is_file():
            errors.append(f"SBOM lock input is missing: {path.relative_to(root)}")
        elif property_values.get(name) != _file_sha256(path):
            errors.append(f"SBOM lock digest differs: {name}")
    return errors


def _clean_room_errors(
    evidence: dict[str, Any],
    root: Path,
    sbom_path: Path,
    promotion: dict[str, Any] | None = None,
    *,
    bundle_path: Path | None = None,
    evidence_path: Path | None = None,
    test_evidence_path: Path | None = None,
    allow_missing_frozen_source: bool = False,
) -> list[str]:
    errors: list[str] = []
    if evidence.get("schema") != "afhf-govuk-okf-clean-room-reproduction.v1":
        errors.append("clean-room evidence schema is invalid")
    if evidence.get("snapshot_kind") != "full_corpus" or evidence.get("sampled") is not False:
        errors.append("clean-room release evidence is not for an unsampled full corpus")
    if evidence.get("release_inputs_passed") is not True:
        errors.append("clean-room full-corpus release inputs did not pass")
    if evidence.get("fixture_reproduction_passed") is not True:
        errors.append("clean-room deterministic reproduction did not pass")
    validators = evidence.get("validators")
    outputs = evidence.get("outputs")
    checkout = evidence.get("checkout")
    if not isinstance(validators, dict) or validators.get("passed") is not True:
        errors.append("clean-room validators did not pass")
    bundle_evidence = outputs.get("bundle") if isinstance(outputs, dict) else None
    if not isinstance(bundle_evidence, dict) or bundle_evidence.get("exact_match") is not True:
        errors.append("clean-room reproduced bundle did not exactly match")
    elif bundle_path is None:
        errors.append("clean-room released bundle path is missing")
    else:
        try:
            if bundle_evidence.get("expected") != _content_summary(
                bundle_path, "released bundle"
            ):
                errors.append("clean-room evidence is not bound to the current released bundle tree")
        except (OSError, ValueError, ReleaseDocumentError) as exc:
            errors.append(str(exc))
    sbom_evidence = outputs.get("sbom") if isinstance(outputs, dict) else None
    if not isinstance(sbom_evidence, dict) or sbom_evidence.get("exact_match") is not True:
        errors.append("clean-room reproduced SBOM did not exactly match")
    elif sbom_evidence.get("expected_sha256") != _file_sha256(sbom_path):
        errors.append("clean-room evidence is not bound to the released SBOM")
    if not isinstance(checkout, dict) or checkout.get("unchanged") is not True:
        errors.append("clean-room verification mutated declared checkout inputs or outputs")
    errors.extend(_clean_room_input_errors(evidence, root))
    network = evidence.get("network")
    if (
        not isinstance(network, dict)
        or network.get("required") is not False
        or network.get("official_source_requests") != 0
        or network.get("external_model_requests") != 0
    ):
        errors.append("clean-room run lacks a zero-network declaration")
    test_evidence = evidence.get("test_evidence")
    if (
        not isinstance(test_evidence, dict)
        or test_evidence.get("scope") != "full_repository"
        or test_evidence.get("tests_passed") is not True
    ):
        errors.append("clean-room release lacks passing full-repository test evidence")
    elif test_evidence_path is not None:
        if not test_evidence_path.is_file():
            errors.append("clean-room full-repository test evidence is missing")
        elif test_evidence.get("sha256") != _file_sha256(test_evidence_path):
            errors.append("clean-room evidence is not bound to the generated full-repository tests")
    if evidence_path is not None and test_evidence_path is not None:
        try:
            ledger_path = _resolve_relative(
                root, "provenance/activity-ledger.jsonl", "activity ledger"
            )
            assert ledger_path is not None
            terminals = []
            for number, line in enumerate(
                ledger_path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ReleaseDocumentError(
                        f"invalid activity-ledger row {number}: {exc}"
                    ) from exc
                if row.get("activity_id") == "ACT-F2-CLEAN-ROOM-RC-TERMINAL-001":
                    terminals.append(row)
            if terminals:
                if len(terminals) != 1:
                    errors.append("clean-room activity ledger has duplicate terminal rows")
                else:
                    terminal_outputs = {
                        row.get("path"): row.get("sha256")
                        for row in terminals[0].get("outputs", [])
                        if isinstance(row, dict)
                    }
                    expected_outputs = {
                        evidence_path.relative_to(root).as_posix(): _file_sha256(
                            evidence_path
                        ),
                        test_evidence_path.relative_to(root).as_posix(): _file_sha256(
                            test_evidence_path
                        ),
                        sbom_path.relative_to(root).as_posix(): _file_sha256(sbom_path),
                    }
                    if terminal_outputs != expected_outputs:
                        errors.append(
                            "clean-room terminal output hashes differ from release artefacts"
                        )
        except (OSError, ValueError, ReleaseDocumentError) as exc:
            errors.append(str(exc))
    if promotion is not None:
        release_control = evidence.get("release_control")
        reproduction = promotion.get("reproduction")
        if (
            not isinstance(release_control, dict)
            or release_control.get("manifest_kind") != "full_corpus_checkpoint"
            or release_control.get("requested_release_kind")
            not in {"machine_release_candidate", "full_programme"}
            or release_control.get("prospective") is not True
            or release_control.get("manifest_sha256")
            != promotion.get("staged_manifest_sha256")
        ):
            errors.append("clean-room evidence is not bound to the exact staged release manifest")
        if (
            not isinstance(release_control, dict)
            or release_control.get("status_sha256") != promotion.get("staged_status_sha256")
        ):
            errors.append("clean-room evidence is not bound to the exact staged release status")
        if (
            not isinstance(reproduction, dict)
            or evidence.get("source") != reproduction.get("source")
            or evidence.get("generated_at") != reproduction.get("generated_at")
            or evidence.get("compiler") != reproduction.get("compiler")
            or evidence.get("source_binding") != reproduction.get("source_binding")
            or not isinstance(release_control, dict)
            or release_control.get("source_binding") != reproduction.get("source_binding")
        ):
            errors.append("clean-room evidence differs from the immutable reproduction contract")
        elif isinstance(reproduction.get("source"), str):
            try:
                source_path = _resolve_relative(
                    root, reproduction["source"], "frozen reproduction source"
                )
                source_binding = reproduction.get("source_binding")
                valid_archived_binding = (
                    isinstance(source_binding, dict)
                    and source_binding.get("path") == reproduction.get("source")
                    and source_binding.get("kind") in {"file", "directory"}
                    and isinstance(source_binding.get("file_count"), int)
                    and not isinstance(source_binding.get("file_count"), bool)
                    and source_binding.get("file_count", -1) >= 1
                    and isinstance(source_binding.get("bytes"), int)
                    and not isinstance(source_binding.get("bytes"), bool)
                    and source_binding.get("bytes", -1) >= 0
                    and _valid_sha256(source_binding.get("tree_sha256"))
                    and (
                        source_binding.get("kind") != "file"
                        or _valid_sha256(source_binding.get("content_sha256"))
                    )
                )
                if source_path and source_path.exists() and _source_binding(
                    source_path, root
                ) != source_binding:
                    errors.append("clean-room frozen-source content/tree binding differs")
                elif source_path and not source_path.exists() and not (
                    allow_missing_frozen_source and valid_archived_binding
                ):
                    errors.append("frozen reproduction source is missing")
            except (OSError, ValueError, ReleaseDocumentError) as exc:
                errors.append(str(exc))
    return errors


def _rights_errors(
    root: Path,
    evidence: dict[str, Any],
    *,
    require_release: bool,
    allow_missing_corpus_inputs: bool,
) -> list[str]:
    return validate_audit_evidence(
        root,
        evidence,
        require_release=require_release,
        allow_missing_corpus_inputs=allow_missing_corpus_inputs,
    )


def _aim_assessment_errors(evidence: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    """Validate the release-facing, non-self-asserting Gate 11 contract."""

    errors: list[str] = []
    if evidence.get("schema") != "afhf-govuk-okf-aim-assessment.v1":
        errors.append("aim assessment schema is invalid")
    if evidence.get("assessment_tier") not in {"machine_release_candidate", "full_programme"}:
        errors.append("aim assessment is not a final-snapshot assessment tier")
    assessed_snapshot = evidence.get("snapshot")
    if not isinstance(assessed_snapshot, dict):
        errors.append("aim assessment snapshot is missing")
    elif (
        assessed_snapshot.get("release_id") != snapshot.get("id")
        or assessed_snapshot.get("kind") != "full_corpus"
        or assessed_snapshot.get("sampled") is not False
    ):
        errors.append("aim assessment is not bound to the unsampled full release snapshot")
    gate = evidence.get("gate_11")
    if (
        not isinstance(gate, dict)
        or gate.get("passed") is not True
        or gate.get("status") != "passed"
        or gate.get("unmet_check_ids") != []
    ):
        errors.append("aim assessment does not pass Gate 11 with zero unmet checks")
    coverage = evidence.get("coverage")
    if coverage != {"aims": 9, "requirements": 95, "controlling_clauses": 21}:
        errors.append("aim assessment does not cover nine aims, 95 requirements and 21 clauses")
    aims = evidence.get("aims")
    if not isinstance(aims, list) or len(aims) != 9:
        errors.append("aim assessment must contain exactly nine aims")
        return errors
    identifiers = [row.get("aim_id") for row in aims if isinstance(row, dict)]
    if len(identifiers) != 9 or len(set(identifiers)) != 9:
        errors.append("aim assessment aim identifiers are missing or duplicated")
    for index, aim in enumerate(aims):
        if not isinstance(aim, dict):
            errors.append(f"aim assessment row {index} is invalid")
            continue
        if aim.get("status") not in AIM_STATUSES:
            errors.append(f"aim assessment row {index} has an invalid status")
        confidence = aim.get("confidence")
        if not isinstance(confidence, dict) or confidence.get("level") not in {"low", "medium", "high"}:
            errors.append(f"aim assessment row {index} has invalid confidence")
        if not isinstance(aim.get("negative_findings"), list) or not aim["negative_findings"]:
            errors.append(f"aim assessment row {index} has no negative findings or limitations")
        if not isinstance(aim.get("evidence"), list) or not aim["evidence"]:
            errors.append(f"aim assessment row {index} has no exact evidence rows")
    return errors


def _artifact_snapshot(evidence: dict[str, Any]) -> object:
    return evidence.get("snapshot", evidence.get("snapshot_id"))


def _question_contract_errors(path: Path, evidence: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    counts = evidence.get("counts")
    ledger = evidence.get("verification_ledger")
    if evidence.get("schema_version") != 2:
        errors.append("question contract is not the independent v2 verifier output")
    if (
        evidence.get("question_contract_passed") is not True
        or evidence.get("machine_validations_passed") is not True
        or evidence.get("publication_ready_candidate") is not True
        or evidence.get("artifact_tier") != "release_verified"
    ):
        errors.append("question-v2 evidence is not a release-verified passing candidate")
    if not isinstance(counts, dict) or counts.get("questions") != 28_800 or counts.get("validation_errors") != 0:
        errors.append("question-v2 evidence does not contain exactly 28,800 valid questions")
    if (
        not isinstance(ledger, dict)
        or ledger.get("count") != 28_800
        or ledger.get("verified") != 28_800
        or ledger.get("failed") != 0
    ):
        errors.append("question-v2 independent verification ledger is incomplete")
    question_snapshot = evidence.get("snapshot_id")
    contract_path = path.with_name("contract.json")
    if question_snapshot is None and contract_path.is_file():
        contract = _load_json(contract_path, "question-v2 contract")
        question_snapshot = contract.get("snapshot", {}).get("snapshot_id")
        if contract.get("publication_ready_candidate") is not True or contract.get("artifact_tier") != "release_candidate":
            errors.append("question-v2 generator contract is not a release candidate")
        manifest_path = path.with_name("manifest.json")
        if manifest_path.is_file():
            matrix_manifest = _load_json(manifest_path, "question-v2 manifest")
            if evidence.get("manifest_root_sha256") != matrix_manifest.get("root_sha256"):
                errors.append("question-v2 verification report is not bound to its matrix manifest")
    if question_snapshot != snapshot.get("id"):
        errors.append("question-v2 evidence snapshot differs from release snapshot")
    return errors


def _evaluation_errors(evidence: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if (
        evidence.get("mode") != "release"
        or evidence.get("snapshot_id") != snapshot.get("id")
        or evidence.get("questions") != 28_800
        or evidence.get("systems") != 10
        or evidence.get("outcomes") != 288_000
        or evidence.get("all_questions_all_systems_complete") is not True
        or evidence.get("release_question_contract_passed") is not True
        or evidence.get("serialization_invariance", {}).get("passed") is not True
        or evidence.get("agent_evaluation_status") != "completed"
        or evidence.get("machine_evaluation_complete") is not True
        or evidence.get("release_eligible") is not True
    ):
        errors.append("evaluation evidence is not a complete matched 28,800 by 10 release run")
    if evidence.get("human_evaluation_status") not in {"not_authorised", "not_yet_testable"}:
        errors.append("machine evaluation evidence crosses the unauthorised human-study boundary")
    if evidence.get("human_ui_of_choice_status") != "not_yet_testable":
        errors.append("machine evaluation makes an unsupported human UI-of-choice claim")
    return errors


def _browser_errors(evidence: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if (
        evidence.get("schema") != "govuk-okf-explorer-browser-evidence.v1"
        or evidence.get("snapshot") != snapshot.get("id")
        or evidence.get("artifact_tier") != "full_release_snapshot"
        or evidence.get("publication_ready") is not True
        or evidence.get("overall_status") != "automated_full_release_evidence_pass"
        or evidence.get("accessibility", {}).get("pass") is not True
        or evidence.get("routing_and_data", {}).get("pass") is not True
        or evidence.get("performance", {}).get("pass") is not True
        or evidence.get("full_release_gates", {}).get("full_corpus_browser_measurement") != "passed"
        or evidence.get("console_exceptions") != []
    ):
        errors.append("full-release browser accessibility, routing and performance evidence did not pass")
    return errors


def _security_errors(root: Path, evidence: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    findings = evidence.get("findings")
    scanned_commit = evidence.get("scanned_commit")
    if (
        evidence.get("schema") != "afhf-govuk-okf-security-scan.v1"
        or evidence.get("snapshot") != snapshot.get("id")
        or evidence.get("scope") != "full_release_repository"
        or evidence.get("status") != "completed"
        or evidence.get("security_scan_passed") is not True
        or not isinstance(evidence.get("scan_id"), str)
        or not evidence.get("scan_id")
    ):
        errors.append("security scan is not a completed passing full-release scan")
    if (
        not isinstance(scanned_commit, str)
        or len(scanned_commit) != 40
        or any(character not in "0123456789abcdef" for character in scanned_commit)
    ):
        errors.append("security scan has no valid scanned commit")
    code_tree = evidence.get("code_tree")
    if (
        not isinstance(code_tree, dict)
        or code_tree.get("paths") != list(SECURITY_SCAN_INPUT_PATHS)
        or code_tree.get("sha256") != _tree_sha256(root, SECURITY_SCAN_INPUT_PATHS)
    ):
        errors.append("security scan is not bound to current code, automation and tests")
    if not isinstance(findings, dict) or findings.get("critical_open") != 0 or findings.get("high_open") != 0:
        errors.append("security scan has open critical or high findings")
    report = evidence.get("report")
    if not isinstance(report, dict):
        errors.append("security scan has no hash-bound report")
    else:
        try:
            report_path = _resolve_relative(root, report.get("path"), "security report")
            if report_path and report.get("sha256") != _file_sha256(report_path):
                errors.append("security scan report hash differs")
        except ReleaseDocumentError as exc:
            errors.append(str(exc))
    return errors


def _provenance_errors(
    root: Path, evidence: dict[str, Any], snapshot: dict[str, Any], *, require_finalized: bool = False
) -> list[str]:
    errors: list[str] = []
    common_invalid = (
        evidence.get("schema") != "afhf-govuk-okf-provenance-validation.v1"
        or evidence.get("snapshot") != snapshot.get("id")
        or evidence.get("provenance_validation_passed") is not True
        or evidence.get("validation_errors") != []
    )
    if common_invalid:
        errors.append("provenance validation is not a passing snapshot-bound result")
    terminals = evidence.get("required_terminal_events")
    candidate_terminals = evidence.get("candidate_terminal_events")
    if require_finalized:
        if (
            evidence.get("validation_mode") != "release"
            or evidence.get("validation_tier") != "release"
            or evidence.get("release_mode") is not True
            or evidence.get("candidate_mode") is not False
            or evidence.get("release_requirements_satisfied") is not True
            or evidence.get("release_blockers") != []
            or evidence.get("publication_workflow_status") != "completed"
            or not isinstance(terminals, dict)
            or terminals.get("required") != 11
            or terminals.get("satisfied") != 11
            or terminals.get("all_satisfied") is not True
        ):
            errors.append("finalized provenance does not satisfy the strict 11-of-11 release contract")
    else:
        publication_terminal = "ACT-F2-PUBLICATION-REGISTRY-TERMINAL-001"
        if (
            evidence.get("validation_mode") != "candidate"
            or evidence.get("validation_tier") != "candidate"
            or evidence.get("candidate_mode") is not True
            or evidence.get("release_mode") is not False
            or evidence.get("candidate_requirements_satisfied") is not True
            or evidence.get("candidate_blockers") != []
            or evidence.get("publication_workflow_status") != "pending_post_publication"
            or not isinstance(candidate_terminals, dict)
            or candidate_terminals.get("required") != 10
            or candidate_terminals.get("satisfied") != 10
            or candidate_terminals.get("all_satisfied") is not True
            or candidate_terminals.get("excluded_post_publication_terminal_activity_id") != publication_terminal
            or not isinstance(terminals, dict)
            or terminals.get("required") != 11
            or terminals.get("satisfied") != 10
            or terminals.get("all_satisfied") is not False
            or terminals.get("candidate_required") != 10
            or terminals.get("candidate_satisfied") != 10
            or terminals.get("candidate_all_satisfied") is not True
            or terminals.get("pending_post_publication_terminal_activity_id") != publication_terminal
        ):
            errors.append("candidate provenance does not satisfy 10 of 11 terminals with only publication pending")
    request_budget = evidence.get("source_request_budget")
    if (
        not isinstance(request_budget, dict)
        or request_budget.get("status") != "final"
        or not isinstance(request_budget.get("final_shared_request_ceiling"), int)
        or not isinstance(request_budget.get("final_shared_request_count"), int)
        or request_budget.get("final_shared_request_count") < 0
        or request_budget.get("final_shared_request_count")
        > request_budget.get("final_shared_request_ceiling")
        or request_budget.get("included_in_model_cost") is not False
    ):
        errors.append("provenance validation does not contain final source-request accounting")
    hash_chain = evidence.get("hash_chain")
    if (
        not isinstance(hash_chain, dict)
        or hash_chain.get("passed") is not True
        or not isinstance(hash_chain.get("ledger_sha256"), str)
        or not isinstance(hash_chain.get("last_entry_sha256"), str)
        or not isinstance(hash_chain.get("hash_chained_v2_rows"), int)
        or hash_chain.get("hash_chained_v2_rows") < 1
    ):
        errors.append("provenance activity-ledger hash chain did not pass")
    unresolved = evidence.get("unresolved_activity_status")
    if (
        not isinstance(unresolved, dict)
        or unresolved.get("unresolved_pending_final_activity_ids") != []
        or unresolved.get("unresolved_in_progress_activity_ids") != []
    ):
        errors.append("provenance retains unresolved pending or in-progress activities")
    paid = evidence.get("external_paid_model_usage")
    if paid != {
        "api_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_gbp": 0.0,
        "totals_complete": True,
    }:
        errors.append("provenance external paid-model totals are not exact zero")
    if evidence.get("fallbacks", {}).get("count", 0) < 4 or evidence.get(
        "source_access_restrictions", {}
    ).get("count", 0) < 4:
        errors.append("provenance fallback or source-access coverage is incomplete")
    required = {
        "activity_ledger_sha256": "provenance/activity-ledger.jsonl",
        "activity_schema_sha256": "provenance/activity-ledger.schema.json",
        "declarations_sha256": "provenance/reproduction-declarations.json",
        "source_request_budget_sha256": "provenance/source-request-budget.json",
        "validator_sha256": "scripts/check_provenance.py",
        "launch_manifest_sha256": "governance/launch-manifest.yaml",
        "model_lock_sha256": "orchestration/models.lock.yaml",
    }
    inputs = evidence.get("inputs")
    if not isinstance(inputs, dict) or not set(required) <= set(inputs):
        errors.append("provenance evidence omits validator or ledger inputs")
        return errors
    for field, relative in sorted(required.items()):
        try:
            path = _resolve_relative(root, relative, "provenance input")
            if path and inputs.get(field) != _file_sha256(path):
                errors.append(f"provenance input hash differs: {relative}")
        except ReleaseDocumentError as exc:
            errors.append(str(exc))
    return errors


def _test_evidence_errors(root: Path, evidence: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if (
        evidence.get("schema") != "afhf-govuk-okf-full-repository-tests.v1"
        or evidence.get("snapshot") != snapshot.get("id")
        or evidence.get("scope") != "full_repository"
        or evidence.get("passed") is not True
        or not isinstance(evidence.get("python_tests_run"), int)
        or evidence.get("python_tests_run") < 1
    ):
        errors.append("full-repository test evidence is not passing and snapshot-bound")
    commands = evidence.get("commands")
    if not isinstance(commands, list) or len(commands) < 3 or any(
        not isinstance(row, dict) or row.get("returncode") != 0 for row in commands or []
    ):
        errors.append("full-repository test evidence does not contain all passing command results")
    code_tree = evidence.get("code_tree")
    if (
        not isinstance(code_tree, dict)
        or code_tree.get("paths") != list(TEST_INPUT_PATHS)
        or code_tree.get("sha256") != _tree_sha256(root)
    ):
        errors.append("full-repository test evidence is not bound to current code and tests")
    return errors


def _proof_errors(reconciliation: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if reconciliation.get("search_partitions_closed") is not True:
        errors.append("Search API partitions are not closed")
    proofs = reconciliation.get("search_partition_proofs")
    if not isinstance(proofs, list) or not proofs:
        errors.append("Search API partition proofs are missing")
    else:
        for index, proof in enumerate(proofs):
            if not isinstance(proof, dict):
                errors.append(f"Search API partition proof {index} is invalid")
                continue
            if str(proof.get("partition", "")).casefold() == "__sample__":
                errors.append("Search API proof contains the sampled partition")
            expected = proof.get("expected")
            passes = proof.get("passes")
            if proof.get("sibling_disjoint") is not True:
                errors.append(f"Search API partition proof {index} does not prove sibling source-identity disjointness")
            canonical_overlap = proof.get("canonical_overlap_with_prior_partitions")
            if not isinstance(canonical_overlap, int) or isinstance(canonical_overlap, bool) or canonical_overlap < 0:
                errors.append(f"Search API partition proof {index} lacks explicit canonical-route overlap accounting")
            if not isinstance(expected, int) or expected < 0:
                errors.append(f"Search API partition proof {index} has an invalid expected count")
            if not isinstance(passes, list) or len(passes) < 2:
                errors.append(f"Search API partition proof {index} lacks opposing complete passes")
                continue
            for pass_index, row in enumerate(passes):
                if (
                    not isinstance(row, dict)
                    or row.get("closed") is not True
                    or row.get("returned_rows") != expected
                ):
                    errors.append(f"Search API partition proof {index} pass {pass_index} is not closed")
                elif row.get("unique_source_rows") != expected:
                    errors.append(
                        f"Search API partition proof {index} pass {pass_index} does not close source-row identities"
                    )
                elif (
                    not isinstance(row.get("unique_urls"), int)
                    or row.get("unique_urls") < 0
                    or row.get("unique_urls") > row.get("unique_source_rows")
                    or row.get("canonical_alias_rows")
                    != row.get("unique_source_rows") - row.get("unique_urls")
                ):
                    errors.append(f"Search API partition proof {index} pass {pass_index} has invalid unique counts")
                if not _valid_sha256(row.get("identity_sha256")):
                    errors.append(f"Search API partition proof {index} pass {pass_index} lacks a source-identity hash")
                if not _valid_sha256(row.get("canonical_url_sha256")):
                    errors.append(f"Search API partition proof {index} pass {pass_index} lacks a canonical-route hash")
            identity_hashes = {row.get("identity_sha256") for row in passes if isinstance(row, dict)}
            canonical_hashes = {row.get("canonical_url_sha256") for row in passes if isinstance(row, dict)}
            if len(identity_hashes) != 1:
                errors.append(f"Search API partition proof {index} opposing source-identity hashes differ")
            if len(canonical_hashes) != 1:
                errors.append(f"Search API partition proof {index} opposing canonical-route hashes differ")
            orders = {str(row.get("order", "")) for row in passes if isinstance(row, dict)}
            if not any(order.startswith("-") for order in orders) or not any(
                order and not order.startswith("-") for order in orders
            ):
                errors.append(f"Search API partition proof {index} lacks opposing sort orders")
    if reconciliation.get("sitemap_byte_stable") is not True:
        errors.append("sitemap byte-stability proof did not pass")
    organisations = reconciliation.get("organisations_proof")
    if not isinstance(organisations, dict) or organisations.get("closed") is not True:
        errors.append("organisations enumeration proof did not close")
    elif (
        not isinstance(organisations.get("reported_total"), int)
        or organisations.get("reported_total") < 1
        or organisations.get("returned_rows") != organisations.get("reported_total")
        or not isinstance(organisations.get("unique_urls"), int)
        or not 0 < organisations.get("unique_urls") <= organisations.get("returned_rows")
    ):
        errors.append("organisations enumeration counts are invalid")
    return errors


def validate_release(
    root: Path,
    *,
    require_publication_ready: bool = False,
    require_finalized: bool = False,
    allow_missing_archived_inputs: bool = False,
) -> list[str]:
    """Return all release-gate failures for ``root`` without changing files."""

    root = root.resolve()
    require_publication_ready = require_publication_ready or require_finalized
    errors: list[str] = []
    try:
        manifest = _load_json(root / MANIFEST_RELATIVE, "release manifest (JSON-compatible YAML)")
        status = _load_json(root / STATUS_RELATIVE, "release status")
    except ReleaseDocumentError as exc:
        return [str(exc)]

    if manifest.get("schema") != "afhf-govuk-okf-release-manifest.v1":
        errors.append("release manifest schema is not afhf-govuk-okf-release-manifest.v1")
    if status.get("schema") != "afhf-govuk-okf-release-status.v1":
        errors.append("release status schema is not afhf-govuk-okf-release-status.v1")
    if manifest.get("release_id") != status.get("release_id"):
        errors.append("release manifest and status release_id differ")
    if not isinstance(manifest.get("publication_ready"), bool) or not isinstance(
        status.get("publication_ready"), bool
    ):
        errors.append("publication_ready must be an explicit boolean in both release documents")
    if manifest.get("publication_ready") is not status.get("publication_ready"):
        errors.append("release manifest and status publication_ready differ")

    snapshot = manifest.get("snapshot")
    if not isinstance(snapshot, dict):
        errors.append("release snapshot must be an object")
        snapshot = {}
    if snapshot.get("id") != manifest.get("release_id"):
        errors.append("release_id must equal snapshot.id")
    if not isinstance(snapshot.get("sampled"), bool):
        errors.append("snapshot.sampled must be an explicit boolean")

    gates = manifest.get("gates")
    if not isinstance(gates, dict):
        errors.append("release manifest gates must be an object")
        gates = {}
    for flag in RELEASE_FLAGS:
        if not isinstance(gates.get(flag), bool):
            errors.append(f"manifest gate {flag} must be an explicit boolean")
        if status.get(flag) != gates.get(flag):
            errors.append(f"release manifest and status disagree on {flag}")
    if not isinstance(status.get("aims_assessed"), bool):
        errors.append("release status aims_assessed must be an explicit boolean")
    if status.get("aims_assessed") is not gates.get("aim_assessment_passed"):
        errors.append("release aims_assessed and aim_assessment_passed differ")

    human_status = status.get("human_evaluation_status")
    human_ui_status = status.get("human_ui_of_choice_status")
    if human_status not in HUMAN_STATUSES:
        errors.append("human_evaluation_status is invalid")
    if human_status != "completed" and human_ui_status != "not_yet_testable":
        errors.append("human UI-of-choice status must remain not_yet_testable without completed human evidence")
    if human_status != "completed" and status.get("full_evaluation_complete") is not False:
        errors.append("full_evaluation_complete must be false without completed human evidence")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        errors.append("release manifest artifacts must be an object")
        artifacts = {}
    try:
        status_artifact = _resolve_relative(root, artifacts.get("status"), "status")
        if status_artifact != (root / STATUS_RELATIVE).resolve():
            errors.append("release manifest status artifact does not identify release/status.json")
    except ReleaseDocumentError as exc:
        errors.append(str(exc))
    try:
        descriptor_path = _resolve_relative(root, artifacts.get("descriptor"), "descriptor")
        descriptor = _load_json(descriptor_path, "Explorer descriptor") if descriptor_path else {}
        data_manifest_relative = _entrypoint_path(descriptor.get("entrypoints", {}).get("data_manifest"))
        if not descriptor_path or not data_manifest_relative:
            raise ReleaseDocumentError("Explorer descriptor has no data_manifest entrypoint")
        data_manifest_path = _resolve_relative(
            descriptor_path.parent, data_manifest_relative, "descriptor data_manifest"
        )
        data_manifest = _load_json(data_manifest_path, "bundle data manifest") if data_manifest_path else {}
    except ReleaseDocumentError as exc:
        errors.append(str(exc))
        descriptor = {}
        data_manifest = {}

    counts = manifest.get("counts")
    if not isinstance(counts, dict) or not isinstance(counts.get("publication_records"), int):
        errors.append("release manifest counts.publication_records must be an integer")
        counts = {}
    descriptor_counts = descriptor.get("counts") if isinstance(descriptor, dict) else None
    if not isinstance(descriptor_counts, dict):
        errors.append("Explorer descriptor counts are missing")
        descriptor_counts = {}
    if descriptor_counts.get("records") != descriptor_counts.get("datasets"):
        errors.append("Explorer descriptor records and datasets counts differ")
    if counts.get("publication_records") != descriptor_counts.get("records"):
        errors.append("release manifest and Explorer descriptor publication-record counts differ")
    if data_manifest.get("counts") != descriptor_counts:
        errors.append("bundle data manifest and Explorer descriptor counts differ")
    if data_manifest.get("snapshot") != snapshot.get("id"):
        errors.append("bundle data manifest snapshot differs from release snapshot")

    if not require_publication_ready:
        if status.get("publication_ready") is False and status.get("status") != "checkpoint":
            errors.append("a non-publishable release must have checkpoint status")
        return errors

    if manifest.get("publication_ready") is not True or status.get("publication_ready") is not True:
        errors.append("publication_ready is false")
    if manifest.get("release_kind") not in {"machine_release_candidate", "full_programme"}:
        errors.append("release_kind is not a publishable release kind")
    snapshot_text = f"{snapshot.get('id', '')} {snapshot.get('kind', '')}".casefold()
    if snapshot.get("sampled") is not False:
        errors.append("sampled snapshots cannot be published")
    if snapshot.get("kind") != "full_corpus":
        errors.append("snapshot.kind must be full_corpus")
    if any(marker in snapshot_text for marker in DISALLOWED_SNAPSHOT_MARKERS):
        errors.append("fixture, sample, capacity, development or test snapshots cannot be published")
    promotion = manifest.get("promotion")
    if (
        not isinstance(promotion, dict)
        or promotion.get("schema") != "afhf-govuk-okf-two-stage-promotion.v1"
        or promotion.get("from") != "full_corpus_checkpoint"
        or not _valid_sha256(promotion.get("staged_manifest_sha256"))
        or not _valid_sha256(promotion.get("staged_status_sha256"))
        or not isinstance(promotion.get("finalized"), bool)
    ):
        errors.append("release lacks a valid two-stage promotion record")
        promotion = {}
    reproduction = promotion.get("reproduction") if isinstance(promotion, dict) else None
    if (
        not isinstance(reproduction, dict)
        or not isinstance(reproduction.get("source"), str)
        or not isinstance(reproduction.get("generated_at"), str)
        or reproduction.get("compiler") not in {"auto", "memory", "disk"}
        or not isinstance(reproduction.get("source_binding"), dict)
    ):
        errors.append("release promotion lacks a complete immutable reproduction contract")
    else:
        try:
            source_path = _resolve_relative(root, reproduction["source"], "frozen reproduction source")
            source_binding = reproduction["source_binding"]
            valid_archived_binding = (
                source_binding.get("path") == reproduction["source"]
                and source_binding.get("kind") in {"file", "directory"}
                and isinstance(source_binding.get("file_count"), int)
                and not isinstance(source_binding.get("file_count"), bool)
                and source_binding.get("file_count", -1) >= 1
                and isinstance(source_binding.get("bytes"), int)
                and not isinstance(source_binding.get("bytes"), bool)
                and source_binding.get("bytes", -1) >= 0
                and _valid_sha256(source_binding.get("tree_sha256"))
                and (
                    source_binding.get("kind") != "file"
                    or _valid_sha256(source_binding.get("content_sha256"))
                )
            )
            if source_path and source_path.exists() and _source_binding(
                source_path, root
            ) != source_binding:
                errors.append("frozen reproduction source content/tree binding differs")
            elif source_path and not source_path.exists() and not (
                allow_missing_archived_inputs and valid_archived_binding
            ):
                errors.append("frozen reproduction source is missing")
        except (OSError, ValueError, ReleaseDocumentError) as exc:
            errors.append(str(exc))
    if status.get("promotion_finalized") is not promotion.get("finalized"):
        errors.append("release status and manifest promotion-finalized states differ")
    if require_finalized and promotion.get("finalized") is not True:
        errors.append("release promotion is not finalized after publication, Pages and registry evidence")
    if promotion.get("finalized") is True and (
        not _valid_sha256(promotion.get("candidate_manifest_sha256"))
        or not _valid_sha256(promotion.get("candidate_status_sha256"))
    ):
        errors.append("finalized promotion lacks exact candidate manifest/status hashes")
    elif promotion.get("finalized") is True and manifest.get("release_kind") == "machine_release_candidate":
        candidate_manifest = dict(manifest)
        candidate_promotion = dict(promotion)
        candidate_promotion.pop("candidate_manifest_sha256", None)
        candidate_promotion.pop("candidate_status_sha256", None)
        candidate_promotion["finalized"] = False
        candidate_manifest["promotion"] = candidate_promotion
        candidate_status = dict(status)
        candidate_status["promotion_finalized"] = False
        candidate_status["reason"] = MACHINE_CANDIDATE_REASON
        if _document_sha256(candidate_manifest) != promotion.get("candidate_manifest_sha256"):
            errors.append("finalized promotion candidate-manifest hash differs")
        if _document_sha256(candidate_status) != promotion.get("candidate_status_sha256"):
            errors.append("finalized promotion candidate-status hash differs")

    if status.get("machine_rc_complete") is not True:
        errors.append("machine_rc_complete is false")
    if status.get("aims_assessed") is not True:
        errors.append("aims_assessed is false")
    if status.get("agent_evaluation_status") != "completed":
        errors.append("agent_evaluation_status is not completed")
    if status.get("completion_statement") not in {MACHINE_MARKER, FULL_MARKER}:
        errors.append("completion_statement is not an authorised release marker")
    if status.get("completion_statement") == FULL_MARKER and (
        human_status != "completed"
        or human_ui_status == "not_yet_testable"
        or status.get("full_evaluation_complete") is not True
        or status.get("programme_complete") is not True
        or manifest.get("release_kind") != "full_programme"
    ):
        errors.append("full programme marker requires completed human evaluation, assessment and programme_complete")
    if status.get("completion_statement") == MACHINE_MARKER and (
        status.get("programme_complete") is not False
        or manifest.get("release_kind") != "machine_release_candidate"
    ):
        errors.append("machine release-candidate marker requires its release kind and programme_complete false")

    for flag in RELEASE_FLAGS:
        if gates.get(flag) is not True or status.get(flag) is not True:
            errors.append(f"required release gate is not true: {flag}")
    if status.get("unexplained_omissions") != 0:
        errors.append("release status unexplained_omissions is not zero")

    sbom_path: Path | None = None
    bundle_path: Path | None = None
    try:
        sbom_path = _resolve_relative(root, artifacts.get("sbom"), "sbom")
        bundle_path = _resolve_relative(root, artifacts.get("bundle"), "bundle")
        if sbom_path:
            errors.extend(_sbom_errors(root, sbom_path))
    except ReleaseDocumentError as exc:
        errors.append(str(exc))

    try:
        reconciliation_path = _resolve_relative(root, artifacts.get("reconciliation"), "reconciliation")
        reconciliation = _load_json(reconciliation_path, "closing reconciliation") if reconciliation_path else {}
    except ReleaseDocumentError as exc:
        errors.append(str(exc))
        reconciliation = {}
    if reconciliation:
        if reconciliation.get("snapshot") != snapshot.get("id"):
            errors.append("closing reconciliation snapshot differs from release snapshot")
        if reconciliation.get("sampled") is not False:
            errors.append("closing reconciliation is sampled")
        dispositions = (
            "represented",
            "alias_of_represented",
            "redirect_only",
            "tombstone_only",
            "exceptioned",
        )
        expected = reconciliation.get("expected_candidate_keys")
        if not isinstance(expected, int) or expected < 1:
            errors.append("closing reconciliation expected_candidate_keys is invalid")
        elif any(not isinstance(reconciliation.get(name), int) or reconciliation.get(name) < 0 for name in dispositions):
            errors.append("closing reconciliation disposition counts are invalid")
        elif sum(reconciliation[name] for name in dispositions) != expected:
            errors.append("closing reconciliation accounting identity does not hold")
        if reconciliation.get("unexplained_omissions") != 0:
            errors.append("closing reconciliation unexplained_omissions is not zero")
        entity_counts = reconciliation.get("entity_class_counts")
        if not isinstance(entity_counts, dict) or not entity_counts:
            errors.append("closing reconciliation entity_class_counts are missing")
        elif expected is not None and sum(entity_counts.values()) != expected:
            errors.append("entity_class_counts do not reconcile to expected_candidate_keys")
        publication_records = reconciliation.get("publication_records")
        if (
            publication_records != counts.get("publication_records")
            or publication_records != descriptor_counts.get("records")
        ):
            errors.append("reconciliation, release manifest and bundle publication-record counts differ")
        errors.extend(_proof_errors(reconciliation))

    try:
        aim_assessment_path = _resolve_relative(root, artifacts.get("aim_assessment"), "aim_assessment")
        aim_assessment = _load_json(aim_assessment_path, "aim_assessment") if aim_assessment_path else {}
        errors.extend(_aim_assessment_errors(aim_assessment, snapshot))
    except ReleaseDocumentError as exc:
        errors.append(str(exc))

    structured_evidence = (
        ("question_contract", lambda path, evidence: _question_contract_errors(path, evidence, snapshot)),
        ("evaluation", lambda path, evidence: _evaluation_errors(evidence, snapshot)),
        ("browser_validation", lambda path, evidence: _browser_errors(evidence, snapshot)),
        ("security_scan", lambda path, evidence: _security_errors(root, evidence, snapshot)),
        (
            "provenance_validation",
            lambda path, evidence: _provenance_errors(
                root, evidence, snapshot, require_finalized=require_finalized
            ),
        ),
        ("full_repository_tests", lambda path, evidence: _test_evidence_errors(root, evidence, snapshot)),
    )
    for artifact_name, validator in structured_evidence:
        try:
            evidence_path = _resolve_relative(root, artifacts.get(artifact_name), artifact_name)
            evidence = _load_json(evidence_path, artifact_name) if evidence_path else {}
            errors.extend(validator(evidence_path, evidence))
        except ReleaseDocumentError as exc:
            errors.append(str(exc))

    simple_evidence = (
        ("semantic_validation", "semantic_validation_passed", "passed"),
        ("citation_verification", "citation_verification_passed", None),
        ("rights_privacy_audit", "rights_privacy_audit_passed", None),
        ("clean_room_reproduction", "clean_room_reproduction_passed", None),
    )
    for artifact_name, pass_field, alternative in simple_evidence:
        try:
            evidence_path = _resolve_relative(root, artifacts.get(artifact_name), artifact_name)
            evidence = _load_json(evidence_path, artifact_name) if evidence_path else {}
            if evidence.get(pass_field) is not True and (alternative is None or evidence.get(alternative) is not True):
                errors.append(f"{artifact_name} evidence does not assert {pass_field}")
            if _artifact_snapshot(evidence) != snapshot.get("id"):
                errors.append(f"{artifact_name} evidence snapshot differs from release snapshot")
            if artifact_name == "rights_privacy_audit":
                errors.extend(
                    _rights_errors(
                        root,
                        evidence,
                        require_release=require_publication_ready,
                        allow_missing_corpus_inputs=allow_missing_archived_inputs,
                    )
                )
            if artifact_name == "clean_room_reproduction" and sbom_path:
                full_tests_path = _resolve_relative(
                    root, artifacts.get("full_repository_tests"), "full_repository_tests"
                )
                errors.extend(
                    _clean_room_errors(
                        evidence,
                        root,
                        sbom_path,
                        promotion,
                        bundle_path=bundle_path,
                        evidence_path=evidence_path,
                        test_evidence_path=full_tests_path,
                        allow_missing_frozen_source=allow_missing_archived_inputs,
                    )
                )
        except ReleaseDocumentError as exc:
            errors.append(str(exc))

    try:
        checksums_path = _resolve_relative(root, artifacts.get("checksums"), "checksums")
        bundle_path = _resolve_relative(root, artifacts.get("bundle"), "bundle")
        if checksums_path and bundle_path:
            if not bundle_path.is_dir():
                errors.append("bundle artifact is not a directory")
            else:
                errors.extend(_checksum_errors(bundle_path, checksums_path))
    except ReleaseDocumentError as exc:
        errors.append(str(exc))

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument(
        "--publication-ready",
        action="store_true",
        help="require every machine release-candidate publication gate",
    )
    parser.add_argument(
        "--finalized",
        action="store_true",
        help="require strict post-publication 11-of-11 provenance and finalized promotion",
    )
    parser.add_argument(
        "--allow-archived-inputs",
        action="store_true",
        help=(
            "permit absent frozen official-source inputs only when their completed "
            "clean-room and rights evidence remains fully hash-bound"
        ),
    )
    args = parser.parse_args(argv)
    errors = validate_release(
        args.root,
        require_publication_ready=args.publication_ready or args.finalized,
        require_finalized=args.finalized,
        allow_missing_archived_inputs=args.allow_archived_inputs,
    )
    if errors:
        print("release validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    status = _load_json(args.root / STATUS_RELATIVE, "release status")
    if args.publication_ready:
        print(f"publication-ready release validated: {status['release_id']}")
    else:
        readiness = "publication-ready" if status.get("publication_ready") else "checkpoint, not publication-ready"
        print(f"release status validated: {status['release_id']} ({readiness})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
