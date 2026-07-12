"""Bounded, integrity-checked readers for frozen JSONL corpus inputs."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from .util import canonical_json_bytes

MAX_COMPRESSED_SHARD_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_SHARD_BYTES = 32 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024


class ShardedJsonlError(ValueError):
    """Raised when a corpus input or one of its integrity proofs is invalid."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolved_input(path: Path) -> Path:
    """Resolve a JSONL file or the standard index within a shard directory."""

    path = path.resolve()
    if path.is_dir():
        path = path / "index.json"
    if not path.is_file():
        raise ShardedJsonlError(f"corpus input does not exist: {path}")
    return path


def input_sha256(path: Path) -> str:
    """Bind an input to its file, or to its shard index when it is a directory."""

    return file_sha256(resolved_input(path))


def _safe_shard(manifest_path: Path, relative_text: str) -> Path:
    relative = Path(relative_text)
    root = manifest_path.parent.resolve()
    candidate = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or candidate.parent != root:
        raise ShardedJsonlError(f"unsafe shard path in {manifest_path}: {relative_text}")
    if not candidate.is_file():
        raise ShardedJsonlError(f"missing shard in {manifest_path}: {relative_text}")
    return candidate


def _iter_one(
    path: Path,
    *,
    compressed_limit: int,
    uncompressed_limit: int,
    require_canonical: bool = False,
) -> Iterator[dict[str, Any]]:
    if path.suffix == ".gz":
        if path.stat().st_size > compressed_limit:
            raise ShardedJsonlError(f"compressed shard exceeds {compressed_limit} bytes: {path}")
        handle = gzip.open(path, "rb")
    else:
        handle = path.open("rb")
    total = 0
    with handle:
        for line_number in range(1, 2**63):
            line = handle.readline(MAX_RECORD_BYTES + 1)
            if not line:
                break
            total += len(line)
            if len(line) > MAX_RECORD_BYTES:
                raise ShardedJsonlError(f"record exceeds {MAX_RECORD_BYTES} bytes: {path}:{line_number}")
            if total > uncompressed_limit:
                raise ShardedJsonlError(f"uncompressed shard exceeds {uncompressed_limit} bytes: {path}")
            if not line.strip():
                continue
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ShardedJsonlError(f"invalid UTF-8 JSON object: {path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ShardedJsonlError(f"record is not an object: {path}:{line_number}")
            if require_canonical and canonical_json_bytes(value) != line:
                raise ShardedJsonlError(f"record is not canonical JSONL: {path}:{line_number}")
            yield value


def iter_jsonl_records(path: Path) -> Iterator[dict[str, Any]]:
    """Stream a JSONL file or a standard content-addressed shard index.

    Every declared file hash, canonical hash, record count and aggregate hash is
    checked.  The function deliberately accepts only the repository's standard
    ``govuk-okf-jsonl-shards.v1`` index rather than guessing at arbitrary JSON.
    """

    source = resolved_input(path)
    if source.suffix != ".json":
        yield from _iter_one(
            source,
            compressed_limit=MAX_COMPRESSED_SHARD_BYTES,
            uncompressed_limit=MAX_UNCOMPRESSED_SHARD_BYTES,
        )
        return

    try:
        manifest = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShardedJsonlError(f"invalid shard index {source}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != "govuk-okf-jsonl-shards.v1":
        raise ShardedJsonlError(f"unsupported shard index: {source}")
    rows = manifest.get("shards")
    if not isinstance(rows, list):
        raise ShardedJsonlError(f"shard index has no shards list: {source}")
    compressed_limit = min(
        int(manifest.get("max_compressed_bytes_per_shard", MAX_COMPRESSED_SHARD_BYTES)),
        MAX_COMPRESSED_SHARD_BYTES,
    )
    uncompressed_limit = min(
        int(manifest.get("max_uncompressed_bytes_per_shard", MAX_UNCOMPRESSED_SHARD_BYTES)),
        MAX_UNCOMPRESSED_SHARD_BYTES,
    )
    aggregate = hashlib.sha256()
    total = 0
    for ordinal, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise ShardedJsonlError(f"invalid shard row {ordinal} in {source}")
        shard = _safe_shard(source, row["path"])
        if shard.stat().st_size != int(row.get("bytes", -1)):
            raise ShardedJsonlError(f"shard byte count mismatch: {shard}")
        if file_sha256(shard) != row.get("file_sha256"):
            raise ShardedJsonlError(f"shard file hash mismatch: {shard}")
        shard_digest = hashlib.sha256()
        shard_count = 0
        for value in _iter_one(
            shard,
            compressed_limit=compressed_limit,
            uncompressed_limit=uncompressed_limit,
            require_canonical=True,
        ):
            encoded = canonical_json_bytes(value)
            shard_digest.update(encoded)
            aggregate.update(encoded)
            shard_count += 1
            total += 1
            yield value
        if shard_count != int(row.get("records", -1)):
            raise ShardedJsonlError(f"shard record count mismatch: {shard}")
        if shard_digest.hexdigest() != row.get("canonical_sha256"):
            raise ShardedJsonlError(f"shard canonical hash mismatch: {shard}")
    if total != int(manifest.get("records", -1)):
        raise ShardedJsonlError(f"aggregate record count mismatch: {source}")
    if aggregate.hexdigest() != manifest.get("canonical_sha256"):
        raise ShardedJsonlError(f"aggregate canonical hash mismatch: {source}")
