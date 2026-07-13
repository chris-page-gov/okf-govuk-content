#!/usr/bin/env python3
"""Append one validated v2 activity record under an exclusive file lock."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "provenance" / "activity-ledger.jsonl"
DEFAULT_SCHEMA = ROOT / "provenance" / "activity-ledger.schema.json"


def _lock_path(ledger_path: Path) -> Path:
    if ledger_path.is_symlink() or ledger_path.parent.is_symlink():
        raise ValueError("activity ledger and its parent cannot be symlinks")
    repository_root = (
        ledger_path.parent.parent.resolve()
        if ledger_path.parent.name == "provenance"
        else ledger_path.parent.resolve()
    )
    resolved_parent = ledger_path.parent.resolve()
    if ledger_path.parent.name == "provenance" and resolved_parent != repository_root / "provenance":
        raise ValueError("activity ledger path escapes the repository provenance directory")
    local_tmp = repository_root / ".tmp"
    if local_tmp.is_symlink():
        raise ValueError("activity-ledger lock directory cannot be a symlink")
    local_tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_directory = local_tmp / "locks"
    if lock_directory.is_symlink():
        raise ValueError("activity-ledger lock directory cannot be a symlink")
    lock_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved_directory = lock_directory.resolve()
    if resolved_directory != repository_root and repository_root not in resolved_directory.parents:
        raise ValueError("activity-ledger lock directory escapes the repository")
    identity = hashlib.sha256(str(ledger_path.resolve()).encode("utf-8")).hexdigest()[:24]
    return lock_directory / f"activity-ledger-{identity}.lock"


@contextmanager
def ledger_lock(ledger_path: Path):
    """Serialize append and promotion transactions through a stable side lock."""

    lock_path = _lock_path(ledger_path)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def canonical_line(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def append_entries(
    entries: list[dict[str, object]],
    ledger_path: Path = DEFAULT_LEDGER,
    schema_path: Path = DEFAULT_SCHEMA,
    *,
    acquire_lock: bool = True,
) -> list[dict[str, str]]:
    if ledger_path.is_symlink() or ledger_path.parent.is_symlink():
        raise ValueError("activity ledger and its parent cannot be symlinks")
    if acquire_lock:
        with ledger_lock(ledger_path):
            return append_entries(
                entries,
                ledger_path,
                schema_path,
                acquire_lock=False,
            )
    if schema_path.is_symlink():
        raise ValueError("activity-ledger schema cannot be a symlink")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, str]] = []
    flags = os.O_CREAT | os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(ledger_path, flags, 0o600)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.seek(0)
        existing = stream.read().splitlines()
        ids = {json.loads(line).get("activity_id") for line in existing if line.strip()}
        for supplied in entries:
            entry = dict(supplied)
            if entry.get("ledger_schema_version") != "2.0":
                raise ValueError("only v2 activity entries may be appended")
            if "previous_entry_sha256" in entry:
                raise ValueError("the append tool, not the caller, binds previous_entry_sha256")
            activity_id = entry.get("activity_id")
            if activity_id in ids:
                raise ValueError(f"duplicate activity_id: {activity_id}")
            entry.setdefault(
                "recorded_at",
                datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            )
            entry["previous_entry_sha256"] = (
                hashlib.sha256(existing[-1].encode("utf-8")).hexdigest() if existing else None
            )
            failures = sorted(
                Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(entry),
                key=lambda item: list(item.absolute_path),
            )
            if failures:
                raise ValueError("; ".join(failure.message for failure in failures))
            line = canonical_line(entry)
            existing.append(line)
            ids.add(activity_id)
            results.append(
                {"activity_id": str(activity_id), "entry_sha256": hashlib.sha256(line.encode("utf-8")).hexdigest()}
            )
        stream.seek(0, os.SEEK_END)
        stream.write("".join(line + "\n" for line in existing[-len(entries) :]))
        stream.flush()
        os.fsync(stream.fileno())
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return results


def append_entry(entry_path: Path, ledger_path: Path = DEFAULT_LEDGER, schema_path: Path = DEFAULT_SCHEMA) -> str:
    value = json.loads(entry_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("activity entry must be an object")
    return append_entries([value], ledger_path, schema_path)[0]["entry_sha256"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("entry", type=Path, help="JSON object or array without previous_entry_sha256")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    args = parser.parse_args()
    value = json.loads(args.entry.read_text(encoding="utf-8"))
    entries = value if isinstance(value, list) else [value]
    if not entries or not all(isinstance(entry, dict) for entry in entries):
        raise ValueError("activity entry file must contain an object or a non-empty object array")
    results = append_entries(entries, args.ledger.resolve(), args.schema.resolve())
    print(json.dumps({"appended": results}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
