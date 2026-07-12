"""Deterministic, resumable T0 to T1 closing-delta reconciliation.

The closing snapshot is deliberately separate from initial enumeration and
hydration.  T1 enumeration is the authority for the current union.  A T0
metadata envelope is reused only when the complete non-observational
enumerator fingerprint matches at T1; every other current route is hydrated.
Routes present at T0 but absent from T1 are actively probed and retained as a
represented, redirect, tombstone, or exception record instead of disappearing.
"""

from __future__ import annotations

import concurrent.futures
import fcntl
import gzip
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import quote, urlparse

from .acquisition import (
    AcquisitionError,
    CONTENT_API_ROOT,
    HostLimiter,
    candidate_key,
    expand_candidate_records,
    merge_records,
    normalise_url,
    request_observation,
    sanitise_content_api,
    write_jsonl_gzip_shards,
    write_text_atomic,
)
from .hydration import _linked_records
from .rendered_gap import RobotsPolicy, parse_robots
from .util import canonical_json_bytes, pretty_json


class ClosingError(RuntimeError):
    """Raised when a T0 to T1 closing snapshot cannot close safely."""


FINGERPRINT_SCHEMA = "govuk-closing-enumerator-fingerprint.v1"
FINGERPRINT_OMITTED_TOP_LEVEL_FIELDS = frozenset(
    {
        "candidate_key",
        "candidate_keys",
        "evidence_locator",
        "evidence_observations",
        "evidence_sha256",
        "evidence_url",
        "hydration_status",
        "content_api_final_url",
        "content_api_status",
        "retrieved_at",
        "snapshot_id",
    }
)
FORBIDDEN_BODY_FIELDS = frozenset({"body", "rendered", "rendered_content"})
OUTCOME_TO_COVERAGE = {
    "represented": "represented",
    "redirect": "redirect_only",
    "tombstone": "tombstone_only",
    "exception": "exceptioned",
}
VALID_COVERAGE = frozenset(
    {"represented", "alias_of_represented", "redirect_only", "tombstone_only", "exceptioned"}
)
DEFAULT_SHARD_RECORDS = 10_000
DEFAULT_MAX_COMPRESSED_SHARD_BYTES = 49 * 1024 * 1024
ROBOTS_URL = "https://www.gov.uk/robots.txt"
DEFAULT_OFFICIAL_REQUEST_CEILING = 1_000_000
REQUEST_ATTEMPTS = 5


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _timestamp_key(value: str) -> tuple[int, float, str]:
    if not value:
        return (0, 0.0, "")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (1, parsed.astimezone(timezone.utc).timestamp(), value)
    except ValueError:
        return (0, 0.0, value)


