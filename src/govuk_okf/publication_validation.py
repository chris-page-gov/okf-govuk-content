"""Bounded-memory validation for a compiled GOV.UK OKF publication."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .publication import (
    DATA_PLANE_BUDGETS,
    DATA_PLANE_SCHEMA_VERSION,
    DOC_MAP_CHUNK_SIZE,
    DOC_MAP_PARTITION_INDEX_WIDTH,
    DOC_MAP_PARTITIONING_CONTRACT,
    MAX_DATA_PLANE_SHARD_COMPRESSED_BYTES,
    MAX_DATA_PLANE_SHARD_UNCOMPRESSED_BYTES,
    POSTINGS_PARTITIONING_CONTRACT,
    PublicationError,
    assertion_id,
    data_plane_manifest_root,
    matches_exact_json_contract,
    postings_entry_serialized_size,
    postings_partition_serialized_size,
    postings_partition_relative_path,
    search_shard,
    semantic_descriptor,
    semantic_route_iri,
    shard_manifest_sha256,
    tokenise,
)
from .util import (
    adjacency_bucket,
    canonical_json_bytes,
    pretty_json,
    read_gzip_json,
    yaml_dump,
    yaml_load_subset,
)

MAX_BOOTSTRAP = 2 * 1024 * 1024
MAX_SHARD = MAX_DATA_PLANE_SHARD_COMPRESSED_BYTES
MAX_UNCOMPRESSED_SHARD = MAX_DATA_PLANE_SHARD_UNCOMPRESSED_BYTES
MAX_ERRORS_RETAINED = 100
SQLITE_CACHE_KIB = 16 * 1024
PROVENANCE_FIELDS = {
    "assertion_id",
    "source",
    "target",
    "kind",
    "source_native_predicate",
    "evidence_type",
    "evidence_url",
    "evidence_sha256",
    "evidence_locator",
    "observed_at",
    "derivation_method",
    "software_version",
    "snapshot_id",
    "assertion_status",
    "confidence",
}
RECORD_PROVENANCE_FIELDS = {
    "evidence_url",
    "evidence_sha256",
    "evidence_locator",
    "retrieved_at",
}
SEMANTIC_COMMON_FIELDS = {
    "@id",
    "@type",
    "title",
    "sourceNativeId",
    "sourceSystem",
    "snapshotId",
    "authority",
    "evidence",
    "retrievedAt",
}
SEMANTIC_REQUIRED: dict[str, set[str]] = {
    "govuk:ContentItem": {"contentId", "basePath", "canonicalUrl", "contentType", "schemaFamily"},
    "govuk:Document": {"contentItem", "locale"},
    "govuk:Route": {"path", "canonicalUrl", "routeKind"},
    "govuk:ContentType": {"sourceName"},
    "govuk:SchemaFamily": {"schemaName", "schemaUri", "sourceCommit"},
    "govuk:Organisation": {"slug", "canonicalUrl", "organisationStatus"},
    "govuk:Taxon": {"basePath", "canonicalUrl", "taxonomyKind"},
    "govuk:WorldTaxon": {"basePath", "canonicalUrl", "locale"},
    "govuk:MainstreamBrowsePage": {"basePath", "canonicalUrl"},
    "govuk:Collection": {"canonicalUrl", "collectionKind"},
    "govuk:Attachment": {"contentItem", "canonicalUrl", "mimeType", "filename", "rightsStatus"},
    "govuk:InferenceActivity": {"startedAt", "endedAt", "derivationMethod"},
}
SEMANTIC_REFERENCE_FIELDS = {
    "contentItem",
    "route",
    "contentType",
    "schemaFamily",
    "subject",
    "object",
    "generatedBy",
}
DATA_PLANE_SHARD_FIELDS = {
    "path",
    "schema",
    "schema_version",
    "snapshot",
    "count",
    "first_key",
    "last_key",
    "compression",
    "compressed_bytes",
    "uncompressed_bytes",
    "sha256",
}
RECORD_SHARD_SCHEMAS = {
    "datasets": "okf-record-shard.v1",
    "publishers": "okf-publisher-shard.v1",
    "resources": "okf-resource-shard.v1",
    "relationships": "okf-relationship-shard.v1",
}
SEARCH_SHARD_SCHEMAS = {
    "result_docs": "okf-search-result-shard.v1",
    "lexicon": "okf-search-lexicon-shard.v1",
    "postings": "okf-search-postings-shard.v1",
    "prefixes": "okf-search-prefix-shard.v1",
    "doc_map": "okf-search-doc-map-shard.v1",
}


def _canonical_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_iri(value: object) -> bool:
    return isinstance(value, str) and value.startswith(("https://", "http://", "urn:"))


def _is_datetime(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return "T" in value


class ErrorLedger:
    def __init__(self) -> None:
        self.total = 0
        self.messages: list[str] = []

    def add(self, message: str) -> None:
        self.total += 1
        if len(self.messages) < MAX_ERRORS_RETAINED:
            self.messages.append(message)


@dataclass(frozen=True)
class ValidationResult:
    error_count: int
    errors: tuple[str, ...]
    datasets: int
    resources: int
    publishers: int
    relationships: int
    semantic_nodes: int

    @property
    def passed(self) -> bool:
        return self.error_count == 0


class PublicationValidator:
    def __init__(self, bundle: Path, database: Path) -> None:
        self.bundle = bundle.resolve()
        self.errors = ErrorLedger()
        self.connection = sqlite3.connect(database)
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute("PRAGMA temp_store=FILE")
        self.connection.execute(f"PRAGMA cache_size=-{SQLITE_CACHE_KIB}")
        self.connection.execute("PRAGMA mmap_size=0")
        self.connection.executescript(
            """
            CREATE TABLE records (
              kind TEXT NOT NULL,
              ordinal INTEGER NOT NULL,
              open TEXT NOT NULL,
              PRIMARY KEY (kind, ordinal)
            ) WITHOUT ROWID;
            CREATE TABLE routes (
              open TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              ordinal INTEGER NOT NULL
            );
            CREATE TABLE expected_identifiers (
              identifier TEXT NOT NULL,
              kind TEXT NOT NULL,
              ordinal INTEGER NOT NULL,
              open TEXT NOT NULL,
              PRIMARY KEY (identifier, kind, ordinal, open)
            ) WITHOUT ROWID;
            CREATE TABLE actual_identifiers (
              identifier TEXT NOT NULL,
              kind TEXT NOT NULL,
              ordinal INTEGER NOT NULL,
              open TEXT NOT NULL,
              PRIMARY KEY (identifier, kind, ordinal, open)
            ) WITHOUT ROWID;
            CREATE TABLE relationships (
              assertion_id TEXT PRIMARY KEY,
              source TEXT NOT NULL,
              target TEXT NOT NULL,
              kind TEXT NOT NULL,
              row_sha256 TEXT NOT NULL
            );
            CREATE TABLE topology_records (
              ordinal INTEGER PRIMARY KEY,
              open TEXT NOT NULL,
              url TEXT NOT NULL,
              hostname TEXT NOT NULL,
              scheme TEXT NOT NULL,
              routing_kind TEXT NOT NULL,
              coverage_disposition TEXT NOT NULL,
              stable_content_identifier INTEGER NOT NULL
            );
            CREATE TABLE topology_redirects (
              dataset_ordinal INTEGER NOT NULL,
              redirect_ordinal INTEGER NOT NULL,
              source_route TEXT NOT NULL,
              source_url TEXT NOT NULL,
              source_host TEXT NOT NULL,
              path TEXT NOT NULL,
              destination TEXT NOT NULL,
              destination_url TEXT NOT NULL,
              destination_host TEXT NOT NULL,
              type TEXT NOT NULL,
              segments_mode TEXT NOT NULL,
              evidence_url TEXT NOT NULL,
              evidence_locator TEXT NOT NULL,
              retrieved_at TEXT NOT NULL,
              PRIMARY KEY (dataset_ordinal, redirect_ordinal)
            ) WITHOUT ROWID;
            CREATE TABLE search_tokens (
              token TEXT PRIMARY KEY,
              shard TEXT NOT NULL,
              df INTEGER NOT NULL
            );
            CREATE TABLE adjacency_counts (
              assertion_id TEXT PRIMARY KEY,
              occurrences INTEGER NOT NULL
            );
            CREATE TABLE semantic_nodes (
              identifier TEXT NOT NULL,
              entity_type TEXT NOT NULL,
              PRIMARY KEY (identifier, entity_type)
            ) WITHOUT ROWID;
            CREATE TABLE semantic_references (
              source TEXT NOT NULL,
              target TEXT NOT NULL,
              field TEXT NOT NULL,
              PRIMARY KEY (source, target, field)
            ) WITHOUT ROWID;
            """
        )
        self.counts = {"datasets": 0, "resources": 0, "publishers": 0, "relationships": 0}
        self.semantic_node_occurrences = 0
        self.data_plane_shards: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        self.connection.close()

    def path(self, relative: object) -> Path:
        value = relative.get("path") if isinstance(relative, dict) else relative
        candidate = Path(str(value or ""))
        if not str(value or ""):
            raise ValueError(f"publication reference has no path: {relative}")
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"unsafe publication path: {relative}")
        resolved = (self.bundle / candidate).resolve()
        if not resolved.is_relative_to(self.bundle):
            raise ValueError(f"publication path escapes bundle: {relative}")
        return resolved

    def verify_reference_hash(self, reference: object, path: Path) -> None:
        if not isinstance(reference, dict):
            return
        expected = str(reference.get("sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError(f"publication reference has no valid SHA-256: {reference}")
        if _file_sha256(path) != expected:
            raise ValueError(f"publication reference SHA-256 differs: {reference}")

    def load_json(
        self,
        relative: object,
        *,
        label: str,
        max_bytes: int | None = None,
        default: object,
    ) -> Any:
        try:
            path = self.path(relative)
            if not path.is_file():
                raise ValueError(f"missing file: {relative}")
            if max_bytes is not None and path.stat().st_size > max_bytes:
                raise ValueError(f"file exceeds {max_bytes} bytes: {relative}")
            self.verify_reference_hash(relative, path)
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.errors.add(f"cannot read {label}: {exc}")
            return default

    def load_gzip(self, relative: object, *, label: str, default: object) -> Any:
        try:
            path = self.path(relative)
            if not path.is_file():
                raise ValueError(f"missing file: {relative}")
            if path.stat().st_size > MAX_SHARD:
                raise ValueError(f"compressed shard exceeds {MAX_SHARD} bytes: {relative}")
            return read_gzip_json(
                path,
                max_compressed_bytes=MAX_SHARD,
                max_uncompressed_bytes=MAX_UNCOMPRESSED_SHARD,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.errors.add(f"cannot read {label}: {exc}")
            return default

    def check_file(self, relative: object, *, label: str, max_bytes: int | None = None) -> bool:
        try:
            path = self.path(relative)
            if not path.is_file():
                raise ValueError(f"missing file: {relative}")
            if max_bytes is not None and path.stat().st_size > max_bytes:
                raise ValueError(f"file exceeds {max_bytes} bytes: {relative}")
            self.verify_reference_hash(relative, path)
        except (OSError, ValueError) as exc:
            self.errors.add(f"invalid {label}: {exc}")
            return False
        return True

    def _uncompressed_size(self, path: Path, compression: object) -> int:
        if compression == "identity":
            return path.stat().st_size
        if compression != "gzip":
            raise ValueError(f"unsupported compression: {compression}")
        size = 0
        with gzip.open(path, "rb") as stream:
            while block := stream.read(1024 * 1024):
                size += len(block)
                if size > MAX_UNCOMPRESSED_SHARD:
                    raise ValueError(
                        f"uncompressed shard exceeds {MAX_UNCOMPRESSED_SHARD} bytes"
                    )
        return size

    def validate_budget_contract(self, manifest: dict[str, Any], label: str) -> None:
        if manifest.get("budgets") != DATA_PLANE_BUDGETS:
            self.errors.add(f"{label} does not use the frozen data-plane budgets")

    def register_shard_metadata(
        self,
        *,
        label: str,
        paths: list[object],
        rows: object,
        expected_schema: str,
        snapshot: object,
    ) -> dict[str, dict[str, Any]]:
        """Fail closed on one complete path-array/metadata pairing."""
        if not isinstance(rows, list):
            self.errors.add(f"{label} shard metadata must be a list")
            rows = []
        metadata: dict[str, dict[str, Any]] = {}
        metadata_paths: list[str] = []
        for value in rows:
            if not isinstance(value, dict):
                self.errors.add(f"{label} shard metadata contains a non-object")
                continue
            path_value = value.get("path")
            if not isinstance(path_value, str) or not path_value:
                self.errors.add(f"{label} shard metadata has no path")
                continue
            metadata_paths.append(path_value)
            if path_value in metadata:
                self.errors.add(f"duplicate {label} shard metadata path: {path_value}")
                continue
            metadata[path_value] = value
            if not DATA_PLANE_SHARD_FIELDS <= set(value):
                self.errors.add(f"{label} shard metadata is incomplete: {path_value}")
            if value.get("schema") != expected_schema:
                self.errors.add(f"{label} shard schema is invalid: {path_value}")
            if value.get("schema_version") != DATA_PLANE_SCHEMA_VERSION:
                self.errors.add(
                    f"{label} shard schema version is invalid: {path_value}"
                )
            if value.get("snapshot") != snapshot:
                self.errors.add(f"{label} shard snapshot is invalid: {path_value}")
            count = value.get("count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                self.errors.add(f"{label} shard count is invalid: {path_value}")
            elif count == 0 and (
                value.get("first_key") is not None
                or value.get("last_key") is not None
            ):
                self.errors.add(f"empty {label} shard has key bounds: {path_value}")
            elif count > 0 and (
                not isinstance(value.get("first_key"), str)
                or not isinstance(value.get("last_key"), str)
            ):
                self.errors.add(f"non-empty {label} shard lacks key bounds: {path_value}")
            if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256", ""))):
                self.errors.add(f"{label} shard hash is invalid: {path_value}")
            try:
                path = self.path(path_value)
                if not path.is_file():
                    raise ValueError("file is missing")
                compressed = path.stat().st_size
                if compressed > MAX_SHARD:
                    raise ValueError(
                        f"compressed shard exceeds {MAX_SHARD} bytes"
                    )
                if compressed != value.get("compressed_bytes"):
                    self.errors.add(
                        f"{label} shard compressed size differs: {path_value}"
                    )
                uncompressed = self._uncompressed_size(
                    path, value.get("compression")
                )
                if uncompressed > MAX_UNCOMPRESSED_SHARD:
                    raise ValueError(
                        f"uncompressed shard exceeds {MAX_UNCOMPRESSED_SHARD} bytes"
                    )
                if uncompressed != value.get("uncompressed_bytes"):
                    self.errors.add(
                        f"{label} shard uncompressed size differs: {path_value}"
                    )
                if _file_sha256(path) != value.get("sha256"):
                    self.errors.add(f"{label} shard SHA-256 differs: {path_value}")
            except (OSError, EOFError, ValueError, gzip.BadGzipFile) as exc:
                self.errors.add(f"cannot verify {label} shard {path_value}: {exc}")
            if path_value in self.data_plane_shards:
                self.errors.add(f"data-plane shard path is reused: {path_value}")
            else:
                self.data_plane_shards[path_value] = value
        expected_paths = [str(path) for path in paths]
        if metadata_paths != expected_paths:
            self.errors.add(f"{label} path array differs from shard metadata")
        return metadata

    def check_shard_observation(
        self,
        metadata: dict[str, Any] | None,
        *,
        label: str,
        count: int,
        first_key: str | None,
        last_key: str | None,
    ) -> None:
        if metadata is None:
            return
        if metadata.get("count") != count:
            self.errors.add(f"{label} count differs from shard metadata")
        if metadata.get("first_key") != first_key:
            self.errors.add(f"{label} first key differs from shard metadata")
        if metadata.get("last_key") != last_key:
            self.errors.add(f"{label} last key differs from shard metadata")

    def validate_root(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        if not self.bundle.is_dir():
            self.errors.add("bundle directory is missing")
            return {}, {}, {}
        if any(path.name == ".DS_Store" for path in self.bundle.rglob("*")):
            self.errors.add("Finder artefact present in bundle")
        descriptor = self.load_json(
            "okf-explorer.json", label="Explorer descriptor", default={}
        )
        semantic = self.load_json(
            "okf-bundle.jsonld", label="semantic JSON-LD descriptor", default={}
        )
        if not isinstance(descriptor, dict) or not isinstance(semantic, dict):
            self.errors.add("root descriptors must be JSON objects")
            return {}, {}, {}
        try:
            yaml_semantic = yaml_load_subset(
                (self.bundle / "okf-bundle.yamlld").read_text(encoding="utf-8")
            )
            if yaml_semantic != semantic:
                self.errors.add(
                    "YAML-LD and JSON-LD projections are not structurally equivalent"
                )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            self.errors.add(f"cannot validate YAML-LD descriptor: {exc}")
        semantic_digest = hashlib.sha256(canonical_json_bytes(semantic)).hexdigest()
        if descriptor.get("semantic_projection_sha256") != semantic_digest:
            self.errors.add("Explorer descriptor semantic projection hash is incorrect")
        if descriptor.get("schema") != "okf-explorer-large-corpus.v1" or descriptor.get(
            "kind"
        ) != "okf-large-corpus":
            self.errors.add("Explorer descriptor does not use the large-corpus contract")
        if isinstance(descriptor.get("counts"), dict) and semantic.get("snapshot"):
            expected_yaml = yaml_dump(
                semantic_descriptor(
                    descriptor["counts"],
                    str(descriptor.get("generated_at")),
                    str(semantic["snapshot"]),
                )
            ) + "\n"
            try:
                if (
                    (self.bundle / "okf-bundle.yamlld").read_text(encoding="utf-8")
                    != expected_yaml
                ):
                    self.errors.add(
                        "YAML-LD is not the canonical projection paired with JSON-LD"
                    )
            except OSError as exc:
                self.errors.add(f"cannot read YAML-LD descriptor: {exc}")
        required_entrypoints = {
            "data_manifest",
            "overview_index",
            "analysis_overview",
            "site_topology",
            "search_manifest",
            "relationship_adjacency",
            "route_index",
            "semantic_projection",
        }
        entrypoints = descriptor.get("entrypoints", {})
        if not isinstance(entrypoints, dict) or not required_entrypoints <= set(entrypoints):
            self.errors.add("Explorer descriptor is missing required entrypoints")
            entrypoints = entrypoints if isinstance(entrypoints, dict) else {}
        for name, relative in entrypoints.items():
            if name != "viewer":
                self.check_file(relative, label=f"descriptor entrypoint {name}")
        entrypoint_integrity = descriptor.get("entrypoint_integrity", {})
        if not isinstance(entrypoint_integrity, dict) or not required_entrypoints <= set(entrypoint_integrity):
            self.errors.add("Explorer descriptor is missing required entrypoint integrity metadata")
            entrypoint_integrity = entrypoint_integrity if isinstance(entrypoint_integrity, dict) else {}
        for name in required_entrypoints:
            reference = entrypoint_integrity.get(name)
            if not isinstance(reference, dict) or reference.get("path") != entrypoints.get(name):
                self.errors.add(f"descriptor entrypoint and integrity path differ: {name}")
                continue
            self.check_file(reference, label=f"descriptor entrypoint integrity {name}")
        manifest = self.load_json(
            entrypoint_integrity.get("data_manifest", entrypoints.get("data_manifest", "data/manifest.json")),
            label="data manifest",
            default={},
        )
        if not isinstance(manifest, dict):
            self.errors.add("data manifest must be a JSON object")
            manifest = {}
        if (
            manifest.get("schema") != "okf-data-manifest.v1"
            or manifest.get("schema_version") != DATA_PLANE_SCHEMA_VERSION
        ):
            self.errors.add("data manifest schema/version is invalid")
        self.validate_budget_contract(manifest, "data manifest")
        integrity = manifest.get("integrity", {})
        if not isinstance(integrity, dict) or (
            integrity.get("schema") != "okf-data-plane-integrity.v1"
            or integrity.get("schema_version") != DATA_PLANE_SCHEMA_VERSION
            or integrity.get("algorithm")
            != "sha256-canonical-shard-leaves-v1"
        ):
            self.errors.add("data-plane integrity root contract is invalid")
            integrity = integrity if isinstance(integrity, dict) else {}
        root_digest = integrity.get("manifest_root_sha256")
        if not re.fullmatch(r"[0-9a-f]{64}", str(root_digest or "")):
            self.errors.add("data-plane manifest root is invalid")
        if descriptor.get("data_plane_manifest_root_sha256") != root_digest:
            self.errors.add("Explorer descriptor data-plane root differs")
        if manifest.get("counts") != descriptor.get("counts"):
            self.errors.add("descriptor and data-manifest counts differ")
        if semantic.get("snapshot") != manifest.get("snapshot"):
            self.errors.add("semantic descriptor and data manifest snapshots differ")
        if (
            semantic.get("generatedAt") != manifest.get("generated_at")
            or descriptor.get("generated_at") != manifest.get("generated_at")
        ):
            self.errors.add(
                "semantic, Explorer and data-manifest generation times differ"
            )
        if manifest.get("performance", {}).get("startup_mode") != "overview-first":
            self.errors.add("manifest does not declare overview-first startup")
        return descriptor, semantic, manifest

    def register_record(self, kind: str, ordinal: int, row: dict[str, Any]) -> None:
        route = str(row.get("open") or "")
        if not route:
            self.errors.add(f"{kind} record at ordinal {ordinal} has no runtime route")
            route = f"__missing__/{kind}/{ordinal}"
        self.connection.execute(
            "INSERT INTO records(kind, ordinal, open) VALUES (?, ?, ?)",
            (kind, ordinal, route),
        )
        try:
            self.connection.execute(
                "INSERT INTO routes(open, kind, ordinal) VALUES (?, ?, ?)",
                (route, kind, ordinal),
            )
        except sqlite3.IntegrityError:
            self.errors.add(f"runtime route collides: {route}")
        for identifier in (
            row.get("open"),
            row.get("url"),
            row.get("@id"),
            row.get("canonical_content_id"),
            row.get("content_id"),
            row.get("attachment_id"),
            row.get("id"),
            row.get("name"),
        ):
            if identifier is not None and str(identifier):
                self.connection.execute(
                    "INSERT OR IGNORE INTO expected_identifiers"
                    "(identifier, kind, ordinal, open) VALUES (?, ?, ?, ?)",
                    (str(identifier), kind, ordinal, route),
                )
        if not RECORD_PROVENANCE_FIELDS <= set(row):
            self.errors.add(f"record provenance incomplete: {route}")
        if not str(row.get("evidence_url", "")).startswith(("https://", "http://")):
            self.errors.add(f"record evidence URL is unsafe: {route}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(row.get("evidence_sha256", ""))):
            self.errors.add(f"record evidence hash is invalid: {route}")
        if not _is_datetime(row.get("retrieved_at")):
            self.errors.add(f"record retrieval timestamp is invalid: {route}")
        if kind == "datasets":
            if row.get("ordinal") != ordinal:
                self.errors.add(f"dataset ordinal is not contiguous at {route}")
            if {"body", "rendered_body", "govspeak", "html"} & set(row):
                self.errors.add(f"body field retained in {route}")
            if not str(row.get("url", "")).startswith(("https://", "http://")):
                self.errors.add(f"unsafe dataset URL in {route}")
            self.register_topology_record(ordinal, route, row)

    def register_topology_record(
        self, ordinal: int, route: str, row: dict[str, Any]
    ) -> None:
        url = str(row.get("url") or row.get("@id") or "")
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").casefold()
        scheme = parsed.scheme.casefold()
        redirects = row.get("redirects") or []
        if not isinstance(redirects, list):
            self.errors.add(f"dataset redirects are not an array: {route}")
            redirects = []
        entity_class = str(row.get("entity_class") or "content_identity")
        coverage = str(row.get("coverage_disposition") or "represented")
        expected_routing_kind = (
            "redirect"
            if redirects
            else "external_boundary"
            if entity_class == "external_boundary" or hostname != "www.gov.uk"
            else "tombstone"
            if coverage == "tombstone_only"
            else "canonical"
        )
        routing_kind = str(row.get("routing_kind") or "")
        if routing_kind != expected_routing_kind:
            self.errors.add(f"dataset routing kind differs from record fields: {route}")
        if not hostname or scheme not in {"http", "https"}:
            self.errors.add(f"dataset cannot be represented in site topology: {route}")
        self.connection.execute(
            "INSERT INTO topology_records(ordinal, open, url, hostname, scheme, "
            "routing_kind, coverage_disposition, stable_content_identifier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ordinal,
                route,
                url,
                hostname,
                scheme,
                routing_kind,
                coverage,
                int(bool(row.get("canonical_content_id"))),
            ),
        )
        for redirect_ordinal, redirect in enumerate(redirects):
            if not isinstance(redirect, dict):
                self.errors.add(f"dataset redirect is not an object: {route}")
                continue
            destination = str(redirect.get("destination") or "")
            destination_url = str(redirect.get("destination_url") or "")
            destination_host = (urlparse(destination_url).hostname or "").casefold()
            if not destination or not destination_url.startswith(("https://", "http://")):
                self.errors.add(f"dataset redirect destination is invalid: {route}")
            self.connection.execute(
                "INSERT INTO topology_redirects(dataset_ordinal, redirect_ordinal, "
                "source_route, source_url, source_host, path, destination, destination_url, "
                "destination_host, type, segments_mode, evidence_url, evidence_locator, "
                "retrieved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ordinal,
                    redirect_ordinal,
                    route,
                    url,
                    hostname,
                    str(redirect.get("path") or parsed.path or "/"),
                    destination,
                    destination_url,
                    destination_host,
                    str(redirect.get("type") or "unknown"),
                    str(redirect.get("segments_mode") or "unknown"),
                    str(row.get("evidence_url") or ""),
                    str(row.get("evidence_locator") or "/"),
                    str(row.get("retrieved_at") or ""),
                ),
            )

    def ingest_records(self, manifest: dict[str, Any], descriptor: dict[str, Any]) -> None:
        chunks = manifest.get("chunks", {})
        if not isinstance(chunks, dict):
            self.errors.add("data manifest chunks must be an object")
            return
        shard_groups = manifest.get("shards", {})
        if not isinstance(shard_groups, dict):
            self.errors.add("data manifest record-shard metadata must be an object")
            shard_groups = {}
        if set(shard_groups) != set(RECORD_SHARD_SCHEMAS):
            self.errors.add("data manifest record-shard groups are incomplete or unknown")
        expected_record_digest = shard_manifest_sha256(shard_groups)
        integrity = manifest.get("integrity", {})
        if not isinstance(integrity, dict) or integrity.get(
            "record_shard_manifest_sha256"
        ) != expected_record_digest:
            self.errors.add("record shard-manifest digest is incorrect")
        for kind in ("datasets", "resources", "publishers"):
            ordinal = 0
            paths = chunks.get(kind, [])
            if not isinstance(paths, list):
                self.errors.add(f"manifest {kind} chunks must be a list")
                continue
            metadata = self.register_shard_metadata(
                label=kind,
                paths=paths,
                rows=shard_groups.get(kind, []),
                expected_schema=RECORD_SHARD_SCHEMAS[kind],
                snapshot=manifest.get("snapshot"),
            )
            for relative in paths:
                rows = self.load_gzip(
                    relative, label=f"{kind} shard {relative}", default=[]
                )
                if not isinstance(rows, list):
                    self.errors.add(f"{kind} shard is not an array: {relative}")
                    continue
                keys = [
                    str(row.get("open", ""))
                    for row in rows
                    if isinstance(row, dict)
                ]
                self.check_shard_observation(
                    metadata.get(str(relative)),
                    label=f"{kind} shard {relative}",
                    count=len(rows),
                    first_key=keys[0] if keys else None,
                    last_key=keys[-1] if keys else None,
                )
                for row in rows:
                    if not isinstance(row, dict):
                        self.errors.add(f"{kind} shard contains a non-object: {relative}")
                        continue
                    self.register_record(kind, ordinal, row)
                    ordinal += 1
            self.counts[kind] = ordinal
            expected = descriptor.get("counts", {}).get(kind)
            if ordinal != expected:
                self.errors.add(f"{kind} count differs from descriptor")
            self.connection.commit()

    def ingest_relationships(self, manifest: dict[str, Any]) -> None:
        paths = manifest.get("chunks", {}).get("relationships", [])
        if not isinstance(paths, list):
            self.errors.add("manifest relationship chunks must be a list")
            return
        shard_groups = manifest.get("shards", {})
        if not isinstance(shard_groups, dict):
            shard_groups = {}
        metadata = self.register_shard_metadata(
            label="relationships",
            paths=paths,
            rows=shard_groups.get("relationships", []),
            expected_schema=RECORD_SHARD_SCHEMAS["relationships"],
            snapshot=manifest.get("snapshot"),
        )
        count = 0
        snapshot = manifest.get("snapshot")
        for relative in paths:
            rows = self.load_gzip(
                relative, label=f"relationship shard {relative}", default=[]
            )
            if not isinstance(rows, list):
                self.errors.add(f"relationship shard is not an array: {relative}")
                continue
            keys = [
                "\0".join(
                    (
                        str(edge.get("source", "")),
                        str(edge.get("kind", "")),
                        str(edge.get("target", "")),
                        str(edge.get("assertion_id", "")),
                    )
                )
                for edge in rows
                if isinstance(edge, dict)
            ]
            self.check_shard_observation(
                metadata.get(str(relative)),
                label=f"relationship shard {relative}",
                count=len(rows),
                first_key=keys[0] if keys else None,
                last_key=keys[-1] if keys else None,
            )
            for edge in rows:
                if not isinstance(edge, dict):
                    self.errors.add(
                        f"relationship shard contains a non-object: {relative}"
                    )
                    continue
                count += 1
                assertion = str(edge.get("assertion_id") or "")
                if not PROVENANCE_FIELDS <= set(edge):
                    self.errors.add(f"relationship provenance incomplete: {assertion}")
                if not str(edge.get("evidence_url", "")).startswith(
                    ("https://", "http://")
                ):
                    self.errors.add(f"relationship evidence URL is unsafe: {assertion}")
                if not re.fullmatch(
                    r"[0-9a-f]{64}", str(edge.get("evidence_sha256", ""))
                ):
                    self.errors.add(f"relationship evidence hash is invalid: {assertion}")
                if edge.get("snapshot_id") != snapshot:
                    self.errors.add(f"relationship snapshot differs from manifest: {assertion}")
                if not _is_datetime(edge.get("observed_at")):
                    self.errors.add(f"relationship observed timestamp is invalid: {assertion}")
                expected_assertion = assertion_id(
                    str(edge.get("source", "")),
                    str(edge.get("kind", "")),
                    str(edge.get("target", "")),
                    str(edge.get("evidence_url", "")),
                    str(edge.get("source_native_predicate", "")),
                    str(edge.get("evidence_locator", "")),
                )
                if assertion != expected_assertion:
                    self.errors.add(
                        f"relationship assertion identifier is not reproducible: {assertion}"
                    )
                try:
                    self.connection.execute(
                        "INSERT INTO relationships(assertion_id, source, target, kind, row_sha256) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            assertion,
                            str(edge.get("source", "")),
                            str(edge.get("target", "")),
                            str(edge.get("kind", "")),
                            hashlib.sha256(canonical_json_bytes(edge)).hexdigest(),
                        ),
                    )
                except sqlite3.IntegrityError:
                    self.errors.add(
                        f"duplicate relationship assertion identifier: {assertion}"
                    )
        self.counts["relationships"] = count
        expected = manifest.get("counts", {}).get("relationships")
        if count != expected:
            self.errors.add("relationship count differs from descriptor")
        unresolved = self.connection.execute(
            "SELECT r.assertion_id FROM relationships r "
            "LEFT JOIN routes s ON s.open=r.source "
            "LEFT JOIN routes t ON t.open=r.target "
            "WHERE s.open IS NULL OR t.open IS NULL LIMIT 1"
        ).fetchone()
        if unresolved:
            self.errors.add(f"relationship endpoint is unresolved: {unresolved[0]}")
        self.connection.commit()

    def validate_route_index(
        self, descriptor: dict[str, Any], data_manifest: dict[str, Any]
    ) -> None:
        entrypoint = descriptor.get("entrypoints", {}).get("route_index")
        route_index = self.load_json(
            entrypoint, label="route-index manifest", default={}
        )
        if not isinstance(route_index, dict):
            self.errors.add("route-index manifest must be an object")
            return
        buckets = route_index.get("buckets", {})
        if (
            route_index.get("schema") != "okf-route-index.v1"
            or route_index.get("schema_version") != DATA_PLANE_SCHEMA_VERSION
            or route_index.get("algorithm") != "fnv1a32-prefix-2"
            or route_index.get("entry_shape") != "identifier-to-typed-matches"
            or not isinstance(buckets, dict)
            or len(buckets) != 256
        ):
            self.errors.add(
                "route index is missing or not the 256-bucket portable contract"
            )
            return
        if (
            route_index.get("snapshot") != data_manifest.get("snapshot")
            or route_index.get("generated_at") != data_manifest.get("generated_at")
        ):
            self.errors.add("route-index snapshot/time differs from data manifest")
        self.validate_budget_contract(route_index, "route-index manifest")
        route_shards = route_index.get("shards", [])
        expected_shard_digest = shard_manifest_sha256(route_shards)
        if route_index.get("shard_manifest_sha256") != expected_shard_digest:
            self.errors.add("route-index shard-manifest digest is incorrect")
        integrity = data_manifest.get("integrity", {})
        if not isinstance(integrity, dict) or integrity.get(
            "route_shard_manifest_sha256"
        ) != expected_shard_digest:
            self.errors.add("data manifest route component root differs")
        metadata = self.register_shard_metadata(
            label="route-index",
            paths=list(buckets.values()),
            rows=route_shards,
            expected_schema="okf-route-shard.v1",
            snapshot=data_manifest.get("snapshot"),
        )
        identifier_count = 0
        match_count = 0
        for bucket, relative in buckets.items():
            payload = self.load_gzip(
                relative, label=f"route-index shard {relative}", default={}
            )
            if not isinstance(payload, dict):
                self.errors.add(f"route-index shard is not an object: {relative}")
                continue
            keys = sorted(str(identifier) for identifier in payload)
            self.check_shard_observation(
                metadata.get(str(relative)),
                label=f"route-index shard {relative}",
                count=len(payload),
                first_key=keys[0] if keys else None,
                last_key=keys[-1] if keys else None,
            )
            shard_metadata = metadata.get(str(relative))
            if shard_metadata is not None and shard_metadata.get(
                "match_count"
            ) != sum(
                len(matches) if isinstance(matches, list) else 0
                for matches in payload.values()
            ):
                self.errors.add(
                    f"route-index match count differs from shard metadata: {relative}"
                )
            identifier_count += len(payload)
            for identifier, matches in payload.items():
                if adjacency_bucket(str(identifier)) != bucket:
                    self.errors.add(
                        f"route-index identifier in wrong bucket: {identifier}"
                    )
                if not isinstance(matches, list) or not matches:
                    self.errors.add(
                        f"route-index identifier has no typed matches: {identifier}"
                    )
                    continue
                match_count += len(matches)
                for match in matches:
                    if not isinstance(match, dict):
                        self.errors.add(
                            f"route-index match is not an object: {identifier}"
                        )
                        continue
                    kind = str(match.get("kind"))
                    ordinal = match.get("ordinal")
                    route = str(match.get("open"))
                    if (
                        kind not in {"datasets", "resources", "publishers"}
                        or not isinstance(ordinal, int)
                        or isinstance(ordinal, bool)
                    ):
                        self.errors.add(
                            f"route-index locator is out of range: {identifier} -> {match}"
                        )
                        continue
                    expected = self.connection.execute(
                        "SELECT open FROM records WHERE kind=? AND ordinal=?",
                        (kind, ordinal),
                    ).fetchone()
                    if expected is None:
                        self.errors.add(
                            f"route-index locator is out of range: {identifier} -> {match}"
                        )
                    elif expected[0] != route:
                        self.errors.add(
                            f"route-index target mismatch: {identifier} -> {match}"
                        )
                    try:
                        self.connection.execute(
                            "INSERT INTO actual_identifiers(identifier, kind, ordinal, open) "
                            "VALUES (?, ?, ?, ?)",
                            (str(identifier), kind, ordinal, route),
                        )
                    except sqlite3.IntegrityError:
                        self.errors.add(
                            f"duplicate route-index typed match: {identifier} -> {match}"
                        )
            self.connection.commit()
        if identifier_count != route_index.get("identifiers"):
            self.errors.add("route-index identifier count differs from its manifest")
        if match_count != route_index.get("entries"):
            self.errors.add("route-index typed-match count differs from its manifest")
        bad_exact = self.connection.execute(
            "SELECT r.open FROM routes r LEFT JOIN actual_identifiers a "
            "ON a.identifier=r.open AND a.open=r.open "
            "GROUP BY r.open HAVING COUNT(a.identifier) != 1 LIMIT 1"
        ).fetchone()
        if bad_exact:
            self.errors.add(
                "every runtime route must have exactly one exact typed route-index match"
            )
        missing = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM (SELECT identifier,kind,ordinal,open "
                "FROM expected_identifiers EXCEPT SELECT identifier,kind,ordinal,open "
                "FROM actual_identifiers)"
            ).fetchone()[0]
        )
        extra = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM (SELECT identifier,kind,ordinal,open "
                "FROM actual_identifiers EXCEPT SELECT identifier,kind,ordinal,open "
                "FROM expected_identifiers)"
            ).fetchone()[0]
        )
        if missing or extra:
            self.errors.add(
                "route-index typed matches do not exactly cover the supported record identifiers"
            )

    def validate_search(
        self, descriptor: dict[str, Any], data_manifest: dict[str, Any]
    ) -> None:
        entrypoint = descriptor.get("entrypoints", {}).get("search_manifest")
        search = self.load_json(entrypoint, label="search manifest", default={})
        if not isinstance(search, dict):
            self.errors.add("search manifest must be an object")
            return
        dataset_count = self.counts["datasets"]
        if (
            search.get("schema") != "okf-static-search.v1"
            or search.get("schema_version") != DATA_PLANE_SCHEMA_VERSION
            or search.get("counts", {}).get("documents") != dataset_count
        ):
            self.errors.add("static search manifest does not cover every dataset")
        if (
            search.get("snapshot") != data_manifest.get("snapshot")
            or search.get("generated_at") != data_manifest.get("generated_at")
        ):
            self.errors.add("search snapshot/time differs from data manifest")
        self.validate_budget_contract(search, "search manifest")
        shard_metadata_path = search.get("shard_metadata", "")
        shard_document = self.load_json(
            shard_metadata_path,
            label="search shard-metadata manifest",
            max_bytes=MAX_SHARD,
            default={},
        )
        if not isinstance(shard_document, dict) or (
            shard_document.get("schema") != "okf-search-shard-manifest.v1"
            or shard_document.get("schema_version") != DATA_PLANE_SCHEMA_VERSION
            or shard_document.get("snapshot") != data_manifest.get("snapshot")
            or shard_document.get("generated_at")
            != data_manifest.get("generated_at")
        ):
            self.errors.add("search shard-metadata manifest schema/snapshot is invalid")
            shard_document = shard_document if isinstance(shard_document, dict) else {}
        shard_groups = shard_document.get("shards", {})
        if not isinstance(shard_groups, dict):
            self.errors.add("search shard metadata must be an object")
            shard_groups = {}
        if set(shard_groups) != set(SEARCH_SHARD_SCHEMAS):
            self.errors.add("search shard groups are incomplete or unknown")
        expected_shard_digest = shard_manifest_sha256(shard_groups)
        if (
            search.get("shard_manifest_sha256") != expected_shard_digest
            or shard_document.get("shard_manifest_sha256")
            != expected_shard_digest
        ):
            self.errors.add("search shard-manifest digest is incorrect")
        integrity = data_manifest.get("integrity", {})
        if not isinstance(integrity, dict) or integrity.get(
            "search_shard_manifest_sha256"
        ) != expected_shard_digest:
            self.errors.add("data manifest search component root differs")
        entrypoints = search.get("entrypoints", {})
        if not isinstance(entrypoints, dict):
            self.errors.add("search entrypoints must be an object")
            entrypoints = {}
        partitioning = search.get("postings_partitioning")
        partitioned_postings = "postings_partitioning" in search
        if partitioned_postings and not matches_exact_json_contract(
            partitioning, POSTINGS_PARTITIONING_CONTRACT
        ):
            self.errors.add(
                "search postings partitioning contract is unsupported or has drifted"
            )
        if partitioned_postings and search.get("lexicon_shard_length") != (
            POSTINGS_PARTITIONING_CONTRACT["logical_shard_length"]
        ):
            self.errors.add(
                "search logical lexicon width differs from postings partitioning"
            )
        result_paths = entrypoints.get("result_docs", [])
        if not isinstance(result_paths, list) or not all(
            isinstance(value, str) and value for value in result_paths
        ):
            self.errors.add("search result-doc entrypoint must be a path list")
            result_paths = []
        lexicon_paths = entrypoints.get("lexicon", {})
        if not isinstance(lexicon_paths, dict) or not all(
            isinstance(key, str)
            and key
            and isinstance(value, str)
            and value
            for key, value in lexicon_paths.items()
        ):
            self.errors.add("search lexicon entrypoint must be a path object")
            lexicon_paths = {}
        posting_paths = entrypoints.get("postings", [])
        if not isinstance(posting_paths, list) or not all(
            isinstance(value, str) and value for value in posting_paths
        ):
            self.errors.add("search postings entrypoint must be a path list")
            posting_paths = []
        prefix_paths = entrypoints.get("prefixes", {})
        if not isinstance(prefix_paths, dict) or not all(
            isinstance(key, str)
            and key
            and isinstance(value, str)
            and value
            for key, value in prefix_paths.items()
        ):
            self.errors.add("search prefix entrypoint must be a path object")
            prefix_paths = {}
        doc_map_value = entrypoints.get("doc_map")
        doc_map_partitioning = search.get("doc_map_partitioning")
        partitioned_doc_map = "doc_map_partitioning" in search
        if partitioned_doc_map:
            if not matches_exact_json_contract(
                doc_map_partitioning, DOC_MAP_PARTITIONING_CONTRACT
            ):
                self.errors.add(
                    "search document-map partitioning contract is unsupported or has drifted"
                )
            if not isinstance(doc_map_value, list) or not all(
                isinstance(value, str) for value in doc_map_value
            ):
                self.errors.add(
                    "partitioned search document-map entrypoint must be a path list"
                )
                doc_map_paths = []
            else:
                doc_map_paths = list(doc_map_value)
        else:
            if not isinstance(doc_map_value, str):
                self.errors.add("legacy search document-map entrypoint must be a path")
                doc_map_paths = []
            else:
                doc_map_paths = [doc_map_value]
        result_metadata = self.register_shard_metadata(
            label="search result-doc",
            paths=result_paths,
            rows=shard_groups.get("result_docs", []),
            expected_schema=SEARCH_SHARD_SCHEMAS["result_docs"],
            snapshot=data_manifest.get("snapshot"),
        )
        lexicon_metadata = self.register_shard_metadata(
            label="search lexicon",
            paths=list(lexicon_paths.values()),
            rows=shard_groups.get("lexicon", []),
            expected_schema=SEARCH_SHARD_SCHEMAS["lexicon"],
            snapshot=data_manifest.get("snapshot"),
        )
        postings_metadata = self.register_shard_metadata(
            label="search postings",
            paths=posting_paths,
            rows=shard_groups.get("postings", []),
            expected_schema=SEARCH_SHARD_SCHEMAS["postings"],
            snapshot=data_manifest.get("snapshot"),
        )
        prefix_metadata = self.register_shard_metadata(
            label="search prefix",
            paths=list(prefix_paths.values()),
            rows=shard_groups.get("prefixes", []),
            expected_schema=SEARCH_SHARD_SCHEMAS["prefixes"],
            snapshot=data_manifest.get("snapshot"),
        )
        doc_map_metadata = self.register_shard_metadata(
            label="search doc-map",
            paths=doc_map_paths,
            rows=shard_groups.get("doc_map", []),
            expected_schema=SEARCH_SHARD_SCHEMAS["doc_map"],
            snapshot=data_manifest.get("snapshot"),
        )
        result_ordinal = 0
        for relative in result_paths:
            rows = self.load_json(
                relative,
                label=f"search result shard {relative}",
                max_bytes=MAX_SHARD,
                default=[],
            )
            if not isinstance(rows, list):
                self.errors.add(f"search result shard is not an array: {relative}")
                continue
            result_keys = [
                str(row.get("open", ""))
                for row in rows
                if isinstance(row, dict)
            ]
            self.check_shard_observation(
                result_metadata.get(str(relative)),
                label=f"search result shard {relative}",
                count=len(rows),
                first_key=result_keys[0] if result_keys else None,
                last_key=result_keys[-1] if result_keys else None,
            )
            for row in rows:
                if not isinstance(row, dict):
                    self.errors.add(f"search result is not an object: {relative}")
                    continue
                expected = self.connection.execute(
                    "SELECT open FROM records WHERE kind='datasets' AND ordinal=?",
                    (result_ordinal,),
                ).fetchone()
                if row.get("ordinal") != result_ordinal:
                    self.errors.add("search-result ordinals are not contiguous")
                if expected is None or row.get("open") != expected[0]:
                    self.errors.add("search-result routes do not match dataset routes")
                result_ordinal += 1
        if result_ordinal != dataset_count:
            self.errors.add("search-result document count differs from datasets")
        token_count = 0
        retained_postings = 0
        uncapped_postings = 0
        shard_length = int(search.get("lexicon_shard_length", 0))
        max_postings = int(search.get("counts", {}).get("max_postings_per_token", 0))
        referenced_posting_paths: set[str] = set()
        for shard, relative in lexicon_paths.items():
            entries = self.load_json(
                relative,
                label=f"search lexicon shard {relative}",
                max_bytes=MAX_SHARD,
                default=[],
            )
            if not isinstance(entries, list):
                self.errors.add(f"search lexicon shard is not an array: {relative}")
                continue
            token_keys = [
                str(entry.get("token", ""))
                for entry in entries
                if isinstance(entry, dict)
            ]
            self.check_shard_observation(
                lexicon_metadata.get(str(relative)),
                label=f"search lexicon shard {relative}",
                count=len(entries),
                first_key=token_keys[0] if token_keys else None,
                last_key=token_keys[-1] if token_keys else None,
            )
            entries_by_path: dict[str, list[dict[str, Any]]] = {}
            path_order: list[str] = []
            previous_lexicon_token: str | None = None
            active_postings_path: str | None = None
            closed_postings_paths: set[str] = set()
            for entry in entries:
                if not isinstance(entry, dict):
                    self.errors.add(f"search lexicon contains a non-object: {relative}")
                    continue
                postings_value = entry.get("postings")
                if not isinstance(postings_value, str) or not postings_value:
                    self.errors.add(
                        f"search lexicon entry has no postings reference: {relative}"
                    )
                    continue
                token = str(entry.get("token", ""))
                if (
                    previous_lexicon_token is not None
                    and token <= previous_lexicon_token
                ):
                    self.errors.add(
                        f"search lexicon tokens are not strictly sorted: {relative}"
                    )
                previous_lexicon_token = token
                if postings_value != active_postings_path:
                    if postings_value in closed_postings_paths:
                        self.errors.add(
                            "search lexicon postings assignments are not contiguous: "
                            f"{shard}"
                        )
                    if active_postings_path is not None:
                        closed_postings_paths.add(active_postings_path)
                    active_postings_path = postings_value
                if postings_value not in entries_by_path:
                    entries_by_path[postings_value] = []
                    path_order.append(postings_value)
                entries_by_path[postings_value].append(entry)
            if entries and not path_order:
                self.errors.add(f"search lexicon shard has no postings: {shard}")
            if not partitioned_postings and len(path_order) != (1 if entries else 0):
                self.errors.add(
                    f"legacy search lexicon shard has inconsistent postings: {shard}"
                )
            try:
                expected_paths = [
                    postings_partition_relative_path(shard, index, len(path_order))
                    for index in range(len(path_order))
                ]
            except PublicationError:
                expected_paths = []
                self.errors.add(f"search postings logical shard is invalid: {shard}")
            if path_order != expected_paths:
                self.errors.add(
                    f"search postings partitions are not canonical or contiguous: {shard}"
                )
            referenced_posting_paths.update(path_order)
            previous_partition_last: str | None = None
            previous_partition_size: int | None = None
            for partition_index, postings_relative in enumerate(path_order):
                payload = self.load_json(
                    postings_relative,
                    label=f"search postings shard {postings_relative}",
                    max_bytes=MAX_SHARD,
                    default={},
                )
                tokens_payload = (
                    payload.get("tokens", {}) if isinstance(payload, dict) else {}
                )
                if not isinstance(tokens_payload, dict):
                    self.errors.add(
                        f"search postings tokens are not an object: {postings_relative}"
                    )
                    tokens_payload = {}
                posting_keys = [str(token) for token in tokens_payload]
                if posting_keys != sorted(posting_keys):
                    self.errors.add(
                        "search postings tokens are not strictly sorted: "
                        f"{postings_relative}"
                    )
                if not posting_keys:
                    self.errors.add(
                        f"search postings partition is empty: {postings_relative}"
                    )
                canonical_size = postings_partition_serialized_size(
                    (token, tokens_payload[token]) for token in posting_keys
                )
                canonical_bytes = pretty_json(
                    {"tokens": tokens_payload}
                ).encode("utf-8")
                try:
                    physical_bytes = self.path(postings_relative).read_bytes()
                except (OSError, ValueError):
                    physical_bytes = b""
                if (
                    len(canonical_bytes) != canonical_size
                    or physical_bytes != canonical_bytes
                ):
                    self.errors.add(
                        "search postings partition is not canonical pretty JSON: "
                        f"{postings_relative}"
                    )
                if posting_keys and previous_partition_last is not None:
                    if previous_partition_last >= posting_keys[0]:
                        self.errors.add(
                            "search postings token ranges overlap or are not ordered: "
                            f"{shard}"
                        )
                    first_rows = tokens_payload[posting_keys[0]]
                    if (
                        partitioned_postings
                        and previous_partition_size is not None
                        and previous_partition_size
                        + postings_entry_serialized_size(
                            posting_keys[0], first_rows, first=False
                        )
                        <= MAX_SHARD
                    ):
                        self.errors.add(
                            "search postings partition split is not greedy: "
                            f"{postings_relative}"
                        )
                if posting_keys:
                    previous_partition_last = posting_keys[-1]
                previous_partition_size = canonical_size
                posting_metadata = postings_metadata.get(postings_relative)
                self.check_shard_observation(
                    posting_metadata,
                    label=f"search postings shard {postings_relative}",
                    count=len(tokens_payload),
                    first_key=posting_keys[0] if posting_keys else None,
                    last_key=posting_keys[-1] if posting_keys else None,
                )
                if partitioned_postings and posting_metadata is not None:
                    expected_partition_fields = {
                        "shard": shard,
                        "partition": partition_index,
                        "partition_count": len(path_order),
                        "partitioning_schema": POSTINGS_PARTITIONING_CONTRACT[
                            "schema"
                        ],
                    }
                    if any(
                        posting_metadata.get(key) != value
                        for key, value in expected_partition_fields.items()
                    ):
                        self.errors.add(
                            "search postings partition metadata differs from its "
                            f"contract: {postings_relative}"
                        )
                if posting_metadata is not None and posting_metadata.get(
                    "posting_count"
                ) != sum(
                    len(rows) if isinstance(rows, list) else 0
                    for rows in tokens_payload.values()
                ):
                    self.errors.add(
                        "search posting count differs from shard metadata: "
                        f"{postings_relative}"
                    )
                partition_entries = entries_by_path.get(postings_relative, [])
                partition_tokens = {
                    str(entry.get("token", "")) for entry in partition_entries
                }
                if set(tokens_payload) != partition_tokens:
                    self.errors.add(
                        "search lexicon/postings token sets differ: "
                        f"{postings_relative}"
                    )
                for entry in partition_entries:
                    token = str(entry.get("token", ""))
                    token_count += 1
                    if search_shard(token, shard_length) != shard or tokenise(
                        token
                    ) != {token}:
                        self.errors.add(
                            "search token is in the wrong shard or not canonical: "
                            f"{token}"
                        )
                    df = entry.get("df")
                    if not isinstance(df, int) or isinstance(df, bool) or df < 0:
                        self.errors.add(
                            f"search token has invalid document frequency: {token}"
                        )
                        df = 0
                    try:
                        self.connection.execute(
                            "INSERT INTO search_tokens(token, shard, df) VALUES (?, ?, ?)",
                            (token, shard, df),
                        )
                    except sqlite3.IntegrityError:
                        self.errors.add(f"duplicate search token: {token}")
                    postings = tokens_payload.get(token, [])
                    if not isinstance(postings, list):
                        self.errors.add(f"search postings are not an array: {token}")
                        postings = []
                    retained_postings += len(postings)
                    uncapped_postings += df
                    if df < len(postings) or len(postings) != min(df, max_postings):
                        self.errors.add(
                            f"search posting count does not match df/cap: {token}"
                        )
                    seen_ordinals: set[int] = set()
                    ranking: list[tuple[int, int]] = []
                    for posting in postings:
                        if (
                            not isinstance(posting, list)
                            or len(posting) != 3
                            or any(
                                not isinstance(value, int)
                                or isinstance(value, bool)
                                for value in posting
                            )
                        ):
                            self.errors.add(f"search postings are malformed: {token}")
                            continue
                        ordinal, score, _mask = posting
                        if ordinal in seen_ordinals:
                            self.errors.add(f"search postings are duplicate: {token}")
                        seen_ordinals.add(ordinal)
                        if not 0 <= ordinal < dataset_count:
                            self.errors.add(
                                f"search posting ordinal is out of range: {token}"
                            )
                        ranking.append((-score, ordinal))
                    if ranking != sorted(ranking):
                        self.errors.add(
                            "search postings are not deterministically ranked: "
                            f"{token}"
                        )
            self.connection.commit()
        manifest_postings = set(posting_paths)
        if manifest_postings != referenced_posting_paths:
            self.errors.add("search postings manifest does not match lexicon references")
        if search.get("counts", {}).get("postings_shards") not in {
            None,
            len(posting_paths),
        }:
            self.errors.add("search postings-shard count differs from manifest")
        for relative in manifest_postings:
            self.check_file(
                relative, label=f"search postings shard {relative}", max_bytes=MAX_SHARD
            )
        for relative in prefix_paths.values():
            payload = self.load_json(
                relative,
                label=f"search prefix shard {relative}",
                max_bytes=MAX_SHARD,
                default={},
            )
            if not isinstance(payload, dict):
                self.errors.add(f"search prefix shard is not an object: {relative}")
                continue
            keys = sorted(str(prefix) for prefix in payload)
            self.check_shard_observation(
                prefix_metadata.get(str(relative)),
                label=f"search prefix shard {relative}",
                count=len(payload),
                first_key=keys[0] if keys else None,
                last_key=keys[-1] if keys else None,
            )
        doc_map_count = 0
        if partitioned_doc_map:
            expected_doc_map_paths = [
                "data/search/doc-map-"
                f"{partition:0{DOC_MAP_PARTITION_INDEX_WIDTH}d}.json"
                for partition in range(len(doc_map_paths))
            ]
            if doc_map_paths != expected_doc_map_paths:
                self.errors.add(
                    "search document-map partitions are not canonical or contiguous"
                )
        for partition, doc_map_path in enumerate(doc_map_paths):
            doc_map = self.load_json(
                doc_map_path,
                label=f"search document map {doc_map_path}",
                max_bytes=MAX_SHARD,
                default={},
            )
            if not isinstance(doc_map, dict):
                self.errors.add(
                    f"search document map is not an object: {doc_map_path}"
                )
                doc_map = {}
            expected_first = doc_map_count
            expected_keys = {
                str(ordinal)
                for ordinal in range(expected_first, expected_first + len(doc_map))
            }
            if set(doc_map) != expected_keys:
                self.errors.add(
                    "search document-map ordinals are not contiguous and exact: "
                    f"{doc_map_path}"
                )
            for key, route in doc_map.items():
                try:
                    ordinal = int(key)
                except (TypeError, ValueError):
                    continue
                expected = self.connection.execute(
                    "SELECT open FROM records WHERE kind='datasets' AND ordinal=?",
                    (ordinal,),
                ).fetchone()
                if expected is None or route != expected[0]:
                    self.errors.add(
                        f"search document-map route differs at ordinal {ordinal}"
                    )
            last = expected_first + len(doc_map) - 1
            self.check_shard_observation(
                doc_map_metadata.get(str(doc_map_path)),
                label=f"search document map {doc_map_path}",
                count=len(doc_map),
                first_key=str(expected_first) if doc_map else None,
                last_key=str(last) if doc_map else None,
            )
            metadata = doc_map_metadata.get(str(doc_map_path))
            if partitioned_doc_map and metadata is not None:
                expected_fields = {
                    "partition": partition,
                    "partition_count": len(doc_map_paths),
                    "first_ordinal": expected_first,
                    "last_ordinal": last,
                    "partitioning_schema": DOC_MAP_PARTITIONING_CONTRACT["schema"],
                }
                if any(
                    metadata.get(key) != value
                    for key, value in expected_fields.items()
                ):
                    self.errors.add(
                        "search document-map partition metadata differs from its "
                        f"contract: {doc_map_path}"
                    )
                if len(doc_map) > DOC_MAP_CHUNK_SIZE:
                    self.errors.add(
                        f"search document-map partition exceeds record cap: {doc_map_path}"
                    )
                if not doc_map:
                    self.errors.add(
                        f"search document-map partition is empty: {doc_map_path}"
                    )
                if partition < len(doc_map_paths) - 1 and len(doc_map) != DOC_MAP_CHUNK_SIZE:
                    self.errors.add(
                        "non-final search document-map partition is not full: "
                        f"{doc_map_path}"
                    )
            doc_map_count += len(doc_map)
        if doc_map_count != dataset_count:
            self.errors.add("search document map count differs from datasets")
        if search.get("counts", {}).get("doc_map_shards") not in {
            None,
            len(doc_map_paths),
        }:
            self.errors.add("search document-map shard count differs from manifest")
        if token_count != search.get("counts", {}).get("tokens"):
            self.errors.add("search token count differs from manifest")
        if retained_postings != search.get("counts", {}).get("postings"):
            self.errors.add("search retained-posting count differs from manifest")
        if uncapped_postings != search.get("counts", {}).get("uncapped_postings"):
            self.errors.add("search uncapped-posting count differs from manifest")

    def validate_adjacency(
        self, descriptor: dict[str, Any], data_manifest: dict[str, Any]
    ) -> None:
        entrypoint = descriptor.get("entrypoints", {}).get("relationship_adjacency")
        manifest = self.load_json(entrypoint, label="adjacency manifest", default={})
        if not isinstance(manifest, dict):
            self.errors.add("adjacency manifest must be an object")
            return
        buckets = manifest.get("buckets", {})
        if (
            manifest.get("schema") != "okf-relationship-adjacency.v1"
            or manifest.get("schema_version") != DATA_PLANE_SCHEMA_VERSION
            or not isinstance(buckets, dict)
            or len(buckets) != 256
        ):
            self.errors.add("adjacency manifest is not the portable 256-bucket contract")
            return
        if (
            manifest.get("snapshot") != data_manifest.get("snapshot")
            or manifest.get("generated_at") != data_manifest.get("generated_at")
        ):
            self.errors.add("adjacency snapshot/time differs from data manifest")
        self.validate_budget_contract(manifest, "adjacency manifest")
        adjacency_shards = manifest.get("shards", [])
        expected_shard_digest = shard_manifest_sha256(adjacency_shards)
        if manifest.get("shard_manifest_sha256") != expected_shard_digest:
            self.errors.add("adjacency shard-manifest digest is incorrect")
        integrity = data_manifest.get("integrity", {})
        if not isinstance(integrity, dict) or integrity.get(
            "adjacency_shard_manifest_sha256"
        ) != expected_shard_digest:
            self.errors.add("data manifest adjacency component root differs")
        metadata = self.register_shard_metadata(
            label="adjacency",
            paths=list(buckets.values()),
            rows=adjacency_shards,
            expected_schema="okf-adjacency-shard.v1",
            snapshot=data_manifest.get("snapshot"),
        )
        if manifest.get("relationships") != self.counts["relationships"]:
            self.errors.add("adjacency relationship count differs from publication")
        for bucket, relative in buckets.items():
            payload = self.load_gzip(
                relative, label=f"adjacency shard {relative}", default={}
            )
            if not isinstance(payload, dict):
                self.errors.add(f"adjacency shard is not an object: {relative}")
                continue
            keys = sorted(str(route) for route in payload)
            self.check_shard_observation(
                metadata.get(str(relative)),
                label=f"adjacency shard {relative}",
                count=len(payload),
                first_key=keys[0] if keys else None,
                last_key=keys[-1] if keys else None,
            )
            shard_metadata = metadata.get(str(relative))
            if shard_metadata is not None and shard_metadata.get(
                "relationship_occurrences"
            ) != sum(
                len(edges) if isinstance(edges, list) else 0
                for edges in payload.values()
            ):
                self.errors.add(
                    f"adjacency occurrence count differs from shard metadata: {relative}"
                )
            for route, edges in payload.items():
                if adjacency_bucket(str(route)) != bucket:
                    self.errors.add(f"adjacency route in wrong bucket: {route}")
                if not isinstance(edges, list):
                    self.errors.add(f"adjacency route edges are not an array: {route}")
                    continue
                for edge in edges:
                    if not isinstance(edge, dict):
                        self.errors.add(f"adjacency route has a non-object edge: {route}")
                        continue
                    if route not in {edge.get("source"), edge.get("target")}:
                        self.errors.add(f"adjacency route has unrelated edge: {route}")
                    assertion = str(edge.get("assertion_id", ""))
                    expected = self.connection.execute(
                        "SELECT row_sha256 FROM relationships WHERE assertion_id=?",
                        (assertion,),
                    ).fetchone()
                    actual_hash = hashlib.sha256(canonical_json_bytes(edge)).hexdigest()
                    if expected is None or expected[0] != actual_hash:
                        self.errors.add(
                            f"adjacency contains an altered or unknown relationship: {assertion}"
                        )
                    self.connection.execute(
                        "INSERT INTO adjacency_counts(assertion_id, occurrences) VALUES (?, 1) "
                        "ON CONFLICT(assertion_id) DO UPDATE SET "
                        "occurrences=adjacency_counts.occurrences+1",
                        (assertion,),
                    )
            self.connection.commit()
        bad = self.connection.execute(
            "SELECT r.assertion_id FROM relationships r "
            "LEFT JOIN adjacency_counts a ON a.assertion_id=r.assertion_id "
            "WHERE COALESCE(a.occurrences,0) != CASE WHEN r.source=r.target THEN 1 ELSE 2 END "
            "LIMIT 1"
        ).fetchone()
        extra = self.connection.execute(
            "SELECT a.assertion_id FROM adjacency_counts a "
            "LEFT JOIN relationships r ON r.assertion_id=a.assertion_id "
            "WHERE r.assertion_id IS NULL LIMIT 1"
        ).fetchone()
        if bad or extra:
            self.errors.add(
                "adjacency does not index every relationship exactly once per distinct endpoint"
            )

    def validate_semantic_projection(
        self, descriptor: dict[str, Any], manifest: dict[str, Any]
    ) -> None:
        entrypoint = descriptor.get("entrypoints", {}).get("semantic_projection")
        semantic = self.load_json(
            entrypoint, label="semantic projection manifest", default={}
        )
        if not isinstance(semantic, dict):
            self.errors.add("semantic projection manifest must be an object")
            return
        if semantic.get("schema") != "govuk-okf-semantic-projection.v1":
            self.errors.add("semantic projection uses the wrong schema")
        if semantic.get("startup") != "lazy":
            self.errors.add("semantic projection is not declared lazy")
        if semantic.get("snapshot") != manifest.get("snapshot"):
            self.errors.add("semantic projection snapshot differs from data manifest")
        if semantic.get("generated_at") != manifest.get("generated_at"):
            self.errors.add("semantic projection time differs from data manifest")
        for entrypoint_name, entrypoint_path in semantic.get("entrypoints", {}).items():
            if entrypoint_name != "vocabulary":
                self.check_file(
                    entrypoint_path,
                    label=f"semantic {entrypoint_name}",
                )
        by_type: Counter[str] = Counter()
        shard_groups = semantic.get("shards", {})
        if not isinstance(shard_groups, dict):
            self.errors.add("semantic shard metadata must be an object")
            shard_groups = {}
        expected_shard_digest = hashlib.sha256(
            canonical_json_bytes(shard_groups)
        ).hexdigest()
        if semantic.get("shard_manifest_sha256") != expected_shard_digest:
            self.errors.add("semantic shard-manifest digest is incorrect")
        shard_rows: dict[str, dict[str, Any]] = {}
        for group, rows in shard_groups.items():
            if not isinstance(rows, list):
                self.errors.add(f"semantic shard metadata group is not a list: {group}")
                continue
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                    self.errors.add(f"semantic shard metadata row is invalid: {group}")
                    continue
                path = str(row["path"])
                if path in shard_rows:
                    self.errors.add(f"duplicate semantic shard metadata path: {path}")
                shard_rows[path] = row
                required = {
                    "path",
                    "schema",
                    "snapshot",
                    "count",
                    "source_row_count",
                    "compressed_bytes",
                    "uncompressed_bytes",
                    "sha256",
                    "first_key",
                    "last_key",
                    "compression",
                }
                if not required <= set(row):
                    self.errors.add(f"semantic shard metadata is incomplete: {path}")
                if row.get("schema") != "govuk-okf-semantic-shard.v1":
                    self.errors.add(f"semantic shard schema is invalid: {path}")
                if row.get("snapshot") != semantic.get("snapshot"):
                    self.errors.add(f"semantic shard snapshot is invalid: {path}")
        seen_shards: set[str] = set()

        def accept_document(document: object, source: str) -> None:
            if not isinstance(document, dict):
                self.errors.add(f"semantic shard is not an object: {source}")
                return
            if document.get("@context") != semantic.get("context"):
                self.errors.add(f"semantic shard context differs from manifest: {source}")
            graph = document.get("@graph")
            if not isinstance(graph, list):
                self.errors.add(f"semantic shard graph is not an array: {source}")
                return
            metadata = shard_rows.get(source)
            if metadata is None:
                self.errors.add(f"semantic graph has no shard metadata: {source}")
            else:
                seen_shards.add(source)
                try:
                    path = self.path(source)
                    if path.stat().st_size != metadata.get("compressed_bytes"):
                        self.errors.add(f"semantic shard compressed size differs: {source}")
                    if _file_sha256(path) != metadata.get("sha256"):
                        self.errors.add(f"semantic shard SHA-256 differs: {source}")
                    if metadata.get("compression") == "gzip":
                        uncompressed_size = len(canonical_json_bytes(document))
                    elif metadata.get("compression") == "identity":
                        uncompressed_size = path.stat().st_size
                    else:
                        self.errors.add(f"semantic shard compression is invalid: {source}")
                        uncompressed_size = -1
                    if uncompressed_size != metadata.get("uncompressed_bytes"):
                        self.errors.add(f"semantic shard uncompressed size differs: {source}")
                except (OSError, ValueError) as exc:
                    self.errors.add(f"cannot verify semantic shard metadata {source}: {exc}")
                keys = sorted(
                    str(node.get("@id"))
                    for node in graph
                    if isinstance(node, dict) and node.get("@id") is not None
                )
                if metadata.get("count") != len(graph):
                    self.errors.add(f"semantic shard node count differs: {source}")
                if metadata.get("first_key") != (keys[0] if keys else None):
                    self.errors.add(f"semantic shard first key differs: {source}")
                if metadata.get("last_key") != (keys[-1] if keys else None):
                    self.errors.add(f"semantic shard last key differs: {source}")
            for node in graph:
                self.validate_semantic_node(node, semantic, by_type, source)

        vocabulary_path = semantic.get("entrypoints", {}).get("vocabulary", "")
        vocabulary = self.load_json(
            vocabulary_path,
            label="semantic vocabulary",
            max_bytes=MAX_SHARD,
            default={},
        )
        accept_document(vocabulary, str(vocabulary_path))
        chunks = semantic.get("chunks", {})
        if not isinstance(chunks, dict):
            self.errors.add("semantic projection chunks must be an object")
            chunks = {}
        for kind in ("entities", "publishers", "resources", "assertions"):
            paths = chunks.get(kind, [])
            if not isinstance(paths, list):
                self.errors.add(f"semantic {kind} chunks must be a list")
                continue
            for relative in paths:
                document = self.load_gzip(
                    relative, label=f"semantic {kind} shard {relative}", default={}
                )
                accept_document(document, str(relative))
            metadata_paths = {
                str(row.get("path"))
                for row in shard_groups.get(kind, [])
                if isinstance(row, dict)
            }
            if set(str(path) for path in paths) != metadata_paths:
                self.errors.add(f"semantic {kind} chunk paths differ from shard metadata")
        if set(shard_rows) != seen_shards:
            self.errors.add("semantic shard metadata includes unchecked or missing paths")
        expected_by_type = semantic.get("by_type", {})
        if dict(sorted(by_type.items())) != expected_by_type:
            self.errors.add("semantic type counts differ from manifest")
        counts = semantic.get("counts", {})
        if counts.get("total_nodes") != self.semantic_node_occurrences:
            self.errors.add("semantic total-node count differs from manifest")
        if counts.get("assertion_nodes") != by_type["govuk:Assertion"]:
            self.errors.add("semantic assertion count differs from manifest")
        if counts.get("source_relationships") != self.counts["relationships"]:
            self.errors.add("semantic source relationship count differs from publication")
        if counts.get("source_datasets") != self.counts["datasets"]:
            self.errors.add("semantic source dataset count differs from publication")
        missing_reference = self.connection.execute(
            "SELECT r.source,r.target,r.field FROM semantic_references r "
            "LEFT JOIN semantic_nodes n ON n.identifier=r.target "
            "WHERE n.identifier IS NULL LIMIT 1"
        ).fetchone()
        if missing_reference:
            self.errors.add(
                "semantic reference does not resolve: "
                f"{missing_reference[0]} {missing_reference[2]} {missing_reference[1]}"
            )
        missing_record = self.connection.execute(
            "SELECT r.open FROM routes r LEFT JOIN semantic_nodes n "
            "ON n.identifier=? || r.open WHERE n.identifier IS NULL LIMIT 1",
            (semantic_route_iri(""),),
        ).fetchone()
        if missing_record:
            self.errors.add(
                f"runtime route has no semantic primary entity: {missing_record[0]}"
            )
        missing_assertion = self.connection.execute(
            "SELECT r.assertion_id FROM relationships r LEFT JOIN semantic_nodes n "
            "ON n.identifier='urn:govuk:' || r.assertion_id "
            "WHERE n.identifier IS NULL LIMIT 1"
        ).fetchone()
        if missing_assertion:
            self.errors.add(
                f"relationship has no semantic assertion: {missing_assertion[0]}"
            )

    def validate_semantic_node(
        self,
        node: object,
        manifest: dict[str, Any],
        by_type: Counter[str],
        source: str,
    ) -> None:
        if not isinstance(node, dict):
            self.errors.add(f"semantic graph contains a non-object node: {source}")
            return
        identifier = node.get("@id")
        entity_type = node.get("@type")
        if not _is_iri(identifier) or not isinstance(entity_type, str):
            self.errors.add(f"semantic node has invalid identity/type: {source}")
            return
        identifier = str(identifier)
        by_type[entity_type] += 1
        self.semantic_node_occurrences += 1
        self.connection.execute(
            "INSERT OR IGNORE INTO semantic_nodes(identifier, entity_type) VALUES (?, ?)",
            (identifier, entity_type),
        )
        snapshot = manifest.get("snapshot")
        if entity_type == "govuk:Evidence":
            required = {
                "@id",
                "@type",
                "title",
                "evidenceUrl",
                "sourceSystem",
                "sourceAuthority",
                "locator",
                "retrievedAt",
                "sha256",
                "mediaType",
                "license",
                "rightsStatus",
                "snapshotId",
                "authority",
            }
            if not required <= set(node):
                self.errors.add(f"semantic Evidence node is incomplete: {identifier}")
            if not re.fullmatch(r"[0-9a-f]{64}", str(node.get("sha256", ""))):
                self.errors.add(f"semantic Evidence hash is invalid: {identifier}")
            if not _is_iri(node.get("evidenceUrl")):
                self.errors.add(f"semantic Evidence URL is invalid: {identifier}")
        elif entity_type == "govuk:Assertion":
            required = {
                "@id",
                "@type",
                "subject",
                "predicate",
                "object",
                "sourceNativePredicate",
                "evidence",
                "retrievedAt",
                "generatedBy",
                "derivationMethod",
                "assertionStatus",
                "authority",
                "confidence",
                "snapshotId",
            }
            if not required <= set(node):
                self.errors.add(f"semantic Assertion node is incomplete: {identifier}")
            if node.get("authority") != "source_native" or node.get(
                "assertionStatus"
            ) != "source_native":
                self.errors.add(f"semantic Assertion authority is invalid: {identifier}")
        else:
            required = SEMANTIC_COMMON_FIELDS | SEMANTIC_REQUIRED.get(entity_type, set())
            if not required <= set(node):
                self.errors.add(
                    f"semantic {entity_type} node is incomplete: {identifier}"
                )
        if node.get("snapshotId") != snapshot:
            self.errors.add(f"semantic node snapshot differs from manifest: {identifier}")
        if "retrievedAt" in node and not _is_datetime(node.get("retrievedAt")):
            self.errors.add(f"semantic node timestamp is invalid: {identifier}")
        references: list[tuple[str, str]] = []
        for field in SEMANTIC_REFERENCE_FIELDS:
            value = node.get(field)
            if isinstance(value, dict) and _is_iri(value.get("@id")):
                references.append((field, str(value["@id"])))
            elif value is not None:
                self.errors.add(
                    f"semantic reference {field} is malformed: {identifier}"
                )
        evidence = node.get("evidence")
        if evidence is not None:
            if not isinstance(evidence, list) or not evidence:
                self.errors.add(f"semantic evidence references are malformed: {identifier}")
            else:
                for reference in evidence:
                    if isinstance(reference, dict) and _is_iri(reference.get("@id")):
                        references.append(("evidence", str(reference["@id"])))
                    else:
                        self.errors.add(
                            f"semantic evidence reference is malformed: {identifier}"
                        )
        for field, target in references:
            self.connection.execute(
                "INSERT OR IGNORE INTO semantic_references(source, target, field) "
                "VALUES (?, ?, ?)",
                (identifier, target, field),
            )

    def validate_bootstrap(self, descriptor: dict[str, Any]) -> None:
        entrypoints = descriptor.get("entrypoints", {})
        paths = [
            "okf-explorer.json",
            entrypoints.get("data_manifest", ""),
            entrypoints.get("overview_index", ""),
            entrypoints.get("analysis_overview", ""),
            entrypoints.get("search_manifest", ""),
        ]
        size = 0
        for relative in paths:
            try:
                size += self.path(relative).stat().st_size
            except (OSError, ValueError) as exc:
                self.errors.add(f"cannot measure bootstrap path {relative}: {exc}")
        if size > MAX_BOOTSTRAP:
            self.errors.add("uncompressed bootstrap metadata exceeds 2 MiB")

    def validate_site_topology(
        self, descriptor: dict[str, Any], manifest: dict[str, Any]
    ) -> None:
        reference = descriptor.get("entrypoints", {}).get("site_topology")
        if not reference:
            self.errors.add("Explorer descriptor has no site-topology entrypoint")
            return
        topology = self.load_json(
            reference,
            label="site topology",
            max_bytes=MAX_UNCOMPRESSED_SHARD,
            default={},
        )
        if not isinstance(topology, dict):
            self.errors.add("site topology must be a JSON object")
            return
        if topology.get("schema") != "govuk-site-topology.v1":
            self.errors.add("site topology schema is invalid")
        if topology.get("snapshot") != manifest.get("snapshot"):
            self.errors.add("site topology snapshot differs from data manifest")
        if topology.get("generated_at") != manifest.get("generated_at"):
            self.errors.add("site topology generation time differs from data manifest")
        if topology.get("status") != "snapshot_projection_not_release_completeness_claim":
            self.errors.add("site topology makes an unsupported status claim")
        counts = topology.get("counts")
        if not isinstance(counts, dict):
            self.errors.add("site topology counts must be an object")
            counts = {}
        if counts.get("published_records") != self.counts["datasets"]:
            self.errors.add("site topology published-record count differs")
        if counts.get("relationship_assertions") != self.counts["relationships"]:
            self.errors.add("site topology relationship count differs")

        overview = self.load_json(
            descriptor.get("entrypoints", {}).get("overview_index"),
            label="overview index for site topology",
            max_bytes=MAX_UNCOMPRESSED_SHARD,
            default={},
        )
        source_count = (
            overview.get("coverage", {}).get("source_records")
            if isinstance(overview, dict)
            else None
        )
        if counts.get("source_records") != source_count:
            self.errors.add("site topology source-record count differs from overview")

        expected_hosts_by_name: dict[str, dict[str, Any]] = {}
        for hostname, scheme, routing_kind, route in self.connection.execute(
            "SELECT hostname, scheme, routing_kind, open FROM topology_records "
            "ORDER BY ordinal"
        ):
            expected = expected_hosts_by_name.setdefault(
                hostname,
                {
                    "hostname": hostname,
                    "record_count": 0,
                    "schemes": set(),
                    "routing_kinds": Counter(),
                    "example_routes": [],
                },
            )
            expected["record_count"] += 1
            expected["schemes"].add(scheme)
            expected["routing_kinds"][routing_kind] += 1
            if len(expected["example_routes"]) < 3:
                expected["example_routes"].append(route)

        def host_kind(hostname: str) -> str:
            if hostname == "www.gov.uk":
                return "main_publishing_estate"
            if hostname == "gov.uk" or hostname.endswith(".gov.uk"):
                return "gov_uk_domain_boundary"
            return "other_external_boundary"

        host_kind_order = {
            "main_publishing_estate": 0,
            "gov_uk_domain_boundary": 1,
            "other_external_boundary": 2,
        }
        expected_hosts = []
        for hostname, value in sorted(
            expected_hosts_by_name.items(),
            key=lambda item: (host_kind_order[host_kind(item[0])], item[0]),
        ):
            expected_hosts.append(
                {
                    "hostname": hostname,
                    "host_kind": host_kind(hostname),
                    "record_count": value["record_count"],
                    "schemes": sorted(value["schemes"]),
                    "routing_kinds": [
                        {"value": key, "count": count}
                        for key, count in sorted(value["routing_kinds"].items())
                    ],
                    "example_routes": value["example_routes"],
                }
            )
        hosts = topology.get("hosts")
        if not isinstance(hosts, list):
            self.errors.add("site topology hosts must be an array")
            hosts = []
        if hosts != expected_hosts:
            self.errors.add("site topology host inventory differs from record shards")

        routing_counts = Counter(
            dict(
                self.connection.execute(
                    "SELECT routing_kind, COUNT(*) FROM topology_records GROUP BY routing_kind"
                )
            )
        )
        coverage_counts = Counter(
            dict(
                self.connection.execute(
                    "SELECT coverage_disposition, COUNT(*) FROM topology_records "
                    "GROUP BY coverage_disposition"
                )
            )
        )
        redirect_count = int(
            self.connection.execute("SELECT COUNT(*) FROM topology_redirects").fetchone()[0]
        )
        stable_identifier_count = int(
            self.connection.execute(
                "SELECT COALESCE(SUM(stable_content_identifier), 0) FROM topology_records"
            ).fetchone()[0]
        )
        cross_host_redirect_count = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM topology_redirects WHERE source_host <> '' "
                "AND destination_host <> '' AND source_host <> destination_host"
            ).fetchone()[0]
        )
        expected_counts = {
            "source_records": source_count,
            "published_records": self.counts["datasets"],
            "hosts": len(expected_hosts),
            "main_publishing_estate_records": sum(
                row["record_count"]
                for row in expected_hosts
                if row["host_kind"] == "main_publishing_estate"
            ),
            "gov_uk_domain_boundary_hosts": sum(
                1 for row in expected_hosts if row["host_kind"] == "gov_uk_domain_boundary"
            ),
            "other_external_boundary_hosts": sum(
                1 for row in expected_hosts if row["host_kind"] == "other_external_boundary"
            ),
            "redirect_records": routing_counts["redirect"],
            "redirect_rules": redirect_count,
            "cross_host_redirect_rules": cross_host_redirect_count,
            "stable_content_identifiers": stable_identifier_count,
            "relationship_assertions": self.counts["relationships"],
        }
        if counts != expected_counts:
            self.errors.add("site topology counts differ from record and relationship shards")

        expected_coverage = [
            {"value": key, "count": count}
            for key, count in sorted(coverage_counts.items())
        ]
        if topology.get("coverage_dispositions") != expected_coverage:
            self.errors.add("site topology coverage dispositions differ from record shards")
        expected_routing = [
            {"value": key, "count": count}
            for key, count in sorted(routing_counts.items())
        ]
        if topology.get("routing_kinds") != expected_routing:
            self.errors.add("site topology routing kinds differ from record shards")
        relationship_counts = Counter(
            dict(
                self.connection.execute(
                    "SELECT kind, COUNT(*) FROM relationships GROUP BY kind"
                )
            )
        )
        expected_relationships = [
            {"value": key, "count": count}
            for key, count in sorted(
                relationship_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ]
        if topology.get("relationship_kinds") != expected_relationships:
            self.errors.add("site topology relationship kinds differ from relationship shards")

        expected_mechanism_counts = {
            "canonical_url": self.counts["datasets"],
            "stable_content_identifier": stable_identifier_count,
            "redirect_rule": redirect_count,
            "external_boundary": routing_counts["external_boundary"],
            "typed_relationship": self.counts["relationships"],
        }
        mechanisms = topology.get("routing_mechanisms")
        if not isinstance(mechanisms, list) or {
            str(row.get("id") or ""): row.get("count")
            for row in mechanisms
            if isinstance(row, dict)
        } != expected_mechanism_counts:
            self.errors.add("site topology routing mechanisms differ from source planes")

        redirect_columns = (
            "source_route, source_url, source_host, path, destination, destination_url, "
            "destination_host, type, segments_mode, evidence_url, evidence_locator, retrieved_at"
        )
        expected_redirects = [
            dict(zip(
                (
                    "source_route",
                    "source_url",
                    "source_host",
                    "path",
                    "destination",
                    "destination_url",
                    "destination_host",
                    "type",
                    "segments_mode",
                    "evidence_url",
                    "evidence_locator",
                    "retrieved_at",
                ),
                row,
                strict=True,
            ))
            for row in self.connection.execute(
                f"SELECT {redirect_columns} FROM topology_redirects "
                "ORDER BY source_url, path, destination_url, type, segments_mode, "
                "dataset_ordinal, redirect_ordinal LIMIT 100"
            )
        ]
        redirects = topology.get("redirect_samples")
        if not isinstance(redirects, list):
            self.errors.add("site topology redirect samples must be an array")
            redirects = []
        if redirects != expected_redirects:
            self.errors.add("site topology redirect sample differs from record shards")
        if topology.get("redirect_samples_complete") != (redirect_count <= 100):
            self.errors.add("site topology redirect-sample completeness flag differs")
        relative = self.path(reference).relative_to(self.bundle).as_posix()
        if manifest.get("indexes", {}).get("site_topology") != relative:
            self.errors.add("data manifest and descriptor site-topology references differ")

    def validate_data_plane_root(
        self, descriptor: dict[str, Any], manifest: dict[str, Any]
    ) -> None:
        integrity = manifest.get("integrity", {})
        if not isinstance(integrity, dict):
            self.errors.add("data-plane integrity root must be an object")
            return
        observed_root = data_plane_manifest_root(self.data_plane_shards.values())
        if integrity.get("manifest_root_sha256") != observed_root:
            self.errors.add("data-plane manifest root does not match shard leaves")
        if descriptor.get("data_plane_manifest_root_sha256") != observed_root:
            self.errors.add("Explorer descriptor does not bind the data-plane root")
        if integrity.get("leaf_count") != len(self.data_plane_shards):
            self.errors.add("data-plane manifest leaf count differs")

    def validate(self) -> ValidationResult:
        descriptor, _root_semantic, manifest = self.validate_root()
        if descriptor and manifest:
            self.ingest_records(manifest, descriptor)
            self.ingest_relationships(manifest)
            self.validate_route_index(descriptor, manifest)
            self.validate_search(descriptor, manifest)
            self.validate_adjacency(descriptor, manifest)
            self.validate_site_topology(descriptor, manifest)
            self.validate_data_plane_root(descriptor, manifest)
            self.validate_semantic_projection(descriptor, manifest)
            self.validate_bootstrap(descriptor)
        self.connection.commit()
        return ValidationResult(
            error_count=self.errors.total,
            errors=tuple(self.errors.messages),
            datasets=self.counts["datasets"],
            resources=self.counts["resources"],
            publishers=self.counts["publishers"],
            relationships=self.counts["relationships"],
            semantic_nodes=self.semantic_node_occurrences,
        )


def validate_bundle(bundle: Path) -> ValidationResult:
    """Validate a publication using only bounded shards and a temporary DB."""
    with tempfile.TemporaryDirectory(prefix="govuk-okf-validate-") as directory:
        validator = PublicationValidator(bundle, Path(directory) / "validation.sqlite3")
        try:
            return validator.validate()
        finally:
            validator.close()
