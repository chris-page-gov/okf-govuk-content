"""Checkpointed, bounded-memory Content API hydration and linked closure."""

from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote, urlparse

from .acquisition import (
    CONTENT_API_ROOT,
    HostLimiter,
    candidate_key,
    expand_candidate_records,
    merge_records,
    normalise_url,
    read_jsonl_gzip,
    request_observation,
    sanitise_content_api,
    write_jsonl_gzip_shards,
    write_text_atomic,
)
from .util import canonical_json_bytes, pretty_json


class HydrationError(RuntimeError):
    """Raised when a hydration closure cannot be represented safely."""


_STORAGE_CEILING_PATTERN = re.compile(
    r"^\s*retained_metadata_storage_gib:\s*([0-9]+)\s*$",
    re.MULTILINE,
)
_STORAGE_OPERATIONAL_PERCENT = 95
_SQLITE_WRITE_SAFETY_FACTOR = 3
_SQLITE_WRITE_FIXED_RESERVE = 8 * 1024 * 1024
_SQLITE_ROW_OVERHEAD_RESERVE = 512
_SQLITE_BATCH_MAX_RECORDS = 1_000
_SQLITE_BATCH_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
_CONTROL_DOCUMENT_RESERVE = 2 * 1024 * 1024
_EXPORT_SHARD_RESERVE = 52 * 1024 * 1024
_MAX_SPOOL_DOCUMENT_BYTES = 256 * 1024 * 1024
_TRANSIENT_SPOOL_RESERVE_PER_REQUEST = _MAX_SPOOL_DOCUMENT_BYTES
_TRANSIENT_SPOOL_MINIMUM_FREE = 256 * 1024 * 1024
_SPOOL_SCHEMA = "govuk-okf-hydration-spool.v1"


def _launch_storage_ceiling_bytes(root: Path) -> int:
    launch = root / "governance" / "launch-manifest.yaml"
    if not launch.is_file():
        raise HydrationError("launch manifest is required to authorise retained hydration storage")
    match = _STORAGE_CEILING_PATTERN.search(launch.read_text(encoding="utf-8"))
    if not match or int(match.group(1)) < 1:
        raise HydrationError("launch manifest has no positive retained_metadata_storage_gib ceiling")
    return int(match.group(1)) * 1024**3


def _regular_file_bytes(root: Path) -> int:
    if root.is_symlink():
        raise HydrationError(f"retained metadata root cannot be a symbolic link: {root}")
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise HydrationError(f"retained metadata tree contains a symbolic link: {path}")
        if path.is_file():
            total += path.stat().st_size
    return total


def read_source_records(path: Path) -> Iterator[dict[str, Any]]:
    if path.is_dir():
        path = path / "index.json"
    if path.suffix == ".json":
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema") != "govuk-okf-jsonl-shards.v1" or not isinstance(manifest.get("shards"), list):
            raise HydrationError(f"{path}: unsupported source-record manifest")
        aggregate = hashlib.sha256()
        total = 0
        manifest_root = path.parent.resolve()
        for row in manifest["shards"]:
            if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                raise HydrationError(f"{path}: invalid source-record shard row")
            relative = Path(row["path"])
            shard = (manifest_root / relative).resolve()
            if relative.is_absolute() or ".." in relative.parts or manifest_root not in shard.parents:
                raise HydrationError(f"{path}: unsafe source-record shard path")
            if (
                shard.stat().st_size > 50 * 1024 * 1024
                or hashlib.sha256(shard.read_bytes()).hexdigest() != row.get("file_sha256")
            ):
                raise HydrationError(f"{path}: source-record shard file hash failed: {relative}")
            shard_count = 0
            shard_digest = hashlib.sha256()
            for value in read_jsonl_gzip(shard):
                encoded = canonical_json_bytes(value)
                aggregate.update(encoded)
                shard_digest.update(encoded)
                total += 1
                shard_count += 1
                yield value
            if shard_count != row.get("records") or shard_digest.hexdigest() != row.get("canonical_sha256"):
                raise HydrationError(f"{path}: source-record shard integrity check failed: {relative}")
        if total != manifest.get("records") or aggregate.hexdigest() != manifest.get("canonical_sha256"):
            raise HydrationError(f"{path}: source-record manifest integrity check failed")
        return
    if path.suffix == ".gz":
        yield from read_jsonl_gzip(path)
        return
    with path.open("rb") as stream:
        number = 0
        while True:
            line = stream.readline(16 * 1024 * 1024 + 1)
            if not line:
                break
            number += 1
            if len(line) > 16 * 1024 * 1024:
                raise HydrationError(f"{path}:{number}: source record exceeds 16 MiB")
            if not line.strip():
                continue
            value = json.loads(line.decode("utf-8"))
            if not isinstance(value, dict):
                raise HydrationError(f"{path}:{number}: source record must be an object")
            yield value


def _record_key(record: dict[str, Any]) -> tuple[str, str]:
    url = normalise_url(str(record.get("canonical_url") or record.get("base_path") or "/"))
    return url, str(record.get("locale") or "en")


def _linked_records(parent: dict[str, Any]) -> Iterator[dict[str, Any]]:
    links = parent.get("links")
    if not isinstance(links, dict):
        return
    evidence_url = str(parent.get("evidence_url") or parent.get("canonical_url") or "")
    evidence_hash = parent.get("evidence_sha256")
    retrieved_at = parent.get("retrieved_at")
    for predicate, values in sorted(links.items()):
        if not isinstance(values, list):
            continue
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                continue
            raw = str(
                value.get("api_path")
                or value.get("web_url")
                or value.get("base_path")
                or value.get("link")
                or value.get("url")
                or ""
            )
            if raw.startswith("/api/content"):
                raw = raw[len("/api/content") :] or "/"
            if not raw:
                continue
            try:
                url = normalise_url(raw)
            except Exception:
                continue
            locale = str(value.get("locale") or parent.get("locale") or "en")
            host = urlparse(url).netloc
            entity_class = "route" if host == "www.gov.uk" else "external_boundary"
            yield {
                "candidate_key": candidate_key(url, locale, entity_class, url),
                "entity_class": entity_class,
                "source_native_id": value.get("content_id") or url,
                "source_id": "structured-linked-content",
                "source_memberships": ["structured-linked-content"],
                "coverage_disposition": "represented",
                "content_id": value.get("content_id"),
                "canonical_url": url,
                "base_path": urlparse(url).path or "/",
                "title": str(value.get("title") or urlparse(url).path.rsplit("/", 1)[-1] or "GOV.UK"),
                "description": "Discovered through a typed field in an admitted Content API record.",
                "document_type": str(value.get("document_type") or "linked_content"),
                "schema_name": str(value.get("schema_name") or "unknown"),
                "locale": locale,
                "links": {},
                "retrieved_at": retrieved_at,
                "evidence_url": evidence_url,
                "evidence_sha256": evidence_hash,
                "evidence_locator": f"/links/{predicate}/{index}",
                "source_adapter": "govuk_content_api_link_closure",
                "discovery_predicate": predicate,
            }


