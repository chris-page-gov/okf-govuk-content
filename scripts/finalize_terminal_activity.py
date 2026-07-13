#!/usr/bin/env python3
"""Construct and append artifact-derived post-hydration terminal activities.

The command never accepts a pre-authored activity row.  It resolves the event
contract from ``provenance/reproduction-declarations.json``, verifies the
canonical artifacts for that event, builds the v2 ledger row, and appends it
under the same side lock as ``append_activity.py``.  Repeating an exact command
is idempotent; an activity ID with different evidence is a hard failure.

The ``source-budget`` operation is a cooperative two-file transaction.  It
updates the final source-request snapshot and appends the matching terminal
while holding the activity-ledger lock.  A small write-ahead journal lets the
next invocation finish a fully written transaction or roll back a partial one.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from append_activity import (  # noqa: E402
    append_entries,
    canonical_line,
    ledger_lock,
)
import check_release as release_checks  # noqa: E402
from govuk_okf.evaluation import verify_question_inputs  # noqa: E402
from govuk_okf.evaluation_projection import verify_release_run  # noqa: E402
from govuk_okf.publication_validation import validate_bundle  # noqa: E402
from govuk_okf.repository_policy import (  # noqa: E402
    compare_api_capture,
    compare_publication_api_capture,
)
from govuk_okf.sharded_jsonl import iter_jsonl_records  # noqa: E402
from govuk_okf.util import canonical_json_bytes  # noqa: E402


DEFAULT_LEDGER = ROOT / "provenance" / "activity-ledger.jsonl"
DEFAULT_SCHEMA = ROOT / "provenance" / "activity-ledger.schema.json"
DEFAULT_DECLARATIONS = ROOT / "provenance" / "reproduction-declarations.json"
DEFAULT_LIVE_COUNTER = ROOT / ".tmp" / "request-budget" / "official-sources.count"
DEFAULT_BUDGET_SNAPSHOT = ROOT / "provenance" / "source-request-budget.json"
DEFAULT_CITATION_AGGREGATE = ROOT / "provenance" / "citation-request-aggregate.json"
DISALLOWED_RELEASE_MARKERS = ("fixture", "sample", "capacity", "development", "test", "pending")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
TAG_RE = re.compile(r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-rc\.[1-9][0-9]*)?$")
LEGACY_INTERVAL_RE = re.compile(r"(?:shared[- ]counter|counter) interval\s+(\d+)\.\.(\d+)", re.I)
LEGACY_ADVANCE_RE = re.compile(r"(?:shared[- ]counter|counter)(?:\s+\S+){0,3}\s+advanced from\s+(\d+)\s+to\s+(\d+)", re.I)

TERMINAL_IDS = {
    "hydration": "ACT-D1-T0-HYDRATION-TERMINAL-001",
    "reconciliation": "ACT-E1-T1-RECONCILIATION-TERMINAL-001",
    "questions": "ACT-C1-RELEASE-V2-TERMINAL-001",
    "evaluation": "ACT-E2-AUTOMATED-EVALUATION-TERMINAL-001",
    "citations": "ACT-F2-RELEASE-SNAPSHOT-CITATION-REVIEWS-TERMINAL-001",
    "shards": "ACT-D1-SHARD-CONTRACT-AUDIT-TERMINAL-001",
    "security": "ACT-D2-RELEASE-SNAPSHOT-SECURITY-SCAN-TERMINAL-001",
    "publication": "ACT-F2-PUBLICATION-REGISTRY-TERMINAL-001",
    "source-budget": "ACT-F2-SOURCE-REQUEST-BUDGET-TERMINAL-001",
}
T0_TERMINAL = "ACT-B1-T0-20260712-TERMINAL-001"
HYDRATION_HARDENING_ACTIVITY = "ACT-D1-HYDRATION-HARDENING-20260713-001"
SHARED_REQUEST_TERMINALS = (
    T0_TERMINAL,
    HYDRATION_HARDENING_ACTIVITY,
    TERMINAL_IDS["hydration"],
    TERMINAL_IDS["reconciliation"],
)


class ClosureError(ValueError):
    """Raised when terminal evidence is absent, inconsistent, or unsafe."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, label: str, *, maximum: int = 64 * 1024 * 1024) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ClosureError(f"{label} is not a regular non-symlink file: {path}")
    if path.stat().st_size > maximum:
        raise ClosureError(f"{label} exceeds {maximum} bytes: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClosureError(f"{label} is invalid: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ClosureError(f"{label} must be an object: {path}")
    return value


def _repository_path(
    root: Path,
    value: str | Path,
    label: str,
    *,
    kind: str = "file",
    must_exist: bool = True,
) -> Path:
    root = root.absolute()
    if root.is_symlink():
        raise ClosureError("repository root cannot be a symbolic link")
    resolved_root = root.resolve()
    supplied = Path(value)
    if supplied.is_absolute():
        candidate = supplied.absolute()
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise ClosureError(f"{label} is outside the repository: {value}") from exc
    else:
        text = supplied.as_posix()
        reference = PurePosixPath(text)
        if not text or reference.is_absolute() or ".." in reference.parts or reference.as_posix() != text:
            raise ClosureError(f"{label} is not a safe repository-relative path: {value}")
        relative = Path(*reference.parts)
        candidate = root / relative
    lexical = root
    for part in relative.parts:
        lexical /= part
        if lexical.is_symlink():
            raise ClosureError(f"{label} contains a symbolic-link component: {value}")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ClosureError(f"{label} escapes the repository: {value}") from exc
    if must_exist:
        if kind == "file" and not resolved.is_file():
            raise ClosureError(f"{label} is missing or not a file: {value}")
        if kind == "directory" and not resolved.is_dir():
            raise ClosureError(f"{label} is missing or not a directory: {value}")
    return resolved


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _output(root: Path, path: Path, state: str = "produced") -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ClosureError(f"terminal output is not a regular file: {path}")
    return {"path": _relative(root, path), "state": state, "sha256": _sha256_file(path)}


def _parse_time(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ClosureError(f"{label} must be an exact UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ClosureError(f"{label} is not an ISO-8601 timestamp: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ClosureError(f"{label} must include an exact UTC offset: {value}")
    return parsed.astimezone(UTC)


def _time_equal(left: str, right: str) -> bool:
    return _parse_time(left, "timestamp") == _parse_time(right, "timestamp")


def _validate_times(started_at: str, ended_at: str, recorded_at: str) -> None:
    start = _parse_time(started_at, "started_at")
    end = _parse_time(ended_at, "ended_at")
    recorded = _parse_time(recorded_at, "recorded_at")
    if start > end:
        raise ClosureError("started_at is after ended_at")
    if end > recorded:
        raise ClosureError("ended_at is after recorded_at")


def _release_snapshot(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "/" in value or "\\" in value:
        raise ClosureError("snapshot must be one safe non-empty identifier")
    normalised = value.casefold()
    if any(marker in normalised for marker in DISALLOWED_RELEASE_MARKERS):
        raise ClosureError(f"snapshot is not release eligible: {value}")
    return value


def _counter_interval(label: str, start: int | None, end: int | None, ledger: str | None) -> dict[str, Any]:
    if not isinstance(start, int) or isinstance(start, bool):
        raise ClosureError(f"{label} requires --request-start")
    if not isinstance(end, int) or isinstance(end, bool):
        raise ClosureError(f"{label} requires --request-end")
    if start < 0 or end < start:
        raise ClosureError(f"{label} request interval is invalid: {start}..{end}")
    return {
        "label": label,
        "start_counter": start,
        "end_counter": end,
        "attempts": end - start,
        "ledger": ledger,
    }


def _declaration(path: Path, activity_id: str) -> dict[str, Any]:
    document = _read_json(path, "reproduction declarations")
    if document.get("schema") != "afhf-govuk-okf-reproduction-declarations.v1":
        raise ClosureError("reproduction declarations use an unsupported schema")
    rows = document.get("final_activity_entries_required")
    if not isinstance(rows, list):
        raise ClosureError("reproduction declarations have no terminal activity contracts")
    matches = [row for row in rows if isinstance(row, dict) and row.get("terminal_activity_id") == activity_id]
    if len(matches) != 1:
        raise ClosureError(f"terminal activity is not declared exactly once: {activity_id}")
    row = matches[0]
    if not isinstance(row.get("event"), str) or not isinstance(row.get("must_bind_release_snapshot"), bool):
        raise ClosureError(f"terminal declaration is incomplete: {activity_id}")
    required = row.get("required_evidence")
    if not isinstance(required, list) or not required or any(not isinstance(item, str) for item in required):
        raise ClosureError(f"terminal declaration has no typed evidence contract: {activity_id}")
    return row


def _required_supersedes(declaration: dict[str, Any]) -> list[str]:
    value = declaration.get("must_supersede")
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return list(value)
    raise ClosureError("terminal declaration has an invalid must_supersede contract")


def _ledger_rows(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if path.is_symlink():
        raise ClosureError("activity ledger cannot be a symbolic link")
    rows: list[dict[str, Any]] = []
    if path.is_file():
        try:
            content = path.read_bytes()
            if content and not content.endswith(b"\n"):
                raise ClosureError("activity ledger does not end at a complete newline-delimited row")
            raw_lines = content.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ClosureError(f"activity ledger is not UTF-8: {exc}") from exc
    else:
        raw_lines = []
    identifiers: set[str] = set()
    for number, raw in enumerate(raw_lines, start=1):
        if not raw:
            raise ClosureError(f"activity ledger has a blank line at {number}")
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ClosureError(f"activity ledger line {number} is invalid JSON: {exc}") from exc
        if not isinstance(row, dict) or not isinstance(row.get("activity_id"), str):
            raise ClosureError(f"activity ledger line {number} is not an activity object")
        activity_id = row["activity_id"]
        if activity_id in identifiers:
            raise ClosureError(f"activity ledger duplicates {activity_id}")
        identifiers.add(activity_id)
        if row.get("ledger_schema_version") == "2.0":
            expected = _sha256_bytes(raw_lines[number - 2].encode("utf-8")) if number > 1 else None
            if row.get("previous_entry_sha256") != expected:
                raise ClosureError(f"activity ledger line {number} does not hash-chain")
        rows.append(row)
    return rows, raw_lines


def _ensure_supersedures(rows: list[dict[str, Any]], declaration: dict[str, Any]) -> None:
    by_id = {row["activity_id"]: row for row in rows}
    already = {
        prior: row["activity_id"]
        for row in rows
        for prior in row.get("supersedes_activity_ids", [])
        if isinstance(prior, str)
    }
    for prior in _required_supersedes(declaration):
        if prior not in by_id:
            raise ClosureError(f"required superseded activity is absent: {prior}")
        if prior in already and already[prior] != declaration["terminal_activity_id"]:
            raise ClosureError(f"required activity {prior} is already superseded by {already[prior]}")


def _base_entry(
    *,
    declaration: dict[str, Any],
    snapshot: str,
    started_at: str,
    ended_at: str,
    recorded_at: str,
    outputs: list[dict[str, Any]],
    results: list[str],
    source_request_usage: dict[str, Any],
    tool: str,
    command: str,
    commit: str | None = None,
    extra_snapshots: Iterable[str] = (),
    model_assisted: bool = False,
) -> dict[str, Any]:
    _validate_times(started_at, ended_at, recorded_at)
    activity_id = str(declaration["terminal_activity_id"])
    snapshots = list(dict.fromkeys([snapshot, *extra_snapshots]))
    if declaration["must_bind_release_snapshot"] is True:
        _release_snapshot(snapshot)
    model: dict[str, Any] | None = None
    work_class = "deterministic"
    tokens: int | str = 0
    cost: int | str = 0
    prompt = {"capture_status": "not_applicable", "objective": "", "reference": None, "sha256": None}
    product_usage: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0, "marginal_cost_gbp": 0}
    if model_assisted:
        work_class = "mixed"
        tokens = "unavailable"
        cost = "unavailable"
        prompt = {
            "capture_status": "bounded_objective_only",
            "objective": str(declaration["event"]),
            "reference": "provenance/reproduction-declarations.json#final_activity_entries_required",
            "sha256": None,
        }
        model = {
            "provider_product": "OpenAI Codex product session",
            "family": "GPT-5",
            "exact_version": "unavailable_to_session",
            "parameters": "unavailable_to_session",
            "independence_limit": "Product-session implementation or review is not an independent provider judgement; artifact and deterministic validation evidence remain authoritative.",
        }
        product_usage = {
            "input_tokens": "unavailable_in_product_session",
            "output_tokens": "unavailable_in_product_session",
            "marginal_cost_gbp": "unavailable_in_product_session",
        }
    entry: dict[str, Any] = {
        "ledger_schema_version": "2.0",
        "activity_id": activity_id,
        "status": "completed",
        "work_class": work_class,
        "started_at": started_at,
        "ended_at": ended_at,
        "recorded_at": recorded_at,
        "commit": commit,
        "agent": {
            "id": "CPython terminal-closure verifier",
            "role": f"{declaration['event']} evidence finalizer",
            "relationship": "deterministic_process",
        },
        "prompt": prompt,
        "model": model,
        "tool_calls": {
            "capture_status": "complete",
            "calls": [{"tool": tool, "command": command, "purpose": str(declaration["event"]), "call_count": 1}],
        },
        "source_snapshots": snapshots,
        "outputs": outputs,
        "validation": {"capture_status": "complete", "results": results},
        "source_request_usage": source_request_usage,
        "usage": {
            "external_paid_model": {"api_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_gbp": 0},
            "product_session": product_usage,
        },
        "tokens": tokens,
        "cost_gbp": cost,
        "external_paid_model_api_calls": 0,
    }
    supersedes = _required_supersedes(declaration)
    if supersedes:
        entry["supersedes_activity_ids"] = supersedes
    return entry


def _not_applicable_requests(evidence: str) -> dict[str, Any]:
    return {
        "status": "not_applicable",
        "attempts": "not_applicable",
        "budget_ledger": None,
        "observation_at": None,
        "included_in_model_cost": False,
        "evidence": evidence,
        "intervals": [],
    }


def _validate_entry_contract(
    entry: dict[str, Any],
    declaration: dict[str, Any],
    root: Path,
) -> None:
    """Validate the declaration-driven contract and every existing output byte."""

    if entry.get("activity_id") != declaration.get("terminal_activity_id"):
        raise ClosureError("terminal entry identity differs from its declaration")
    snapshots = entry.get("source_snapshots")
    if declaration.get("must_bind_release_snapshot") is True and (
        not isinstance(snapshots, list) or not snapshots or not _release_snapshot(str(snapshots[0]))
    ):
        raise ClosureError("terminal entry does not bind a release-eligible snapshot")
    expected_supersedes = _required_supersedes(declaration)
    if entry.get("supersedes_activity_ids", []) != expected_supersedes:
        raise ClosureError("terminal entry supersedures differ from its declaration")
    outputs = entry.get("outputs")
    if not isinstance(outputs, list):
        raise ClosureError("terminal entry has no output bindings")
    paths = [row.get("path") for row in outputs if isinstance(row, dict)]
    if len(paths) != len(outputs) or len(paths) != len(set(paths)):
        raise ClosureError("terminal output paths are missing or duplicated")
    for required in declaration.get("required_output_paths", []):
        if paths.count(required) != 1:
            raise ClosureError(f"terminal entry does not bind exactly one required output: {required}")
    for row in outputs:
        path = _repository_path(root, str(row.get("path") or ""), "terminal output")
        expected = row.get("sha256")
        if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
            raise ClosureError(f"terminal output has no exact SHA-256: {row.get('path')}")
        if _sha256_file(path) != expected:
            raise ClosureError(f"terminal output changed before append: {row.get('path')}")
    request = entry.get("source_request_usage")
    if not isinstance(request, dict):
        raise ClosureError("terminal entry has no source-request usage")
    intervals = request.get("intervals")
    if not isinstance(intervals, list):
        raise ClosureError("terminal entry has no structured request-interval declaration")
    for interval in intervals:
        if not isinstance(interval, dict):
            raise ClosureError("terminal request interval is not an object")
        start = interval.get("start_counter")
        end = interval.get("end_counter")
        attempts = interval.get("attempts")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or start < 0
            or end < start
            or attempts != end - start
        ):
            raise ClosureError("terminal request interval arithmetic does not reconcile")
    if request.get("status") == "exact":
        attempts = request.get("attempts")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or not intervals:
            raise ClosureError("exact terminal source requests require a structured exact interval")
        if sum(int(row["attempts"]) for row in intervals) != attempts:
            raise ClosureError("terminal request intervals differ from the exact request total")
    elif intervals:
        raise ClosureError("non-exact terminal source-request usage cannot contain intervals")


def _exact_requests(interval: dict[str, Any], observation_at: str, evidence: str) -> dict[str, Any]:
    return {
        "status": "exact",
        "attempts": interval["attempts"],
        "budget_ledger": interval["ledger"],
        "observation_at": observation_at,
        "included_in_model_cost": False,
        "evidence": evidence,
        "intervals": [interval],
    }


def _stream_shards(root: Path, relative: object, expected_count: int, label: str) -> tuple[Path, int]:
    if not isinstance(relative, str):
        raise ClosureError(f"{label} has no shard index path")
    path = _repository_path(root, relative, f"{label} shard index")
    count = sum(1 for _ in iter_jsonl_records(path))
    if count != expected_count:
        raise ClosureError(f"{label} shard count differs: {count} != {expected_count}")
    return path, count


def _sum_attempts(source_manifest: dict[str, Any]) -> int:
    rows = source_manifest.get("sources")
    if not isinstance(rows, list) or not rows:
        raise ClosureError("source manifest has no request observations")
    total = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ClosureError(f"source manifest observation {index} is not an object")
        value = row.get("attempts", row.get("acquisition_attempt", 1))
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ClosureError(f"source manifest observation {index} has invalid attempts")
        total += value
    return total


def build_hydration(args: argparse.Namespace, declaration: dict[str, Any]) -> dict[str, Any]:
    root = args.root
    reconciliation_path = _repository_path(
        root,
        args.reconciliation or f"corpus/reconciliation/{args.snapshot}-hydrated.json",
        "T0 hydration reconciliation",
    )
    manifest_path = _repository_path(
        root,
        args.manifest or f"corpus/records/{args.snapshot}/manifest.json",
        "T0 hydration manifest",
    )
    reconciliation = _read_json(reconciliation_path, "T0 hydration reconciliation")
    manifest = _read_json(manifest_path, "T0 hydration manifest")
    interval = _counter_interval(
        "t0_hydration",
        args.request_start,
        args.request_end,
        ".tmp/request-budget/official-sources.count",
    )
    proof = reconciliation.get("hydration_proof") or {}
    gap = reconciliation.get("rendered_gap_proof") or {}
    accounting = gap.get("request_accounting") or {}
    counts = [reconciliation.get(key) for key in ("represented", "alias_of_represented", "redirect_only", "tombstone_only", "exceptioned")]
    expected = reconciliation.get("expected_candidate_keys")
    if (
        reconciliation.get("schema_version") != 1
        or reconciliation.get("snapshot") != args.snapshot
        or reconciliation.get("hydrated") is not True
        or reconciliation.get("sampled") is not False
        or reconciliation.get("unexplained_omissions") != 0
        or proof.get("closed") is not True
        or proof.get("pending") != 0
        or reconciliation.get("complete_page_bodies_retained") is True
        or gap.get("closed") is not True
        or gap.get("retained_body_bytes") != 0
        or not isinstance(expected, int)
        or any(not isinstance(value, int) for value in counts)
        or sum(counts) != expected
    ):
        raise ClosureError("T0 hydration reconciliation is not a closed unsampled metadata-only corpus")
    if accounting.get("this_hydration_attempts") != interval["attempts"]:
        raise ClosureError("T0 hydration request interval differs from the artifact attempt count")
    if accounting.get("programme_cumulative_attempts") != interval["end_counter"]:
        raise ClosureError("T0 hydration request end differs from the artifact cumulative counter")
    if manifest.get("snapshot") != args.snapshot or manifest.get("complete_page_bodies_retained") is not False:
        raise ClosureError("T0 hydration manifest snapshot/body boundary is invalid")
    if (
        manifest.get("reconciliation") != _relative(root, reconciliation_path)
        or manifest.get("source_record_manifest") != reconciliation.get("hydrated_records_path")
        or manifest.get("candidate_record_manifest") != reconciliation.get("candidate_ledger_path")
        or manifest.get("rendered_gap_proof") != gap
    ):
        raise ClosureError("T0 hydration manifest is not exactly bound to its reconciliation and shards")
    if not isinstance(manifest.get("completed_at"), str) or not _time_equal(manifest["completed_at"], args.ended_at):
        raise ClosureError("--ended-at must equal the hydration manifest completed_at")
    if manifest.get("source_records") != reconciliation.get("publication_records"):
        raise ClosureError("T0 hydration source-record count differs between control documents")
    if manifest.get("candidate_records") != expected:
        raise ClosureError("T0 hydration candidate count differs between control documents")
    source_path, source_count = _stream_shards(
        root,
        reconciliation.get("hydrated_records_path"),
        int(reconciliation["publication_records"]),
        "hydrated source records",
    )
    candidate_path, candidate_count = _stream_shards(
        root,
        reconciliation.get("candidate_ledger_path"),
        expected,
        "hydrated candidates",
    )
    return _base_entry(
        declaration=declaration,
        snapshot=args.snapshot,
        started_at=args.started_at,
        ended_at=args.ended_at,
        recorded_at=args.recorded_at,
        outputs=[
            _output(root, manifest_path),
            _output(root, reconciliation_path),
            _output(root, source_path),
            _output(root, candidate_path),
        ],
        results=[
            f"Hydration queue closed with {source_count} source records, {candidate_count} candidate keys and zero unexplained omissions.",
            f"Exact shared-counter interval {interval['start_counter']}..{interval['end_counter']} records {interval['attempts']} Content API, rendered-page and robots attempts.",
            "Rendered-gap evidence retains zero body bytes and the manifest declares that complete page bodies were not retained.",
            f"Hydrated source and candidate indexes passed bounded canonical shard validation with SHA-256 {_sha256_file(source_path)} and {_sha256_file(candidate_path)}.",
        ],
        source_request_usage=_exact_requests(
            interval,
            args.ended_at,
            f"Exact shared-counter interval {interval['start_counter']}..{interval['end_counter']} equals the hydration manifest request accounting.",
        ),
        tool="CPython terminal-closure verifier",
        command=f"python3 scripts/finalize_terminal_activity.py hydration --snapshot {args.snapshot} ...",
    )


def build_reconciliation(args: argparse.Namespace, declaration: dict[str, Any]) -> dict[str, Any]:
    root = args.root
    snapshot = _release_snapshot(args.snapshot)
    reconciliation_path = _repository_path(
        root,
        args.reconciliation or f"corpus/reconciliation/{snapshot}.json",
        "closing reconciliation",
    )
    manifest_path = _repository_path(
        root,
        args.manifest or f"corpus/records/{snapshot}/manifest.json",
        "closing manifest",
    )
    reconciliation = _read_json(reconciliation_path, "closing reconciliation")
    manifest = _read_json(manifest_path, "closing manifest")
    t1_snapshot = reconciliation.get("t1_snapshot")
    if not isinstance(t1_snapshot, str) or not t1_snapshot:
        raise ClosureError("closing reconciliation has no T1 snapshot")
    t1_manifest_path = _repository_path(
        root,
        args.t1_manifest or f"corpus/source-manifests/{t1_snapshot}/manifest.json",
        "T1 source manifest",
    )
    t1_manifest = _read_json(t1_manifest_path, "T1 source manifest")
    interval = _counter_interval(
        "t1_enumeration_and_closing",
        args.request_start,
        args.request_end,
        ".tmp/request-budget/official-sources.count",
    )
    accounting = reconciliation.get("request_accounting") or {}
    proof = reconciliation.get("hydration_proof") or {}
    counts = [reconciliation.get(key) for key in ("represented", "alias_of_represented", "redirect_only", "tombstone_only", "exceptioned")]
    expected = reconciliation.get("expected_candidate_keys")
    if (
        reconciliation.get("schema_version") != 1
        or reconciliation.get("snapshot") != snapshot
        or reconciliation.get("hydrated") is not True
        or reconciliation.get("sampled") is not False
        or reconciliation.get("pending") != 0
        or reconciliation.get("unexplained_omissions") != 0
        or reconciliation.get("complete_page_bodies_retained") is not False
        or proof.get("closed") is not True
        or proof.get("pending") != 0
        or reconciliation.get("search_partitions_closed") is not True
        or reconciliation.get("sitemap_byte_stable") is not True
        or not isinstance(expected, int)
        or any(not isinstance(value, int) for value in counts)
        or sum(counts) != expected
    ):
        raise ClosureError("closing reconciliation is not a closed unsampled release snapshot")
    if manifest.get("snapshot") != snapshot or manifest.get("reconciliation") != reconciliation:
        raise ClosureError("closing manifest is not exactly bound to the reconciliation")
    if t1_manifest.get("snapshot") != t1_snapshot:
        raise ClosureError("T1 source manifest is bound to another snapshot")
    t1_attempts = _sum_attempts(t1_manifest)
    closing_attempts = accounting.get("closing_stage_used")
    if not isinstance(closing_attempts, int) or closing_attempts < 0:
        raise ClosureError("closing request accounting has no exact closing-stage total")
    if accounting.get("reserved") != 0 or accounting.get("other_concurrent_stage_used") != 0:
        raise ClosureError("closing request accounting has unsettled or concurrent attempts")
    if accounting.get("used") != interval["end_counter"]:
        raise ClosureError("closing request accounting differs from --request-end")
    if accounting.get("prior_stage_used") != interval["start_counter"] + t1_attempts:
        raise ClosureError("T1 acquisition interval does not reconcile with the closing baseline")
    if interval["attempts"] != t1_attempts + closing_attempts:
        raise ClosureError("T1 plus closing artifact attempts differ from the supplied request interval")
    for key in ("started_at", "completed_at"):
        if not isinstance(t1_manifest.get(key), str):
            raise ClosureError(f"T1 source manifest has no exact {key}")
    if _parse_time(args.started_at, "started_at") > _parse_time(t1_manifest["started_at"], "T1 started_at"):
        raise ClosureError("terminal started_at is after T1 enumeration started")
    if _parse_time(args.ended_at, "ended_at") < _parse_time(t1_manifest["completed_at"], "T1 completed_at"):
        raise ClosureError("terminal ended_at is before T1 enumeration completed")
    watermark = reconciliation.get("closing_watermark")
    if not isinstance(watermark, str) or _parse_time(args.ended_at, "ended_at") < _parse_time(watermark, "closing watermark"):
        raise ClosureError("terminal ended_at is before the closing watermark")
    source_path, source_count = _stream_shards(
        root,
        reconciliation.get("hydrated_records_path"),
        int(reconciliation["publication_records"]),
        "closing source records",
    )
    candidate_path, candidate_count = _stream_shards(
        root,
        reconciliation.get("candidate_ledger_path"),
        expected,
        "closing candidates",
    )
    return _base_entry(
        declaration=declaration,
        snapshot=snapshot,
        started_at=args.started_at,
        ended_at=args.ended_at,
        recorded_at=args.recorded_at,
        outputs=[
            _output(root, reconciliation_path),
            _output(root, manifest_path),
            _output(root, t1_manifest_path),
            _output(root, source_path),
            _output(root, candidate_path),
        ],
        results=[
            f"T1 {t1_snapshot} and closing snapshot {snapshot} reconcile {candidate_count} candidates to {source_count} publication records with zero unexplained omissions.",
            f"Exact shared-counter interval {interval['start_counter']}..{interval['end_counter']} comprises {t1_attempts} T1 enumerator and {closing_attempts} closing attempts.",
            f"Closing watermark is {watermark}; Search partitions, sitemap stability, queue closure and entity-class accounting passed.",
            f"Closing source and candidate shard indexes are hash-bound as {_sha256_file(source_path)} and {_sha256_file(candidate_path)}.",
        ],
        source_request_usage=_exact_requests(
            interval,
            args.ended_at,
            f"Exact shared-counter interval {interval['start_counter']}..{interval['end_counter']} reconciles T1 enumeration plus closing-stage accounting.",
        ),
        tool="CPython terminal-closure verifier",
        command=f"python3 scripts/finalize_terminal_activity.py reconciliation --snapshot {snapshot} ...",
    )


def build_questions(args: argparse.Namespace, declaration: dict[str, Any]) -> dict[str, Any]:
    root = args.root
    snapshot = _release_snapshot(args.snapshot)
    questions = _repository_path(root, args.questions or "questions/release-v2", "release-v2 questions", kind="directory")
    manifest, contract, report = verify_question_inputs(questions, "release")
    counts = manifest.get("counts") or {}
    if (
        manifest.get("snapshot_id") != snapshot
        or counts.get("questions") != 28_800
        or counts.get("primary_personas") != 48
        or counts.get("stories") != 288
        or counts.get("persona_suite_entries") != 4_800
        or not isinstance(report, dict)
        or report.get("question_contract_passed") is not True
        or report.get("counts", {}).get("questions") != 28_800
        or report.get("verification_ledger", {}).get("verified") != 28_800
        or report.get("verification_ledger", {}).get("failed") != 0
    ):
        raise ClosureError("release-v2 question and independent gold contract is incomplete")
    outputs = [
        _output(root, questions / name)
        for name in ("manifest.json", "contract.json", "verification-report.json", "verification-ledger.jsonl")
    ]
    return _base_entry(
        declaration=declaration,
        snapshot=snapshot,
        started_at=args.started_at,
        ended_at=args.ended_at,
        recorded_at=args.recorded_at,
        outputs=outputs,
        results=[
            "Independent deterministic verification passed all 28,800 release-v2 questions and 28,800 gold ledger rows with zero failures.",
            "The release matrix contains 48 primary personas, 288 stories and 4,800 persona-suite memberships.",
            f"Question manifest root is {manifest['root_sha256']} and independent verification root is {report['question_verifications_sha256']}.",
            f"Trusted snapshot/reconciliation bindings passed for exact release snapshot {snapshot}.",
        ],
        source_request_usage=_not_applicable_requests("Question generation and verification used the frozen local corpus and made no official-source request."),
        tool="independent deterministic question verifier",
        command=f"python3 scripts/finalize_terminal_activity.py questions --snapshot {snapshot} ...",
    )


def build_evaluation(args: argparse.Namespace, declaration: dict[str, Any]) -> dict[str, Any]:
    root = args.root
    snapshot = _release_snapshot(args.snapshot)
    run = _repository_path(root, args.evaluation_run, "immutable evaluation run", kind="directory")
    questions = _repository_path(root, args.questions or "questions/release-v2", "release questions", kind="directory")
    bundle = _repository_path(root, args.bundle or "bundle", "release bundle", kind="directory")
    results_root = _repository_path(root, args.results or "evaluation/results", "evaluation results", kind="directory")
    verified = verify_release_run(run, questions=questions, bundle=bundle, repository_root=root)
    manifest = verified["manifest"]
    status = verified["status"]
    projection_path = results_root / "projection.json"
    projection = _read_json(projection_path, "evaluation projection")
    paired = _read_json(results_root / "paired-comparisons.json", "paired comparisons")
    if (
        manifest.get("snapshot_id") != snapshot
        or manifest.get("questions") != 28_800
        or manifest.get("outcomes") != 288_000
        or status.get("machine_evaluation_complete") is not True
        or status.get("network_requests") != 0
        or status.get("model_usage") != {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_gbp": 0.0}
        or projection.get("snapshot_id") != snapshot
        or projection.get("source_run_root_sha256") != manifest.get("root_sha256")
        or projection.get("source_trace_root_sha256") != verified["trace_verification"]["root_sha256"]
        or not paired.get("comparisons")
        or any(not isinstance(row.get("ci_95"), list) or not isinstance(row.get("familywise_ci_95"), list) for row in paired["comparisons"])
    ):
        raise ClosureError("automated evaluation is not a complete independently replayed release matrix")
    outputs = [
        _output(root, run / "manifest.json"),
        _output(root, run / "trace-manifest.json"),
        *[
            _output(root, results_root / name)
            for name in ("projection.json", "status.json", "metrics.json", "paired-comparisons.json", "report.md")
        ],
    ]
    return _base_entry(
        declaration=declaration,
        snapshot=snapshot,
        started_at=args.started_at,
        ended_at=args.ended_at,
        recorded_at=args.recorded_at,
        outputs=outputs,
        results=[
            f"All 28,800 questions ran against {manifest['systems']} systems for 288,000 independently replayed traces.",
            f"Immutable evaluation run root is {manifest['root_sha256']} and trace root is {verified['trace_verification']['root_sha256']}.",
            f"{len(paired['comparisons'])} paired metric comparisons retain ordinary and familywise 95% confidence intervals.",
            "Evaluation recorded exact zero model calls, model tokens, paid cost and network requests; human evaluation remains not authorised.",
        ],
        source_request_usage=_not_applicable_requests("The evaluation used frozen local questions and bundle bytes and made no official-source request."),
        tool="independent deterministic evaluation replay",
        command=f"python3 scripts/finalize_terminal_activity.py evaluation --snapshot {snapshot} ...",
    )


def build_citations(args: argparse.Namespace, declaration: dict[str, Any]) -> dict[str, Any]:
    root = args.root
    snapshot = _release_snapshot(args.snapshot)
    verification_path = _repository_path(root, args.citation_verification or "release/citation-verification.json", "citation verification")
    report_path = _repository_path(root, args.citation_report or "reports/citation-verification.md", "citation report")
    aggregate_path = _repository_path(root, args.citation_aggregate or DEFAULT_CITATION_AGGREGATE.relative_to(ROOT), "citation request aggregate")
    verification = _read_json(verification_path, "citation verification")
    aggregate = _read_json(aggregate_path, "citation request aggregate")
    interval = _counter_interval("citation_verification", args.request_start, args.request_end, None)
    summary = verification.get("summary") or {}
    totals = aggregate.get("totals") or {}
    terminal = aggregate.get("terminal_evidence") or {}
    if (
        verification.get("snapshot_id") != snapshot
        or verification.get("citation_verification_passed") is not True
        or summary.get("citations_failed") != 0
        or summary.get("blocking_failures") != 0
        or summary.get("joint_claim_reviews_failed") != 0
        or summary.get("citations_passed") != summary.get("released_citations")
        or summary.get("joint_claim_reviews_passed") != summary.get("joint_claim_reviews_required")
        or totals.get("attempts") != interval["attempts"]
        or terminal.get("waivers") != 0
        or terminal.get("blockers") != 0
    ):
        raise ClosureError("citation verification is not a complete all-pass release-snapshot review")
    if aggregate.get("started_at") != args.started_at or aggregate.get("ended_at") != args.ended_at:
        raise ClosureError("citation --started-at/--ended-at must exactly match the request aggregate")
    return _base_entry(
        declaration=declaration,
        snapshot=snapshot,
        started_at=args.started_at,
        ended_at=args.ended_at,
        recorded_at=args.recorded_at,
        outputs=[_output(root, verification_path), _output(root, report_path), _output(root, aggregate_path)],
        results=[
            f"All {summary['released_citations']} release citations and {summary['joint_claim_reviews_required']} joint-support reviews passed for {snapshot}.",
            f"Citation request interval {interval['start_counter']}..{interval['end_counter']} records exactly {interval['attempts']} separately accounted attempts.",
            "Independent semantic review reports zero citation failures, joint-review failures, waivers and blockers.",
            f"Final citation verification and aggregate hashes are {_sha256_file(verification_path)} and {_sha256_file(aggregate_path)}.",
        ],
        source_request_usage=_exact_requests(
            interval,
            args.ended_at,
            "The citation collector/reviewer counter is separate from the shared GOV.UK acquisition ledger and from model cost.",
        ),
        tool="citation release-snapshot verifier",
        command=f"python3 scripts/finalize_terminal_activity.py citations --snapshot {snapshot} ...",
        model_assisted=True,
    )


def build_shards(args: argparse.Namespace, declaration: dict[str, Any]) -> dict[str, Any]:
    root = args.root
    snapshot = _release_snapshot(args.snapshot)
    bundle = _repository_path(root, args.bundle or "bundle", "release bundle", kind="directory")
    release_manifest_path = _repository_path(root, args.release_manifest or "release/manifest.yaml", "release manifest")
    release_manifest = _read_json(release_manifest_path, "release manifest")
    result = validate_bundle(bundle)
    if not result.passed:
        raise ClosureError("publication shard validation failed: " + "; ".join(result.errors))
    data_manifest_path = bundle / "data" / "manifest.json"
    data_manifest = _read_json(data_manifest_path, "bundle data manifest")
    integrity = data_manifest.get("integrity") or {}
    checksums_path = bundle / "checksums.json"
    checksums = _read_json(checksums_path, "bundle checksums")
    if (
        data_manifest.get("snapshot") != snapshot
        or release_manifest.get("release_id") != snapshot
        or release_manifest.get("snapshot", {}).get("id") != snapshot
        or release_manifest.get("counts", {}).get("publication_records") != result.datasets
        or integrity.get("schema") != "okf-data-plane-integrity.v1"
        or not SHA256_RE.fullmatch(str(integrity.get("manifest_root_sha256") or ""))
        or not isinstance(integrity.get("leaf_count"), int)
        or checksums.get("algorithm") != "sha256"
        or not isinstance(checksums.get("file_count"), int)
    ):
        raise ClosureError("publication manifests are not bound to one complete release shard contract")
    return _base_entry(
        declaration=declaration,
        snapshot=snapshot,
        started_at=args.started_at,
        ended_at=args.ended_at,
        recorded_at=args.recorded_at,
        outputs=[_output(root, release_manifest_path), _output(root, data_manifest_path), _output(root, checksums_path)],
        results=[
            f"Publication validation passed {result.datasets} records, {result.resources} resources, {result.publishers} publishers and {result.relationships} relationships.",
            f"The data-plane root {integrity['manifest_root_sha256']} binds {integrity['leaf_count']} shard leaves.",
            f"The complete bundle checksum manifest binds {checksums['file_count']} files as {_sha256_file(checksums_path)}.",
            f"Every shard passed schema, hash, compressed/uncompressed size and key-range validation for {snapshot}.",
        ],
        source_request_usage=_not_applicable_requests("The shard audit used frozen local release bytes and made no official-source request."),
        tool="static publication validator",
        command=f"python3 scripts/finalize_terminal_activity.py shards --snapshot {snapshot} ...",
    )


def build_security(args: argparse.Namespace, declaration: dict[str, Any]) -> dict[str, Any]:
    root = args.root
    snapshot = _release_snapshot(args.snapshot)
    if not args.scan_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,255}", args.scan_id):
        raise ClosureError("--scan-id is required and has an invalid format")
    if not isinstance(args.scanned_commit, str) or not COMMIT_RE.fullmatch(args.scanned_commit):
        raise ClosureError("--scanned-commit must be a full lowercase 40-hex commit")
    security_path = _repository_path(root, args.security_json or "release/security-scan.json", "release security scan")
    report_path = _repository_path(root, args.security_report or "reports/security.md", "security report")
    evidence = _read_json(security_path, "release security scan")
    errors = release_checks._security_errors(root, evidence, {"id": snapshot})
    if evidence.get("scan_id") != args.scan_id:
        errors.append("explicit scan ID differs from release security evidence")
    if evidence.get("scanned_commit") != args.scanned_commit:
        errors.append("explicit scanned commit differs from release security evidence")
    report = evidence.get("report") or {}
    if report.get("path") != _relative(root, report_path) or report.get("sha256") != _sha256_file(report_path):
        errors.append("explicit security report differs from release security evidence")
    if errors:
        raise ClosureError("; ".join(errors))
    findings = evidence["findings"]
    return _base_entry(
        declaration=declaration,
        snapshot=snapshot,
        started_at=args.started_at,
        ended_at=args.ended_at,
        recorded_at=args.recorded_at,
        outputs=[_output(root, security_path), _output(root, report_path)],
        results=[
            f"Codex Security scan {args.scan_id} completed against commit {args.scanned_commit} and exact code tree {evidence['code_tree']['sha256']}.",
            f"Open critical and high findings are {findings['critical_open']} and {findings['high_open']}.",
            f"Release security evidence and report are hash-bound as {_sha256_file(security_path)} and {_sha256_file(report_path)}.",
            "The release scan is distinct from and supersedes the declared pre-release scan terminal.",
        ],
        source_request_usage=_not_applicable_requests("The security scan used frozen repository bytes and made no official GOV.UK source request."),
        tool="Codex Security plus deterministic release evidence verifier",
        command=f"python3 scripts/finalize_terminal_activity.py security --snapshot {snapshot} --scan-id {args.scan_id} ...",
        commit=args.scanned_commit,
        extra_snapshots=[f"git:{args.scanned_commit}"],
        model_assisted=True,
    )


def _https_url(value: str | None, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ClosureError(f"{label} is required")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.netloc != parsed.hostname
        or parsed.query
        or parsed.fragment
        or unquote(parsed.path) != parsed.path
    ):
        raise ClosureError(f"{label} must be a canonical absolute HTTPS URL")
    return value


def _github_url(value: str | None, label: str, pattern: str) -> str:
    value = _https_url(value, label)
    parsed = urlsplit(value)
    if parsed.hostname != "github.com" or not re.fullmatch(pattern, parsed.path):
        raise ClosureError(f"{label} has an unexpected GitHub repository/path")
    return value


def _write_exact_json(path: Path, value: dict[str, Any]) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8") != content:
            raise ClosureError(f"existing generated evidence conflicts: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_publication(args: argparse.Namespace, declaration: dict[str, Any]) -> dict[str, Any]:
    root = args.root
    snapshot = _release_snapshot(args.snapshot)
    if not isinstance(args.commit, str) or not COMMIT_RE.fullmatch(args.commit):
        raise ClosureError("--commit must be the published full lowercase 40-hex commit")
    if not isinstance(args.tag, str) or not TAG_RE.fullmatch(args.tag):
        raise ClosureError("--tag must be a canonical final or release-candidate semantic tag")
    repository_pr_url = _github_url(args.repository_pr_url, "repository PR URL", r"/chris-page-gov/okf-govuk-content/pull/[1-9][0-9]*")
    ci_url = _github_url(args.ci_url, "CI URL", r"/chris-page-gov/okf-govuk-content/actions/runs/[1-9][0-9]*")
    release_url = _github_url(args.release_url, "release URL", rf"/chris-page-gov/okf-govuk-content/releases/tag/{re.escape(args.tag)}")
    registry_pr_url = _github_url(args.registry_pr_url, "registry PR URL", r"/chris-page-gov/okf-explorer/pull/[1-9][0-9]*")
    pages_url = _https_url(args.pages_url, "Pages URL")
    if pages_url != "https://chris-page-gov.github.io/okf-govuk-content/":
        raise ClosureError("Pages URL differs from the repository publication contract")

    inputs = {
        "repository_pr": _repository_path(root, args.repository_pr_json, "repository PR API evidence"),
        "ci": _repository_path(root, args.ci_json, "CI API evidence"),
        "release": _repository_path(root, args.release_json, "release API evidence"),
        "pages_smoke": _repository_path(root, args.pages_smoke, "Pages live smoke evidence"),
        "registry_pr": _repository_path(root, args.registry_pr_json, "registry PR API evidence"),
        "branch_protection": _repository_path(root, args.branch_protection, "branch-protection API evidence"),
        "publication_settings": _repository_path(root, args.publication_settings, "publication-settings API evidence"),
    }
    values = {name: _read_json(path, name.replace("_", " ") + " evidence") for name, path in inputs.items()}
    repository_pr = values["repository_pr"]
    ci = values["ci"]
    release = values["release"]
    pages = values["pages_smoke"]
    registry = values["registry_pr"]
    if (
        repository_pr.get("html_url") != repository_pr_url
        or repository_pr.get("merged_at") is None
        or repository_pr.get("merge_commit_sha") != args.commit
    ):
        raise ClosureError("repository PR evidence is not merged at the published commit")
    if ci.get("html_url") != ci_url or ci.get("head_sha") != args.commit or ci.get("conclusion") != "success":
        raise ClosureError("CI evidence is not a successful run for the published commit")
    if (
        release.get("html_url") != release_url
        or release.get("tag_name") != args.tag
        or release.get("draft") is not False
        or not release.get("published_at")
    ):
        raise ClosureError("release API evidence is not a published matching tag")
    if (
        pages.get("schema") != "govuk-okf-pages-live-smoke.v1"
        or pages.get("base_url") != pages_url
        or pages.get("snapshot") != snapshot
        or pages.get("passed") is not True
        or pages.get("errors") != []
        or not pages.get("results")
        or not pages.get("range_results")
        or any(row.get("passed") is not True for row in [*pages["results"], *pages["range_results"]])
    ):
        raise ClosureError("Pages live-smoke evidence is incomplete or failed")
    if registry.get("html_url") != registry_pr_url or registry.get("state") not in {"open", "closed"}:
        raise ClosureError("Explorer registry PR evidence is absent or invalid")
    if registry.get("state") == "closed" and registry.get("merged_at") is None:
        raise ClosureError("closed Explorer registry PR evidence is not merged")
    local_branch = _read_json(root / ".github" / "branch-protection.json", "branch protection policy")
    branch_errors = compare_api_capture(local_branch, values["branch_protection"])
    policy = _read_json(root / ".github" / "repository-policy.json", "repository policy")
    publication_errors = compare_publication_api_capture(policy, values["publication_settings"])
    if branch_errors or publication_errors:
        raise ClosureError("; ".join([*branch_errors, *publication_errors]))
    external_times = [
        repository_pr.get("merged_at"),
        ci.get("updated_at") or ci.get("run_started_at"),
        release.get("published_at"),
        pages.get("checked_at"),
        registry.get("merged_at") or registry.get("created_at"),
    ]
    for label, value in zip(("PR", "CI", "release", "Pages", "registry"), external_times, strict=True):
        if not isinstance(value, str) or _parse_time(value, f"{label} evidence time") > _parse_time(args.ended_at, "ended_at"):
            raise ClosureError(f"publication ended_at is before or lacks exact {label} evidence")
    publication_document = {
        "schema": "afhf-govuk-okf-publication-verification.v1",
        "snapshot": snapshot,
        "recorded_at": args.recorded_at,
        "commit": args.commit,
        "tag": args.tag,
        "urls": {
            "repository_pr": repository_pr_url,
            "ci": ci_url,
            "release": release_url,
            "pages": pages_url,
            "registry_pr": registry_pr_url,
        },
        "states": {
            "repository_pr_merged_at": repository_pr["merged_at"],
            "ci_conclusion": ci["conclusion"],
            "release_published_at": release["published_at"],
            "pages_checked_at": pages["checked_at"],
            "registry_pr_state": registry["state"],
            "registry_pr_merged_at": registry.get("merged_at"),
            "protected_main_matches_policy": True,
            "immutable_release_and_pages_settings_match_policy": True,
        },
        "input_sha256": {name: _sha256_file(path) for name, path in sorted(inputs.items())},
        "publication_verification_passed": True,
    }
    publication_output = _repository_path(
        root,
        args.publication_output or "release/publication-verification.json",
        "publication verification output",
        must_exist=False,
    )
    _write_exact_json(publication_output, publication_document)
    return _base_entry(
        declaration=declaration,
        snapshot=snapshot,
        started_at=args.started_at,
        ended_at=args.ended_at,
        recorded_at=args.recorded_at,
        outputs=[
            _output(root, publication_output),
            *[_output(root, path) for _, path in sorted(inputs.items())],
        ],
        results=[
            f"Repository PR {repository_pr_url} merged at published commit {args.commit}; CI {ci_url} concluded success.",
            f"Protected-main and immutable-release/Pages API read-backs match the checked repository policy for tag {args.tag}.",
            f"Release {release_url} is published and exact-snapshot Pages smoke passed at {pages_url}.",
            f"Explorer registry PR is recorded at {registry_pr_url}; normalized publication evidence is {_sha256_file(publication_output)}.",
        ],
        source_request_usage=_not_applicable_requests("Publication checks contacted GitHub/Pages rather than an official GOV.UK source and are not model cost."),
        tool="GitHub API and Pages live-evidence verifier",
        command=f"python3 scripts/finalize_terminal_activity.py publication --snapshot {snapshot} ...",
        commit=args.commit,
        extra_snapshots=[f"git:{args.commit}"],
    )


BUILDERS = {
    "hydration": build_hydration,
    "reconciliation": build_reconciliation,
    "questions": build_questions,
    "evaluation": build_evaluation,
    "citations": build_citations,
    "shards": build_shards,
    "security": build_security,
    "publication": build_publication,
}


def _append_idempotent(
    entry: dict[str, Any],
    ledger: Path,
    schema: Path,
    declaration: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    with ledger_lock(ledger):
        _validate_entry_contract(entry, declaration, root)
        rows, _ = _ledger_rows(ledger)
        _ensure_supersedures(rows, declaration)
        existing = next((row for row in rows if row.get("activity_id") == entry["activity_id"]), None)
        if existing is not None:
            candidate = dict(entry)
            candidate["previous_entry_sha256"] = existing.get("previous_entry_sha256")
            if canonical_line(candidate) != canonical_line(existing):
                raise ClosureError(f"conflicting existing terminal activity: {entry['activity_id']}")
            return {
                "status": "already_present",
                "activity_id": entry["activity_id"],
                "entry_sha256": _sha256_bytes(canonical_line(existing).encode("utf-8")),
            }
        result = append_entries([entry], ledger, schema, acquire_lock=False)[0]
        return {"status": "appended", **result}


def _extract_interval(row: dict[str, Any]) -> dict[str, Any]:
    request = row.get("source_request_usage") or {}
    intervals = request.get("intervals")
    if isinstance(intervals, list) and len(intervals) == 1 and isinstance(intervals[0], dict):
        interval = dict(intervals[0])
    else:
        evidence = str(request.get("evidence") or "")
        match = LEGACY_INTERVAL_RE.search(evidence) or LEGACY_ADVANCE_RE.search(evidence)
        if not match:
            raise ClosureError(f"{row.get('activity_id')} has no structured or parseable exact request interval")
        start, end = map(int, match.groups())
        interval = {
            "label": str(row.get("activity_id")),
            "start_counter": start,
            "end_counter": end,
            "attempts": end - start,
            "ledger": request.get("budget_ledger"),
        }
    start = interval.get("start_counter")
    end = interval.get("end_counter")
    attempts = interval.get("attempts")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or start < 0
        or end < start
        or attempts != end - start
        or request.get("attempts") != attempts
        or request.get("status") != "exact"
    ):
        raise ClosureError(f"{row.get('activity_id')} request interval does not reconcile")
    return interval


def _recursive_observations(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if "requested_url" in value and "status" in value:
            yield value
        for child in value.values():
            yield from _recursive_observations(child)
    elif isinstance(value, list):
        for child in value:
            yield from _recursive_observations(child)


def _preflight_attempts(root: Path) -> int:
    value = _read_json(root / "research" / "source-preflight.json", "source preflight", maximum=128 * 1024 * 1024)
    return sum(int(row.get("attempts") or row.get("acquisition_attempt") or 1) for row in _recursive_observations(value))


def _authorised_ceiling(root: Path) -> int:
    launch = (root / "governance" / "launch-manifest.yaml").read_text(encoding="utf-8")
    match = re.search(r"^\s*official_source_requests:\s*([0-9]+)\s*$", launch, re.MULTILINE)
    if not match:
        raise ClosureError("launch manifest has no official-source request ceiling")
    return int(match.group(1))


def _atomic_bytes(path: Path, content: bytes | None) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ClosureError(f"transaction path cannot be a symlink: {path}")
    if content is None:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _locked_counter(path: Path) -> Iterable[Any]:
    """Hold the acquisition counter inode so no official request can race finalization."""

    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "r+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield stream
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _transaction_path(root: Path, ledger: Path, budget: Path) -> Path:
    identity = _sha256_bytes(f"{ledger.resolve()}\0{budget.resolve()}".encode("utf-8"))[:24]
    directory = root / ".tmp" / "locks"
    if directory.is_symlink():
        raise ClosureError("transaction directory cannot be a symbolic link")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / f"terminal-budget-{identity}.txn.json"


def _recover_transaction(journal: Path, ledger: Path, budget: Path) -> str | None:
    if not journal.exists():
        return None
    document = _read_json(journal, "terminal budget transaction journal", maximum=4 * 1024 * 1024)
    required = {
        "schema",
        "old_ledger_b64",
        "old_budget_b64",
        "old_ledger_sha256",
        "old_budget_sha256",
        "new_ledger_sha256",
        "new_budget_sha256",
    }
    if document.get("schema") != "afhf-govuk-okf-terminal-budget-transaction.v1" or not required <= set(document):
        raise ClosureError("terminal budget transaction journal has an invalid contract")
    try:
        old_ledger = base64.b64decode(document["old_ledger_b64"], validate=True)
        old_budget_value = document.get("old_budget_b64")
        old_budget = (
            base64.b64decode(old_budget_value, validate=True)
            if isinstance(old_budget_value, str)
            else None
        )
    except (TypeError, ValueError) as exc:
        raise ClosureError("terminal budget transaction journal has invalid rollback bytes") from exc
    if (
        document.get("old_ledger_sha256") != _sha256_bytes(old_ledger)
        or document.get("old_budget_sha256")
        != (_sha256_bytes(old_budget) if old_budget is not None else None)
        or not SHA256_RE.fullmatch(str(document.get("new_ledger_sha256") or ""))
        or not SHA256_RE.fullmatch(str(document.get("new_budget_sha256") or ""))
    ):
        raise ClosureError("terminal budget transaction journal hashes do not reconcile")
    current_ledger = ledger.read_bytes() if ledger.is_file() else b""
    current_budget = budget.read_bytes() if budget.is_file() else None
    if (
        _sha256_bytes(current_ledger) == document.get("new_ledger_sha256")
        and current_budget is not None
        and _sha256_bytes(current_budget) == document.get("new_budget_sha256")
    ):
        journal.unlink()
        return "completed"
    current_ledger_hash = _sha256_bytes(current_ledger)
    current_budget_hash = _sha256_bytes(current_budget) if current_budget is not None else None
    if current_ledger_hash not in {
        document["old_ledger_sha256"],
        document["new_ledger_sha256"],
    } or current_budget_hash not in {
        document["old_budget_sha256"],
        document["new_budget_sha256"],
    }:
        raise ClosureError("transaction files changed outside the recorded terminal budget transaction")
    _atomic_bytes(ledger, old_ledger)
    _atomic_bytes(budget, old_budget)
    journal.unlink()
    return "rolled_back"


def _write_journal(
    journal: Path,
    *,
    old_ledger: bytes,
    old_budget: bytes | None,
    new_ledger: bytes,
    new_budget: bytes,
) -> None:
    value = {
        "schema": "afhf-govuk-okf-terminal-budget-transaction.v1",
        "old_ledger_b64": base64.b64encode(old_ledger).decode("ascii"),
        "old_budget_b64": base64.b64encode(old_budget).decode("ascii") if old_budget is not None else None,
        "old_ledger_sha256": _sha256_bytes(old_ledger),
        "old_budget_sha256": _sha256_bytes(old_budget) if old_budget is not None else None,
        "new_ledger_sha256": _sha256_bytes(new_ledger),
        "new_budget_sha256": _sha256_bytes(new_budget),
    }
    _atomic_bytes(journal, (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))


def close_source_budget(args: argparse.Namespace, declaration: dict[str, Any]) -> dict[str, Any]:
    root = args.root
    snapshot = _release_snapshot(args.snapshot)
    live_counter = _repository_path(root, args.live_counter, "live official-source counter")
    budget_path = _repository_path(root, args.budget_snapshot, "source-request budget output", must_exist=False)
    citation_path = _repository_path(root, args.citation_aggregate or DEFAULT_CITATION_AGGREGATE.relative_to(ROOT), "citation request aggregate")
    if args.request_start != 0:
        raise ClosureError("source-budget --request-start must be zero for the complete programme counter")
    _validate_times(args.started_at, args.ended_at, args.recorded_at)
    with ledger_lock(args.ledger), _locked_counter(live_counter) as counter_stream:
        journal = _transaction_path(root, args.ledger, budget_path)
        recovery = _recover_transaction(journal, args.ledger, budget_path)
        rows, raw_lines = _ledger_rows(args.ledger)
        _ensure_supersedures(rows, declaration)
        by_id = {row["activity_id"]: row for row in rows}
        missing = [activity_id for activity_id in SHARED_REQUEST_TERMINALS if activity_id not in by_id]
        if missing:
            raise ClosureError("shared request terminals are missing: " + ", ".join(missing))
        intervals = [_extract_interval(by_id[activity_id]) for activity_id in SHARED_REQUEST_TERMINALS]
        for left, right in zip(intervals, intervals[1:]):
            if left["end_counter"] != right["start_counter"]:
                raise ClosureError("T0 census, hydration and T1 request intervals are not contiguous")
        try:
            counter_stream.seek(0)
            live = int(counter_stream.read().strip())
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ClosureError(f"live official-source counter is invalid: {exc}") from exc
        if args.request_end != live:
            raise ClosureError("source-budget --request-end must equal the exact live shared counter")
        if intervals[-1]["end_counter"] != live:
            raise ClosureError("final shared request counter differs from the T1 terminal interval")
        ceiling = _authorised_ceiling(root)
        if live < 0 or live > ceiling:
            raise ClosureError("live shared request counter exceeds the authorised ceiling")
        citation = _read_json(citation_path, "citation request aggregate")
        citation_attempts = (citation.get("totals") or {}).get("attempts")
        citation_terminal = by_id.get(TERMINAL_IDS["citations"])
        if (
            not isinstance(citation_attempts, int)
            or not isinstance(citation_terminal, dict)
            or citation_terminal.get("source_request_usage", {}).get("attempts") != citation_attempts
        ):
            raise ClosureError("separate citation request aggregate does not match its terminal activity")
        preflight = _preflight_attempts(root)
        pre_t0_counter = intervals[0]["start_counter"]
        if preflight > pre_t0_counter:
            raise ClosureError("source preflight attempts exceed the exact counter at T0 start")
        historical_after_preflight = pre_t0_counter - preflight
        budget = {
            "schema": "afhf-govuk-okf-source-request-budget.v1",
            "recorded_at": args.recorded_at,
            "snapshot_id": snapshot,
            "status": "final",
            "authorised_ceiling": ceiling,
            "consumed_attempts_at_observation": live,
            "remaining_attempts_at_observation": ceiling - live,
            "preflight_attempts": preflight,
            "included_in_model_cost": False,
            "live_ledger": _relative(root, live_counter),
            "preflight_evidence": "research/source-preflight.json",
            "scope": "Final shared official-source request counter after exact T0 census, the interrupted pre-hardening hydration request, T0 hydration, T1 enumeration and closing intervals; citation verification remains separately aggregated and is not model cost.",
            "shared_request_intervals": [
                {**interval, "activity_id": activity_id}
                for activity_id, interval in zip(SHARED_REQUEST_TERMINALS, intervals, strict=True)
            ],
            "pre_terminal_shared_counter": pre_t0_counter,
            "pre_t0_accounting": {
                "counter_at_t0_start": pre_t0_counter,
                "source_preflight_attempts": preflight,
                "additional_historical_shared_attempts": historical_after_preflight,
                "per_run_breakdown_status": (
                    "not_applicable"
                    if historical_after_preflight == 0
                    else "unavailable_not_reconstructed"
                ),
                "limitation_evidence_activity_id": "ACT-B1-CAPACITY-20260712-001",
            },
            "citation_request_aggregate": {
                "path": _relative(root, citation_path),
                "sha256": _sha256_file(citation_path),
                "attempts": citation_attempts,
            },
            "final_entries_required": [],
        }
        budget_bytes = (json.dumps(budget, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        programme_interval = _counter_interval("programme_total", 0, live, _relative(root, live_counter))
        entry = _base_entry(
            declaration=declaration,
            snapshot=snapshot,
            started_at=args.started_at,
            ended_at=args.ended_at,
            recorded_at=args.recorded_at,
            outputs=[
                {"path": _relative(root, budget_path), "state": "modified", "sha256": _sha256_bytes(budget_bytes)},
                _output(root, citation_path),
            ],
            results=[
                f"Final shared official-source counter is exactly {live} of the authorised {ceiling}; {ceiling - live} attempts remain.",
                "T0 census, the interrupted pre-hardening hydration request, T0 hydration and T1/closing intervals are exact, ordered, contiguous and end at the live shared counter.",
                f"The counter at T0 start is {pre_t0_counter}: source preflight records {preflight} attempts and the remaining {historical_after_preflight} historical attempts retain their declared unavailable per-run breakdown.",
                f"Citation verification remains separate at {citation_attempts} attempts with aggregate SHA-256 {_sha256_file(citation_path)}.",
            ],
            source_request_usage={
                "status": "exact",
                "attempts": live,
                "budget_ledger": _relative(root, live_counter),
                "observation_at": args.ended_at,
                "included_in_model_cost": False,
                "evidence": "Exact final shared programme counter; per-stage intervals and the separate citation aggregate are recorded in provenance/source-request-budget.json.",
                "intervals": [programme_interval],
            },
            tool="locked source-budget and activity-ledger transaction",
            command=f"python3 scripts/finalize_terminal_activity.py source-budget --snapshot {snapshot} --request-start 0 --request-end {live} ...",
        )
        for output in entry["outputs"]:
            if output["path"] == _relative(root, budget_path):
                continue
            output_path = _repository_path(root, output["path"], "source-budget terminal output")
            if _sha256_file(output_path) != output["sha256"]:
                raise ClosureError(f"source-budget terminal output changed before transaction: {output['path']}")
        existing = by_id.get(entry["activity_id"])
        if existing is not None:
            candidate = dict(entry)
            candidate["previous_entry_sha256"] = existing.get("previous_entry_sha256")
            if canonical_line(candidate) != canonical_line(existing) or not budget_path.is_file() or budget_path.read_bytes() != budget_bytes:
                raise ClosureError("conflicting existing final source-budget terminal or snapshot")
            return {
                "status": "already_present",
                "activity_id": entry["activity_id"],
                "entry_sha256": _sha256_bytes(canonical_line(existing).encode("utf-8")),
                "recovery": recovery,
            }
        previous = _sha256_bytes(raw_lines[-1].encode("utf-8")) if raw_lines else None
        final_entry = dict(entry)
        final_entry["previous_entry_sha256"] = previous
        line = canonical_line(final_entry).encode("utf-8") + b"\n"
        old_ledger = args.ledger.read_bytes() if args.ledger.is_file() else b""
        old_budget = budget_path.read_bytes() if budget_path.is_file() else None
        new_ledger = old_ledger + line
        _write_journal(
            journal,
            old_ledger=old_ledger,
            old_budget=old_budget,
            new_ledger=new_ledger,
            new_budget=budget_bytes,
        )
        try:
            _atomic_bytes(budget_path, budget_bytes)
            appended = append_entries([entry], args.ledger, args.schema, acquire_lock=False)[0]
            if args.ledger.read_bytes() != new_ledger or budget_path.read_bytes() != budget_bytes:
                raise ClosureError("source-budget transaction did not produce the exact expected bytes")
        except BaseException:
            _atomic_bytes(args.ledger, old_ledger)
            _atomic_bytes(budget_path, old_budget)
            if journal.exists():
                journal.unlink()
            raise
        journal.unlink()
        return {"status": "appended", **appended, "recovery": recovery}


def _common_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser], name: str) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--declarations", type=Path)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--ended-at", required=True)
    parser.add_argument("--recorded-at", required=True)
    parser.add_argument("--request-start", type=int)
    parser.add_argument("--request-end", type=int)
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    hydration = _common_parser(subparsers, "hydration")
    hydration.add_argument("--reconciliation")
    hydration.add_argument("--manifest")

    reconciliation = _common_parser(subparsers, "reconciliation")
    reconciliation.add_argument("--reconciliation")
    reconciliation.add_argument("--manifest")
    reconciliation.add_argument("--t1-manifest")

    questions = _common_parser(subparsers, "questions")
    questions.add_argument("--questions")

    evaluation = _common_parser(subparsers, "evaluation")
    evaluation.add_argument("--evaluation-run", required=True)
    evaluation.add_argument("--questions")
    evaluation.add_argument("--bundle")
    evaluation.add_argument("--results")

    citations = _common_parser(subparsers, "citations")
    citations.add_argument("--citation-verification")
    citations.add_argument("--citation-report")
    citations.add_argument("--citation-aggregate")

    shards = _common_parser(subparsers, "shards")
    shards.add_argument("--bundle")
    shards.add_argument("--release-manifest")

    security = _common_parser(subparsers, "security")
    security.add_argument("--security-json")
    security.add_argument("--security-report")
    security.add_argument("--scan-id", required=True)
    security.add_argument("--scanned-commit", required=True)

    publication = _common_parser(subparsers, "publication")
    publication.add_argument("--commit", required=True)
    publication.add_argument("--tag", required=True)
    publication.add_argument("--repository-pr-url", required=True)
    publication.add_argument("--ci-url", required=True)
    publication.add_argument("--release-url", required=True)
    publication.add_argument("--pages-url", required=True)
    publication.add_argument("--registry-pr-url", required=True)
    publication.add_argument("--repository-pr-json", required=True)
    publication.add_argument("--ci-json", required=True)
    publication.add_argument("--release-json", required=True)
    publication.add_argument("--pages-smoke", required=True)
    publication.add_argument("--registry-pr-json", required=True)
    publication.add_argument("--branch-protection", required=True)
    publication.add_argument("--publication-settings", required=True)
    publication.add_argument("--publication-output")

    budget = _common_parser(subparsers, "source-budget")
    budget.add_argument("--live-counter", default=str(DEFAULT_LIVE_COUNTER.relative_to(ROOT)))
    budget.add_argument("--budget-snapshot", default=str(DEFAULT_BUDGET_SNAPSHOT.relative_to(ROOT)))
    budget.add_argument("--citation-aggregate")
    return parser.parse_args(argv)


def _resolve_control_paths(args: argparse.Namespace) -> None:
    args.root = args.root.absolute().resolve()
    args.ledger = _repository_path(
        args.root,
        args.ledger or "provenance/activity-ledger.jsonl",
        "activity ledger",
        must_exist=False,
    )
    args.schema = _repository_path(
        args.root,
        args.schema or "provenance/activity-ledger.schema.json",
        "activity schema",
    )
    args.declarations = _repository_path(
        args.root,
        args.declarations or "provenance/reproduction-declarations.json",
        "reproduction declarations",
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _resolve_control_paths(args)
    activity_id = TERMINAL_IDS[args.operation]
    declaration = _declaration(args.declarations, activity_id)
    if args.operation == "source-budget":
        result = close_source_budget(args, declaration)
    else:
        entry = BUILDERS[args.operation](args, declaration)
        result = _append_idempotent(entry, args.ledger, args.schema, declaration, args.root)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ClosureError as exc:
        print(f"terminal closure failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
