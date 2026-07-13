"""Deterministic GitHub Release-asset packs for the publication data plane.

The logical bundle keeps its existing virtual shard paths.  Release packaging
concatenates those immutable shard bytes into bounded, content-verified packs;
the Pages control plane publishes an offset index that lets a browser recover
one virtual shard with a single HTTP byte-range request.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import stat
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

PACK_INDEX_SCHEMA = "govuk-okf-github-release-pack-index.v1"
PACK_ALGORITHM = "concatenated-byte-ranges-v1"
GITHUB_RELEASE_ASSET_LIMIT = 1000
GITHUB_RELEASE_ASSET_MAX_BYTES = 2 * 1024 * 1024 * 1024
# Pages serves these packs as same-origin range resources.  A 64 MiB ceiling
# keeps individual CDN objects and browser range arithmetic comfortably bounded
# while remaining far below GitHub Releases' strict per-asset limit.
DEFAULT_MAX_PACK_BYTES = 64 * 1024 * 1024
MAX_LOGICAL_SHARD_BYTES = 64 * 1024 * 1024
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class DataPlanePackError(ValueError):
    """Raised when the logical data plane or a pack/index is inconsistent."""


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _sha256_file(path: Path, *, offset: int = 0, length: int | None = None) -> str:
    digest = hashlib.sha256()
    remaining = length
    with path.open("rb") as stream:
        if offset:
            stream.seek(offset)
        while remaining is None or remaining > 0:
            size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            chunk = stream.read(size)
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    if remaining not in (None, 0):
        raise DataPlanePackError(f"short read while hashing {path}")
    return digest.hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataPlanePackError(f"{label} is missing or invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise DataPlanePackError(f"{label} must be an object")
    return value


def _safe_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise DataPlanePackError("data-plane shard has no path")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() != value:
        raise DataPlanePackError(f"unsafe data-plane shard path: {value}")
    return value


def _resolved_directory_without_symlinks(path: Path) -> Path:
    """Resolve an existing directory only after every lexical component is non-symlinked."""

    absolute = Path(os.path.abspath(path))
    components = [*reversed(absolute.parents), absolute]
    final_mode = None
    for component in components:
        final_mode = component.lstat().st_mode
        if stat.S_ISLNK(final_mode):
            raise DataPlanePackError(f"symlinked directory component: {component}")
    if final_mode is None or not stat.S_ISDIR(final_mode):
        raise DataPlanePackError(f"asset root is not a directory: {absolute}")
    return absolute.resolve(strict=True)


def _rows(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DataPlanePackError(f"{label} must be an array")
    if not all(isinstance(row, dict) for row in value):
        raise DataPlanePackError(f"{label} contains a non-object")
    return value


def collect_data_plane_rows(bundle: Path) -> list[dict[str, Any]]:
    """Return the canonical, complete virtual-shard inventory for ``bundle``."""

    bundle = bundle.resolve()
    manifest = _load_json(bundle / "data" / "manifest.json", "data manifest")
    collected: list[dict[str, Any]] = []
    for kind, values in sorted((manifest.get("shards") or {}).items()):
        collected.extend(_rows(values, f"data manifest {kind} shards"))

    for relative, label in (
        ("data/routes/manifest.json", "route manifest"),
        ("data/adjacency/manifest.json", "adjacency manifest"),
    ):
        path = bundle / relative
        if path.is_file():
            collected.extend(_rows(_load_json(path, label).get("shards"), f"{label} shards"))

    search_manifest_path = bundle / "data/search/manifest.json"
    if search_manifest_path.is_file():
        search_manifest = _load_json(search_manifest_path, "search manifest")
        metadata_path = _safe_path(search_manifest.get("shard_metadata"))
        search_metadata = _load_json(bundle / metadata_path, "search shard metadata")
        for kind, values in sorted((search_metadata.get("shards") or {}).items()):
            collected.extend(_rows(values, f"search {kind} shards"))

    semantic_path = bundle / "data/semantic/manifest.json"
    if semantic_path.is_file():
        semantic = _load_json(semantic_path, "semantic manifest")
        for kind, values in sorted((semantic.get("shards") or {}).items()):
            collected.extend(_rows(values, f"semantic {kind} shards"))

    by_path: dict[str, dict[str, Any]] = {}
    for source in collected:
        path_value = _safe_path(source.get("path"))
        sha256 = str(source.get("sha256") or "").lower()
        if not SHA256_PATTERN.fullmatch(sha256):
            raise DataPlanePackError(f"data-plane shard has no valid SHA-256: {path_value}")
        local = (bundle / path_value).resolve()
        if not local.is_relative_to(bundle) or not local.is_file() or local.is_symlink():
            raise DataPlanePackError(f"data-plane shard is missing or unsafe: {path_value}")
        size = local.stat().st_size
        if size < 1 or size > MAX_LOGICAL_SHARD_BYTES:
            raise DataPlanePackError(f"data-plane shard exceeds the 64 MiB logical-byte ceiling: {path_value}")
        declared_size = source.get("compressed_bytes")
        if not isinstance(declared_size, int) or isinstance(declared_size, bool) or declared_size != size:
            raise DataPlanePackError(f"data-plane shard size differs: {path_value}")
        if _sha256_file(local) != sha256:
            raise DataPlanePackError(f"data-plane shard SHA-256 differs: {path_value}")
        row = {
            "path": path_value,
            "bytes": size,
            "sha256": sha256,
            "compression": str(source.get("compression") or "identity"),
        }
        if row["compression"] not in {"identity", "gzip"}:
            raise DataPlanePackError(f"unsupported data-plane shard compression: {path_value}")
        existing = by_path.get(path_value)
        if existing is not None and existing != row:
            raise DataPlanePackError(f"conflicting data-plane shard metadata: {path_value}")
        by_path[path_value] = row
    return [by_path[path_value] for path_value in sorted(by_path)]


def _index_root(packs: list[dict[str, Any]], entries: list[dict[str, Any]]) -> str:
    material = {"algorithm": PACK_ALGORITHM, "packs": packs, "entries": entries}
    return hashlib.sha256(_canonical_bytes(material)).hexdigest()


def _transport_bytes(path: Path, compression: str) -> tuple[bytes, str]:
    payload = path.read_bytes()
    if compression != "identity":
        return payload, "identity"
    return gzip.compress(payload, compresslevel=9, mtime=0), "gzip"


def build_release_packs(
    *,
    bundle: Path,
    assets: Path,
    repository: str,
    tag: str,
    max_pack_bytes: int = DEFAULT_MAX_PACK_BYTES,
) -> dict[str, Any]:
    """Create deterministic bounded packs and return their Pages offset index."""

    if not 0 < max_pack_bytes <= DEFAULT_MAX_PACK_BYTES:
        raise DataPlanePackError("pack ceiling must be positive and no greater than 64 MiB")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise DataPlanePackError("repository must be OWNER/REPOSITORY")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", tag):
        raise DataPlanePackError("release tag contains unsupported URL characters")
    bundle = bundle.resolve()
    assets.mkdir(parents=True, exist_ok=True)
    rows = collect_data_plane_rows(bundle)
    snapshot = str(_load_json(bundle / "data/manifest.json", "data manifest").get("snapshot") or "")
    if not snapshot:
        raise DataPlanePackError("data manifest has no snapshot")

    packs: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    stream = None
    digest = None
    pack_path: Path | None = None
    pack_size = 0
    pack_id = -1

    def start_pack() -> None:
        nonlocal stream, digest, pack_path, pack_size, pack_id
        pack_id += 1
        asset_name = f"okf-govuk-data-{tag}-{pack_id:05d}.pack.gz"
        pack_path = assets / asset_name
        if pack_path.exists():
            raise DataPlanePackError(f"pack target already exists: {pack_path}")
        stream = pack_path.open("xb")
        digest = hashlib.sha256()
        pack_size = 0

    def finish_pack() -> None:
        nonlocal stream, digest, pack_path
        if stream is None or digest is None or pack_path is None:
            return
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        asset_name = pack_path.name
        packs.append(
            {
                "id": f"pack-{pack_id:05d}",
                "asset_name": asset_name,
                "bytes": pack_size,
                "sha256": digest.hexdigest(),
                "path": f"data-packs/{asset_name}",
                "release_url": (
                    f"https://github.com/{repository}/releases/download/"
                    f"{quote(tag, safe='')}/{quote(asset_name, safe='')}"
                ),
            }
        )
        stream = digest = pack_path = None

    try:
        for row in rows:
            source = bundle / row["path"]
            payload, transport_compression = _transport_bytes(source, str(row["compression"]))
            packed_length = len(payload)
            if packed_length > max_pack_bytes:
                raise DataPlanePackError(f"one encoded data-plane shard exceeds the pack ceiling: {row['path']}")
            if stream is None or (pack_size and pack_size + packed_length > max_pack_bytes):
                finish_pack()
                start_pack()
            assert stream is not None and digest is not None
            offset = pack_size
            stream.write(payload)
            digest.update(payload)
            pack_size += packed_length
            entries.append(
                {
                    **row,
                    "pack": f"pack-{pack_id:05d}",
                    "offset": offset,
                    "packed_bytes": packed_length,
                    "packed_sha256": hashlib.sha256(payload).hexdigest(),
                    "transport_compression": transport_compression,
                }
            )
        finish_pack()
    except Exception:
        if stream is not None:
            stream.close()
        raise

    # Release evidence and control assets also consume slots.  Fail with a wide
    # deterministic margin instead of relying on the platform's exact boundary.
    if len(packs) > GITHUB_RELEASE_ASSET_LIMIT - 100:
        raise DataPlanePackError("data plane would leave fewer than 100 release-asset slots for controls")
    index = {
        "schema": PACK_INDEX_SCHEMA,
        "schema_version": "1.0",
        "algorithm": PACK_ALGORITHM,
        "repository": repository,
        "tag": tag,
        "snapshot": snapshot,
        "max_pack_bytes": max_pack_bytes,
        "packs": packs,
        "entries": entries,
        "counts": {
            "packs": len(packs),
            "virtual_shards": len(entries),
            "packed_bytes": sum(int(row["bytes"]) for row in packs),
            "source_bytes": sum(int(row["bytes"]) for row in entries),
        },
    }
    index["index_root_sha256"] = _index_root(packs, entries)
    return index


def write_pack_index(index: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(index))


def verify_release_packs(index: dict[str, Any], assets: Path) -> list[str]:
    """Validate a complete offset index and every local pack byte."""

    errors: list[str] = []
    if index.get("schema") != PACK_INDEX_SCHEMA or index.get("algorithm") != PACK_ALGORITHM:
        return ["release data-plane index schema or algorithm is invalid"]
    packs_value = index.get("packs")
    entries_value = index.get("entries")
    if not isinstance(packs_value, list) or not isinstance(entries_value, list):
        return ["release data-plane packs or entries are not arrays"]
    max_pack_bytes = index.get("max_pack_bytes")
    if (
        not isinstance(max_pack_bytes, int)
        or isinstance(max_pack_bytes, bool)
        or not 0 < max_pack_bytes <= DEFAULT_MAX_PACK_BYTES
    ):
        errors.append("release data-plane pack ceiling is invalid or exceeds 64 MiB")
        max_pack_bytes = DEFAULT_MAX_PACK_BYTES
    if len(packs_value) > GITHUB_RELEASE_ASSET_LIMIT - 100:
        errors.append("release data plane leaves fewer than 100 GitHub Release asset slots")
    repository = index.get("repository")
    tag = index.get("tag")
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        errors.append("release data-plane repository is invalid")
        repository = "invalid/invalid"
    if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", tag):
        errors.append("release data-plane tag is invalid")
        tag = "invalid"
    try:
        assets_root = _resolved_directory_without_symlinks(assets)
    except (OSError, DataPlanePackError) as exc:
        return [*errors, f"release data-plane asset directory is missing or unsafe: {exc}"]
    packs: dict[str, dict[str, Any]] = {}
    pack_paths: dict[str, Path] = {}
    for row in packs_value:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            errors.append("release data-plane pack row is malformed")
            continue
        pack_id = row["id"]
        if not re.fullmatch(r"pack-[0-9]{5}", pack_id) or pack_id in packs:
            errors.append(f"duplicate release data-plane pack: {pack_id}")
            continue
        asset_name = row.get("asset_name")
        if (
            not isinstance(asset_name, str)
            or not asset_name
            or Path(asset_name).name != asset_name
            or asset_name in {".", ".."}
        ):
            errors.append(f"release data-plane pack name is unsafe: {pack_id}")
            continue
        expected_path = f"data-packs/{asset_name}"
        expected_release_url = (
            f"https://github.com/{repository}/releases/download/"
            f"{quote(tag, safe='')}/{quote(asset_name, safe='')}"
        )
        metadata_valid = True
        if not asset_name.endswith(".pack.gz") or row.get("path") != expected_path:
            errors.append(f"release data-plane Pages pack path is invalid: {pack_id}")
            metadata_valid = False
        if row.get("release_url") != expected_release_url:
            errors.append(f"release data-plane Release mirror URL is invalid: {pack_id}")
            metadata_valid = False
        if not isinstance(row.get("sha256"), str) or not SHA256_PATTERN.fullmatch(row["sha256"]):
            errors.append(f"release data-plane pack hash is malformed: {pack_id}")
            metadata_valid = False
        expected_bytes = row.get("bytes")
        if (
            not isinstance(expected_bytes, int)
            or isinstance(expected_bytes, bool)
            or not 0 < expected_bytes <= max_pack_bytes
        ):
            errors.append(f"release data-plane pack size differs or exceeds its 64 MiB ceiling: {pack_id}")
            metadata_valid = False
        if not metadata_valid:
            continue

        path = assets / asset_name
        try:
            if path.is_symlink():
                raise DataPlanePackError("symlinked pack")
            resolved = path.resolve(strict=True)
            if resolved.parent != assets_root or not resolved.is_file():
                raise DataPlanePackError("pack does not resolve to a regular file in the asset directory")
            size = resolved.stat().st_size
            if size != expected_bytes or not 0 < size <= max_pack_bytes:
                errors.append(f"release data-plane pack size differs or exceeds its 64 MiB ceiling: {pack_id}")
                continue
            if _sha256_file(resolved, length=size) != row.get("sha256"):
                errors.append(f"release data-plane pack hash differs: {pack_id}")
                continue
        except (OSError, DataPlanePackError) as exc:
            errors.append(f"release data-plane pack file is missing or unsafe: {pack_id}: {exc}")
            continue
        packs[pack_id] = row
        pack_paths[pack_id] = resolved
    observed_paths: set[str] = set()
    cursors = {pack_id: 0 for pack_id in packs}
    for row in entries_value:
        if not isinstance(row, dict):
            errors.append("release data-plane entry is not an object")
            continue
        try:
            virtual_path = _safe_path(row.get("path"))
        except DataPlanePackError as exc:
            errors.append(str(exc))
            continue
        if virtual_path in observed_paths:
            errors.append(f"duplicate release data-plane path: {virtual_path}")
            continue
        observed_paths.add(virtual_path)
        pack_id = row.get("pack")
        offset = row.get("offset")
        packed_length = row.get("packed_bytes")
        source_length = row.get("bytes")
        if (
            pack_id not in packs
            or not isinstance(offset, int)
            or not isinstance(packed_length, int)
            or not isinstance(source_length, int)
        ):
            errors.append(f"release data-plane range is malformed: {virtual_path}")
            continue
        if (
            not isinstance(row.get("sha256"), str)
            or not SHA256_PATTERN.fullmatch(row["sha256"])
            or not isinstance(row.get("packed_sha256"), str)
            or not SHA256_PATTERN.fullmatch(row["packed_sha256"])
            or row.get("compression") not in {"identity", "gzip"}
        ):
            errors.append(f"release data-plane entry hash or compression is invalid: {virtual_path}")
            continue
        if (
            offset != cursors[pack_id]
            or offset < 0
            or not 0 < packed_length <= max_pack_bytes
            or not 0 < source_length <= MAX_LOGICAL_SHARD_BYTES
        ):
            errors.append(f"release data-plane ranges are not contiguous: {virtual_path}")
            continue
        cursors[pack_id] += packed_length
        pack_path = pack_paths[pack_id]
        try:
            with pack_path.open("rb") as stream:
                stream.seek(offset)
                packed = stream.read(packed_length)
            if len(packed) != packed_length or hashlib.sha256(packed).hexdigest() != row.get("packed_sha256"):
                errors.append(f"release data-plane transport range hash differs: {virtual_path}")
                continue
            if not packed.startswith(b"\x1f\x8b"):
                errors.append(f"release data-plane transport range is not gzip-framed: {virtual_path}")
                continue
            transport_compression = row.get("transport_compression")
            if transport_compression == "gzip":
                with gzip.GzipFile(fileobj=BytesIO(packed), mode="rb") as compressed:
                    source = compressed.read(min(source_length, MAX_LOGICAL_SHARD_BYTES) + 1)
            elif transport_compression == "identity" and row.get("compression") == "gzip":
                source = packed
            else:
                errors.append(f"release data-plane transport compression is invalid: {virtual_path}")
                continue
            if len(source) != source_length or hashlib.sha256(source).hexdigest() != row.get("sha256"):
                errors.append(f"release data-plane source range hash differs: {virtual_path}")
                continue
            if row.get("compression") == "gzip":
                with gzip.GzipFile(fileobj=BytesIO(source), mode="rb") as compressed:
                    if len(compressed.read(MAX_LOGICAL_SHARD_BYTES + 1)) > MAX_LOGICAL_SHARD_BYTES:
                        errors.append(f"release data-plane logical gzip expands beyond 64 MiB: {virtual_path}")
        except (OSError, DataPlanePackError, gzip.BadGzipFile, EOFError) as exc:
            errors.append(f"release data-plane range is unreadable: {virtual_path}: {exc}")
    for pack_id, cursor in cursors.items():
        if cursor != packs[pack_id].get("bytes"):
            errors.append(f"release data-plane pack has unindexed bytes: {pack_id}")
    if index.get("index_root_sha256") != _index_root(packs_value, entries_value):
        errors.append("release data-plane index root differs")
    counts = index.get("counts")
    if not isinstance(counts, dict) or counts != {
        "packs": len(packs_value),
        "virtual_shards": len(entries_value),
        "packed_bytes": sum(int(row.get("bytes") or 0) for row in packs_value if isinstance(row, dict)),
        "source_bytes": sum(int(row.get("bytes") or 0) for row in entries_value if isinstance(row, dict)),
    }:
        errors.append("release data-plane counts differ")
    return errors


def data_plane_paths(rows: Iterable[dict[str, Any]]) -> set[str]:
    return {str(row["path"]) for row in rows}
