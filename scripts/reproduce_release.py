#!/usr/bin/env python3
"""Rebuild a frozen publication in a clean temporary directory and compare it.

The default contract reproduces the checked-in fixture. ``--check`` performs no
checkout writes. A full-corpus run can set the machine-release gate only when
the release manifest and independent full-repository test evidence identify the
same unsampled snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "tests" / "fixtures" / "corpus" / "source-records.jsonl"
DEFAULT_BUNDLE = ROOT / "bundle"
DEFAULT_SBOM = ROOT / "release" / "sbom.cdx.json"
DEFAULT_EVIDENCE = ROOT / "release" / "clean-room-reproduction.json"
DEFAULT_DECLARATIONS = ROOT / "provenance" / "reproduction-declarations.json"
DEFAULT_ACTIVITY_LEDGER = ROOT / "provenance" / "activity-ledger.jsonl"
DEFAULT_RELEASE_MANIFEST = ROOT / "release" / "manifest.yaml"
DEFAULT_GENERATED_AT = "2026-07-11T23:30:00Z"
DEFAULT_SNAPSHOT = "fixture-2026-07-11"
DISALLOWED_RELEASE_MARKERS = ("fixture", "sample", "capacity", "development", "test")
COPY_INPUTS = (
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
IGNORED_NAMES = {".DS_Store", "__pycache__", "node_modules"}


class ReproductionError(ValueError):
    """Raised when clean-room inputs or evidence fail closed."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _include(path: Path) -> bool:
    return (
        path.name not in IGNORED_NAMES
        and not path.name.startswith("._")
        and path.suffix not in {".pyc", ".pyo"}
    )


def file_rows(path: Path) -> list[dict[str, Any]]:
    """Return sorted relative file hashes for a file or directory."""

    if not path.exists():
        raise ReproductionError(f"missing reproduction input: {path}")
    if path.is_file():
        candidates = [(Path(path.name), path)]
    elif path.is_dir():
        candidates = [
            (candidate.relative_to(path), candidate)
            for candidate in sorted(path.rglob("*"))
            if candidate.is_file()
            and all(
                _include(part) for part in candidate.relative_to(path).parents
            )
            and _include(candidate)
        ]
    else:
        raise ReproductionError(f"unsupported reproduction input: {path}")
    return [
        {
            "path": relative.as_posix(),
            "bytes": candidate.stat().st_size,
            "sha256": _file_sha256(candidate),
        }
        for relative, candidate in candidates
    ]


def manifest_summary(path: Path) -> dict[str, Any]:
    rows = file_rows(path)
    canonical = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return {
        "file_count": len(rows),
        "bytes": sum(row["bytes"] for row in rows),
        "tree_sha256": _sha256_bytes(canonical),
        "rows": rows,
    }


def _compact_manifest(summary: dict[str, Any]) -> dict[str, Any]:
    return {key: summary[key] for key in ("file_count", "bytes", "tree_sha256")}


def compare_trees(expected: Path, actual: Path) -> dict[str, Any]:
    expected_manifest = manifest_summary(expected)
    actual_manifest = manifest_summary(actual)
    expected_rows = {row["path"]: row for row in expected_manifest["rows"]}
    actual_rows = {row["path"]: row for row in actual_manifest["rows"]}
    differences = []
    for relative in sorted(set(expected_rows) | set(actual_rows)):
        expected_row = expected_rows.get(relative)
        actual_row = actual_rows.get(relative)
        if expected_row != actual_row:
            differences.append(
                {
                    "path": relative,
                    "expected_sha256": expected_row.get("sha256") if expected_row else None,
                    "actual_sha256": actual_row.get("sha256") if actual_row else None,
                }
            )
    return {
        "exact_match": not differences,
        "expected": _compact_manifest(expected_manifest),
        "actual": _compact_manifest(actual_manifest),
        "differences": differences[:100],
        "difference_count": len(differences),
    }


def _copy(source: Path, destination: Path) -> None:
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return
    if source.is_dir():
        shutil.copytree(
            source,
            destination,
            ignore=lambda _directory, names: [
                name
                for name in names
                if name in IGNORED_NAMES
                or name.startswith("._")
                or Path(name).suffix in {".pyc", ".pyo"}
            ],
        )
        return
    raise ReproductionError(f"cannot copy missing input: {source}")


