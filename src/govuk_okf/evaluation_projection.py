"""Verify an immutable evaluation run and publish its release evidence projection."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .evaluation import (
    BundleIndex,
    HARNESS_VERSION,
    InputContract,
    MAX_JSON_VALUE_BYTES,
    MAX_TRACE_SHARD_RECORDS,
    MAX_TRACE_UNCOMPRESSED_BYTES,
    OutcomeStore,
    RELEASE_QUESTION_COUNT,
    SYSTEM_BY_ID,
    SYSTEMS,
    TRACE_SCHEMA,
    aggregate_metrics,
    canonical_json,
    failure_analysis,
    grade_result,
    iter_questions,
    make_trace,
    paired_comparisons,
    serialization_invariance,
    slice_analysis,
    verify_bundle_inputs,
    verify_question_inputs,
    write_report,
)


RELEASE_OUTCOME_COUNT = RELEASE_QUESTION_COUNT * len(SYSTEMS)
PROJECTED_FILES = (
    "failure-analysis.json",
    "metrics.json",
    "paired-comparisons.json",
    "report.md",
    "slices.json",
    "status.json",
    "usage.json",
)
REQUIRED_RUN_FILES = tuple(sorted((*PROJECTED_FILES, "trace-manifest.json")))
MAX_CONTROL_BYTES = 64 * 1024 * 1024
MAX_TRACE_COMPRESSED_BYTES = MAX_TRACE_UNCOMPRESSED_BYTES + 1024 * 1024
MAX_TRACE_LATENCY_NS = 300 * 1_000_000_000
MAX_RECORDED_WALL_SECONDS = 365 * 24 * 60 * 60
ZERO_TRACE_USAGE = {
    "model_calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cost_gbp": 0.0,
    "network_requests": 0,
}


class EvaluationProjectionError(ValueError):
    """Raised when a release run or canonical projection is not trustworthy."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        if path.is_symlink():
            raise EvaluationProjectionError(f"{label} cannot be a symbolic link: {path}")
        if path.stat().st_size > MAX_CONTROL_BYTES:
            raise EvaluationProjectionError(f"{label} exceeds the control-document byte ceiling: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except EvaluationProjectionError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationProjectionError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvaluationProjectionError(f"{label} must be a JSON object: {path}")
    return value


def _assert_no_symlink_components(root: Path, path: Path, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise EvaluationProjectionError(f"{label} escapes the repository root: {path}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise EvaluationProjectionError(f"{label} cannot traverse a symbolic link: {current}")


def _repository_path(
    repository_root: Path,
    value: Path,
    label: str,
    *,
    must_exist: bool,
) -> Path:
    if repository_root.is_symlink():
        raise EvaluationProjectionError("repository root cannot be a symbolic link")
    root = repository_root.resolve()
    if ".." in value.parts:
        raise EvaluationProjectionError(f"{label} cannot contain parent traversal")
    candidate = value if value.is_absolute() else root / value
    candidate = Path(os.path.abspath(candidate))
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise EvaluationProjectionError(f"{label} escapes the repository root: {candidate}") from exc
    _assert_no_symlink_components(root, candidate, label)
    if must_exist and not candidate.exists():
        raise EvaluationProjectionError(f"{label} is missing: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise EvaluationProjectionError(f"{label} resolves outside the repository root: {candidate}")
    return resolved


def _safe_run_file(run: Path, value: object) -> tuple[Path, str]:
    relative = Path(str(value or ""))
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise EvaluationProjectionError(f"unsafe evaluation run path: {value!r}")
    lexical = run / relative
    _assert_no_symlink_components(run, lexical, "evaluation run file")
    path = lexical.resolve()
    if lexical.is_symlink() or not path.is_relative_to(run) or not path.is_file():
        raise EvaluationProjectionError(f"evaluation run file is missing or escapes the run: {relative.as_posix()}")
    return path, relative.as_posix()


def _verify_current_release_inputs(questions: Path, bundle: Path) -> dict[str, Any]:
    """Independently bind projection to the current verified questions/bundle."""

    try:
        question_manifest, question_contract, question_report = verify_question_inputs(
            questions, "release"
        )
        bundle_descriptor, bundle_manifest = verify_bundle_inputs(bundle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvaluationProjectionError(
            f"current release questions or bundle are not independently valid: {exc}"
        ) from exc
    question_snapshot = str(question_manifest.get("snapshot_id") or "")
    bundle_snapshot = str(bundle_manifest.get("snapshot") or "")
    question_counts = question_manifest.get("counts")
    if (
        not question_snapshot
        or question_snapshot != bundle_snapshot
        or not isinstance(question_counts, dict)
        or question_counts.get("questions") != RELEASE_QUESTION_COUNT
        or not isinstance(question_report, dict)
        or question_report.get("question_contract_passed") is not True
    ):
        raise EvaluationProjectionError(
            "current release questions and bundle do not share a complete verified snapshot"
        )
    expected_questions: dict[str, dict[str, Any]] = {}
    try:
        for question in iter_questions(questions):
            question_id = str(question.get("question_id") or "")
            subset = {
                "question_id": question_id,
                "checksum": question.get("checksum"),
                "wording": question.get("wording"),
            }
            if (
                not question_id
                or question_id in expected_questions
                or not _valid_sha256(subset["checksum"])
                or not isinstance(subset["wording"], str)
                or not subset["wording"]
            ):
                raise EvaluationProjectionError(
                    f"current release question identity is invalid or duplicated: {question_id!r}"
                )
            expected_questions[question_id] = subset
    except EvaluationProjectionError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvaluationProjectionError(
            f"current release question bindings cannot be verified: {exc}"
        ) from exc
    if len(expected_questions) != RELEASE_QUESTION_COUNT:
        raise EvaluationProjectionError(
            "current release question bindings do not contain the complete question set"
        )
    return {
        "snapshot_id": bundle_snapshot,
        "question_manifest_sha256": _sha256_file(questions / "manifest.json"),
        "bundle_manifest_sha256": _sha256_file(bundle / "data" / "manifest.json"),
        "questions": expected_questions,
        "question_manifest": question_manifest,
        "question_contract": question_contract,
        "question_report": question_report,
        "bundle_descriptor": bundle_descriptor,
        "bundle_manifest": bundle_manifest,
    }


def _verify_trace_manifest(
    run: Path,
    trace_manifest: dict[str, Any],
    *,
    run_manifest: dict[str, Any],
    inventoried_paths: set[str],
    current_inputs: dict[str, Any],
    outcomes: OutcomeStore,
) -> dict[str, Any]:
    """Stream, hash and structurally verify every matched evaluation trace."""

    if (
        trace_manifest.get("schema") != "govuk-okf-agent-evaluation-traces.v1"
        or trace_manifest.get("compression") != "gzip"
        or trace_manifest.get("canonical_encoding")
        != "UTF-8 RFC 8259 JSON Lines with sorted keys"
        or trace_manifest.get("records") != RELEASE_OUTCOME_COUNT
        or trace_manifest.get("max_uncompressed_bytes_per_shard")
        != MAX_TRACE_UNCOMPRESSED_BYTES
    ):
        raise EvaluationProjectionError("evaluation trace manifest contract is invalid")
    max_records = trace_manifest.get("max_records_per_shard")
    if (
        not isinstance(max_records, int)
        or isinstance(max_records, bool)
        or not 1 <= max_records <= MAX_TRACE_SHARD_RECORDS
    ):
        raise EvaluationProjectionError("evaluation trace shard record bound is invalid")
    shards = trace_manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        raise EvaluationProjectionError("evaluation trace manifest does not enumerate trace shards")

    declared_paths: list[str] = []
    verified_shards: list[dict[str, Any]] = []
    total_records = 0
    previous_pair: tuple[str, str] | None = None
    system_counts: Counter[str] = Counter()
    question_digests = {system.system_id: hashlib.sha256() for system in SYSTEMS}
    valid_systems = set(question_digests)
    expected_run_id = run_manifest.get("run_id")
    expected_snapshot = run_manifest.get("snapshot_id")
    expected_input_contract = {
        "snapshot_id": expected_snapshot,
        "question_manifest_sha256": run_manifest.get("question_manifest_sha256"),
        "bundle_manifest_sha256": run_manifest.get("bundle_manifest_sha256"),
        "matched_conditions": True,
        "git_sha": run_manifest.get("git_sha"),
        "git_dirty": run_manifest.get("git_dirty"),
        "python_version": run_manifest.get("python_version"),
        "sqlite_version": run_manifest.get("sqlite_version"),
    }
    if (
        expected_snapshot != current_inputs.get("snapshot_id")
        or expected_input_contract["question_manifest_sha256"]
        != current_inputs.get("question_manifest_sha256")
        or expected_input_contract["bundle_manifest_sha256"]
        != current_inputs.get("bundle_manifest_sha256")
        or not _valid_sha256(expected_input_contract["question_manifest_sha256"])
        or not _valid_sha256(expected_input_contract["bundle_manifest_sha256"])
        or not isinstance(expected_input_contract["git_sha"], str)
        or not expected_input_contract["git_sha"]
        or expected_input_contract["git_dirty"] is not False
        or not isinstance(expected_input_contract["python_version"], str)
        or not expected_input_contract["python_version"]
        or not isinstance(expected_input_contract["sqlite_version"], str)
        or not expected_input_contract["sqlite_version"]
    ):
        raise EvaluationProjectionError("evaluation run manifest lacks exact trace input bindings")
    expected_system_contract = hashlib.sha256(
        canonical_json([asdict(system) for system in SYSTEMS]).encode("utf-8")
    ).hexdigest()
    if run_manifest.get("system_contract_sha256") != expected_system_contract:
        raise EvaluationProjectionError("evaluation run system contract differs from the verifier")
    expected_questions = current_inputs.get("questions")
    if not isinstance(expected_questions, dict) or len(expected_questions) != RELEASE_QUESTION_COUNT:
        raise EvaluationProjectionError("independently verified release questions are unavailable")

    for ordinal, item in enumerate(shards):
        if not isinstance(item, dict):
            raise EvaluationProjectionError("evaluation trace shard entry is not an object")
        path, relative = _safe_run_file(run, item.get("path"))
        relative_path = Path(relative)
        if (
            relative_path.parent != Path("traces")
            or not relative_path.name.endswith(".jsonl.gz")
        ):
            raise EvaluationProjectionError(f"evaluation trace shard path is invalid: {relative}")
        if relative in declared_paths:
            raise EvaluationProjectionError(f"duplicate evaluation trace shard path: {relative}")
        declared_paths.append(relative)
        declared_records = item.get("records")
        declared_bytes = item.get("bytes")
        if (
            not isinstance(declared_records, int)
            or isinstance(declared_records, bool)
            or not 1 <= declared_records <= max_records
            or not isinstance(declared_bytes, int)
            or isinstance(declared_bytes, bool)
            or declared_bytes < 1
            or declared_bytes > MAX_TRACE_COMPRESSED_BYTES
            or path.stat().st_size != declared_bytes
            or not _valid_sha256(item.get("file_sha256"))
            or not _valid_sha256(item.get("canonical_sha256"))
            or _sha256_file(path) != item.get("file_sha256")
        ):
            raise EvaluationProjectionError(f"evaluation trace shard declaration is invalid: {relative}")

        canonical_digest = hashlib.sha256()
        decoded_bytes = 0
        observed_records = 0
        first_key: str | None = None
        last_key: str | None = None
        try:
            with gzip.open(path, "rb") as stream:
                while True:
                    line = stream.readline(MAX_JSON_VALUE_BYTES + 1)
                    if not line:
                        break
                    if len(line) > MAX_JSON_VALUE_BYTES or not line.endswith(b"\n"):
                        raise EvaluationProjectionError(
                            f"evaluation trace record is oversized or unterminated: {relative}"
                        )
                    decoded_bytes += len(line)
                    if decoded_bytes > MAX_TRACE_UNCOMPRESSED_BYTES:
                        raise EvaluationProjectionError(
                            f"evaluation trace shard exceeds its decoded byte bound: {relative}"
                        )
                    canonical_digest.update(line)
                    try:
                        trace = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise EvaluationProjectionError(
                            f"evaluation trace record is invalid JSON: {relative}: {exc}"
                        ) from exc
                    if not isinstance(trace, dict) or line != (canonical_json(trace) + "\n").encode("utf-8"):
                        raise EvaluationProjectionError(
                            f"evaluation trace record is not canonical JSON: {relative}"
                        )
                    system = trace.get("system")
                    question = trace.get("question")
                    input_contract = trace.get("input_contract")
                    system_id = system.get("system_id") if isinstance(system, dict) else None
                    question_id = question.get("question_id") if isinstance(question, dict) else None
                    question_checksum = question.get("checksum") if isinstance(question, dict) else None
                    question_wording = question.get("wording") if isinstance(question, dict) else None
                    output = trace.get("output")
                    if (
                        trace.get("schema") != TRACE_SCHEMA
                        or trace.get("harness_version") != HARNESS_VERSION
                        or trace.get("run_id") != expected_run_id
                        or system_id not in valid_systems
                        or system != asdict(SYSTEM_BY_ID[str(system_id)])
                        or not isinstance(question_id, str)
                        or not question_id
                        or not _valid_sha256(question_checksum)
                        or not isinstance(question_wording, str)
                        or not question_wording
                        or set(question) != {"question_id", "checksum", "wording"}
                        or question != expected_questions.get(question_id)
                        or input_contract != expected_input_contract
                        or not isinstance(output, dict)
                        or not _valid_sha256(trace.get("output_sha256"))
                        or hashlib.sha256(canonical_json(output).encode("utf-8")).hexdigest()
                        != trace.get("output_sha256")
                        or trace.get("usage") != ZERO_TRACE_USAGE
                    ):
                        raise EvaluationProjectionError(
                            f"evaluation trace record does not match the release run: {relative}"
                        )
                    try:
                        outcomes.add(trace)
                    except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
                        raise EvaluationProjectionError(
                            f"evaluation trace does not have the complete make_trace structure: {relative}"
                        ) from exc
                    pair = (str(system_id), question_id)
                    if previous_pair is not None and pair <= previous_pair:
                        raise EvaluationProjectionError(
                            "evaluation traces are duplicated or not in canonical matched-matrix order"
                        )
                    previous_pair = pair
                    system_counts[str(system_id)] += 1
                    question_digests[str(system_id)].update(
                        (canonical_json(question) + "\n").encode("utf-8")
                    )
                    first_key = first_key or question_id
                    last_key = question_id
                    observed_records += 1
        except EvaluationProjectionError:
            raise
        except (EOFError, OSError) as exc:
            raise EvaluationProjectionError(
                f"evaluation trace shard cannot be decompressed: {relative}: {exc}"
            ) from exc
        if (
            observed_records != declared_records
            or canonical_digest.hexdigest() != item.get("canonical_sha256")
            or item.get("first_key") != first_key
            or item.get("last_key") != last_key
        ):
            raise EvaluationProjectionError(
                f"evaluation trace shard contents do not match its declaration: {relative}"
            )
        total_records += observed_records
        outcomes.commit()
        verified_shards.append(
            {
                "ordinal": ordinal,
                "path": relative,
                "records": observed_records,
                "bytes": declared_bytes,
                "file_sha256": item["file_sha256"],
                "canonical_sha256": item["canonical_sha256"],
            }
        )

    if declared_paths != sorted(declared_paths):
        raise EvaluationProjectionError("evaluation trace shard declarations are not sorted")
    inventoried_traces = {path for path in inventoried_paths if path.startswith("traces/")}
    if set(declared_paths) != inventoried_traces:
        raise EvaluationProjectionError(
            "evaluation trace manifest and immutable run inventory disagree"
        )
    if total_records != RELEASE_OUTCOME_COUNT:
        raise EvaluationProjectionError("evaluation trace shards do not cover every matched outcome")
    if set(system_counts) != valid_systems or any(
        system_counts[system_id] != RELEASE_QUESTION_COUNT for system_id in valid_systems
    ):
        raise EvaluationProjectionError("evaluation trace shards do not contain every system/question pair")
    digest_values = {digest.hexdigest() for digest in question_digests.values()}
    if len(digest_values) != 1:
        raise EvaluationProjectionError("evaluation systems were not evaluated against the same questions")
    expected_root = hashlib.sha256(
        "".join(
            f"{item['path']}\0{item['canonical_sha256']}\n" for item in verified_shards
        ).encode("utf-8")
    ).hexdigest()
    if trace_manifest.get("root_sha256") != expected_root:
        raise EvaluationProjectionError("evaluation trace root digest does not match its shards")
    return {
        "records": total_records,
        "shards": len(verified_shards),
        "root_sha256": expected_root,
    }


def _input_contract(run_manifest: dict[str, Any], current_inputs: dict[str, Any]) -> InputContract:
    return InputContract(
        mode="release",
        question_manifest=current_inputs["question_manifest"],
        question_contract=current_inputs["question_contract"],
        verification_report=current_inputs["question_report"],
        bundle_manifest=current_inputs["bundle_manifest"],
        bundle_descriptor=current_inputs["bundle_descriptor"],
        question_manifest_sha256=current_inputs["question_manifest_sha256"],
        bundle_manifest_sha256=current_inputs["bundle_manifest_sha256"],
        snapshot_id=current_inputs["snapshot_id"],
        expected_questions=RELEASE_QUESTION_COUNT,
        release_question_contract_passed=True,
        git_sha=str(run_manifest["git_sha"]),
        git_dirty=False,
        python_version=str(run_manifest["python_version"]),
        sqlite_version=str(run_manifest["sqlite_version"]),
    )


def _replay_and_verify_traces(
    *,
    run_manifest: dict[str, Any],
    current_inputs: dict[str, Any],
    questions: Path,
    bundle: Path,
    outcomes: OutcomeStore,
    temporary: Path,
) -> None:
    """Rerun the deterministic matrix and compare every semantic trace field."""

    contract = _input_contract(run_manifest, current_inputs)
    try:
        index = BundleIndex(bundle, temporary / "replay-index.sqlite", contract)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise EvaluationProjectionError(f"evaluation replay index cannot be built: {exc}") from exc
    question_count = 0
    replayed_outcomes = 0
    try:
        for question in iter_questions(questions):
            question_count += 1
            if question_count > RELEASE_QUESTION_COUNT:
                raise EvaluationProjectionError("evaluation replay exceeds the release question bound")
            try:
                outcomes.register_question(question)
            except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
                raise EvaluationProjectionError(
                    "current release question cannot be registered for deterministic replay"
                ) from exc
            for system in SYSTEMS:
                row = outcomes.connection.execute(
                    "SELECT trace_json FROM outcomes WHERE question_id=? AND system_id=?",
                    (question["question_id"], system.system_id),
                ).fetchone()
                if row is None:
                    raise EvaluationProjectionError(
                        "evaluation replay cannot find every question/system trace"
                    )
                observed = json.loads(str(row[0]))
                try:
                    search = index.search(system, str(question["wording"]))
                    metrics, failures, gold = grade_result(question, search, index)
                    expected = make_trace(
                        run_id=str(run_manifest["run_id"]),
                        system=system,
                        question=question,
                        search=search,
                        metrics=metrics,
                        failures=failures,
                        gold=gold,
                        contract=contract,
                    )
                except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
                    raise EvaluationProjectionError(
                        "evaluation trace cannot be independently replayed from current inputs"
                    ) from exc
                efficiency = observed.get("efficiency")
                latency = efficiency.get("latency_ns") if isinstance(efficiency, dict) else None
                if (
                    not isinstance(latency, int)
                    or isinstance(latency, bool)
                    or not 0 <= latency <= MAX_TRACE_LATENCY_NS
                ):
                    raise EvaluationProjectionError(
                        "evaluation trace latency is not a bounded integer observation"
                    )
                expected["efficiency"]["latency_ns"] = latency
                if observed != expected:
                    raise EvaluationProjectionError(
                        "evaluation trace differs from independent deterministic replay: "
                        f"{system.system_id}/{question['question_id']}"
                    )
                replayed_outcomes += 1
        outcomes.commit()
    finally:
        index.close()
    if question_count != RELEASE_QUESTION_COUNT or replayed_outcomes != RELEASE_OUTCOME_COUNT:
        raise EvaluationProjectionError("evaluation replay did not cover the complete 28,800 by 10 matrix")


def _expected_status(
    run_manifest: dict[str, Any],
    current_inputs: dict[str, Any],
    invariance: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_manifest["run_id"],
        "mode": "release",
        "snapshot_id": current_inputs["snapshot_id"],
        "questions": RELEASE_QUESTION_COUNT,
        "systems": len(SYSTEMS),
        "outcomes": RELEASE_OUTCOME_COUNT,
        "all_questions_all_systems_complete": True,
        "release_question_contract_passed": True,
        "serialization_invariance": invariance,
        "model_usage": {
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_gbp": 0.0,
        },
        "network_requests": 0,
        "agent_evaluation_status": "completed",
        "human_evaluation_status": "not_authorised",
        "human_ui_of_choice_status": "not_yet_testable",
        "machine_evaluation_complete": True,
        "full_evaluation_complete": False,
        "programme_complete": False,
        "release_eligible": True,
        "claim_boundary": (
            "Machine results cover metadata discovery, retrieval ranking, typed relationships, "
            "citation/provenance and abstention. No human preference or body-content answering claim is made."
        ),
    }


def _verify_usage(run: Path, run_manifest: dict[str, Any]) -> None:
    usage = _load_json(run / "usage.json", "evaluation usage evidence")
    wall_seconds = usage.pop("wall_seconds", None)
    new_outcomes = usage.pop("new_outcomes_this_invocation", None)
    if (
        not isinstance(wall_seconds, (int, float))
        or isinstance(wall_seconds, bool)
        or not math.isfinite(float(wall_seconds))
        or not 0 <= float(wall_seconds) <= MAX_RECORDED_WALL_SECONDS
        or not isinstance(new_outcomes, int)
        or isinstance(new_outcomes, bool)
        or not 0 <= new_outcomes <= RELEASE_OUTCOME_COUNT
    ):
        raise EvaluationProjectionError("evaluation usage has invalid bounded runtime observations")
    expected = {
        "schema_version": 1,
        "harness_version": HARNESS_VERSION,
        "execution": "deterministic local Python and SQLite/FTS5",
        "runtime": {
            "git_sha": run_manifest["git_sha"],
            "git_dirty": False,
            "python_version": run_manifest["python_version"],
            "sqlite_version": run_manifest["sqlite_version"],
        },
        "model_usage": {
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_gbp": 0.0,
        },
        "source_access": {
            "mode": "frozen local bundle and independently verified question assets",
            "network_requests": 0,
            "restrictions": [
                "No GOV.UK page body is fetched or retained.",
                "No external search, authenticated source, model provider or paid API is contacted.",
            ],
        },
        "licensing_and_fair_use_triggers": [
            "Evaluation traces retain public metadata identifiers, titles, URLs and short evidence fields only.",
            "Attachment and page bodies are not copied into traces.",
        ],
        "fallbacks_used": [
            "SQLite FTS5 supplies the reproducible lexical baseline; unavailable dense, live Search API, "
            "GOV.UK Chat and internal GovGraph systems remain non-run comparators.",
            "Normal paired cluster intervals are used without a third-party statistics dependency.",
        ],
    }
    if usage != expected:
        raise EvaluationProjectionError("evaluation usage differs from the deterministic run contract")


def _verify_reconstructed_artifacts(
    *,
    run: Path,
    run_manifest: dict[str, Any],
    current_inputs: dict[str, Any],
    outcomes: OutcomeStore,
    temporary: Path,
) -> dict[str, Any]:
    reconstructed = {
        "metrics.json": aggregate_metrics(outcomes.connection),
        "paired-comparisons.json": paired_comparisons(outcomes.connection),
        "slices.json": slice_analysis(outcomes.connection),
        "failure-analysis.json": failure_analysis(outcomes.connection),
    }
    invariance = serialization_invariance(outcomes.connection)
    if invariance.get("passed") is not True:
        raise EvaluationProjectionError("independently reconstructed serialization invariance failed")
    status = _expected_status(run_manifest, current_inputs, invariance)
    reconstructed["status.json"] = status
    for relative, expected in reconstructed.items():
        path = run / relative
        _load_json(path, f"reconstructed {relative}")
        try:
            observed = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise EvaluationProjectionError(f"cannot read reconstructed evidence input: {relative}") from exc
        if observed != _canonical_json(expected):
            raise EvaluationProjectionError(
                f"evaluation {relative} differs from independently reconstructed traces"
            )
    _verify_usage(run, run_manifest)
    expected_report = temporary / "reconstructed-report.md"
    write_report(
        expected_report,
        run_id=str(run_manifest["run_id"]),
        mode="release",
        metrics=reconstructed["metrics.json"],
        status=status,
    )
    try:
        if (run / "report.md").read_bytes() != expected_report.read_bytes():
            raise EvaluationProjectionError(
                "evaluation report.md differs from independently reconstructed traces"
            )
    except OSError as exc:
        raise EvaluationProjectionError(f"evaluation report cannot be compared: {exc}") from exc
    return status


def verify_release_run(
    run: Path,
    *,
    questions: Path,
    bundle: Path,
    repository_root: Path,
) -> dict[str, Any]:
    """Verify every immutable run file plus the release status boundary."""

    run = _repository_path(repository_root, run, "evaluation run", must_exist=True)
    questions = _repository_path(
        repository_root, questions, "release question assets", must_exist=True
    )
    bundle = _repository_path(repository_root, bundle, "release bundle", must_exist=True)
    if not run.is_dir():
        raise EvaluationProjectionError(f"evaluation run directory is missing: {run}")
    if not questions.is_dir() or not bundle.is_dir():
        raise EvaluationProjectionError("release questions and bundle must be directories")
    current_inputs = _verify_current_release_inputs(questions, bundle)
    if (run / ".work").exists():
        raise EvaluationProjectionError("evaluation run is incomplete because .work still exists")
    for path in run.rglob("*"):
        if path.is_symlink():
            raise EvaluationProjectionError(f"evaluation run cannot contain symbolic links: {path}")
    manifest_path = run / "manifest.json"
    manifest = _load_json(manifest_path, "evaluation run manifest")
    if manifest.get("schema_version") != 1 or manifest.get("harness_version") != HARNESS_VERSION:
        raise EvaluationProjectionError("evaluation run manifest schema or harness version is invalid")
    if manifest.get("mode") != "release" or manifest.get("release_eligible") is not True:
        raise EvaluationProjectionError("evaluation run manifest is not release eligible")
    if (
        manifest.get("questions") != RELEASE_QUESTION_COUNT
        or manifest.get("systems") != len(SYSTEMS)
        or manifest.get("outcomes") != RELEASE_OUTCOME_COUNT
        or manifest.get("trace_records") != RELEASE_OUTCOME_COUNT
    ):
        raise EvaluationProjectionError("evaluation run manifest does not contain the complete matched matrix")

    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise EvaluationProjectionError("evaluation run manifest has no file inventory")
    verified: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise EvaluationProjectionError("evaluation run manifest file entry is not an object")
        path, relative = _safe_run_file(run, item.get("path"))
        if relative in seen:
            raise EvaluationProjectionError(f"duplicate evaluation run manifest path: {relative}")
        seen.add(relative)
        size = path.stat().st_size
        digest = _sha256_file(path)
        if item.get("bytes") != size or item.get("sha256") != digest:
            raise EvaluationProjectionError(f"evaluation run file does not match its manifest: {relative}")
        verified.append({"path": relative, "bytes": size, "sha256": digest})
    if verified != sorted(verified, key=lambda row: row["path"]):
        raise EvaluationProjectionError("evaluation run manifest file inventory is not sorted")
    actual = {
        path.relative_to(run).as_posix()
        for path in run.rglob("*")
        if path.is_file()
        and ".work" not in path.parts
        and path.name != ".DS_Store"
        and path.relative_to(run).as_posix() not in {"manifest.json", "checksums.txt"}
    }
    if actual != seen:
        raise EvaluationProjectionError("evaluation run contains unmanifested or missing files")
    expected_root = hashlib.sha256(
        "".join(f"{item['path']}\0{item['sha256']}\n" for item in verified).encode("utf-8")
    ).hexdigest()
    if manifest.get("root_sha256") != expected_root:
        raise EvaluationProjectionError("evaluation run root digest does not match its file inventory")

    checksum_paths = sorted(
        path
        for path in run.rglob("*")
        if path.is_file()
        and ".work" not in path.parts
        and path.name not in {"checksums.txt", ".DS_Store"}
    )
    expected_checksums = "".join(
        f"{_sha256_file(path)}  {path.relative_to(run).as_posix()}\n" for path in checksum_paths
    )
    checksum_path = run / "checksums.txt"
    if checksum_path.is_symlink():
        raise EvaluationProjectionError("evaluation run checksum ledger cannot be a symbolic link")
    try:
        observed_checksums = checksum_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EvaluationProjectionError(f"invalid evaluation run checksum ledger: {exc}") from exc
    if observed_checksums != expected_checksums:
        raise EvaluationProjectionError("evaluation run checksum ledger does not match the immutable run")

    missing_projection_inputs = sorted(set(REQUIRED_RUN_FILES) - seen)
    if missing_projection_inputs:
        raise EvaluationProjectionError(
            "evaluation run lacks required projection files: " + ", ".join(missing_projection_inputs)
        )
    status = _load_json(run / "status.json", "evaluation release status")
    if (
        status.get("run_id") != manifest.get("run_id")
        or status.get("snapshot_id") != manifest.get("snapshot_id")
        or status.get("mode") != "release"
        or status.get("questions") != RELEASE_QUESTION_COUNT
        or status.get("systems") != len(SYSTEMS)
        or status.get("outcomes") != RELEASE_OUTCOME_COUNT
        or status.get("all_questions_all_systems_complete") is not True
        or status.get("release_question_contract_passed") is not True
        or status.get("machine_evaluation_complete") is not True
        or status.get("agent_evaluation_status") != "completed"
        or status.get("release_eligible") is not True
    ):
        raise EvaluationProjectionError("evaluation status is not a complete release-bound matched run")
    if status.get("network_requests") != 0 or status.get("model_usage") != {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_gbp": 0.0,
    }:
        raise EvaluationProjectionError("deterministic evaluation status records external or model usage")
    if (
        status.get("human_evaluation_status") not in {"not_authorised", "not_yet_testable"}
        or status.get("human_ui_of_choice_status") != "not_yet_testable"
        or status.get("full_evaluation_complete") is not False
        or status.get("programme_complete") is not False
    ):
        raise EvaluationProjectionError("evaluation status crosses the authorised machine-only claim boundary")
    trace_manifest = _load_json(run / "trace-manifest.json", "evaluation trace manifest")
    with tempfile.TemporaryDirectory(prefix="govuk-okf-evaluation-verify-") as temporary_directory:
        temporary = Path(temporary_directory)
        try:
            outcomes = OutcomeStore(
                temporary / "verified-outcomes.sqlite",
                f"projection:{manifest['root_sha256']}",
            )
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise EvaluationProjectionError(
                f"evaluation reconstruction store cannot be created: {exc}"
            ) from exc
        try:
            trace_verification = _verify_trace_manifest(
                run,
                trace_manifest,
                run_manifest=manifest,
                inventoried_paths=seen,
                current_inputs=current_inputs,
                outcomes=outcomes,
            )
            _replay_and_verify_traces(
                run_manifest=manifest,
                current_inputs=current_inputs,
                questions=questions,
                bundle=bundle,
                outcomes=outcomes,
                temporary=temporary,
            )
            reconstructed_status = _verify_reconstructed_artifacts(
                run=run,
                run_manifest=manifest,
                current_inputs=current_inputs,
                outcomes=outcomes,
                temporary=temporary,
            )
        finally:
            outcomes.close()
    return {
        "manifest": manifest,
        "manifest_sha256": _sha256_file(manifest_path),
        "status": reconstructed_status,
        "trace_verification": trace_verification,
        "current_inputs": {
            key: current_inputs[key]
            for key in ("snapshot_id", "question_manifest_sha256", "bundle_manifest_sha256")
        },
        "verified_files": verified,
    }


def project_release_results(
    *,
    run: Path,
    questions: Path,
    bundle: Path,
    output: Path,
    source_reference: str,
    repository_root: Path,
) -> dict[str, Any]:
    """Atomically publish the small canonical evidence set from a verified run."""

    if repository_root.is_symlink():
        raise EvaluationProjectionError("repository root cannot be a symbolic link")
    root = repository_root.resolve()
    run = _repository_path(root, run, "evaluation run", must_exist=True)
    questions = _repository_path(root, questions, "release question assets", must_exist=True)
    bundle = _repository_path(root, bundle, "release bundle", must_exist=True)
    output = _repository_path(root, output, "canonical evaluation output", must_exist=False)
    if output == root:
        raise EvaluationProjectionError("canonical evaluation output cannot be the repository root")
    if output == run or output.is_relative_to(run) or run.is_relative_to(output):
        raise EvaluationProjectionError("canonical results must be disjoint from the immutable run")
    reference = Path(source_reference)
    if reference.is_absolute() or ".." in reference.parts or not reference.parts:
        raise EvaluationProjectionError("source_reference must be a safe repository-relative path")
    if reference.as_posix() != run.relative_to(root).as_posix():
        raise EvaluationProjectionError("source_reference does not identify the verified repository run")
    verified = verify_release_run(
        run,
        questions=questions,
        bundle=bundle,
        repository_root=root,
    )
    manifest = verified["manifest"]
    projection_files = [
        {
            "path": relative,
            "bytes": (run / relative).stat().st_size,
            "sha256": _sha256_file(run / relative),
        }
        for relative in PROJECTED_FILES
    ]
    projection = {
        "schema": "govuk-okf-evaluation-results-projection.v1",
        "source_run": reference.as_posix(),
        "source_run_manifest_sha256": verified["manifest_sha256"],
        "source_run_root_sha256": manifest["root_sha256"],
        "source_trace_manifest": f"{reference.as_posix()}/trace-manifest.json",
        "source_trace_manifest_sha256": _sha256_file(run / "trace-manifest.json"),
        "source_trace_root_sha256": verified["trace_verification"]["root_sha256"],
        "source_trace_shards": verified["trace_verification"]["shards"],
        "source_questions": questions.relative_to(root).as_posix(),
        "source_question_manifest_sha256": verified["current_inputs"][
            "question_manifest_sha256"
        ],
        "source_bundle": bundle.relative_to(root).as_posix(),
        "source_bundle_manifest_sha256": verified["current_inputs"][
            "bundle_manifest_sha256"
        ],
        "run_id": manifest["run_id"],
        "snapshot_id": manifest["snapshot_id"],
        "questions": manifest["questions"],
        "systems": manifest["systems"],
        "outcomes": manifest["outcomes"],
        "release_eligible": True,
        "files": projection_files,
    }
    projection_text = _canonical_json(projection)
    if output.exists():
        if output.is_symlink() or not output.is_dir():
            raise EvaluationProjectionError("canonical evaluation output must be a non-symlink directory")
        for path in output.rglob("*"):
            if path.is_symlink():
                raise EvaluationProjectionError(
                    f"canonical evaluation output cannot contain symbolic links: {path}"
                )
        current = output / "projection.json"
        if current.is_symlink():
            raise EvaluationProjectionError("canonical evaluation projection control cannot be a symbolic link")
        if current.is_file() and current.read_text(encoding="utf-8") == projection_text:
            for item in projection_files:
                target = output / item["path"]
                if (
                    target.is_symlink()
                    or not target.is_file()
                    or target.stat().st_size != item["bytes"]
                    or _sha256_file(target) != item["sha256"]
                ):
                    raise EvaluationProjectionError("existing canonical evaluation projection is corrupt")
            return projection
        raise EvaluationProjectionError(f"canonical evaluation output already exists for another run: {output}")

    output.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_components(root, output.parent, "canonical evaluation output parent")
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    if temporary.exists():
        raise EvaluationProjectionError(f"stale evaluation projection transaction exists: {temporary}")
    try:
        temporary.mkdir()
        for relative in PROJECTED_FILES:
            shutil.copyfile(run / relative, temporary / relative)
        (temporary / "projection.json").write_text(projection_text, encoding="utf-8")
        os.replace(temporary, output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return projection