def _sha256_input_artifact(path: Path) -> str:
    """Digest a file or directory without depending on filesystem mtimes."""

    if path.is_file():
        return _sha256_file(path)
    if not path.is_dir():
        raise ClosingError(f"input artefact does not exist: {path}")
    digest = hashlib.sha256()
    files = sorted(item for item in path.rglob("*") if item.is_file())
    for item in files:
        relative = item.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(item).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _resolve_input_shard(root: Path, manifest: Path, value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        local = (manifest.parent / candidate).resolve()
        rooted = (root / candidate).resolve()
        resolved = local if local.is_file() else rooted
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ClosingError(f"input shard escapes repository root: {value}") from exc
    if not resolved.is_file():
        raise ClosingError(f"input shard does not exist: {resolved}")
    return resolved


def _manifest_shard_rows(value: dict[str, Any]) -> list[Any]:
    for candidate in (
        value.get("shards"),
        value.get("record_shards"),
        value.get("source_record_shards"),
        value.get("candidate_record_shards"),
        value.get("records", {}).get("shards") if isinstance(value.get("records"), dict) else None,
    ):
        if isinstance(candidate, list):
            return candidate
    raise ClosingError("record manifest has no shards list")


def _read_jsonl_shard(path: Path) -> Iterator[dict[str, Any]]:
    if path.stat().st_size >= 50 * 1024 * 1024:
        raise ClosingError(f"source-record shard is not below 50 MiB: {path}")
    try:
        total = 0
        with gzip.open(path, "rb") as stream:
            number = 0
            while True:
                line = stream.readline(16 * 1024 * 1024 + 1)
                if not line:
                    break
                number += 1
                total += len(line)
                if len(line) > 16 * 1024 * 1024:
                    raise ClosingError(f"{path}:{number}: source record exceeds 16 MiB")
                if total > 32 * 1024 * 1024:
                    raise ClosingError(f"source-record shard expands beyond 32 MiB: {path}")
                if not line.strip():
                    continue
                value = json.loads(line.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ClosingError(f"{path}:{number}: source record must be an object")
                yield value
    except ClosingError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClosingError(f"invalid source-record shard {path}: {exc}") from exc


def iter_record_input(path: Path, root: Path) -> Iterator[dict[str, Any]]:
    """Read a legacy file, a shard manifest, or a manifest-bearing directory."""

    path = path.resolve()
    if path.is_dir():
        manifests = [
            candidate
            for candidate in (
                path / "index.json",
                path / "manifest.json",
                path / "record-manifest.json",
            )
            if candidate.is_file()
        ]
        if manifests:
            try:
                manifest_value = json.loads(manifests[0].read_text(encoding="utf-8"))
                _manifest_shard_rows(manifest_value)
            except (json.JSONDecodeError, ClosingError):
                legacy = path / "source-records.jsonl.gz"
                if legacy.is_file():
                    yield from _read_jsonl_shard(legacy)
                    return
                raise
            yield from iter_record_input(manifests[0], root)
            return
        shards = sorted(path.rglob("*.jsonl.gz"))
        if not shards:
            raise ClosingError(f"record directory has no manifest or JSONL gzip shards: {path}")
        for shard in shards:
            yield from _read_jsonl_shard(shard)
        return
    if not path.is_file():
        raise ClosingError(f"record input does not exist: {path}")
    if path.suffix == ".json":
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ClosingError(f"record manifest exceeds 16 MiB: {path}")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ClosingError(f"invalid record manifest {path}: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ClosingError(f"record manifest is not an object: {path}")
        aggregate_digest = hashlib.sha256()
        aggregate_count = 0
        for row in _manifest_shard_rows(manifest):
            if isinstance(row, str):
                shard_value = row
                expected_file_sha = None
                expected_count = None
            elif isinstance(row, dict):
                shard_value = str(row.get("path") or row.get("url") or "")
                expected_file_sha = row.get("file_sha256") or row.get("sha256")
                expected_count = row.get("count")
            else:
                raise ClosingError(f"invalid shard entry in {path}")
            if not shard_value:
                raise ClosingError(f"shard entry has no path in {path}")
            shard = _resolve_input_shard(root, path, shard_value)
            expected_bytes = (
                row.get("bytes", row.get("compressed_bytes"))
                if isinstance(row, dict)
                else None
            )
            if expected_bytes is not None and shard.stat().st_size != int(expected_bytes):
                raise ClosingError(f"input shard byte count mismatch: {shard}")
            if expected_file_sha and _sha256_file(shard) != str(expected_file_sha):
                raise ClosingError(f"input shard file digest mismatch: {shard}")
            count = 0
            canonical_digest = hashlib.sha256()
            for record in _read_jsonl_shard(shard):
                count += 1
                encoded = canonical_json_bytes(record)
                canonical_digest.update(encoded)
                aggregate_digest.update(encoded)
                aggregate_count += 1
                yield record
            expected_count = (
                expected_count
                if expected_count is not None
                else row.get("records")
                if isinstance(row, dict)
                else None
            )
            if expected_count is not None and count != int(expected_count):
                raise ClosingError(f"input shard count mismatch for {shard}: {count} != {expected_count}")
            if isinstance(row, dict) and row.get("canonical_sha256"):
                if canonical_digest.hexdigest() != str(row["canonical_sha256"]):
                    raise ClosingError(f"input shard canonical digest mismatch: {shard}")
        expected_total = manifest.get(
            "records",
            manifest.get("count", manifest.get("source_records", manifest.get("candidate_records"))),
        )
        if expected_total is not None and aggregate_count != int(expected_total):
            raise ClosingError(
                f"input manifest count mismatch for {path}: {aggregate_count} != {expected_total}"
            )
        expected_aggregate = manifest.get(
            "canonical_sha256",
            manifest.get("source_records_sha256", manifest.get("candidate_records_sha256")),
        )
        if expected_aggregate and aggregate_digest.hexdigest() != str(expected_aggregate):
            raise ClosingError(f"input manifest canonical digest mismatch: {path}")
        return
    if path.suffix == ".gz":
        yield from _read_jsonl_shard(path)
        return
    with path.open("rb") as stream:
        number = 0
        total = 0
        while True:
            line = stream.readline(16 * 1024 * 1024 + 1)
            if not line:
                break
            number += 1
            total += len(line)
            if len(line) > 16 * 1024 * 1024:
                raise ClosingError(f"{path}:{number}: source record exceeds 16 MiB")
            if total > 32 * 1024 * 1024:
                raise ClosingError(f"source-record file exceeds 32 MiB: {path}")
            if not line.strip():
                continue
            value = json.loads(line.decode("utf-8"))
            if not isinstance(value, dict):
                raise ClosingError(f"{path}:{number}: source record must be an object")
            yield value


def write_record_shards(
    root: Path,
    directory: Path,
    records: Iterable[dict[str, Any]],
    *,
    schema: str,
    snapshot: str,
    max_records: int = DEFAULT_SHARD_RECORDS,
    max_compressed_bytes: int = DEFAULT_MAX_COMPRESSED_SHARD_BYTES,
) -> dict[str, Any]:
    """Write the repository-standard immutable JSONL gzip shard manifest."""

    try:
        output = write_jsonl_gzip_shards(
            directory.parent,
            directory.name,
            records,
            max_records=max_records,
            max_compressed_bytes=max_compressed_bytes,
        )
    except Exception as exc:
        raise ClosingError(f"could not write {schema} shards: {exc}") from exc
    output_root = Path(output["root"])
    manifest_path = output_root / "index.json"
    shards = [
        {
            "ordinal": ordinal,
            "path": (output_root / row["path"]).relative_to(root).as_posix(),
            "count": int(row["records"]),
            "canonical_sha256": row["canonical_sha256"],
            "file_sha256": row["file_sha256"],
            "compressed_bytes": int(row["bytes"]),
        }
        for ordinal, row in enumerate(output["shards"])
    ]
    return {
        "schema_version": 1,
        "schema": schema,
        "shard_manifest_schema": output["schema"],
        "snapshot": snapshot,
        "format": "canonical-jsonl+gzip",
        "ordering": "input order",
        "maximum_records_per_shard": int(output["max_records_per_shard"]),
        "maximum_compressed_bytes_per_shard": int(output["max_compressed_bytes_per_shard"]),
        "count": int(output["records"]),
        "canonical_sha256": output["canonical_sha256"],
        "shards": shards,
        "manifest_path": manifest_path.relative_to(root).as_posix(),
        "manifest_file_sha256": _sha256_file(manifest_path),
    }


def _assert_body_free(value: Any, location: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_BODY_FIELDS:
                raise ClosingError(f"complete page body field is prohibited at {location}/{key}")
            _assert_body_free(item, f"{location}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_body_free(item, f"{location}/{index}")


def enumerator_fingerprint(record: dict[str, Any]) -> str:
    """Hash every enumerator field except explicitly observational metadata.

    The exclusion list is intentionally narrow.  Source memberships, native
    identifiers, lifecycle dates, titles, descriptions, links, locale, and
    source-native types all participate.  An unknown new field therefore makes
    the record changed rather than being silently ignored.
    """

    _assert_body_free(record)
    stable = {
        key: value
        for key, value in record.items()
        if key not in FINGERPRINT_OMITTED_TOP_LEVEL_FIELDS
    }
    return hashlib.sha256(canonical_json_bytes(stable)).hexdigest()


def _record_key(record: dict[str, Any]) -> tuple[str, str]:
    url = normalise_url(str(record.get("canonical_url") or record.get("base_path") or "/"))
    return url, str(record.get("locale") or "en")


def _is_linked_only(record: dict[str, Any]) -> bool:
    memberships = {str(value) for value in record.get("source_memberships", [])}
    return bool(memberships) and memberships <= {
        "structured-linked-content",
        "rendered-link-gap",
        "rendered-links",
    }


def _declared_outcome(record: dict[str, Any]) -> str:
    coverage = str(record.get("coverage_disposition") or "represented")
    if coverage == "exceptioned":
        return "exception"
    if coverage == "tombstone_only":
        return "tombstone"
    if coverage == "redirect_only" or record.get("document_type") == "redirect" or record.get("redirects"):
        return "redirect"
    return "represented"


def _is_reusable_hydrated(record: dict[str, Any]) -> bool:
    """Reject an exact census match when T0 enrichment itself was incomplete."""

    if _declared_outcome(record) == "exception":
        return False
    hydration_status = str(record.get("hydration_status") or "")
    if hydration_status in {"content_api_unavailable", "content_api_exception"}:
        return False
    rendered_status = str(record.get("rendered_gap_status") or "")
    if rendered_status in {"exception", "robots_blocked"}:
        return False
    retryable_constraint_classes = {
        "closing_probe",
        "content_api_hydration",
        "rendered_link_gap_detector",
    }
    constraints = record.get("constraints")
    if isinstance(constraints, list) and any(
        isinstance(row, dict) and str(row.get("class")) in retryable_constraint_classes
        for row in constraints
    ):
        return False
    if hydration_status in {"content_api_represented", "external_boundary"}:
        return True
    return bool(record.get("content_id")) or urlparse(_record_key(record)[0]).netloc != "www.gov.uk"


def _public_evidence(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key
        in {
            "acquisition_attempt",
            "attempts",
            "bytes_retained",
            "elapsed_ms",
            "error",
            "family",
            "final_url",
            "headers",
            "id",
            "ok",
            "partial",
            "requested_url",
            "retrieved_at",
            "sha256",
            "status",
        }
    }


def _closing_constraint(
    url: str,
    reason: str,
    evidence: dict[str, Any],
    closing_label: str,
) -> dict[str, Any]:
    identity = hashlib.sha256(
        f"{closing_label}\0{url}\0{evidence.get('status')}\0{reason}".encode("utf-8")
    ).hexdigest()[:24]
    reviewed = str(evidence.get("retrieved_at") or "1970-01-01T00:00:00Z")[:10]
    return {
        "id": f"closing-exception-{identity}",
        "class": "closing_probe",
        "reason": reason,
        "status": int(evidence.get("status") or 0),
        "evidence_url": evidence.get("requested_url") or url,
        "evidence_sha256": evidence.get("sha256"),
        "owner": "corpus-maintainer",
        "review_date": reviewed,
        "retry": "next acquisition window",
    }


def _validate_reconciliation(path: Path, role: str) -> tuple[dict[str, Any], str]:
    try:
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ClosingError(f"{role} reconciliation exceeds the 32 MiB control-plane limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClosingError(f"cannot read {role} reconciliation {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ClosingError(f"{role} reconciliation must be an object")
    if value.get("sampled") is not False:
        raise ClosingError(f"{role} reconciliation is sampled or does not declare sampled=false")
    if int(value.get("unexplained_omissions", -1)) != 0:
        raise ClosingError(f"{role} reconciliation has pending or unexplained candidates")
    if role == "T0 hydrated":
        proof = value.get("hydration_proof")
        if value.get("hydrated") is not True or not isinstance(proof, dict):
            raise ClosingError("T0 reconciliation is not a hydrated closure")
        if proof.get("closed") is not True or int(proof.get("pending", -1)) != 0:
            raise ClosingError("T0 hydration still has pending work")
    elif role == "T1 enumeration":
        search_proofs = value.get("search_partition_proofs")
        required = {
            "search_partitions_closed": value.get("search_partitions_closed") is True,
            "search_partition_proofs": isinstance(search_proofs, list) and bool(search_proofs),
            "sitemap_byte_stable": value.get("sitemap_byte_stable") is True,
            "sitemap_proof.closed": isinstance(value.get("sitemap_proof"), dict)
            and value["sitemap_proof"].get("closed") is True,
            "organisations_proof.closed": isinstance(value.get("organisations_proof"), dict)
            and value["organisations_proof"].get("closed") is True,
            "navigation_proof.closed": isinstance(value.get("navigation_proof"), dict)
            and value["navigation_proof"].get("closed") is True,
        }
        failed = [name for name, passed in required.items() if not passed]
        if failed:
            raise ClosingError("T1 enumeration is not authoritative and closed: " + ", ".join(failed))
        for index, proof in enumerate(search_proofs):
            if not isinstance(proof, dict) or not isinstance(proof.get("expected"), int):
                raise ClosingError(f"T1 Search partition proof {index} is invalid")
            expected = int(proof["expected"])
            passes = proof.get("passes")
            if not isinstance(passes, list) or len(passes) < 2:
                raise ClosingError(f"T1 Search partition proof {index} lacks opposing passes")
            orders = {str(row.get("order") or "") for row in passes if isinstance(row, dict)}
            if not any(order.startswith("-") for order in orders) or not any(
                order and not order.startswith("-") for order in orders
            ):
                raise ClosingError(f"T1 Search partition proof {index} does not use opposing orders")
            for pass_index, row in enumerate(passes):
                if not isinstance(row, dict):
                    raise ClosingError(
                        f"T1 Search partition proof {index} pass {pass_index} did not close"
                    )
                unique_source_rows = row.get("unique_source_rows")
                if unique_source_rows is None:
                    # Compatibility with pre-source-row proof fixtures. Live
                    # Search acquisitions always emit unique_source_rows.
                    unique_source_rows = row.get("unique_urls")
                unique_urls = row.get("unique_urls")
                alias_rows = row.get("canonical_alias_rows", 0)
                if (
                    row.get("closed") is not True
                    or row.get("returned_rows") != expected
                    or unique_source_rows != expected
                    or not isinstance(unique_urls, int)
                    or not isinstance(alias_rows, int)
                    or unique_urls < 0
                    or alias_rows < 0
                    or unique_urls + alias_rows != expected
                ):
                    raise ClosingError(
                        f"T1 Search partition proof {index} pass {pass_index} did not close"
                    )
            identities = {
                str(row.get("identity_sha256") or "")
                for row in passes
                if isinstance(row, dict) and row.get("identity_sha256")
            }
            if identities and len(identities) != 1:
                raise ClosingError(
                    f"T1 Search partition proof {index} opposing passes disagree"
                )
    return value, hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class ClosingDelta:
    """Close a fully hydrated T0 against a fully re-enumerated T1."""

    def __init__(
        self,
        root: Path,
        t0_label: str,
        t1_label: str,
        t0_enumeration_path: Path,
        t0_hydrated_path: Path,
        t1_enumeration_path: Path,
        t0_reconciliation_path: Path,
        t1_reconciliation_path: Path,
        *,
        closing_label: str | None = None,
        requests_per_second: float = 8.0,
        www_requests_per_second: float = 2.0,
        official_request_ceiling: int = DEFAULT_OFFICIAL_REQUEST_CEILING,
        workers: int = 16,
        batch_size: int = 256,
    ) -> None:
        if (
            workers < 1
            or batch_size < 1
            or official_request_ceiling < 1
            or official_request_ceiling > DEFAULT_OFFICIAL_REQUEST_CEILING
        ):
            raise ClosingError(
                "workers and batch_size must be positive; official_request_ceiling must be 1..1,000,000"
            )
        if requests_per_second <= 0 or www_requests_per_second <= 0:
            raise ClosingError("Content API and public www request rates must be positive")
        self.root = root.resolve()
        self.t0_label = t0_label
        self.t1_label = t1_label
        if self.t0_label == self.t1_label:
            raise ClosingError("T0 and T1 must be distinct immutable snapshots")
        self.label = closing_label or f"{t1_label}-closed"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", self.label):
            raise ClosingError("closing label must be one safe repository path segment")
        if self.label in {self.t0_label, self.t1_label}:
            raise ClosingError("closing label must be distinct from both immutable input snapshots")
        self.t0_enumeration_path = t0_enumeration_path.resolve()
        self.t0_hydrated_path = t0_hydrated_path.resolve()
        self.t1_enumeration_path = t1_enumeration_path.resolve()
        self.t0_reconciliation_path = t0_reconciliation_path.resolve()
        self.t1_reconciliation_path = t1_reconciliation_path.resolve()
        for input_name, input_path in (
            ("T0 enumeration", self.t0_enumeration_path),
            ("T0 hydrated records", self.t0_hydrated_path),
            ("T1 enumeration", self.t1_enumeration_path),
            ("T0 reconciliation", self.t0_reconciliation_path),
            ("T1 reconciliation", self.t1_reconciliation_path),
        ):
            try:
                input_path.relative_to(self.root)
            except ValueError as exc:
                raise ClosingError(f"{input_name} input escapes the repository root") from exc
        self.workers = workers
        self.batch_size = batch_size
        self.content_rate = requests_per_second
        self.www_rate = www_requests_per_second
        self.official_request_ceiling = official_request_ceiling
        self.global_budget_path = (
            self.root / ".tmp" / "request-budget" / "official-sources.count"
        )
        self.cache_root = self.root / "corpus" / "cache" / self.label / "closing"
        self.database_path = self.cache_root / "checkpoint.sqlite"
        self.records_root = self.root / "corpus" / "records" / self.label
        self.inventory_root = self.root / "corpus" / "inventory"
        self.reconciliation_root = self.root / "corpus" / "reconciliation"
        self.drift_root = self.root / "corpus" / "drift" / self.label
        self.content_limiter = HostLimiter(
            requests_per_second,
            state_path=self.root / ".tmp" / "rate-limits" / "content-api.timestamp",
            budget_path=self.global_budget_path,
            max_requests=self.official_request_ceiling,
        )
        self.www_limiter = HostLimiter(
            www_requests_per_second,
            state_path=self.root / ".tmp" / "rate-limits" / "www.gov.uk.timestamp",
            budget_path=self.global_budget_path,
            max_requests=self.official_request_ceiling,
        )
        self.robots_policy: RobotsPolicy | None = None

    def _connect(self) -> sqlite3.Connection:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=60)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inputs (
                role TEXT NOT NULL,
                url TEXT NOT NULL,
                locale TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                record_sha256 TEXT NOT NULL,
                record_json TEXT NOT NULL,
                PRIMARY KEY (role, url, locale)
            );
            CREATE INDEX IF NOT EXISTS inputs_role_key ON inputs(role, url, locale);
            CREATE TABLE IF NOT EXISTS work (
                url TEXT NOT NULL,
                locale TEXT NOT NULL,
                delta_class TEXT NOT NULL CHECK (delta_class IN ('added', 'changed', 'unchanged', 'removed')),
                work_kind TEXT NOT NULL CHECK (
                    work_kind IN (
                        'reuse', 'hydrate', 'hydrate_missing_t0', 'hydrate_unreusable_t0',
                        'linked_only', 'closing_probe'
                    )
                ),
                t0_fingerprint TEXT,
                t1_fingerprint TEXT,
                input_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending', 'complete')),
                attempts INTEGER NOT NULL DEFAULT 0,
                closing_disposition TEXT CHECK (
                    closing_disposition IN ('represented', 'redirect', 'tombstone', 'exception')
                ),
                coverage_disposition TEXT CHECK (
                    coverage_disposition IN (
                        'represented', 'alias_of_represented', 'redirect_only', 'tombstone_only', 'exceptioned'
                    )
                ),
                result_json TEXT,
                evidence_json TEXT,
                linked_discovered INTEGER NOT NULL DEFAULT 0,
                probe_performed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (url, locale)
            );
            CREATE INDEX IF NOT EXISTS work_state_key ON work(state, url, locale);
            CREATE INDEX IF NOT EXISTS work_delta ON work(delta_class, url, locale);
            CREATE TABLE IF NOT EXISTS candidates (
                candidate_key TEXT PRIMARY KEY,
                entity_class TEXT NOT NULL,
                source_native_id TEXT NOT NULL,
                locale TEXT NOT NULL,
                coverage_disposition TEXT NOT NULL,
                candidate_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS request_budget (
                request_class TEXT PRIMARY KEY CHECK (request_class IN ('content_api', 'www_public')),
                used INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
                reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0)
            );
            INSERT OR IGNORE INTO request_budget(request_class, used, reserved)
            VALUES ('content_api', 0, 0);
            INSERT OR IGNORE INTO request_budget(request_class, used, reserved)
            VALUES ('www_public', 0, 0);
            """
        )
        return connection

    def _checkpoint_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=60)
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _meta(connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    @staticmethod
    def _set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def _prepare_robots(self, connection: sqlite3.Connection) -> RobotsPolicy:
        body, evidence = self._observe(
            "www_public", ROBOTS_URL, self.www_limiter, max_bytes=1024 * 1024
        )
        if not evidence.get("ok") or evidence.get("partial"):
            raise ClosingError("current robots.txt could not be verified; closing probes are blocked")
        policy = parse_robots(body, evidence)
        previous = self._meta(connection, "robots_sha256")
        completed_probes = int(
            connection.execute("SELECT COUNT(*) FROM work WHERE probe_performed=1").fetchone()[0]
        )
        if previous is not None and previous != policy.sha256 and completed_probes:
            raise ClosingError("robots.txt changed after closing probes began; use a new closing label")
        self._set_meta(connection, "robots_sha256", policy.sha256)
        self._set_meta(
            connection,
            "robots_evidence_json",
            canonical_json_bytes(
                {
                    "url": policy.source_url,
                    "sha256": policy.sha256,
                    "retrieved_at": policy.retrieved_at,
                    "rules": len(policy.rules),
                }
            ).decode("utf-8"),
        )
        connection.commit()
        self.robots_policy = policy
        return policy

    def _reserve_requests(self, request_class: str, maximum: int) -> None:
        connection = self._checkpoint_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            used, reserved = connection.execute(
                "SELECT COALESCE(SUM(used), 0), COALESCE(SUM(reserved), 0) FROM request_budget"
            ).fetchone()
            baseline = int(self._meta(connection, "programme_request_baseline") or 0)
            programme_used = max(self._global_request_count(), baseline + int(used))
            if programme_used + int(reserved) + maximum > self.official_request_ceiling:
                connection.rollback()
                raise ClosingError(
                    "official request ceiling would be exceeded; closing work is checkpointed"
                )
            connection.execute(
                "UPDATE request_budget SET reserved=reserved+? WHERE request_class=?",
                (maximum, request_class),
            )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _global_request_count(self) -> int:
        if not self.global_budget_path.is_file():
            return 0
        with self.global_budget_path.open("r", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            try:
                value = int(stream.read().strip() or "0")
            except ValueError as exc:
                raise ClosingError("shared official request-budget ledger is invalid") from exc
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return value

    def _settle_requests(self, request_class: str, reserved: int, used: int) -> None:
        connection = self._checkpoint_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT reserved FROM request_budget WHERE request_class=?", (request_class,)
            ).fetchone()
            if current is None or int(current[0]) < reserved:
                connection.rollback()
                raise ClosingError("official request reservation ledger is inconsistent")
            connection.execute(
                "UPDATE request_budget SET reserved=reserved-?, used=used+? WHERE request_class=?",
                (reserved, used, request_class),
            )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _observe(
        self,
        request_class: str,
        url: str,
        limiter: HostLimiter,
        *,
        max_bytes: int,
    ) -> tuple[bytes, dict[str, Any]]:
        self._reserve_requests(request_class, REQUEST_ATTEMPTS)
        try:
            body, evidence = request_observation(
                url,
                limiter=limiter,
                max_bytes=max_bytes,
                attempts=REQUEST_ATTEMPTS,
            )
        except Exception as exc:
            # A process-level failure leaves no reliable attempt count. Charge
            # the full reservation so a restart cannot overspend the ceiling.
            self._settle_requests(request_class, REQUEST_ATTEMPTS, REQUEST_ATTEMPTS)
            if isinstance(exc, AcquisitionError):
                raise ClosingError(str(exc)) from exc
            raise
        attempts = int(
            evidence.get("acquisition_attempt") or evidence.get("attempts") or 1
        )
        attempts = max(1, min(REQUEST_ATTEMPTS, attempts))
        self._settle_requests(request_class, REQUEST_ATTEMPTS, attempts)
        return body, evidence

    def _request_accounting(self, connection: sqlite3.Connection) -> dict[str, Any]:
        rows = {
            str(request_class): {"used": int(used), "reserved": int(reserved)}
            for request_class, used, reserved in connection.execute(
                "SELECT request_class, used, reserved FROM request_budget ORDER BY request_class"
            )
        }
        closing_used = sum(row["used"] for row in rows.values())
        reserved = sum(row["reserved"] for row in rows.values())
        baseline = int(self._meta(connection, "programme_request_baseline") or 0)
        programme_used = max(self._global_request_count(), baseline + closing_used)
        concurrent_used = max(0, programme_used - baseline - closing_used)
        rows.setdefault("content_api", {"used": 0, "reserved": 0}).update(
            {
                "requests_per_second": self.content_rate,
                "state_ledger": ".tmp/rate-limits/content-api.timestamp",
            }
        )
        rows.setdefault("www_public", {"used": 0, "reserved": 0}).update(
            {
                "requests_per_second": self.www_rate,
                "state_ledger": ".tmp/rate-limits/www.gov.uk.timestamp",
            }
        )
        return {
            "ceiling": self.official_request_ceiling,
            "used": programme_used,
            "closing_stage_used": closing_used,
            "prior_stage_used": baseline,
            "other_concurrent_stage_used": concurrent_used,
            "reserved": reserved,
            "remaining": self.official_request_ceiling - programme_used - reserved,
            "retries_included": True,
            "accounting_method": "durable pre-reservation with observed-attempt settlement",
            "uncertain_failure_charge": REQUEST_ATTEMPTS,
            "global_budget_ledger": ".tmp/request-budget/official-sources.count",
            "by_class": rows,
        }

    def _freeze_request_accounting(self, connection: sqlite3.Connection) -> dict[str, Any]:
        existing = self._meta(connection, "final_request_accounting_json")
        if existing is not None:
            return json.loads(existing)
        accounting = self._request_accounting(connection)
        if accounting["reserved"] != 0:
            raise ClosingError("cannot freeze request accounting with unsettled reservations")
        if accounting["used"] > accounting["ceiling"]:
            raise ClosingError("cannot freeze request accounting above the official ceiling")
        self._set_meta(
            connection,
            "final_request_accounting_json",
            canonical_json_bytes(accounting).decode("utf-8"),
        )
        connection.commit()
        return accounting

    def _load_input_role(
        self,
        connection: sqlite3.Connection,
        role: str,
        path: Path,
        *,
        enumerated: bool,
    ) -> tuple[int, str]:
        digest = hashlib.sha256()
        count = 0
        for record in iter_record_input(path, self.root):
            _assert_body_free(record)
            try:
                url, locale = _record_key(record)
            except (AcquisitionError, ValueError) as exc:
                raise ClosingError(f"{role} contains an invalid route identity: {exc}") from exc
            record = dict(record)
            record["canonical_url"] = url
            encoded = canonical_json_bytes(record)
            record_sha = hashlib.sha256(encoded).hexdigest()
            fingerprint = enumerator_fingerprint(record) if enumerated else record_sha
            digest.update(encoded)
            existing = connection.execute(
                "SELECT record_sha256 FROM inputs WHERE role=? AND url=? AND locale=?",
                (role, url, locale),
            ).fetchone()
            if existing and existing[0] != record_sha:
                raise ClosingError(f"{role} has divergent duplicate {url} ({locale})")
            connection.execute(
                "INSERT OR IGNORE INTO inputs"
                "(role, url, locale, fingerprint, record_sha256, record_json) VALUES (?, ?, ?, ?, ?, ?)",
                (role, url, locale, fingerprint, record_sha, encoded.decode("utf-8")),
            )
            count += 1
            if count % 10_000 == 0:
                connection.commit()
        connection.commit()
        canonical_digest = digest.hexdigest()
        previous = self._meta(connection, f"{role}_canonical_sha256")
        if previous is not None and previous != canonical_digest:
            raise ClosingError(f"{role} changed after closing work began; use a new closing label")
        stored = int(
            connection.execute("SELECT COUNT(*) FROM inputs WHERE role=?", (role,)).fetchone()[0]
        )
        if stored != count:
            raise ClosingError(f"{role} input count is ambiguous: {stored} stored, {count} read")
        self._set_meta(connection, f"{role}_canonical_sha256", canonical_digest)
        self._set_meta(connection, f"{role}_records", str(count))
        self._set_meta(connection, f"{role}_artifact_sha256", _sha256_input_artifact(path))
        connection.commit()
        return count, canonical_digest

    def prepare(self, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        own_connection = connection is None
        connection = connection or self._connect()
        try:
            t0_reconciliation, t0_reconciliation_sha = _validate_reconciliation(
                self.t0_reconciliation_path, "T0 hydrated"
            )
            t1_reconciliation, t1_reconciliation_sha = _validate_reconciliation(
                self.t1_reconciliation_path, "T1 enumeration"
            )
            if t0_reconciliation.get("snapshot") != self.t0_label:
                raise ClosingError("T0 reconciliation snapshot does not match the requested T0 label")
            if t1_reconciliation.get("snapshot") != self.t1_label:
                raise ClosingError("T1 reconciliation snapshot does not match the requested T1 label")
            request_contract = canonical_json_bytes(
                {
                    "content_api_requests_per_second": self.content_rate,
                    "www_public_requests_per_second": self.www_rate,
                    "official_request_ceiling": self.official_request_ceiling,
                    "maximum_attempts_per_observation": REQUEST_ATTEMPTS,
                }
            ).decode("utf-8")
            current_programme_requests = self._global_request_count()
            if current_programme_requests > self.official_request_ceiling:
                raise ClosingError("shared official request ceiling was already exceeded")
            baseline_value = self._meta(connection, "programme_request_baseline")
            if baseline_value is None:
                self._set_meta(
                    connection,
                    "programme_request_baseline",
                    str(current_programme_requests),
                )
            previous_request_contract = self._meta(connection, "request_contract_json")
            request_rows = connection.execute(
                "SELECT COALESCE(SUM(used), 0), COALESCE(SUM(reserved), 0) FROM request_budget"
            ).fetchone()
            if (
                previous_request_contract is not None
                and previous_request_contract != request_contract
                and (int(request_rows[0]) or int(request_rows[1]))
            ):
                raise ClosingError("request rate or ceiling contract changed after acquisition began")
            self._set_meta(connection, "request_contract_json", request_contract)
            for key, digest in (
                ("t0_reconciliation_sha256", t0_reconciliation_sha),
                ("t1_reconciliation_sha256", t1_reconciliation_sha),
            ):
                previous = self._meta(connection, key)
                if previous is not None and previous != digest:
                    raise ClosingError(f"{key} changed after closing work began")
                self._set_meta(connection, key, digest)
            counts: dict[str, int] = {}
            digests: dict[str, str] = {}
            for role, path, enumerated in (
                ("t0_enumeration", self.t0_enumeration_path, True),
                ("t0_hydrated", self.t0_hydrated_path, False),
                ("t1_enumeration", self.t1_enumeration_path, True),
            ):
                counts[role], digests[role] = self._load_input_role(
                    connection, role, path, enumerated=enumerated
                )
            bindings = {
                "T0 enumeration": (
                    t0_reconciliation.get("inventory_canonical_sha256"),
                    digests["t0_enumeration"],
                ),
                "T0 hydrated records": (
                    t0_reconciliation.get("hydrated_records_canonical_sha256"),
                    digests["t0_hydrated"],
                ),
                "T1 enumeration": (
                    t1_reconciliation.get("inventory_canonical_sha256"),
                    digests["t1_enumeration"],
                ),
            }
            for label, (declared, observed) in bindings.items():
                if not isinstance(declared, str) or declared != observed:
                    raise ClosingError(f"{label} is not bound to its reconciliation digest")
            if int(t0_reconciliation.get("publication_records", -1)) != counts["t0_hydrated"]:
                raise ClosingError("T0 hydrated record count does not match its reconciliation")
            if int(t1_reconciliation.get("publication_records", -1)) != counts["t1_enumeration"]:
                raise ClosingError("T1 enumeration record count does not match its reconciliation")

            existing_work = int(connection.execute("SELECT COUNT(*) FROM work").fetchone()[0])
            if not existing_work:
                t1_cursor = connection.execute(
                    "SELECT url, locale, fingerprint, record_json FROM inputs "
                    "WHERE role='t1_enumeration' ORDER BY url, locale"
                )
                for url, locale, t1_fingerprint, record_json in t1_cursor:
                    record = json.loads(record_json)
                    t0 = connection.execute(
                        "SELECT fingerprint FROM inputs WHERE role='t0_enumeration' AND url=? AND locale=?",
                        (url, locale),
                    ).fetchone()
                    t0_hydrated = connection.execute(
                        "SELECT record_json FROM inputs WHERE role='t0_hydrated' AND url=? AND locale=?",
                        (url, locale),
                    ).fetchone()
                    reusable_t0 = bool(
                        t0_hydrated and _is_reusable_hydrated(json.loads(t0_hydrated[0]))
                    )
                    t0_fingerprint = str(t0[0]) if t0 else None
                    delta_class = (
                        "added"
                        if t0 is None
                        else "unchanged"
                        if t0_fingerprint == t1_fingerprint
                        else "changed"
                    )
                    if _is_linked_only(record):
                        work_kind = "linked_only"
                    elif delta_class == "unchanged" and reusable_t0:
                        work_kind = "reuse"
                    elif delta_class == "unchanged":
                        work_kind = (
                            "hydrate_unreusable_t0" if t0_hydrated else "hydrate_missing_t0"
                        )
                    else:
                        work_kind = "hydrate"
                    connection.execute(
                        "INSERT INTO work"
                        "(url, locale, delta_class, work_kind, t0_fingerprint, t1_fingerprint, input_json, state) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                        (
                            url,
                            locale,
                            delta_class,
                            work_kind,
                            t0_fingerprint,
                            t1_fingerprint,
                            record_json,
                        ),
                    )

                removed_cursor = connection.execute(
                    "SELECT DISTINCT url, locale FROM inputs WHERE role IN ('t0_enumeration', 't0_hydrated') "
                    "AND NOT EXISTS (SELECT 1 FROM inputs current "
                    "WHERE current.role='t1_enumeration' AND current.url=inputs.url AND current.locale=inputs.locale) "
                    "ORDER BY url, locale"
                )
                for url, locale in removed_cursor:
                    old = connection.execute(
                        "SELECT fingerprint, record_json FROM inputs "
                        "WHERE role='t0_hydrated' AND url=? AND locale=?",
                        (url, locale),
                    ).fetchone()
                    if old is None:
                        old = connection.execute(
                            "SELECT fingerprint, record_json FROM inputs "
                            "WHERE role='t0_enumeration' AND url=? AND locale=?",
                            (url, locale),
                        ).fetchone()
                    t0_enum = connection.execute(
                        "SELECT fingerprint FROM inputs "
                        "WHERE role='t0_enumeration' AND url=? AND locale=?",
                        (url, locale),
                    ).fetchone()
                    connection.execute(
                        "INSERT INTO work"
                        "(url, locale, delta_class, work_kind, t0_fingerprint, t1_fingerprint, input_json, state) "
                        "VALUES (?, ?, 'removed', 'closing_probe', ?, NULL, ?, 'pending')",
                        (url, locale, str(t0_enum[0]) if t0_enum else None, old[1]),
                    )
                self._set_meta(connection, "prepared", "true")
                connection.commit()

            work_counts = {
                str(delta): int(count)
                for delta, count in connection.execute(
                    "SELECT delta_class, COUNT(*) FROM work GROUP BY delta_class ORDER BY delta_class"
                )
            }
            return {
                "schema_version": 1,
                "fingerprint_schema": FINGERPRINT_SCHEMA,
                "t0_snapshot": self.t0_label,
                "t1_snapshot": self.t1_label,
                "closing_snapshot": self.label,
                "input_records": counts,
                "delta_counts": work_counts,
                "t0_reconciliation_snapshot": t0_reconciliation.get("snapshot"),
                "t1_reconciliation_snapshot": t1_reconciliation.get("snapshot"),
            }
        finally:
            if own_connection:
                connection.close()

    def _reuse(
        self, row: sqlite3.Row | tuple[Any, ...]
    ) -> tuple[dict[str, Any], str, list[dict[str, Any]], list[dict[str, Any]], bool]:
        url, locale, delta_class, work_kind, t0_fingerprint, t1_fingerprint, input_json = row
        if t0_fingerprint != t1_fingerprint:
            raise ClosingError(f"reuse fingerprint mismatch for {url} ({locale})")
        connection = self._checkpoint_connection()
        try:
            previous = connection.execute(
                "SELECT record_json, record_sha256 FROM inputs "
                "WHERE role='t0_hydrated' AND url=? AND locale=?",
                (url, locale),
            ).fetchone()
        finally:
            connection.close()
        if previous is None:
            raise ClosingError(f"reuse has no T0 hydrated record for {url} ({locale})")
        old = json.loads(previous[0])
        current = json.loads(input_json)
        _assert_body_free(old)
        _assert_body_free(current)
        result = merge_records(old, current)
        result["canonical_url"] = url
        result["locale"] = locale
        result["source_memberships"] = sorted(set(current.get("source_memberships", [])))
        result["snapshot_id"] = self.label
        outcome = _declared_outcome(current)
        result["closing_disposition"] = outcome
        result["coverage_disposition"] = OUTCOME_TO_COVERAGE[outcome]
        result["retrieved_at"] = current.get("retrieved_at")
        result["metadata_retrieved_at"] = old.get("retrieved_at")
        result["evidence_url"] = current.get("evidence_url")
        result["evidence_sha256"] = current.get("evidence_sha256")
        result["evidence_locator"] = current.get("evidence_locator") or "/"
        result["closing_reuse"] = {
            "method": "exact_non_observational_enumerator_fingerprint",
            "fingerprint_schema": FINGERPRINT_SCHEMA,
            "t0_snapshot": self.t0_label,
            "t1_snapshot": self.t1_label,
            "t0_fingerprint": t0_fingerprint,
            "t1_fingerprint": t1_fingerprint,
            "t0_hydrated_record_sha256": previous[1],
            "matched": True,
        }
        _assert_body_free(result)
        return result, outcome, [], [], False

    def _probe_or_hydrate(
        self, row: sqlite3.Row | tuple[Any, ...]
    ) -> tuple[dict[str, Any], str, list[dict[str, Any]], list[dict[str, Any]], bool]:
        url, locale, delta_class, work_kind, t0_fingerprint, t1_fingerprint, input_json = row
        record = json.loads(input_json)
        _assert_body_free(record)
        host = urlparse(url).netloc
        if host != "www.gov.uk":
            outcome = _declared_outcome(record)
            result = dict(record)
            result.update(
                {
                    "snapshot_id": self.label,
                    "closing_disposition": outcome,
                    "coverage_disposition": OUTCOME_TO_COVERAGE[outcome],
                    "closing_work_kind": work_kind,
                    "closing_delta_class": delta_class,
                    "metadata_only": True,
                }
            )
            return result, outcome, [], [], False

        path = urlparse(url).path or "/"
        endpoint = CONTENT_API_ROOT + (quote(path, safe="/%:@-._~") if path != "/" else "")
        body, content_evidence = self._observe(
            "content_api",
            endpoint,
            self.content_limiter,
            max_bytes=64 * 1024 * 1024,
        )
        evidence_rows = [_public_evidence(content_evidence)]
        hydrated: dict[str, Any] | None = None
        content_reason = ""
        if content_evidence.get("ok") and not content_evidence.get("partial"):
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError("Content API response is not an object")
                hydrated = sanitise_content_api(payload, content_evidence)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                content_reason = f"invalid Content API metadata response: {exc}"
        elif content_evidence.get("partial"):
            content_reason = "Content API response exceeded the bounded metadata envelope"
        else:
            content_reason = str(
                content_evidence.get("error") or f"HTTP {int(content_evidence.get('status') or 0)}"
            )

        linked: list[dict[str, Any]] = []
        constraints = list(record.get("constraints") or [])
        if hydrated is not None:
            result = merge_records(record, hydrated)
            outcome = (
                "redirect"
                if result.get("document_type") == "redirect" or result.get("redirects")
                else "represented"
            )
            linked = list(_linked_records(result))
        else:
            if self.robots_policy is None:
                raise ClosingError("robots policy was not prepared before a public closing probe")
            if not self.robots_policy.allows(url):
                status = 0
                final_url = url
                public_evidence = {
                    "requested_url": url,
                    "final_url": url,
                    "status": 0,
                    "ok": False,
                    "partial": False,
                    "error": "public closing probe prohibited by current robots policy",
                    "retrieved_at": self.robots_policy.retrieved_at,
                    "sha256": self.robots_policy.sha256,
                }
                evidence_rows.append(public_evidence)
                outcome = "exception"
            else:
                # A one-byte bounded GET is enough to observe final URL and
                # status. The transient byte and page body are never stored.
                _, public_evidence = self._observe(
                    "www_public", url, self.www_limiter, max_bytes=1
                )
                public_evidence = _public_evidence(public_evidence)
                evidence_rows.append(public_evidence)
                status = int(public_evidence.get("status") or 0)
                final_url = str(public_evidence.get("final_url") or url)
                try:
                    final_url = normalise_url(final_url)
                except Exception:
                    final_url = str(public_evidence.get("final_url") or url)
                if 200 <= status < 400 and final_url != url:
                    outcome = "redirect"
                elif 200 <= status < 400:
                    outcome = "represented"
                elif status in {404, 410}:
                    outcome = "tombstone"
                else:
                    outcome = "exception"
            result = dict(record)
            constraint = _closing_constraint(url, content_reason, content_evidence, self.label)
            constraints.append(constraint)
            if outcome == "redirect":
                result["closing_redirect"] = {"from": url, "to": final_url}
                result["redirects"] = [{"from": url, "to": final_url}]
            elif outcome == "tombstone":
                result["lifecycle_state"] = "tombstone"
                result["tombstone_status"] = status
            elif outcome == "exception":
                public_reason = str(public_evidence.get("error") or f"HTTP {status}")
                constraints.append(_closing_constraint(url, public_reason, public_evidence, self.label))

        result["canonical_url"] = url
        result["locale"] = locale
        result["snapshot_id"] = self.label
        result["closing_disposition"] = outcome
        result["coverage_disposition"] = OUTCOME_TO_COVERAGE[outcome]
        result["closing_work_kind"] = work_kind
        result["closing_delta_class"] = delta_class
        result["closing_evidence"] = evidence_rows
        result["constraints"] = constraints
        result["metadata_only"] = True
        _assert_body_free(result)
        return result, outcome, linked, evidence_rows, True

    def _admit_link(self, connection: sqlite3.Connection, record: dict[str, Any]) -> None:
        _assert_body_free(record)
        url, locale = _record_key(record)
        existing = connection.execute(
            "SELECT state, input_json, result_json, t1_fingerprint FROM work WHERE url=? AND locale=?",
            (url, locale),
        ).fetchone()
        if existing:
            state, input_json, result_json, t1_fingerprint = existing
            if state == "complete" and result_json:
                current = json.loads(result_json)
                closing_fields = {
                    key: current.get(key)
                    for key in (
                        "closing_delta_class",
                        "closing_disposition",
                        "closing_evidence",
                        "closing_reuse",
                        "closing_work_kind",
                        "coverage_disposition",
                        "metadata_only",
                        "snapshot_id",
                    )
                    if key in current
                }
                merged = merge_records(current, record)
                merged.update(closing_fields)
                _assert_body_free(merged)
                connection.execute(
                    "UPDATE work SET result_json=?, linked_discovered=1 WHERE url=? AND locale=?",
                    (canonical_json_bytes(merged).decode("utf-8"), url, locale),
                )
            else:
                current = json.loads(input_json)
                merged = merge_records(current, record)
                connection.execute(
                    "UPDATE work SET input_json=?, t1_fingerprint=?, linked_discovered=1 "
                    "WHERE url=? AND locale=?",
                    (
                        canonical_json_bytes(merged).decode("utf-8"),
                        enumerator_fingerprint(merged) if t1_fingerprint else None,
                        url,
                        locale,
                    ),
                )
            return
        t0 = connection.execute(
            "SELECT fingerprint FROM inputs WHERE role='t0_enumeration' AND url=? AND locale=?",
            (url, locale),
        ).fetchone()
        t0_any = t0 or connection.execute(
            "SELECT fingerprint FROM inputs WHERE role='t0_hydrated' AND url=? AND locale=?",
            (url, locale),
        ).fetchone()
        connection.execute(
            "INSERT INTO work"
            "(url, locale, delta_class, work_kind, t0_fingerprint, t1_fingerprint, "
            "input_json, state, linked_discovered) "
            "VALUES (?, ?, ?, 'linked_only', ?, ?, ?, 'pending', 1)",
            (
                url,
                locale,
                "changed" if t0_any else "added",
                str(t0[0]) if t0 else None,
                enumerator_fingerprint(record),
                canonical_json_bytes(record).decode("utf-8"),
            ),
        )

    def run(self, *, work_limit: int | None = None) -> dict[str, Any]:
        if work_limit is not None and work_limit < 1:
            raise ClosingError("work_limit must be positive when supplied")
        connection = self._connect()
        try:
            preparation = self.prepare(connection)
            pending_probes = int(
                connection.execute(
                    "SELECT COUNT(*) FROM work WHERE state='pending' AND work_kind!='reuse' "
                    "AND url LIKE 'https://www.gov.uk/%'"
                ).fetchone()[0]
            )
            if pending_probes:
                self._prepare_robots(connection)
            processed = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
                while work_limit is None or processed < work_limit:
                    remaining = self.batch_size
                    if work_limit is not None:
                        remaining = min(remaining, work_limit - processed)
                    rows = connection.execute(
                        "SELECT url, locale, delta_class, work_kind, t0_fingerprint, t1_fingerprint, input_json "
                        "FROM work WHERE state='pending' ORDER BY url, locale LIMIT ?",
                        (remaining,),
                    ).fetchall()
                    if not rows:
                        break
                    futures = {
                        pool.submit(self._reuse if row[3] == "reuse" else self._probe_or_hydrate, row): row
                        for row in rows
                    }
                    completed: list[
                        tuple[
                            tuple[Any, ...],
                            dict[str, Any],
                            str,
                            list[dict[str, Any]],
                            list[dict[str, Any]],
                            bool,
                        ]
                    ] = []
                    for future in concurrent.futures.as_completed(futures):
                        row = futures[future]
                        result, outcome, linked, evidence, probed = future.result()
                        completed.append((row, result, outcome, linked, evidence, probed))
                    connection.execute("BEGIN")
                    try:
                        for row, result, outcome, linked, evidence, probed in sorted(
                            completed, key=lambda item: (item[0][0], item[0][1])
                        ):
                            url, locale = row[0], row[1]
                            connection.execute(
                                "UPDATE work SET state='complete', attempts=attempts+1, "
                                "closing_disposition=?, coverage_disposition=?, result_json=?, evidence_json=?, "
                                "probe_performed=? WHERE url=? AND locale=?",
                                (
                                    outcome,
                                    OUTCOME_TO_COVERAGE[outcome],
                                    canonical_json_bytes(result).decode("utf-8"),
                                    canonical_json_bytes(evidence).decode("utf-8"),
                                    int(probed),
                                    url,
                                    locale,
                                ),
                            )
                            for linked_record in linked:
                                self._admit_link(connection, linked_record)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    processed += len(completed)

            pending = int(connection.execute("SELECT COUNT(*) FROM work WHERE state='pending'").fetchone()[0])
            complete = int(connection.execute("SELECT COUNT(*) FROM work WHERE state='complete'").fetchone()[0])
            outcomes = {
                str(outcome): int(count)
                for outcome, count in connection.execute(
                    "SELECT closing_disposition, COUNT(*) FROM work WHERE state='complete' "
                    "GROUP BY closing_disposition ORDER BY closing_disposition"
                )
            }
            work_kinds = {
                str(kind): int(count)
                for kind, count in connection.execute(
                    "SELECT work_kind, COUNT(*) FROM work GROUP BY work_kind ORDER BY work_kind"
                )
            }
            deltas = {
                str(delta): int(count)
                for delta, count in connection.execute(
                    "SELECT delta_class, COUNT(*) FROM work GROUP BY delta_class ORDER BY delta_class"
                )
            }
            request_accounting = (
                self._freeze_request_accounting(connection)
                if pending == 0
                else self._request_accounting(connection)
            )
            return {
                **preparation,
                "processed_this_run": processed,
                "work_records": complete + pending,
                "complete": complete,
                "pending": pending,
                "closed": pending == 0,
                "sampled": work_limit is not None,
                "closing_dispositions": outcomes,
                "work_kinds": work_kinds,
                "delta_counts": deltas,
                "request_accounting": request_accounting,
            }
        finally:
            connection.close()

    def _build_candidates(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM candidates")
        connection.commit()
        inserted = 0
        reader = self._checkpoint_connection()
        try:
            cursor = reader.execute(
                "SELECT url, locale, closing_disposition, result_json FROM work "
                "WHERE state='complete' ORDER BY url, locale"
            )
            for url, locale, outcome, result_json in cursor:
                record = json.loads(result_json)
                candidates = expand_candidate_records(record, self.label)
                source_class = str(record.get("entity_class") or "")
                supplemental_class = (
                    "organisation"
                    if record.get("document_type") == "organisation"
                    else source_class
                    if source_class in {"organisation", "resource"}
                    else ""
                )
                if supplemental_class and all(
                    candidate.get("entity_class") != supplemental_class
                    for candidate in candidates
                ):
                    template = dict(candidates[0])
                    native_id = str(record.get("content_id") or record.get("source_native_id") or url)
                    template.update(
                        {
                            "candidate_key": candidate_key(
                                url, locale, supplemental_class, native_id
                            ),
                            "entity_class": supplemental_class,
                            "source_native_id": native_id,
                            "route_or_resource_uri": url,
                        }
                    )
                    candidates.append(template)
                forced = OUTCOME_TO_COVERAGE[str(outcome)]
                for candidate in candidates:
                    candidate["coverage_disposition"] = forced
                    candidate["disposition_target"] = (
                        record.get("closing_redirect", {}).get("to") if outcome == "redirect" else None
                    )
                    candidate["snapshot_id"] = self.label
                    candidate["closing_route"] = url
                    candidate["closing_disposition"] = outcome
                    key = str(candidate["candidate_key"])
                    existing = connection.execute(
                        "SELECT candidate_json, coverage_disposition FROM candidates WHERE candidate_key=?",
                        (key,),
                    ).fetchone()
                    if existing:
                        current = json.loads(existing[0])
                        if existing[1] != candidate["coverage_disposition"]:
                            raise ClosingError(f"candidate {key} has conflicting closing dispositions")
                        current["source_memberships"] = sorted(
                            set(current.get("source_memberships", []))
                            | set(candidate.get("source_memberships", []))
                        )
                        current["evidence_ids"] = sorted(
                            set(current.get("evidence_ids", []))
                            | set(candidate.get("evidence_ids", []))
                        )
                        candidate = current
                    connection.execute(
                        "INSERT OR REPLACE INTO candidates"
                        "(candidate_key, entity_class, source_native_id, locale, "
                        "coverage_disposition, candidate_json) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            key,
                            str(candidate["entity_class"]),
                            str(candidate["source_native_id"]),
                            str(candidate.get("locale") or locale),
                            str(candidate["coverage_disposition"]),
                            canonical_json_bytes(candidate).decode("utf-8"),
                        ),
                    )
                    inserted += 1
                    if inserted % 10_000 == 0:
                        connection.commit()
        finally:
            reader.close()
        connection.commit()

        alias_updates = 0
        reader = self._checkpoint_connection()
        try:
            groups = reader.execute(
                "SELECT entity_class, source_native_id, locale FROM candidates "
                "WHERE entity_class IN ('content_identity', 'document', 'edition') "
                "AND coverage_disposition='represented' "
                "GROUP BY entity_class, source_native_id, locale HAVING COUNT(*) > 1"
            )
            for entity_class, source_native_id, locale in groups:
                keys = reader.execute(
                    "SELECT candidate_key, candidate_json FROM candidates WHERE entity_class=? "
                    "AND source_native_id=? AND locale=? AND coverage_disposition='represented' "
                    "ORDER BY candidate_key",
                    (entity_class, source_native_id, locale),
                )
                first = keys.fetchone()
                if first is None:
                    continue
                represented = str(first[0])
                for key, candidate_json in keys:
                    candidate = json.loads(candidate_json)
                    candidate["coverage_disposition"] = "alias_of_represented"
                    candidate["disposition_target"] = represented
                    connection.execute(
                        "UPDATE candidates SET coverage_disposition='alias_of_represented', candidate_json=? "
                        "WHERE candidate_key=?",
                        (canonical_json_bytes(candidate).decode("utf-8"), key),
                    )
                    alias_updates += 1
                    if alias_updates % 10_000 == 0:
                        connection.commit()
        finally:
            reader.close()
        connection.commit()

    def _write_delta_ledgers(self, connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        self.drift_root.mkdir(parents=True, exist_ok=True)
        summaries: dict[str, dict[str, Any]] = {}
        for delta in ("added", "changed", "unchanged", "removed"):
            def rows(delta_class: str = delta) -> Iterator[dict[str, Any]]:
                cursor = connection.execute(
                    "SELECT url, locale, work_kind, t0_fingerprint, t1_fingerprint, "
                    "closing_disposition, linked_discovered, probe_performed FROM work "
                    "WHERE delta_class=? ORDER BY url, locale",
                    (delta_class,),
                )
                for row in cursor:
                    yield {
                        "url": row[0],
                        "locale": row[1],
                        "delta_class": delta_class,
                        "work_kind": row[2],
                        "t0_fingerprint": row[3],
                        "t1_fingerprint": row[4],
                        "closing_disposition": row[5],
                        "linked_discovered": bool(row[6]),
                        "probe_performed": bool(row[7]),
                    }

            summaries[delta] = write_record_shards(
                self.root,
                self.drift_root / delta,
                rows(),
                schema="govuk-closing-set-difference.v1",
                snapshot=self.label,
            )
            identity_digest = hashlib.sha256()
            for url, locale in connection.execute(
                "SELECT url, locale FROM work WHERE delta_class=? ORDER BY url, locale",
                (delta,),
            ):
                identity_digest.update(f"{url}\0{locale}\n".encode("utf-8"))
            summaries[delta]["identity_sha256"] = identity_digest.hexdigest()
        return summaries

    def export(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            if self._meta(connection, "prepared") != "true":
                raise ClosingError("closing inputs have not been prepared")
            t1_proof, t1_proof_sha256 = _validate_reconciliation(
                self.t1_reconciliation_path, "T1 enumeration"
            )
            if t1_proof_sha256 != self._meta(connection, "t1_reconciliation_sha256"):
                raise ClosingError("T1 reconciliation changed after closing work began")
            pending = int(connection.execute("SELECT COUNT(*) FROM work WHERE state='pending'").fetchone()[0])
            invalid = int(
                connection.execute(
                    "SELECT COUNT(*) FROM work WHERE state!='complete' OR closing_disposition IS NULL "
                    "OR coverage_disposition IS NULL OR result_json IS NULL"
                ).fetchone()[0]
            )
            if pending or invalid:
                raise ClosingError(f"closing delta has {pending} pending and {invalid} unexplained work items")
            request_accounting = self._freeze_request_accounting(connection)
            if request_accounting["reserved"] != 0:
                raise ClosingError("official request ledger has unsettled reservations")
            if request_accounting["used"] > request_accounting["ceiling"]:
                raise ClosingError("official request ceiling was exceeded")
            removed_routes = int(
                connection.execute(
                    "SELECT COUNT(*) FROM work WHERE delta_class='removed' AND url LIKE 'https://www.gov.uk/%'"
                ).fetchone()[0]
            )
            probed_removed_routes = int(
                connection.execute(
                    "SELECT COUNT(*) FROM work WHERE delta_class='removed' "
                    "AND url LIKE 'https://www.gov.uk/%' AND probe_performed=1"
                ).fetchone()[0]
            )
            if removed_routes != probed_removed_routes:
                raise ClosingError(
                    f"{removed_routes - probed_removed_routes} T0 routes absent from T1 were not closing-probed"
                )

            self.records_root.mkdir(parents=True, exist_ok=True)
            self.inventory_root.mkdir(parents=True, exist_ok=True)
            self.reconciliation_root.mkdir(parents=True, exist_ok=True)
            closing_watermark = ""
            closing_watermark_key = _timestamp_key("")

            def records() -> Iterator[dict[str, Any]]:
                nonlocal closing_watermark, closing_watermark_key
                for (record_json,) in connection.execute(
                    "SELECT result_json FROM work ORDER BY url, locale"
                ):
                    record = json.loads(record_json)
                    _assert_body_free(record)
                    candidates = [str(record.get("retrieved_at") or "")]
                    for evidence in record.get("closing_evidence", []):
                        if isinstance(evidence, dict):
                            candidates.append(str(evidence.get("retrieved_at") or ""))
                    for candidate in candidates:
                        key = _timestamp_key(candidate)
                        if key > closing_watermark_key:
                            closing_watermark = candidate
                            closing_watermark_key = key
                    yield record

            record_manifest = write_record_shards(
                self.root,
                self.records_root / "source-records",
                records(),
                schema="govuk-closing-source-records.v1",
                snapshot=self.label,
            )
            record_count = int(record_manifest["count"])
            record_digest = str(record_manifest["canonical_sha256"])

            self._build_candidates(connection)

            def candidates() -> Iterator[dict[str, Any]]:
                for (candidate_json,) in connection.execute(
                    "SELECT candidate_json FROM candidates ORDER BY candidate_key"
                ):
                    yield json.loads(candidate_json)

            candidate_manifest = write_record_shards(
                self.root,
                self.inventory_root / self.label / "candidates",
                candidates(),
                schema="govuk-closing-candidates.v1",
                snapshot=self.label,
            )
            candidate_count = int(candidate_manifest["count"])
            candidate_digest = str(candidate_manifest["canonical_sha256"])
            delta_summaries = self._write_delta_ledgers(connection)

            disposition_counts = {
                str(disposition): int(count)
                for disposition, count in connection.execute(
                    "SELECT coverage_disposition, COUNT(*) FROM candidates "
                    "GROUP BY coverage_disposition ORDER BY coverage_disposition"
                )
            }
            entity_accounting: dict[str, dict[str, Any]] = {}
            for entity_class, disposition, count in connection.execute(
                "SELECT entity_class, coverage_disposition, COUNT(*) FROM candidates "
                "GROUP BY entity_class, coverage_disposition ORDER BY entity_class, coverage_disposition"
            ):
                row = entity_accounting.setdefault(
                    str(entity_class),
                    {
                        "expected_candidate_keys": 0,
                        "represented": 0,
                        "alias_of_represented": 0,
                        "redirect_only": 0,
                        "tombstone_only": 0,
                        "exceptioned": 0,
                        "unexplained_omissions": 0,
                    },
                )
                row[str(disposition)] = int(count)
                row["expected_candidate_keys"] += int(count)
            for row in entity_accounting.values():
                accounted = sum(int(row[status]) for status in VALID_COVERAGE)
                row["unexplained_omissions"] = int(row["expected_candidate_keys"]) - accounted
                row["accounting_closed"] = row["unexplained_omissions"] == 0
            entity_class_counts = {
                entity_class: int(row["expected_candidate_keys"])
                for entity_class, row in sorted(entity_accounting.items())
            }

            accounted = sum(disposition_counts.get(status, 0) for status in VALID_COVERAGE)
            unexplained = candidate_count - accounted
            if unexplained or any(not row["accounting_closed"] for row in entity_accounting.values()):
                raise ClosingError("closing candidate accounting did not close by entity class")

            closing_outcomes = {
                str(outcome): int(count)
                for outcome, count in connection.execute(
                    "SELECT closing_disposition, COUNT(*) FROM work "
                    "GROUP BY closing_disposition ORDER BY closing_disposition"
                )
            }
            work_kinds = {
                str(kind): int(count)
                for kind, count in connection.execute(
                    "SELECT work_kind, COUNT(*) FROM work GROUP BY work_kind ORDER BY work_kind"
                )
            }
            input_proofs = {
                role: {
                    "records": int(self._meta(connection, f"{role}_records") or 0),
                    "canonical_sha256": self._meta(connection, f"{role}_canonical_sha256"),
                    "artifact_sha256": self._meta(connection, f"{role}_artifact_sha256"),
                }
                for role in ("t0_enumeration", "t0_hydrated", "t1_enumeration")
            }
            robots_evidence = json.loads(self._meta(connection, "robots_evidence_json") or "{}")
            reconciliation = {
                "schema_version": 1,
                "snapshot": self.label,
                "t0_snapshot": self.t0_label,
                "t1_snapshot": self.t1_label,
                "t1_reenumeration_authoritative": True,
                "sampled": False,
                "hydrated": True,
                "closing_watermark": closing_watermark,
                "metadata_only": True,
                "complete_page_bodies_retained": False,
                "fingerprint_proof": {
                    "schema": FINGERPRINT_SCHEMA,
                    "algorithm": (
                        "sha256(canonical-json(all top-level enumerator fields except declared "
                        "observational fields))"
                    ),
                    "omitted_top_level_fields": sorted(FINGERPRINT_OMITTED_TOP_LEVEL_FIELDS),
                    "reuse_requires_exact_match": True,
                    "reused_records": work_kinds.get("reuse", 0),
                },
                "source_inputs": input_proofs,
                "request_accounting": request_accounting,
                "source_reconciliations": {
                    "t0_hydrated_sha256": self._meta(connection, "t0_reconciliation_sha256"),
                    "t1_enumeration_sha256": self._meta(connection, "t1_reconciliation_sha256"),
                },
                "source_counts": dict(t1_proof.get("source_counts") or {}),
                "search_partition_proofs": list(t1_proof.get("search_partition_proofs") or []),
                "search_partitions_closed": t1_proof.get("search_partitions_closed") is True,
                "sitemap_byte_stable": t1_proof.get("sitemap_byte_stable") is True,
                "sitemap_proof": dict(t1_proof.get("sitemap_proof") or {}),
                "organisations_proof": dict(t1_proof.get("organisations_proof") or {}),
                "navigation_proof": dict(t1_proof.get("navigation_proof") or {}),
                "set_differences": delta_summaries,
                "closing_probe_proof": {
                    "t0_routes_absent_from_t1": removed_routes,
                    "actively_probed": probed_removed_routes,
                    "closed": removed_routes == probed_removed_routes,
                    "robots": robots_evidence,
                },
                "work_kinds": work_kinds,
                "closing_dispositions": closing_outcomes,
                "expected_candidate_keys": candidate_count,
                "represented": disposition_counts.get("represented", 0),
                "alias_of_represented": disposition_counts.get("alias_of_represented", 0),
                "redirect_only": disposition_counts.get("redirect_only", 0),
                "tombstone_only": disposition_counts.get("tombstone_only", 0),
                "exceptioned": disposition_counts.get("exceptioned", 0),
                "unexplained_omissions": unexplained,
                "pending": 0,
                "hydration_proof": {
                    "queue_records": record_count,
                    "pending": 0,
                    "closed": True,
                    "status_counts": closing_outcomes,
                },
                "entity_class_counts": entity_class_counts,
                "entity_class_accounting": entity_accounting,
                "publication_records": record_count,
                "hydrated_records_path": record_manifest["manifest_path"],
                "hydrated_records_manifest": record_manifest["manifest_path"],
                "hydrated_records_manifest_file_sha256": record_manifest[
                    "manifest_file_sha256"
                ],
                "hydrated_records_canonical_sha256": record_digest,
                "hydrated_record_shards": record_manifest["shards"],
                "candidate_ledger_path": candidate_manifest["manifest_path"],
                "candidate_ledger_manifest": candidate_manifest["manifest_path"],
                "candidate_ledger_manifest_file_sha256": candidate_manifest[
                    "manifest_file_sha256"
                ],
                "candidate_ledger_canonical_sha256": candidate_digest,
                "candidate_ledger_shards": candidate_manifest["shards"],
            }
            if isinstance(t1_proof.get("rendered_gap_proof"), dict):
                reconciliation["rendered_gap_proof"] = dict(t1_proof["rendered_gap_proof"])
            target = self.reconciliation_root / f"{self.label}.json"
            write_text_atomic(target, pretty_json(reconciliation))
            manifest = {
                "schema_version": 1,
                "snapshot": self.label,
                "t0_snapshot": self.t0_label,
                "t1_snapshot": self.t1_label,
                "closing_watermark": closing_watermark,
                "metadata_only": True,
                "complete_page_bodies_retained": False,
                "source_records": record_count,
                "source_records_sha256": record_digest,
                "source_records_manifest": record_manifest["manifest_path"],
                "source_records_manifest_file_sha256": record_manifest[
                    "manifest_file_sha256"
                ],
                "source_record_shards": record_manifest["shards"],
                "candidate_records": candidate_count,
                "candidate_records_sha256": candidate_digest,
                "candidate_records_manifest": candidate_manifest["manifest_path"],
                "candidate_records_manifest_file_sha256": candidate_manifest[
                    "manifest_file_sha256"
                ],
                "candidate_record_shards": candidate_manifest["shards"],
                "request_accounting": request_accounting,
                "reconciliation": target.relative_to(self.root).as_posix(),
                "reconciliation_sha256": hashlib.sha256(
                    canonical_json_bytes(reconciliation)
                ).hexdigest(),
                "reconciliation_file_sha256": _sha256_file(target),
            }
            write_text_atomic(self.records_root / "manifest.json", pretty_json(manifest))
            return reconciliation
        finally:
            connection.close()