def _relative_label(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _copy_inputs(workspace: Path, source: Path) -> tuple[Path, dict[str, Any]]:
    components = []
    for relative_text in COPY_INPUTS:
        relative = Path(relative_text)
        source_path = ROOT / relative
        _copy(source_path, workspace / relative)
        summary = manifest_summary(source_path)
        components.append({"path": relative.as_posix(), **_compact_manifest(summary)})
    source_destination = workspace / "inputs" / (source.name if source.is_file() else "source")
    _copy(source, source_destination)
    source_summary = manifest_summary(source)
    components.append(
        {
            "path": "frozen_source",
            "source": _relative_label(source),
            **_compact_manifest(source_summary),
        }
    )
    canonical = json.dumps(components, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return source_destination, {
        "schema": "afhf-govuk-okf-reproduction-input-manifest.v1",
        "components": components,
        "component_count": len(components),
        "tree_sha256": _sha256_bytes(canonical),
    }


def _run_command(
    command: list[str],
    *,
    display: list[str],
    workspace: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
            "LC_ALL": "C",
        }
    )
    try:
        result = subprocess.run(
            command,
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        stdout = result.stdout.replace(str(workspace), "{workspace}")
        stderr = result.stderr.replace(str(workspace), "{workspace}")
        return {
            "command": display,
            "returncode": result.returncode,
            "stdout_sha256": _sha256_bytes(stdout.encode("utf-8")),
            "stderr_sha256": _sha256_bytes(stderr.encode("utf-8")),
            "stdout_tail": stdout[-1000:],
            "stderr_tail": stderr[-1000:],
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stdout = stdout.replace(str(workspace), "{workspace}")
        stderr = stderr.replace(str(workspace), "{workspace}")
        return {
            "command": display,
            "returncode": None,
            "timed_out": True,
            "stdout_sha256": _sha256_bytes(stdout.encode("utf-8")),
            "stderr_sha256": _sha256_bytes(stderr.encode("utf-8")),
            "stdout_tail": stdout[-1000:],
            "stderr_tail": stderr[-1000:],
        }


def _tool_version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return (result.stdout or result.stderr).strip() or None


def activity_usage(path: Path) -> dict[str, Any]:
    activities = 0
    known_tokens = 0
    unknown_token_activities = 0
    known_cost_gbp = 0.0
    unknown_cost_activities = 0
    external_calls = 0
    external_input_tokens = 0
    external_output_tokens = 0
    external_cost_gbp = 0.0
    source_request_exact_attempts = 0
    source_request_checkpoints: list[int] = []
    source_request_pending_activities = 0
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReproductionError(f"invalid activity ledger row {number}: {exc}") from exc
        # Legacy rows carried exact_model_version at top level. Version 2 rows
        # deliberately place the structured model identity under ``model`` and
        # use null for deterministic work. Reproduction summarizes both forms;
        # the stricter hash-chain and schema checks live in check_provenance.py.
        required = {"activity_id", "tokens", "cost_gbp", "external_paid_model_api_calls"}
        if not isinstance(row, dict) or not required <= set(row):
            raise ReproductionError(f"activity ledger row {number} lacks usage fields")
        if row.get("ledger_schema_version") == "2.0" and "model" not in row:
            raise ReproductionError(f"activity ledger v2 row {number} lacks structured model identity")
        activities += 1
        tokens = row["tokens"]
        if isinstance(tokens, int) and not isinstance(tokens, bool):
            known_tokens += tokens
        else:
            unknown_token_activities += 1
        cost = row["cost_gbp"]
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            known_cost_gbp += float(cost)
        else:
            unknown_cost_activities += 1
        calls = row["external_paid_model_api_calls"]
        if not isinstance(calls, int) or isinstance(calls, bool) or calls < 0:
            raise ReproductionError(f"activity ledger row {number} has invalid external call count")
        external_calls += calls
        structured = row.get("usage", {}) if isinstance(row, dict) else {}
        external = structured.get("external_paid_model", {}) if isinstance(structured, dict) else {}
        if isinstance(external, dict):
            for field, target in (
                ("input_tokens", "input"),
                ("output_tokens", "output"),
                ("cost_gbp", "cost"),
            ):
                value = external.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if target == "input":
                        external_input_tokens += int(value)
                    elif target == "output":
                        external_output_tokens += int(value)
                    else:
                        external_cost_gbp += float(value)
        request_usage = row.get("source_request_usage", {}) if isinstance(row, dict) else {}
        if isinstance(request_usage, dict):
            status = request_usage.get("status")
            attempts = request_usage.get("attempts")
            if status == "exact" and isinstance(attempts, int) and not isinstance(attempts, bool):
                source_request_exact_attempts += attempts
            elif status == "checkpoint" and isinstance(attempts, int) and not isinstance(attempts, bool):
                source_request_checkpoints.append(attempts)
            elif status == "pending_final":
                source_request_pending_activities += 1
    return {
        "activities": activities,
        "known_tokens": known_tokens,
        "activities_with_unavailable_tokens": unknown_token_activities,
        "known_cost_gbp": known_cost_gbp,
        "activities_with_unavailable_cost": unknown_cost_activities,
        "external_paid_model_api_calls": external_calls,
        "external_paid_model_input_tokens": external_input_tokens,
        "external_paid_model_output_tokens": external_output_tokens,
        "external_paid_model_cost_gbp": external_cost_gbp,
        "source_request_exact_attempts": source_request_exact_attempts,
        "latest_source_request_checkpoint": max(source_request_checkpoints, default=None),
        "source_request_activities_pending_final_count": source_request_pending_activities,
        "source_requests_included_in_model_cost": False,
        "ledger_sha256": _file_sha256(path),
    }


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReproductionError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReproductionError(f"{label} must be an object: {path}")
    return value


def _release_inputs_pass(
    *,
    release_kind: str,
    snapshot: str,
    snapshot_kind: str,
    sampled: bool,
    source: Path,
    release_manifest: Path,
    test_evidence: Path | None,
) -> tuple[bool, list[str], dict[str, Any] | None]:
    failures = []
    tests = None
    if release_kind not in {"machine_release_candidate", "full_programme"}:
        failures.append("release kind is a fixture checkpoint")
    if snapshot_kind != "full_corpus" or sampled:
        failures.append("snapshot is not an unsampled full corpus")
    if any(marker in snapshot.casefold() for marker in DISALLOWED_RELEASE_MARKERS):
        failures.append("snapshot identifier contains a non-release marker")
    if "tests/fixtures" in _relative_label(source):
        failures.append("fixture source cannot pass clean-room release")
    manifest = _load_object(release_manifest, "release manifest (JSON-compatible YAML)")
    if manifest.get("release_id") != snapshot or manifest.get("release_kind") != release_kind:
        failures.append("release manifest identity differs from the reproduction contract")
    manifest_snapshot = manifest.get("snapshot")
    if (
        not isinstance(manifest_snapshot, dict)
        or manifest_snapshot.get("kind") != "full_corpus"
        or manifest_snapshot.get("sampled") is not False
    ):
        failures.append("release manifest does not identify an unsampled full corpus")
    if test_evidence is None:
        failures.append("independent full-repository test evidence is missing")
    else:
        tests = _load_object(test_evidence, "test evidence")
        if (
            tests.get("snapshot") != snapshot
            or tests.get("scope") != "full_repository"
            or tests.get("tests_passed") is not True
        ):
            failures.append("full-repository test evidence does not pass for this snapshot")
    return not failures, failures, tests


def _workspace_state(paths: Iterable[Path]) -> dict[str, Any]:
    rows = []
    for path in paths:
        summary = manifest_summary(path)
        rows.append({"path": _relative_label(path), **_compact_manifest(summary)})
    canonical = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return {"paths": rows, "sha256": _sha256_bytes(canonical)}


def reproduce(args: argparse.Namespace) -> dict[str, Any]:
    source = args.source.resolve()
    expected_bundle = args.expected_bundle.resolve()
    expected_sbom = args.sbom.resolve()
    declarations = _load_object(args.declarations.resolve(), "reproduction declarations")
    if declarations.get("schema") != "afhf-govuk-okf-reproduction-declarations.v1":
        raise ReproductionError("reproduction declarations use an unsupported schema")
    if declarations.get("network", {}).get("required") is not False:
        raise ReproductionError("clean reproduction must declare that network access is not required")
    protected_paths = list(dict.fromkeys([
        *(ROOT / relative for relative in COPY_INPUTS),
        source,
        expected_bundle,
        expected_sbom,
        args.declarations.resolve(),
        args.activity_ledger.resolve(),
    ]))
    before = _workspace_state(protected_paths)
    release_inputs_passed, release_failures, test_evidence = _release_inputs_pass(
        release_kind=args.release_kind,
        snapshot=args.snapshot_id,
        snapshot_kind=args.snapshot_kind,
        sampled=args.sampled,
        source=source,
        release_manifest=args.release_manifest.resolve(),
        test_evidence=args.test_evidence.resolve() if args.test_evidence else None,
    )
    recorded_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with tempfile.TemporaryDirectory(prefix="okf-govuk-clean-room-") as directory:
        workspace = Path(directory) / "workspace"
        workspace.mkdir()
        source_copy, input_manifest = _copy_inputs(workspace, source)
        python = sys.executable
        bundle = workspace / "bundle"
        sbom = workspace / "release" / "sbom.cdx.json"
        source_display = "{workspace}/" + source_copy.relative_to(workspace).as_posix()
        commands = [
            (
                [
                    python,
                    "scripts/build_bundle.py",
                    "--source",
                    str(source_copy),
                    "--output",
                    str(bundle),
                    "--generated-at",
                    args.generated_at,
                    "--snapshot-id",
                    args.snapshot_id,
                    "--compiler",
                    args.compiler,
                ],
                [
                    "{python}",
                    "scripts/build_bundle.py",
                    "--source",
                    source_display,
                    "--output",
                    "{workspace}/bundle",
                    "--generated-at",
                    args.generated_at,
                    "--snapshot-id",
                    args.snapshot_id,
                    "--compiler",
                    args.compiler,
                ],
            ),
            (
                [python, "scripts/check_publication.py", "--bundle", str(bundle)],
                ["{python}", "scripts/check_publication.py", "--bundle", "{workspace}/bundle"],
            ),
            (
                [python, "scripts/build_checksums.py", "--bundle", str(bundle)],
                ["{python}", "scripts/build_checksums.py", "--bundle", "{workspace}/bundle"],
            ),
            (
                [python, "scripts/build_checksums.py", "--bundle", str(bundle), "--check"],
                ["{python}", "scripts/build_checksums.py", "--bundle", "{workspace}/bundle", "--check"],
            ),
            (
                [python, "scripts/build_sbom.py", "--output", str(sbom)],
                ["{python}", "scripts/build_sbom.py", "--output", "{workspace}/release/sbom.cdx.json"],
            ),
            (
                [python, "scripts/build_sbom.py", "--output", str(sbom), "--check"],
                ["{python}", "scripts/build_sbom.py", "--output", "{workspace}/release/sbom.cdx.json", "--check"],
            ),
        ]
        command_results = []
        for command, display in commands:
            result = _run_command(
                command,
                display=display,
                workspace=workspace,
                timeout_seconds=args.timeout_seconds,
            )
            command_results.append(result)
            if result.get("returncode") != 0:
                break
        validators_passed = len(command_results) == len(commands) and all(
            result.get("returncode") == 0 for result in command_results
        )
        if bundle.is_dir():
            bundle_comparison = compare_trees(expected_bundle, bundle)
        else:
            bundle_comparison = {
                "exact_match": False,
                "expected": _compact_manifest(manifest_summary(expected_bundle)),
                "actual": None,
                "differences": [{"path": "bundle", "actual_sha256": None}],
                "difference_count": 1,
            }
        sbom_match = sbom.is_file() and expected_sbom.is_file() and sbom.read_bytes() == expected_sbom.read_bytes()
        reproduced_sbom_sha256 = _file_sha256(sbom) if sbom.is_file() else None

    after = _workspace_state(protected_paths)
    checkout_unchanged = before == after
    fixture_reproduction_passed = (
        validators_passed
        and bundle_comparison["exact_match"]
        and sbom_match
        and checkout_unchanged
    )
    clean_room_reproduction_passed = fixture_reproduction_passed and release_inputs_passed
    reason = (
        "Clean temporary rebuild exactly matched the checked publication and "
        "locked-dependency SBOM, but this is a representative fixture checkpoint "
        "and cannot pass the full-corpus release gate."
        if fixture_reproduction_passed and not release_inputs_passed
        else "Clean temporary rebuild and all full-corpus release inputs passed."
        if clean_room_reproduction_passed
        else "Clean temporary rebuild did not satisfy every deterministic comparison and release-input gate."
    )
    evidence = {
        "schema": "afhf-govuk-okf-clean-room-reproduction.v1",
        "snapshot": args.snapshot_id,
        "snapshot_kind": args.snapshot_kind,
        "sampled": args.sampled,
        "release_kind": args.release_kind,
        "recorded_at": recorded_at,
        "source": _relative_label(source),
        "generated_at": args.generated_at,
        "compiler": args.compiler,
        "fixture_reproduction_passed": fixture_reproduction_passed,
        "release_inputs_passed": release_inputs_passed,
        "clean_room_reproduction_passed": clean_room_reproduction_passed,
        "reason": reason,
        "release_input_failures": release_failures,
        "inputs": input_manifest,
        "commands": command_results,
        "validators": {
            "passed": validators_passed,
            "scope": "clean_temp_publication_build_checksums_and_sbom",
        },
        "outputs": {
            "bundle": bundle_comparison,
            "sbom": {
                "path": _relative_label(expected_sbom),
                "expected_sha256": _file_sha256(expected_sbom),
                "reproduced_sha256": reproduced_sbom_sha256,
                "exact_match": sbom_match,
            },
        },
        "checkout": {
            "before_sha256": before["sha256"],
            "after_sha256": after["sha256"],
            "unchanged": checkout_unchanged,
            "verification_mode_mutates_checkout": False,
        },
        "environment": {
            "python": {
                "implementation": platform.python_implementation(),
                "version": platform.python_version(),
                "executable": sys.executable,
            },
            "platform": platform.platform(),
            "machine": platform.machine(),
            "tools": {
                "git": _tool_version(["git", "--version"]),
                "node": _tool_version(["node", "--version"]),
                "npm": _tool_version(["npm", "--version"]),
                "uv": _tool_version(["uv", "--version"]),
            },
        },
        "network": declarations["network"],
        "model_and_cost": {
            "declaration": declarations["model_and_cost"],
            "activity_ledger_summary": activity_usage(args.activity_ledger.resolve()),
        },
        "rights_and_fair_use": declarations["rights_and_fair_use"],
        "source_access_restrictions": declarations["source_access_restrictions"],
        "fallbacks": declarations["fallbacks"],
        "test_evidence": {
            "path": _relative_label(args.test_evidence.resolve()),
            "sha256": _file_sha256(args.test_evidence.resolve()),
            "scope": test_evidence.get("scope"),
            "tests_passed": test_evidence.get("tests_passed"),
        }
        if args.test_evidence and test_evidence
        else None,
    }
    return evidence


def validate_evidence(document: dict[str, Any], *, require_release: bool = False) -> list[str]:
    errors = []
    if document.get("schema") != "afhf-govuk-okf-clean-room-reproduction.v1":
        errors.append("clean-room evidence schema is invalid")
    required_booleans = (
        "fixture_reproduction_passed",
        "release_inputs_passed",
        "clean_room_reproduction_passed",
    )
    for field in required_booleans:
        if not isinstance(document.get(field), bool):
            errors.append(f"{field} must be an explicit boolean")
    validators = document.get("validators")
    outputs = document.get("outputs")
    checkout = document.get("checkout")
    if not isinstance(validators, dict) or validators.get("passed") is not True:
        errors.append("clean-room validators did not pass")
    if not isinstance(outputs, dict) or outputs.get("bundle", {}).get("exact_match") is not True:
        errors.append("clean-room bundle did not exactly match")
    if not isinstance(outputs, dict) or outputs.get("sbom", {}).get("exact_match") is not True:
        errors.append("clean-room SBOM did not exactly match")
    if not isinstance(checkout, dict) or checkout.get("unchanged") is not True:
        errors.append("clean-room verification changed declared checkout inputs or outputs")
    network = document.get("network")
    if (
        not isinstance(network, dict)
        or network.get("required") is not False
        or network.get("official_source_requests") != 0
        or network.get("external_model_requests") != 0
    ):
        errors.append("clean-room run does not have a zero-network declaration")
    if require_release:
        if document.get("clean_room_reproduction_passed") is not True:
            errors.append("clean_room_reproduction_passed is false")
        if document.get("release_inputs_passed") is not True:
            errors.append("full-corpus release inputs did not pass")
        if document.get("snapshot_kind") != "full_corpus" or document.get("sampled") is not False:
            errors.append("clean-room release evidence is not for an unsampled full corpus")
        test_evidence = document.get("test_evidence")
        if (
            not isinstance(test_evidence, dict)
            or test_evidence.get("scope") != "full_repository"
            or test_evidence.get("tests_passed") is not True
        ):
            errors.append("clean-room release lacks passing full-repository test evidence")
    return errors


def _stable_evidence_fields(document: dict[str, Any]) -> dict[str, Any]:
    return {
        key: document.get(key)
        for key in (
            "schema",
            "snapshot",
            "snapshot_kind",
            "sampled",
            "release_kind",
            "source",
            "generated_at",
            "compiler",
            "fixture_reproduction_passed",
            "release_inputs_passed",
            "clean_room_reproduction_passed",
            "release_input_failures",
            "inputs",
            "validators",
            "outputs",
            "network",
            "rights_and_fair_use",
            "source_access_restrictions",
            "fallbacks",
            "model_and_cost",
            "test_evidence",
        )
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--expected-bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--sbom", type=Path, default=DEFAULT_SBOM)
    parser.add_argument("--evidence-output", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--declarations", type=Path, default=DEFAULT_DECLARATIONS)
    parser.add_argument("--activity-ledger", type=Path, default=DEFAULT_ACTIVITY_LEDGER)
    parser.add_argument("--release-manifest", type=Path, default=DEFAULT_RELEASE_MANIFEST)
    parser.add_argument("--test-evidence", type=Path)
    parser.add_argument("--generated-at", default=DEFAULT_GENERATED_AT)
    parser.add_argument("--snapshot-id", default=DEFAULT_SNAPSHOT)
    parser.add_argument("--snapshot-kind", choices=("fixture", "full_corpus"), default="fixture")
    parser.add_argument(
        "--release-kind",
        choices=("fixture", "machine_release_candidate", "full_programme"),
        default="fixture",
    )
    parser.add_argument("--sampled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--compiler", choices=("auto", "memory", "disk"), default="auto")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=21600,
        help="per-command timeout; the default permits a bounded full-corpus rebuild",
    )
    parser.add_argument("--check", action="store_true", help="run and verify without writing the checkout")
    parser.add_argument("--require-release", action="store_true", help="require the full-corpus machine release gate")
    args = parser.parse_args(argv)
    if args.timeout_seconds < 1:
        print("timeout-seconds must be positive", file=sys.stderr)
        return 1
    try:
        document = reproduce(args)
        errors = validate_evidence(document, require_release=args.require_release)
        if args.check:
            existing = _load_object(args.evidence_output.resolve(), "checked-in clean-room evidence")
            if _stable_evidence_fields(existing) != _stable_evidence_fields(document):
                errors.append("checked-in clean-room evidence is stale")
        else:
            args.evidence_output.parent.mkdir(parents=True, exist_ok=True)
            args.evidence_output.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if errors:
            print("clean-room reproduction failed:", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1
        state = (
            "release gate passed"
            if document["clean_room_reproduction_passed"]
            else "fixture reproduced; release gate remains false"
        )
        print(f"clean-room reproduction verified: {state}")
        return 0
    except (OSError, UnicodeDecodeError, ReproductionError, json.JSONDecodeError) as exc:
        print(f"clean-room reproduction failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