def _constraint(record: dict[str, Any], status: int, reason: str, evidence: dict[str, Any]) -> dict[str, Any]:
    url, _ = _record_key(record)
    identifier = "constraint-" + hashlib.sha256(f"{url}\0{status}\0{reason}".encode("utf-8")).hexdigest()[:24]
    return {
        "id": identifier,
        "class": "content_api_hydration",
        "status": status,
        "reason": reason,
        "evidence_url": evidence.get("requested_url") or CONTENT_API_ROOT + urlparse(url).path,
        "evidence_sha256": evidence.get("sha256"),
        "owner": "corpus-maintainer",
        "review_date": str(evidence.get("retrieved_at") or datetime.now(timezone.utc).isoformat())[:10],
        "retry": "next closing acquisition",
    }


class CorpusHydrator:
    """Hydrate a census and recursively close typed Content API links."""

    def __init__(
        self,
        root: Path,
        label: str,
        source_path: Path,
        *,
        requests_per_second: float = 8.0,
        workers: int = 16,
        batch_size: int = 256,
        retained_storage_bytes: int | None = None,
    ) -> None:
        if workers < 1 or batch_size < 1:
            raise HydrationError("workers and batch_size must be positive")
        self.root = root.resolve()
        self.label = label
        self.source_path = source_path.resolve()
        self.workers = workers
        self.batch_size = batch_size
        self.cache_root = self.root / "corpus" / "cache" / label / "hydration"
        self.database_path = self.cache_root / "checkpoint.sqlite"
        self.spool_root = (
            self.root
            / ".tmp"
            / "hydration-spool"
            / hashlib.sha256(label.encode("utf-8")).hexdigest()[:24]
        )
        self.spool_quarantine_root = self.spool_root.with_name(
            f"{self.spool_root.name}-quarantine"
        )
        self.records_root = self.root / "corpus" / "records" / label
        self.inventory_root = self.root / "corpus" / "inventory"
        self.reconciliation_root = self.root / "corpus" / "reconciliation"
        self.storage_ceiling_bytes = (
            retained_storage_bytes
            if retained_storage_bytes is not None
            else _launch_storage_ceiling_bytes(self.root)
        )
        if self.storage_ceiling_bytes < 1:
            raise HydrationError("retained hydration storage ceiling must be positive")
        self.storage_operational_bytes = (
            self.storage_ceiling_bytes * _STORAGE_OPERATIONAL_PERCENT // 100
        )
        self.limiter = HostLimiter(
            requests_per_second,
            state_path=self.root / ".tmp" / "rate-limits" / "content-api.timestamp",
            budget_path=self.root / ".tmp" / "request-budget" / "official-sources.count",
            max_requests=1_000_000,
        )

    @staticmethod
    def _queue_schema() -> str:
        return """
            CREATE TABLE queue (
                url TEXT NOT NULL,
                locale TEXT NOT NULL,
                input_json TEXT,
                state TEXT NOT NULL CHECK (state IN ('pending', 'complete')),
                hydration_status TEXT,
                record_json TEXT,
                PRIMARY KEY (url, locale),
                CHECK (
                    (state = 'pending' AND input_json IS NOT NULL AND record_json IS NULL)
                    OR
                    (state = 'complete' AND input_json IS NULL AND record_json IS NOT NULL)
                )
            )
        """

    @staticmethod
    def _candidate_schema() -> str:
        return """
            CREATE TABLE candidates (
                candidate_key TEXT PRIMARY KEY,
                entity_class TEXT NOT NULL,
                source_native_id TEXT NOT NULL,
                locale TEXT NOT NULL,
                candidate_json TEXT NOT NULL
            )
        """

    def _migrate_queue_schema(self, connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1]): row
            for row in connection.execute("PRAGMA table_info(queue)").fetchall()
        }
        input_column = columns.get("input_json")
        if input_column is None:
            raise HydrationError("hydration queue has no input_json column")
        if not bool(input_column[3]):
            invalid = int(
                connection.execute(
                    "SELECT COUNT(*) FROM queue WHERE "
                    "(state='pending' AND (input_json IS NULL OR record_json IS NOT NULL)) OR "
                    "(state='complete' AND (input_json IS NOT NULL OR record_json IS NULL))"
                ).fetchone()[0]
            )
            if invalid:
                raise HydrationError("hydration queue violates the nullable-input state contract")
            return

        invalid_complete = int(
            connection.execute(
                "SELECT COUNT(*) FROM queue WHERE state='complete' AND record_json IS NULL"
            ).fetchone()[0]
        )
        if invalid_complete:
            raise HydrationError("legacy hydration queue has completed rows without record payloads")
        # The migration temporarily retains the old table while SQLite writes
        # the replacement into the WAL.  Admit that worst-case duplicate before
        # changing the legacy checkpoint, rather than detecting the ceiling
        # only after the copy has already consumed it.
        database_bytes = self.database_path.stat().st_size
        self._assert_retained_storage(
            phase="legacy hydration queue migration reservation",
            reserve_bytes=database_bytes + _SQLITE_WRITE_FIXED_RESERVE,
        )
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("ALTER TABLE queue RENAME TO queue_legacy_input")
            connection.execute(self._queue_schema())
            connection.execute(
                "INSERT INTO queue(url, locale, input_json, state, hydration_status, record_json) "
                "SELECT url, locale, CASE WHEN state='complete' THEN NULL ELSE input_json END, "
                "state, hydration_status, record_json FROM queue_legacy_input"
            )
            connection.execute("DROP TABLE queue_legacy_input")
            connection.execute("CREATE INDEX queue_state_url ON queue(state, url, locale)")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def retained_storage_bytes(self) -> int:
        """Return retained corpus bytes governed by the launch storage ceiling."""

        return _regular_file_bytes(self.root / "corpus")

    def _spool_contract(self) -> dict[str, Any]:
        try:
            source_path = self.source_path.relative_to(self.root).as_posix()
        except ValueError:
            source_path = self.source_path.as_posix()
        return {
            "hydrator": type(self).__name__,
            "snapshot": self.label,
            "source_path": source_path,
        }

    def _spool_contract_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self._spool_contract())).hexdigest()

    def _spool_path(self, url: str, locale: str) -> Path:
        digest = hashlib.sha256(f"{url}\0{locale}".encode("utf-8")).hexdigest()
        return self.spool_root / f"{digest}.json"

    def _assert_spool_path_safe(self) -> None:
        temporary_root = self.root / ".tmp"
        spool_parent = self.spool_root.parent
        resolved_temporary = temporary_root.resolve()
        for path in (
            temporary_root,
            spool_parent,
            self.spool_root,
            self.spool_quarantine_root,
        ):
            if path.is_symlink():
                raise HydrationError("hydration spool path cannot contain a symbolic-link root")
            if path.exists() and not path.is_dir():
                raise HydrationError(f"hydration spool path component is not a directory: {path}")
            resolved = path.resolve(strict=False)
            if path != temporary_root and resolved_temporary not in resolved.parents:
                raise HydrationError("hydration spool path escapes the repository temporary root")

    def _ensure_private_spool_directory(self, path: Path) -> None:
        self._assert_spool_path_safe()
        if path not in {self.spool_root, self.spool_quarantine_root}:
            raise HydrationError(f"refusing undeclared hydration spool directory: {path}")
        temporary_root = self.root / ".tmp"
        for directory in (temporary_root, self.spool_root.parent, path):
            if directory.is_symlink():
                raise HydrationError("hydration spool path cannot contain a symbolic-link root")
            directory.mkdir(mode=0o700, exist_ok=True)
            if not directory.is_dir() or directory.is_symlink():
                raise HydrationError(f"hydration spool path is not a safe directory: {directory}")
        os.chmod(self.spool_root.parent, 0o700)
        os.chmod(path, 0o700)
        self._assert_spool_path_safe()

    @staticmethod
    def _write_private_atomic(path: Path, value: str) -> None:
        if not path.parent.is_dir() or path.parent.is_symlink():
            raise HydrationError(f"hydration spool parent is not a safe directory: {path.parent}")
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            directory = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _quarantine_spool(self, path: Path, reason: str) -> None:
        self._assert_spool_path_safe()
        if path.is_symlink():
            raise HydrationError(f"hydration spool document cannot be a symbolic link: {path}")
        stat = path.stat()
        quarantine = self.spool_quarantine_root
        self._ensure_private_spool_directory(quarantine)
        suffix = hashlib.sha256(
            f"{path.name}\0{stat.st_size}\0{stat.st_mtime_ns}\0{reason}".encode("utf-8")
        ).hexdigest()[:16]
        target = quarantine / f"{path.stem}-{suffix}.json"
        os.replace(path, target)
        self._write_private_atomic(target.with_suffix(".reason.txt"), reason + "\n")

    def _clear_spool(self) -> None:
        self._assert_spool_path_safe()
        for path in (self.spool_root, self.spool_quarantine_root):
            if path.exists():
                if path.is_symlink() or not path.is_dir():
                    raise HydrationError(f"refusing unsafe hydration spool cleanup path: {path}")
                shutil.rmtree(path)

    def _read_spool(
        self,
        url: str,
        locale: str,
        input_json: str,
    ) -> tuple[dict[str, Any], str, list[dict[str, Any]]] | None:
        self._assert_spool_path_safe()
        path = self._spool_path(url, locale)
        if not path.is_file():
            return None
        if path.is_symlink():
            raise HydrationError(f"hydration spool document cannot be a symbolic link: {path}")
        if path.stat().st_size > _MAX_SPOOL_DOCUMENT_BYTES:
            self._quarantine_spool(path, "spool document exceeded the bounded read envelope")
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._quarantine_spool(path, f"spool document could not be decoded: {type(exc).__name__}")
            return None
        if not isinstance(document, dict) or document.get("schema") != _SPOOL_SCHEMA:
            self._quarantine_spool(path, "spool document had an invalid schema")
            return None
        expected_payload_hash = document.get("payload_sha256")
        payload = {key: value for key, value in document.items() if key != "payload_sha256"}
        if (
            not isinstance(expected_payload_hash, str)
            or hashlib.sha256(canonical_json_bytes(payload)).hexdigest() != expected_payload_hash
        ):
            self._quarantine_spool(path, "spool document failed payload integrity validation")
            return None
        if (
            document.get("url") != url
            or document.get("locale") != locale
            or document.get("input_sha256")
            != hashlib.sha256(input_json.encode("utf-8")).hexdigest()
            or document.get("contract_sha256") != self._spool_contract_sha256()
        ):
            self._quarantine_spool(path, "spool document did not match the current input contract")
            return None
        record = document.get("record")
        status = document.get("status")
        linked = document.get("linked")
        if (
            not isinstance(record, dict)
            or not isinstance(status, str)
            or not isinstance(linked, list)
            or any(not isinstance(row, dict) for row in linked)
        ):
            self._quarantine_spool(path, "spool document had an invalid result payload")
            return None
        return record, status, linked

    def _write_spool(
        self,
        url: str,
        locale: str,
        input_json: str,
        result: tuple[dict[str, Any], str, list[dict[str, Any]]],
    ) -> None:
        self._assert_spool_path_safe()
        record, status, linked = result
        payload = {
            "schema": _SPOOL_SCHEMA,
            "snapshot": self.label,
            "url": url,
            "locale": locale,
            "input_sha256": hashlib.sha256(input_json.encode("utf-8")).hexdigest(),
            "contract_sha256": self._spool_contract_sha256(),
            "record": record,
            "status": status,
            "linked": linked,
        }
        document = {
            **payload,
            "payload_sha256": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        }
        encoded = pretty_json(document)
        if len(encoded.encode("utf-8")) > _MAX_SPOOL_DOCUMENT_BYTES:
            raise HydrationError("hydration result exceeds the durable transient spool envelope")
        self._ensure_private_spool_directory(self.spool_root)
        path = self._spool_path(url, locale)
        if path.is_symlink():
            raise HydrationError(f"hydration spool document cannot be a symbolic link: {path}")
        self._write_private_atomic(path, encoded)

    def _hydrate_and_spool(
        self,
        url: str,
        locale: str,
        input_json: str,
    ) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
        result = self._hydrate_one(input_json)
        self._write_spool(url, locale, input_json, result)
        return result

    def _discard_spool(self, url: str, locale: str) -> None:
        self._assert_spool_path_safe()
        path = self._spool_path(url, locale)
        if path.is_file() and not path.is_symlink():
            path.unlink()
        if self.spool_root.is_dir() and not any(self.spool_root.iterdir()):
            self.spool_root.rmdir()

    @staticmethod
    def _sqlite_write_reserve_bytes(payload_bytes: int, row_count: int) -> int:
        if payload_bytes < 0 or row_count < 0:
            raise HydrationError("SQLite write reservation inputs cannot be negative")
        return (
            (payload_bytes + row_count * _SQLITE_ROW_OVERHEAD_RESERVE)
            * _SQLITE_WRITE_SAFETY_FACTOR
            + _SQLITE_WRITE_FIXED_RESERVE
        )

    def _assert_sqlite_write_storage(
        self,
        *,
        phase: str,
        payload_bytes: int,
        row_count: int,
        additional_reserve_bytes: int = 0,
    ) -> int:
        return self._assert_retained_storage(
            phase=phase,
            reserve_bytes=(
                self._sqlite_write_reserve_bytes(payload_bytes, row_count)
                + additional_reserve_bytes
            ),
        )

    def _assert_retained_storage(
        self,
        *,
        phase: str,
        reserve_bytes: int = 0,
        use_authorised_ceiling: bool = False,
    ) -> int:
        observed = self.retained_storage_bytes()
        limit = self.storage_ceiling_bytes if use_authorised_ceiling else self.storage_operational_bytes
        if observed + reserve_bytes > limit:
            raise HydrationError(
                f"retained metadata storage would exceed the {phase} limit: "
                f"{observed}+{reserve_bytes}>{limit} bytes "
                f"(authorised ceiling {self.storage_ceiling_bytes})"
            )
        return observed

    def _checkpoint_wal_for_storage(self, connection: sqlite3.Connection, *, force: bool = False) -> None:
        wal = self.database_path.with_name(self.database_path.name + "-wal")
        if force or (
            wal.is_file()
            and self.retained_storage_bytes() > self.storage_operational_bytes * 9 // 10
        ):
            busy, _pages, _checkpointed = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if busy:
                raise HydrationError("hydration WAL could not be checkpointed for storage safety")

    def _storage_accounting(self, *, transient_export_peak_bytes: int) -> dict[str, Any]:
        observed = self.retained_storage_bytes()
        checkpoint = sum(
            path.stat().st_size
            for path in (
                self.database_path,
                self.database_path.with_name(self.database_path.name + "-wal"),
                self.database_path.with_name(self.database_path.name + "-shm"),
            )
            if path.is_file()
        )
        return {
            "scope": "retained files below corpus/; transient export/VACUUM files are deleted before completion",
            "authorised_ceiling_bytes": self.storage_ceiling_bytes,
            "operational_stop_bytes": self.storage_operational_bytes,
            "observed_retained_bytes_before_control_documents": observed,
            "control_document_reserve_bytes": _CONTROL_DOCUMENT_RESERVE,
            "conservative_accounted_bytes": observed + _CONTROL_DOCUMENT_RESERVE,
            "hydration_checkpoint_bytes": checkpoint,
            "transient_export_peak_bytes": transient_export_peak_bytes,
            "transient_export_state_deleted_after_verification": True,
            "within_authorised_ceiling": observed + _CONTROL_DOCUMENT_RESERVE
            <= self.storage_ceiling_bytes,
        }

    def _augment_export_documents(
        self,
        connection: sqlite3.Connection,
        reconciliation: dict[str, Any],
        manifest: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate and add subclass proof before any control document is committed."""

        return reconciliation, manifest

    def _export_write_admission(self, phase: str) -> Callable[[Path, int], None]:
        def check(_path: Path, pending_bytes: int) -> None:
            self._assert_retained_storage(
                phase=phase,
                reserve_bytes=pending_bytes + _CONTROL_DOCUMENT_RESERVE,
            )

        return check

    def _reset_candidate_working_set(self, connection: sqlite3.Connection) -> None:
        connection.execute("DROP TABLE IF EXISTS candidates")
        connection.execute(self._candidate_schema())
        connection.commit()
        self._checkpoint_wal_for_storage(connection, force=True)

    def _compact_checkpoint(self, connection: sqlite3.Connection) -> None:
        database_size = self.database_path.stat().st_size
        if shutil.disk_usage(self.cache_root).free < database_size + 128 * 1024 * 1024:
            raise HydrationError("insufficient temporary disk headroom to compact hydration checkpoint")
        connection.execute("VACUUM")
        self._checkpoint_wal_for_storage(connection, force=True)

    def _cleanup_failed_export(
        self,
        connection: sqlite3.Connection,
        created_outputs: list[Path],
        control_documents: dict[Path, str | None],
    ) -> list[str]:
        errors: list[str] = []
        try:
            self._reset_candidate_working_set(connection)
            self._compact_checkpoint(connection)
        except Exception as exc:  # cleanup must report every retained-state failure
            errors.append(f"candidate checkpoint cleanup failed: {exc}")
        for path in reversed(created_outputs):
            try:
                resolved = path.resolve()
                allowed_roots = (self.records_root.resolve(), (self.inventory_root / self.label).resolve())
                if path.is_symlink() or not any(root in resolved.parents for root in allowed_roots):
                    raise HydrationError(f"refusing unsafe export cleanup path: {path}")
                if path.exists():
                    shutil.rmtree(path)
            except Exception as exc:
                errors.append(f"output cleanup failed for {path}: {exc}")
        for path, previous in control_documents.items():
            try:
                if previous is None:
                    if path.exists():
                        path.unlink()
                else:
                    write_text_atomic(path, previous)
            except Exception as exc:
                errors.append(f"control-document rollback failed for {path}: {exc}")
        return errors

    def _cleanup_orphan_export_builds(self) -> None:
        """Remove only owned, contained shard build trees left by interruption."""

        corpus_path = self.root / "corpus"
        if corpus_path.is_symlink():
            raise HydrationError(f"retained metadata root cannot be a symbolic link: {corpus_path}")
        corpus_root = corpus_path.resolve(strict=False)
        for parent, name in (
            (self.records_root, "source-records"),
            (self.inventory_root / self.label, "hydrated-candidates"),
        ):
            try:
                relative_parent = parent.relative_to(corpus_path)
            except ValueError as exc:
                raise HydrationError(f"export parent escapes the retained corpus root: {parent}") from exc
            current = corpus_path
            for part in relative_parent.parts:
                if part in {"", ".", ".."}:
                    raise HydrationError(f"export parent has an unsafe path component: {parent}")
                current = current / part
                if current.is_symlink():
                    raise HydrationError(f"export parent cannot contain a symbolic link: {current}")
                if current.exists() and not current.is_dir():
                    raise HydrationError(f"export parent component is not a directory: {current}")
            resolved_parent = parent.resolve(strict=False)
            if corpus_root not in resolved_parent.parents:
                raise HydrationError(f"export parent escapes the retained corpus root: {parent}")
            if not parent.exists():
                continue
            if not parent.is_dir():
                raise HydrationError(f"export parent is not a directory: {parent}")
            for path in parent.glob(f".{name}.building-*"):
                if path.is_symlink() or not path.is_dir():
                    raise HydrationError(f"refusing unsafe orphan export build path: {path}")
                resolved = path.resolve()
                if resolved_parent not in resolved.parents:
                    raise HydrationError(f"orphan export build escapes its owned parent: {path}")
                shutil.rmtree(path)

    def _connect(self) -> sqlite3.Connection:
        # Opening SQLite can itself create the database, WAL, schema pages, and
        # recovery frames.  Reserve that bounded write before the connection
        # mutates retained state.
        self._assert_retained_storage(
            phase="hydration checkpoint connection reservation",
            reserve_bytes=_SQLITE_WRITE_FIXED_RESERVE,
        )
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
            """
        )
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='queue'"
        ).fetchone():
            connection.execute(self._queue_schema())
            connection.execute("CREATE INDEX queue_state_url ON queue(state, url, locale)")
        else:
            self._migrate_queue_schema(connection)
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidates'"
        ).fetchone():
            connection.execute(self._candidate_schema())
        connection.commit()
        # Recover and truncate any rolled-back tail left by an interrupted
        # preparation before new retained-storage accounting or requests.
        self._checkpoint_wal_for_storage(connection, force=True)
        return connection

    def prepare(self, connection: sqlite3.Connection) -> int:
        def source_signature() -> tuple[int, str]:
            digest = hashlib.sha256()
            count = 0
            for record in read_source_records(self.source_path):
                url, _locale = _record_key(record)
                record["canonical_url"] = url
                digest.update(canonical_json_bytes(record))
                count += 1
            return count, digest.hexdigest()

        count, source_digest = source_signature()
        previous = connection.execute("SELECT value FROM meta WHERE key='source_sha256'").fetchone()
        completed = int(
            connection.execute("SELECT COUNT(*) FROM queue WHERE state='complete'").fetchone()[0]
        )
        if completed and (previous is None or previous[0] != source_digest):
            raise HydrationError("source inventory changed after hydration began; use a new snapshot label")
        replace_unstarted = completed == 0
        source_changed = previous is not None and previous[0] != source_digest
        if replace_unstarted:
            checkpoint_bytes = self.database_path.stat().st_size
            self._assert_retained_storage(
                phase="stale hydration queue reset reservation",
                reserve_bytes=checkpoint_bytes + _SQLITE_WRITE_FIXED_RESERVE,
            )
        if source_changed:
            self._clear_spool()

        digest = hashlib.sha256()
        inserted_count = 0
        insert_batch: list[tuple[str, str, str]] = []
        insert_batch_bytes = 0

        def flush_insert_batch() -> None:
            nonlocal insert_batch_bytes
            if not insert_batch:
                return
            self._assert_sqlite_write_storage(
                phase="hydration preparation pre-write",
                payload_bytes=insert_batch_bytes,
                row_count=len(insert_batch),
            )
            connection.executemany(
                ("INSERT INTO " if replace_unstarted else "INSERT OR IGNORE INTO ")
                + "queue(url, locale, input_json, state) "
                "VALUES (?, ?, ?, 'pending')",
                insert_batch,
            )
            insert_batch.clear()
            insert_batch_bytes = 0

        connection.execute("BEGIN")
        try:
            if replace_unstarted:
                connection.execute("DELETE FROM queue")
                connection.execute("DELETE FROM candidates")
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rendered_selection'"
                ).fetchone():
                    connection.execute("DELETE FROM rendered_selection")
                connection.execute("DELETE FROM meta WHERE key LIKE 'rendered_selection_%'")
            for record in read_source_records(self.source_path):
                url, locale = _record_key(record)
                record["canonical_url"] = url
                payload = canonical_json_bytes(record)
                digest.update(payload)
                payload_text = payload.decode("utf-8")
                row_bytes = (
                    len(url.encode("utf-8"))
                    + len(locale.encode("utf-8"))
                    + len(payload)
                )
                if insert_batch and (
                    len(insert_batch) >= _SQLITE_BATCH_MAX_RECORDS
                    or insert_batch_bytes + row_bytes > _SQLITE_BATCH_MAX_PAYLOAD_BYTES
                ):
                    flush_insert_batch()
                insert_batch.append((url, locale, payload_text))
                insert_batch_bytes += row_bytes
                if (
                    len(insert_batch) >= _SQLITE_BATCH_MAX_RECORDS
                    or insert_batch_bytes >= _SQLITE_BATCH_MAX_PAYLOAD_BYTES
                ):
                    flush_insert_batch()
                inserted_count += 1
            flush_insert_batch()
            if inserted_count != count or digest.hexdigest() != source_digest:
                raise HydrationError("source inventory changed while hydration preparation was running")
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('source_sha256', ?)",
                (source_digest,),
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('source_records', ?)",
                (str(count),),
            )
            connection.commit()
            self._checkpoint_wal_for_storage(connection, force=True)
            self._assert_retained_storage(phase="prepared hydration checkpoint")
        except Exception:
            connection.rollback()
            self._checkpoint_wal_for_storage(connection, force=True)
            raise
        return count

    def _hydrate_one(self, input_json: str) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
        record = json.loads(input_json)
        url, _ = _record_key(record)
        if urlparse(url).netloc != "www.gov.uk":
            record["hydration_status"] = "external_boundary"
            return record, "external_boundary", []
        path = urlparse(url).path or "/"
        endpoint = CONTENT_API_ROOT + (quote(path, safe="/%:@-._~") if path != "/" else "")
        body, evidence = request_observation(endpoint, limiter=self.limiter, max_bytes=64 * 1024 * 1024)
        status = int(evidence.get("status") or 0)
        hydrated: dict[str, Any] | None = None
        reason = ""
        if evidence.get("ok") and not evidence.get("partial"):
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError("Content API response is not an object")
                hydrated = sanitise_content_api(payload, evidence)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                reason = f"invalid Content API metadata response: {exc}"
        elif evidence.get("partial"):
            reason = "Content API response exceeded the 64 MiB bounded metadata envelope"
        else:
            reason = str(evidence.get("error") or f"HTTP {status}")

        if hydrated is not None:
            result = merge_records(record, hydrated)
            result["coverage_disposition"] = (
                "redirect_only" if result.get("document_type") == "redirect" else "represented"
            )
            hydration_status = "content_api_represented"
        else:
            result = dict(record)
            constraints = list(result.get("constraints") or [])
            constraints.append(_constraint(record, status, reason, evidence))
            result["constraints"] = constraints
            # Search/sitemap/structured-link observations still represent the
            # route. A failed enrichment is a constraint, not a false claim
            # that the already observed route vanished from the accounting.
            result["coverage_disposition"] = str(result.get("coverage_disposition") or "represented")
            result["evidence_url"] = evidence.get("requested_url") or endpoint
            result["evidence_sha256"] = evidence.get("sha256")
            result["evidence_locator"] = "/"
            result["retrieved_at"] = evidence.get("retrieved_at")
            hydration_status = (
                "content_api_unavailable" if status in {404, 410} else "content_api_exception"
            )
        result["hydration_status"] = hydration_status
        result["content_api_status"] = status
        result["content_api_final_url"] = evidence.get("final_url")
        result["content_api_attempts"] = int(evidence.get("acquisition_attempt") or 1)
        linked = list(_linked_records(result))
        return result, hydration_status, linked

    def run(self, *, request_limit: int | None = None) -> dict[str, Any]:
        connection = self._connect()
        try:
            self._assert_retained_storage(phase="hydration start")
            source_count = self.prepare(connection)
            processed_this_run = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
                while request_limit is None or processed_this_run < request_limit:
                    self._assert_retained_storage(
                        phase="pre-request hydration checkpoint",
                        reserve_bytes=_SQLITE_WRITE_FIXED_RESERVE,
                    )
                    remaining = self.batch_size
                    if request_limit is not None:
                        remaining = min(remaining, request_limit - processed_this_run)
                    rows = connection.execute(
                        "SELECT url, locale, input_json FROM queue WHERE state='pending' ORDER BY url, locale LIMIT ?",
                        (remaining,),
                    ).fetchall()
                    if not rows:
                        break
                    completed_rows: list[
                        tuple[str, str, dict[str, Any], str, list[dict[str, Any]]]
                    ] = []
                    missing_rows: list[tuple[str, str, str]] = []
                    for url, locale, input_json in rows:
                        cached = self._read_spool(str(url), str(locale), str(input_json))
                        if cached is None:
                            missing_rows.append((str(url), str(locale), str(input_json)))
                        else:
                            record, status, linked = cached
                            completed_rows.append((str(url), str(locale), record, status, linked))

                    if missing_rows:
                        free = shutil.disk_usage(self.root).free
                        spool_capacity = max(
                            0,
                            (free - _TRANSIENT_SPOOL_MINIMUM_FREE)
                            // _TRANSIENT_SPOOL_RESERVE_PER_REQUEST,
                        )
                        admitted = missing_rows[: min(spool_capacity, self.workers)]
                        if not admitted and not completed_rows:
                            raise HydrationError(
                                "insufficient temporary disk headroom to durably spool a hydration request"
                            )
                    else:
                        admitted = []
                    futures = {
                        pool.submit(self._hydrate_and_spool, url, locale, input_json): (url, locale)
                        for url, locale, input_json in admitted
                    }
                    future_errors: list[Exception] = []
                    for future in concurrent.futures.as_completed(futures):
                        url, locale = futures[future]
                        try:
                            record, status, linked = future.result()
                        except Exception as exc:
                            future_errors.append(exc)
                        else:
                            completed_rows.append((url, locale, record, status, linked))
                    if future_errors:
                        # Every successful sibling is already fsync'd in the
                        # spool.  Observe all futures before raising so no
                        # completed request result is abandoned in memory.
                        raise future_errors[0]
                    retained_payload_bytes = sum(
                        len(canonical_json_bytes(record))
                        + sum(len(canonical_json_bytes(linked_record)) for linked_record in linked)
                        for _url, _locale, record, _status, linked in completed_rows
                    )
                    self._assert_sqlite_write_storage(
                        phase="hydration batch reservation",
                        payload_bytes=retained_payload_bytes,
                        row_count=sum(
                            1 + len(linked)
                            for _url, _locale, _record, _status, linked in completed_rows
                        ),
                    )
                    connection.execute("BEGIN")
                    try:
                        for url, locale, record, status, linked in sorted(completed_rows):
                            connection.execute(
                                "UPDATE queue SET state='complete', hydration_status=?, record_json=?, "
                                "input_json=NULL "
                                "WHERE url=? AND locale=?",
                                (status, canonical_json_bytes(record).decode("utf-8"), url, locale),
                            )
                            for linked_record in linked:
                                linked_url, linked_locale = _record_key(linked_record)
                                connection.execute(
                                    "INSERT OR IGNORE INTO queue(url, locale, input_json, state) "
                                    "VALUES (?, ?, ?, 'pending')",
                                    (
                                        linked_url,
                                        linked_locale,
                                        canonical_json_bytes(linked_record).decode("utf-8"),
                                    ),
                                )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    for url, locale, _record, _status, _linked in completed_rows:
                        self._discard_spool(url, locale)
                    self._checkpoint_wal_for_storage(connection)
                    self._assert_retained_storage(phase="committed hydration checkpoint")
                    processed_this_run += len(completed_rows)

            pending = int(connection.execute("SELECT COUNT(*) FROM queue WHERE state='pending'").fetchone()[0])
            complete = int(connection.execute("SELECT COUNT(*) FROM queue WHERE state='complete'").fetchone()[0])
            status_counts = {
                str(status): int(count)
                for status, count in connection.execute(
                    "SELECT hydration_status, COUNT(*) FROM queue WHERE state='complete' GROUP BY hydration_status"
                )
            }
            return {
                "schema_version": 1,
                "snapshot": self.label,
                "source_records": source_count,
                "processed_this_run": processed_this_run,
                "queue_records": complete + pending,
                "complete": complete,
                "pending": pending,
                "closed": pending == 0,
                "sampled": request_limit is not None,
                "status_counts": dict(sorted(status_counts.items())),
            }
        finally:
            connection.close()

    def export(self, enumeration_reconciliation: Path | None = None) -> dict[str, Any]:
        # A process kill can bypass the writer's exception cleanup.  Reclaim
        # only its fixed-prefix, contained build directories before opening
        # SQLite or applying the next storage reservation.
        self._cleanup_orphan_export_builds()
        reconciliation_target = self.reconciliation_root / f"{self.label}-hydrated.json"
        manifest_target = self.records_root / "manifest.json"
        control_documents = {
            path: path.read_text(encoding="utf-8") if path.is_file() else None
            for path in (reconciliation_target, manifest_target)
        }
        created_outputs: list[Path] = []
        connection = self._connect()
        try:
            pending = int(connection.execute("SELECT COUNT(*) FROM queue WHERE state='pending'").fetchone()[0])
            if pending:
                raise HydrationError(f"hydration closure has {pending} pending records")
            self.records_root.mkdir(parents=True, exist_ok=True)
            self.inventory_root.mkdir(parents=True, exist_ok=True)
            self.reconciliation_root.mkdir(parents=True, exist_ok=True)

            def records() -> Iterator[dict[str, Any]]:
                cursor = connection.execute("SELECT record_json FROM queue ORDER BY url, locale")
                for (record_json,) in cursor:
                    yield json.loads(record_json)

            self._assert_retained_storage(
                phase="source shard export reservation",
                reserve_bytes=_EXPORT_SHARD_RESERVE,
            )
            existing_source_outputs = {
                path.resolve() for path in self.records_root.glob("source-records-*") if path.is_dir()
            }
            source_output = write_jsonl_gzip_shards(
                self.records_root,
                "source-records",
                records(),
                before_write=self._export_write_admission("source shard export pre-write"),
            )
            source_target = Path(source_output["root"]) / "index.json"
            if Path(source_output["root"]).resolve() not in existing_source_outputs:
                created_outputs.append(Path(source_output["root"]))
            record_count = int(source_output["records"])
            record_digest = str(source_output["canonical_sha256"])
            self._verify_shard_export(source_target, record_count, record_digest)
            self._assert_retained_storage(
                phase="verified source shard export",
                reserve_bytes=_EXPORT_SHARD_RESERVE,
            )

            self._reset_candidate_working_set(connection)
            self._assert_retained_storage(
                phase="candidate materialization reservation",
                reserve_bytes=_EXPORT_SHARD_RESERVE,
            )
            candidate_batch: list[dict[str, Any]] = []
            candidate_batch_bytes = 0

            def merge_candidate(
                current: dict[str, Any],
                incoming: dict[str, Any],
            ) -> dict[str, Any]:
                current["source_memberships"] = sorted(
                    set(current.get("source_memberships", []))
                    | set(incoming.get("source_memberships", []))
                )
                current["evidence_ids"] = sorted(
                    set(current.get("evidence_ids", []))
                    | set(incoming.get("evidence_ids", []))
                )
                return current

            def flush_candidate_batch() -> None:
                nonlocal candidate_batch_bytes
                if not candidate_batch:
                    return
                combined: dict[str, dict[str, Any]] = {}
                for candidate in candidate_batch:
                    key = str(candidate["candidate_key"])
                    if key in combined:
                        combined[key] = merge_candidate(combined[key], candidate)
                    else:
                        combined[key] = candidate
                prepared: list[tuple[str, str, str, str, str]] = []
                payload_bytes = 0
                for key in sorted(combined):
                    candidate = combined[key]
                    existing = connection.execute(
                        "SELECT candidate_json FROM candidates WHERE candidate_key=?", (key,)
                    ).fetchone()
                    if existing:
                        candidate = merge_candidate(json.loads(existing[0]), candidate)
                    entity_class = str(candidate["entity_class"])
                    native_id = str(candidate["source_native_id"])
                    locale = str(candidate.get("locale") or "en")
                    candidate_json = canonical_json_bytes(candidate).decode("utf-8")
                    prepared.append((key, entity_class, native_id, locale, candidate_json))
                    payload_bytes += sum(
                        len(value.encode("utf-8"))
                        for value in (key, entity_class, native_id, locale, candidate_json)
                    )
                self._assert_sqlite_write_storage(
                    phase="candidate materialization pre-write",
                    payload_bytes=payload_bytes,
                    row_count=len(prepared),
                    additional_reserve_bytes=_EXPORT_SHARD_RESERVE,
                )
                try:
                    connection.executemany(
                        "INSERT OR REPLACE INTO candidates"
                        "(candidate_key, entity_class, source_native_id, locale, candidate_json) "
                        "VALUES (?, ?, ?, ?, ?)",
                        prepared,
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                self._checkpoint_wal_for_storage(connection)
                self._assert_retained_storage(
                    phase="candidate materialization checkpoint",
                    reserve_bytes=_EXPORT_SHARD_RESERVE,
                )
                candidate_batch.clear()
                candidate_batch_bytes = 0

            for record in records():
                for candidate in expand_candidate_records(record, self.label):
                    encoded_bytes = len(canonical_json_bytes(candidate))
                    if candidate_batch and (
                        len(candidate_batch) >= _SQLITE_BATCH_MAX_RECORDS
                        or candidate_batch_bytes + encoded_bytes
                        > _SQLITE_BATCH_MAX_PAYLOAD_BYTES
                    ):
                        flush_candidate_batch()
                    candidate_batch.append(candidate)
                    candidate_batch_bytes += encoded_bytes
                    if (
                        len(candidate_batch) >= _SQLITE_BATCH_MAX_RECORDS
                        or candidate_batch_bytes >= _SQLITE_BATCH_MAX_PAYLOAD_BYTES
                    ):
                        flush_candidate_batch()
            flush_candidate_batch()
            self._assert_retained_storage(
                phase="candidate materialization completion",
                reserve_bytes=_EXPORT_SHARD_RESERVE,
            )

            previous_identity: tuple[str, str, str] | None = None
            represented_key = ""
            alias_updates: list[tuple[str, str]] = []
            alias_update_bytes = 0

            def flush_alias_updates() -> None:
                nonlocal alias_update_bytes
                if not alias_updates:
                    return
                self._assert_sqlite_write_storage(
                    phase="candidate alias pre-write",
                    payload_bytes=alias_update_bytes,
                    row_count=len(alias_updates),
                    additional_reserve_bytes=_EXPORT_SHARD_RESERVE,
                )
                try:
                    connection.executemany(
                        "UPDATE candidates SET candidate_json=? WHERE candidate_key=?",
                        alias_updates,
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                self._checkpoint_wal_for_storage(connection)
                self._assert_retained_storage(
                    phase="candidate alias checkpoint",
                    reserve_bytes=_EXPORT_SHARD_RESERVE,
                )
                alias_updates.clear()
                alias_update_bytes = 0

            cursor = connection.execute(
                "SELECT candidate_key, entity_class, source_native_id, locale, candidate_json "
                "FROM candidates WHERE entity_class IN ('content_identity', 'document', 'edition') "
                "ORDER BY entity_class, source_native_id, locale, candidate_key"
            )
            for key, entity_class, native_id, locale, candidate_json in cursor:
                identity = (entity_class, native_id, locale)
                if identity != previous_identity:
                    previous_identity = identity
                    represented_key = key
                    continue
                candidate = json.loads(candidate_json)
                candidate["coverage_disposition"] = "alias_of_represented"
                candidate["disposition_target"] = represented_key
                encoded = canonical_json_bytes(candidate).decode("utf-8")
                row_bytes = len(encoded.encode("utf-8")) + len(str(key).encode("utf-8"))
                if alias_updates and (
                    len(alias_updates) >= _SQLITE_BATCH_MAX_RECORDS
                    or alias_update_bytes + row_bytes > _SQLITE_BATCH_MAX_PAYLOAD_BYTES
                ):
                    flush_alias_updates()
                alias_updates.append((encoded, str(key)))
                alias_update_bytes += row_bytes
                if (
                    len(alias_updates) >= _SQLITE_BATCH_MAX_RECORDS
                    or alias_update_bytes >= _SQLITE_BATCH_MAX_PAYLOAD_BYTES
                ):
                    flush_alias_updates()
            flush_alias_updates()
            self._assert_retained_storage(
                phase="candidate alias completion",
                reserve_bytes=_EXPORT_SHARD_RESERVE,
            )

            def candidates() -> Iterator[dict[str, Any]]:
                for (candidate_json,) in connection.execute(
                    "SELECT candidate_json FROM candidates ORDER BY candidate_key"
                ):
                    yield json.loads(candidate_json)

            self._assert_retained_storage(
                phase="candidate shard export reservation",
                reserve_bytes=_EXPORT_SHARD_RESERVE,
            )
            candidate_parent = self.inventory_root / self.label
            existing_candidate_outputs = {
                path.resolve()
                for path in candidate_parent.glob("hydrated-candidates-*")
                if path.is_dir()
            }
            candidate_output = write_jsonl_gzip_shards(
                candidate_parent,
                "hydrated-candidates",
                candidates(),
                before_write=self._export_write_admission("candidate shard export pre-write"),
            )
            candidate_target = Path(candidate_output["root"]) / "index.json"
            if Path(candidate_output["root"]).resolve() not in existing_candidate_outputs:
                created_outputs.append(Path(candidate_output["root"]))
            candidate_count = int(candidate_output["records"])
            candidate_digest = str(candidate_output["canonical_sha256"])
            self._verify_shard_export(candidate_target, candidate_count, candidate_digest)
            self._assert_retained_storage(
                phase="verified candidate shard export",
                reserve_bytes=_CONTROL_DOCUMENT_RESERVE,
            )
            disposition_counts: collections.Counter[str] = collections.Counter()
            entity_counts: collections.Counter[str] = collections.Counter()
            for candidate in candidates():
                disposition_counts[str(candidate["coverage_disposition"])] += 1
                entity_counts[str(candidate["entity_class"])] += 1
            valid_dispositions = {
                "represented",
                "alias_of_represented",
                "redirect_only",
                "tombstone_only",
                "exceptioned",
            }
            accounted = sum(
                count for disposition, count in disposition_counts.items() if disposition in valid_dispositions
            )
            hydration_status = {
                str(status): int(count)
                for status, count in connection.execute(
                    "SELECT hydration_status, COUNT(*) FROM queue GROUP BY hydration_status"
                )
            }
            enumeration: dict[str, Any] = {}
            if enumeration_reconciliation is not None:
                enumeration = json.loads(enumeration_reconciliation.read_text(encoding="utf-8"))
            reconciliation = {
                **enumeration,
                "schema_version": 1,
                "snapshot": self.label,
                "hydrated": True,
                "sampled": bool(enumeration.get("sampled", False)),
                "expected_candidate_keys": candidate_count,
                "publication_records": record_count,
                "represented": disposition_counts["represented"],
                "alias_of_represented": disposition_counts["alias_of_represented"],
                "redirect_only": disposition_counts["redirect_only"],
                "tombstone_only": disposition_counts["tombstone_only"],
                "exceptioned": disposition_counts["exceptioned"],
                "unexplained_omissions": candidate_count - accounted,
                "entity_class_counts": dict(sorted(entity_counts.items())),
                "hydration_proof": {
                    "queue_records": record_count,
                    "pending": 0,
                    "closed": True,
                    "status_counts": dict(sorted(hydration_status.items())),
                },
                "hydrated_records_path": source_target.relative_to(self.root).as_posix(),
                "hydrated_record_shards": [
                    (Path(source_output["root"]) / row["path"]).relative_to(self.root).as_posix()
                    for row in source_output["shards"]
                ],
                "hydrated_records_canonical_sha256": record_digest,
                "candidate_ledger_path": candidate_target.relative_to(self.root).as_posix(),
                "candidate_ledger_shards": [
                    (Path(candidate_output["root"]) / row["path"]).relative_to(self.root).as_posix()
                    for row in candidate_output["shards"]
                ],
                "candidate_ledger_canonical_sha256": candidate_digest,
            }
            if reconciliation["unexplained_omissions"] != 0:
                raise HydrationError("hydrated candidate reconciliation did not close")

            # Candidate rows are an export-only working index.  The immutable,
            # content-addressed candidate shards above are verified before the
            # transient table is dropped and the checkpoint is compacted.
            transient_export_peak_bytes = self.retained_storage_bytes()
            self._reset_candidate_working_set(connection)
            self._compact_checkpoint(connection)
            self._assert_retained_storage(
                phase="post-export retained corpus",
                reserve_bytes=_CONTROL_DOCUMENT_RESERVE,
                use_authorised_ceiling=True,
            )
            reconciliation["storage_accounting"] = self._storage_accounting(
                transient_export_peak_bytes=transient_export_peak_bytes
            )
            manifest = {
                "schema_version": 1,
                "snapshot": self.label,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "metadata_only": True,
                "complete_page_bodies_retained": False,
                "source_records": record_count,
                "source_records_sha256": record_digest,
                "source_record_manifest": source_target.relative_to(self.root).as_posix(),
                "source_record_shards": reconciliation["hydrated_record_shards"],
                "candidate_records": candidate_count,
                "candidate_records_sha256": candidate_digest,
                "candidate_record_manifest": candidate_target.relative_to(self.root).as_posix(),
                "candidate_record_shards": reconciliation["candidate_ledger_shards"],
                "reconciliation": reconciliation_target.relative_to(self.root).as_posix(),
                "storage_accounting": reconciliation["storage_accounting"],
            }
            reconciliation, manifest = self._augment_export_documents(
                connection,
                reconciliation,
                manifest,
            )
            if not isinstance(reconciliation, dict) or not isinstance(manifest, dict):
                raise HydrationError("hydration export augmentation returned invalid control documents")
            reconciliation_text = pretty_json(reconciliation)
            manifest_text = pretty_json(manifest)
            control_bytes = len(reconciliation_text.encode("utf-8")) + len(
                manifest_text.encode("utf-8")
            )
            self._assert_retained_storage(
                phase="hydration control-document reservation",
                reserve_bytes=control_bytes * 2 + _CONTROL_DOCUMENT_RESERVE,
            )
            write_text_atomic(reconciliation_target, reconciliation_text)
            write_text_atomic(manifest_target, manifest_text)
            persisted_reconciliation = json.loads(reconciliation_target.read_text(encoding="utf-8"))
            persisted_manifest = json.loads(
                manifest_target.read_text(encoding="utf-8")
            )
            if (
                persisted_reconciliation.get("hydrated_records_canonical_sha256") != record_digest
                or persisted_reconciliation.get("candidate_ledger_canonical_sha256")
                != candidate_digest
                or persisted_manifest.get("source_records_sha256") != record_digest
                or persisted_manifest.get("candidate_records_sha256") != candidate_digest
            ):
                raise HydrationError("durable hydration control documents failed verification")
            self._assert_retained_storage(
                phase="completed hydration export",
                use_authorised_ceiling=True,
            )
            return reconciliation
        except Exception as exc:
            cleanup_errors = self._cleanup_failed_export(
                connection,
                created_outputs,
                control_documents,
            )
            if cleanup_errors:
                raise HydrationError(
                    f"hydration export failed and rollback was incomplete: {'; '.join(cleanup_errors)}"
                ) from exc
            raise
        finally:
            connection.close()

    @staticmethod
    def _verify_shard_export(path: Path, expected_count: int, expected_digest: str) -> None:
        count = 0
        digest = hashlib.sha256()
        for record in read_source_records(path):
            digest.update(canonical_json_bytes(record))
            count += 1
        if count != expected_count or digest.hexdigest() != expected_digest:
            raise HydrationError(f"durable shard export failed verification: {path}")
