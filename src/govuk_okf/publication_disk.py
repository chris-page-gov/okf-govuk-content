"""Disk-backed, bounded-memory compiler for full GOV.UK corpus publications.

The fixture compiler in :mod:`govuk_okf.publication` deliberately remains a
small, easy-to-review list implementation.  This module preserves its output
contract while spilling corpus entities and inverted-index rows to SQLite.
Only bounded output chunks or one record/posting list are materialised in
Python at a time.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, BinaryIO, overload
from urllib.parse import urlparse

from .publication import (
    DATA_PLANE_BUDGETS,
    DATA_PLANE_SCHEMA_VERSION,
    FIELD_MASKS,
    FIELD_WEIGHTS,
    LINK_KINDS,
    MAX_POSTINGS,
    ORGANISATION_LINKS,
    RECORD_CHUNK_SIZE,
    RESULT_CHUNK_SIZE,
    ROOT,
    PublicationError,
    _emit_publication,
    data_plane_shard_metadata,
    dataset_name,
    link_dataset_route,
    link_url,
    normalise_source_record,
    publisher_from_link,
    relationship,
    search_shard,
    shard_manifest_sha256,
    tokenise,
)
from .sharded_jsonl import ShardedJsonlError, iter_jsonl_records
from .util import adjacency_bucket, canonical_json_bytes, pretty_json, slugify

MAX_SOURCE_LINE_BYTES = 64 * 1024 * 1024
SQLITE_CACHE_KIB = 24 * 1024
INGEST_COMMIT_INTERVAL = 10_000
FINALISE_BATCH_SIZE = 1_000
ALLOWED_DISPOSITIONS = {
    "represented",
    "alias_of_represented",
    "redirect_only",
    "tombstone_only",
    "exceptioned",
}


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_value(text: str) -> dict[str, Any]:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise PublicationError("disk compiler invariant failed: stored row is not an object")
    return value


def source_files(source: Path) -> list[Path]:
    """Resolve one source JSONL file or an unambiguous deterministic shard set."""
    source = source.resolve()
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise PublicationError(f"source path does not exist: {source}")

    direct = [source / "source-records.jsonl", source / "source-records.jsonl.gz"]
    direct = [path for path in direct if path.is_file()]
    if len(direct) == 1:
        return direct
    if len(direct) > 1:
        raise PublicationError(
            f"source directory has ambiguous plain and gzip source-records files: {source}"
        )

    candidates = [
        path
        for path in source.rglob("*")
        if path.is_file()
        and not any(part.startswith(".") for part in path.relative_to(source).parts)
        and (path.name.endswith(".jsonl") or path.name.endswith(".jsonl.gz"))
    ]
    if not candidates:
        raise PublicationError(f"source directory contains no JSONL files: {source}")

    explicit_shards = [
        path
        for path in candidates
        if path.name.startswith(("records-", "part-"))
    ]
    if explicit_shards:
        selected = explicit_shards
    else:
        named_sources = [
            path
            for path in candidates
            if path.name.endswith("-source-records.jsonl")
            or path.name.endswith("-source-records.jsonl.gz")
        ]
        if len(named_sources) > 1:
            raise PublicationError(
                "source directory contains multiple snapshot source-records files; "
                "select one snapshot or a records-/part- shard directory"
            )
        selected = named_sources or candidates
    return sorted(selected, key=lambda path: path.relative_to(source).as_posix())


def _open_binary(path: Path) -> BinaryIO:
    if path.name.endswith(".gz"):
        return gzip.open(path, "rb")
    return path.open("rb")


def iter_source_records(source: Path) -> Iterator[dict[str, Any]]:
    """Yield bounded JSONL records from a file or deterministic directory."""
    resolved = source.resolve()
    if (resolved.is_dir() and (resolved / "index.json").is_file()) or (
        resolved.is_file() and resolved.suffix == ".json"
    ):
        try:
            yield from iter_jsonl_records(resolved)
        except ShardedJsonlError as exc:
            raise PublicationError(str(exc)) from exc
        return
    for path in source_files(source):
        with _open_binary(path) as stream:
            number = 0
            while True:
                payload = stream.readline(MAX_SOURCE_LINE_BYTES + 1)
                if not payload:
                    break
                number += 1
                if len(payload) > MAX_SOURCE_LINE_BYTES:
                    raise PublicationError(
                        f"{path}:{number}: source record exceeds {MAX_SOURCE_LINE_BYTES} bytes"
                    )
                if not payload.strip():
                    continue
                try:
                    value = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise PublicationError(f"{path}:{number}: invalid UTF-8 JSON: {exc}") from exc
                if not isinstance(value, dict):
                    raise PublicationError(f"{path}:{number}: record must be an object")
                yield value


class SQLiteJSONSequence(Sequence[dict[str, Any]]):
    """Repeatable JSON row sequence addressed by a dense output index."""

    def __init__(self, connection: sqlite3.Connection, table: str) -> None:
        if table not in {"datasets", "publishers", "resources", "relationships"}:
            raise ValueError(f"unsupported sequence table: {table}")
        self.connection = connection
        self.table = table
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        self.length = int(row[0])

    def __len__(self) -> int:
        return self.length

    def __iter__(self) -> Iterator[dict[str, Any]]:
        cursor = self.connection.execute(
            f"SELECT row_json FROM {self.table} ORDER BY output_index"
        )
        for (row_json,) in cursor:
            yield _json_value(str(row_json))

    @overload
    def __getitem__(self, index: int) -> dict[str, Any]: ...

    @overload
    def __getitem__(self, index: slice) -> list[dict[str, Any]]: ...

    def __getitem__(self, index: int | slice) -> dict[str, Any] | list[dict[str, Any]]:
        if isinstance(index, slice):
            start, stop, step = index.indices(self.length)
            if step != 1:
                return [self[position] for position in range(start, stop, step)]
            cursor = self.connection.execute(
                f"SELECT row_json FROM {self.table} "
                "WHERE output_index >= ? AND output_index < ? ORDER BY output_index",
                (start, stop),
            )
            return [_json_value(str(row_json)) for (row_json,) in cursor]
        position = index + self.length if index < 0 else index
        if position < 0 or position >= self.length:
            raise IndexError(index)
        row = self.connection.execute(
            f"SELECT row_json FROM {self.table} WHERE output_index = ?", (position,)
        ).fetchone()
        if row is None:
            raise IndexError(index)
        return _json_value(str(row[0]))


def _indented(rendered: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line for line in rendered.splitlines())


def _pretty_entry(key: str, value: object, indent: int) -> str:
    rendered_key = json.dumps(key, ensure_ascii=False)
    rendered_value = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    lines = rendered_value.splitlines()
    prefix = " " * indent
    result = prefix + rendered_key + ": " + lines[0]
    if len(lines) > 1:
        result += "\n" + "\n".join(prefix + line for line in lines[1:])
    return result


def _write_pretty_sequence(path: Path, values: Iterable[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    iterator = iter(values)
    try:
        first = next(iterator)
    except StopIteration:
        path.write_text("[]\n", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("[\n")
        stream.write(_indented(json.dumps(first, ensure_ascii=False, indent=2, sort_keys=True), 2))
        for value in iterator:
            stream.write(",\n")
            stream.write(_indented(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), 2))
        stream.write("\n]\n")


def _write_pretty_mapping(path: Path, entries: Iterable[tuple[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    iterator = iter(entries)
    try:
        first_key, first_value = next(iterator)
    except StopIteration:
        path.write_text("{}\n", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("{\n")
        stream.write(_pretty_entry(first_key, first_value, 2))
        for key, value in iterator:
            stream.write(",\n")
            stream.write(_pretty_entry(key, value, 2))
        stream.write("\n}\n")


class _CanonicalGzipWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.raw = path.open("wb")
        self.compressed = gzip.GzipFile(
            filename="", mode="wb", fileobj=self.raw, mtime=0, compresslevel=9
        )

    def write(self, value: str) -> None:
        self.compressed.write(value.encode("utf-8"))

    def close(self) -> None:
        self.compressed.close()
        self.raw.close()

    def __enter__(self) -> _CanonicalGzipWriter:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class DiskCompiler:
    """Compile source envelopes to a disk-resident canonical intermediate."""

    def __init__(self, database: Path, observed_at: str, snapshot_id: str) -> None:
        self.database = database
        self.observed_at = observed_at
        self.snapshot_id = snapshot_id
        self.connection = sqlite3.connect(database)
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute("PRAGMA temp_store=FILE")
        self.connection.execute(f"PRAGMA cache_size=-{SQLITE_CACHE_KIB}")
        self.connection.execute("PRAGMA mmap_size=0")
        self.connection.execute("PRAGMA locking_mode=EXCLUSIVE")
        self._create_schema()
        self.source_count = 0
        self.invalid_dispositions = 0
        self.next_dataset_seq = 0

    def close(self) -> None:
        self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE source_records (
              seq INTEGER PRIMARY KEY,
              raw_json TEXT NOT NULL,
              disposition TEXT
            );
            CREATE TABLE datasets (
              seq INTEGER PRIMARY KEY,
              output_index INTEGER UNIQUE,
              open TEXT NOT NULL,
              url TEXT NOT NULL,
              row_json TEXT NOT NULL
            );
            CREATE TABLE route_lookup (url TEXT PRIMARY KEY, open TEXT NOT NULL);
            CREATE TABLE route_exists (open TEXT PRIMARY KEY);
            CREATE TABLE publishers (
              open TEXT PRIMARY KEY,
              output_index INTEGER UNIQUE,
              row_json TEXT NOT NULL
            );
            CREATE TABLE resources (
              open TEXT PRIMARY KEY,
              output_index INTEGER UNIQUE,
              row_json TEXT NOT NULL
            );
            CREATE TABLE relationships (
              assertion_id TEXT PRIMARY KEY,
              output_index INTEGER UNIQUE,
              source TEXT NOT NULL,
              kind TEXT NOT NULL,
              target TEXT NOT NULL,
              row_json TEXT NOT NULL
            );
            CREATE TABLE identifiers (
              bucket TEXT NOT NULL,
              identifier TEXT NOT NULL,
              kind TEXT NOT NULL,
              ordinal INTEGER NOT NULL,
              open TEXT NOT NULL,
              PRIMARY KEY (identifier, kind, ordinal, open)
            ) WITHOUT ROWID;
            CREATE TABLE adjacency (
              bucket TEXT NOT NULL,
              route TEXT NOT NULL,
              kind TEXT NOT NULL,
              source TEXT NOT NULL,
              target TEXT NOT NULL,
              assertion_id TEXT NOT NULL,
              row_json TEXT NOT NULL,
              PRIMARY KEY (route, assertion_id)
            ) WITHOUT ROWID;
            CREATE TABLE search_postings (
              token TEXT NOT NULL,
              score INTEGER NOT NULL,
              ordinal INTEGER NOT NULL,
              mask INTEGER NOT NULL,
              shard TEXT NOT NULL,
              PRIMARY KEY (token, score DESC, ordinal)
            ) WITHOUT ROWID;
            CREATE TABLE token_stats (
              shard TEXT NOT NULL,
              token TEXT NOT NULL,
              df INTEGER NOT NULL,
              PRIMARY KEY (shard, token)
            ) WITHOUT ROWID;
            CREATE TABLE prefixes (
              shard TEXT NOT NULL,
              prefix TEXT NOT NULL,
              token TEXT NOT NULL,
              df INTEGER NOT NULL,
              PRIMARY KEY (shard, prefix, token)
            ) WITHOUT ROWID;
            """
        )

    def ingest(self, source: Path) -> None:
        for seq, record in enumerate(iter_source_records(source)):
            normalised = normalise_source_record(record, self.observed_at)
            normalised.pop("_source")
            route = str(normalised["open"])
            url = str(normalised["url"])
            disposition = record.get("coverage_disposition")
            self.connection.execute(
                "INSERT INTO source_records(seq, raw_json, disposition) VALUES (?, ?, ?)",
                (seq, _json_text(record), disposition),
            )
            self.connection.execute(
                "INSERT INTO datasets(seq, open, url, row_json) VALUES (?, ?, ?, ?)",
                (seq, route, url, _json_text(normalised)),
            )
            self.connection.execute(
                "INSERT INTO route_lookup(url, open) VALUES (?, ?) "
                "ON CONFLICT(url) DO UPDATE SET open=excluded.open",
                (url.rstrip("/") or "/", route),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO route_exists(open) VALUES (?)", (route,)
            )
            self.source_count = seq + 1
            if disposition not in ALLOWED_DISPOSITIONS:
                self.invalid_dispositions += 1
            if self.source_count % INGEST_COMMIT_INTERVAL == 0:
                self.connection.commit()
        self.next_dataset_seq = self.source_count
        self.connection.commit()

    def _route_for_url(self, url: str) -> str | None:
        row = self.connection.execute(
            "SELECT open FROM route_lookup WHERE url = ?", (url.rstrip("/") or "/",)
        ).fetchone()
        return str(row[0]) if row else None

    def _route_is_present(self, route: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM route_exists WHERE open = ?", (route,)
            ).fetchone()
            is not None
        )

    def _insert_stub(self, target_route: str, source: dict[str, Any]) -> None:
        if self._route_is_present(target_route):
            return
        stub = normalise_source_record(source, self.observed_at)
        stub.pop("_source")
        seq = self.next_dataset_seq
        self.next_dataset_seq += 1
        self.connection.execute(
            "INSERT INTO datasets(seq, open, url, row_json) VALUES (?, ?, ?, ?)",
            (seq, str(stub["open"]), str(stub["url"]), _json_text(stub)),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO route_exists(open) VALUES (?)", (target_route,)
        )
        self.connection.execute(
            "INSERT INTO route_lookup(url, open) VALUES (?, ?) "
            "ON CONFLICT(url) DO UPDATE SET open=excluded.open",
            (str(stub["url"]).rstrip("/") or "/", target_route),
        )

    def _retain_entity(
        self,
        table: str,
        row: dict[str, Any],
        *,
        identity_fields: tuple[str, ...],
    ) -> dict[str, Any]:
        if table not in {"publishers", "resources"}:
            raise ValueError(table)
        route = str(row["open"])
        existing_row = self.connection.execute(
            f"SELECT row_json FROM {table} WHERE open = ?", (route,)
        ).fetchone()
        if existing_row is None:
            self.connection.execute(
                f"INSERT INTO {table}(open, row_json) VALUES (?, ?)",
                (route, _json_text(row)),
            )
            return row
        existing = _json_value(str(existing_row[0]))
        changed = False
        for field in identity_fields:
            left = existing.get(field)
            right = row.get(field)
            if left and right and left != right:
                raise PublicationError(
                    f"conflicting {route} identity field {field}: {left!r} != {right!r}"
                )
            if not left and right:
                existing[field] = right
                changed = True
        if changed:
            self.connection.execute(
                f"UPDATE {table} SET row_json = ? WHERE open = ?",
                (_json_text(existing), route),
            )
        return existing

    def _retain_relationship(self, edge: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO relationships(assertion_id, source, kind, target, row_json) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(assertion_id) DO UPDATE SET "
            "source=excluded.source, kind=excluded.kind, target=excluded.target, "
            "row_json=excluded.row_json",
            (
                edge["assertion_id"],
                edge["source"],
                edge["kind"],
                edge["target"],
                _json_text(edge),
            ),
        )

    def transform(self) -> None:
        last_seq = -1
        while True:
            rows = self.connection.execute(
                "SELECT seq, raw_json FROM source_records WHERE seq > ? ORDER BY seq LIMIT ?",
                (last_seq, FINALISE_BATCH_SIZE),
            ).fetchall()
            if not rows:
                break
            for seq, raw_json in rows:
                last_seq = int(seq)
                record = _json_value(str(raw_json))
                dataset_row = self.connection.execute(
                    "SELECT row_json FROM datasets WHERE seq = ?", (seq,)
                ).fetchone()
                if dataset_row is None:
                    raise PublicationError(f"disk compiler lost dataset sequence {seq}")
                dataset = _json_value(str(dataset_row[0]))
                source_route = str(dataset["open"])
                evidence_url = str(dataset["evidence_url"])
                source_observed_at = str(record.get("retrieved_at") or self.observed_at)
                evidence_sha256 = (
                    str(record["evidence_sha256"])
                    if record.get("evidence_sha256")
                    else hashlib.sha256(canonical_json_bytes(record)).hexdigest()
                )
                base_locator = str(record.get("evidence_locator") or "/")
                links = record.get("links") or {}
                if isinstance(links, dict):
                    for native_predicate, values in sorted(links.items()):
                        if not isinstance(values, list):
                            continue
                        kind = LINK_KINDS.get(
                            native_predicate, native_predicate.replace("_", " ")
                        )
                        for value in values:
                            if not isinstance(value, dict):
                                continue
                            locator = f"{base_locator.rstrip('/')}/links/{native_predicate}"
                            if native_predicate in ORGANISATION_LINKS:
                                publisher = publisher_from_link(
                                    value,
                                    evidence_url=evidence_url,
                                    evidence_sha256=evidence_sha256,
                                    evidence_locator=locator,
                                    retrieved_at=source_observed_at,
                                )
                                publisher = self._retain_entity(
                                    "publishers",
                                    publisher,
                                    identity_fields=("url", "content_id"),
                                )
                                target = str(publisher["open"])
                                if (
                                    native_predicate == "primary_publishing_organisation"
                                    and not dataset.get("publisher")
                                ):
                                    dataset["publisher"] = publisher["name"]
                                    dataset["publisher_title"] = publisher["title"]
                            else:
                                target_url = link_url(value)
                                target = self._route_for_url(target_url) or link_dataset_route(value)
                                self._insert_stub(
                                    target,
                                    {
                                        "content_id": value.get("content_id"),
                                        "base_path": urlparse(target_url).path,
                                        "canonical_url": target_url,
                                        "title": value.get("title")
                                        or "Referenced GOV.UK content",
                                        "description": "Discovered through a typed Content API relationship.",
                                        "document_type": value.get("document_type")
                                        or "linked_content",
                                        "schema_name": value.get("schema_name") or "unknown",
                                        "locale": value.get("locale") or "en",
                                        "source_id": "structured-linked-content",
                                        "evidence_url": evidence_url,
                                    },
                                )
                            self._retain_relationship(
                                relationship(
                                    source_route,
                                    target,
                                    kind,
                                    evidence_url,
                                    source_observed_at,
                                    native_predicate,
                                    evidence_sha256=evidence_sha256,
                                    evidence_locator=locator,
                                    snapshot_id=self.snapshot_id,
                                )
                            )

                details = record.get("details") or {}
                attachments = details.get("attachments", []) if isinstance(details, dict) else []
                for attachment_index, attachment in enumerate(attachments):
                    if not isinstance(attachment, dict):
                        continue
                    attachment_url = str(attachment.get("url") or "")
                    if not attachment_url.startswith(("https://", "http://")):
                        continue
                    attachment_id = str(
                        attachment.get("id")
                        or hashlib.sha256(attachment_url.encode("utf-8")).hexdigest()[:24]
                    )
                    name = slugify(attachment_id)
                    route = f"resource/{name}"
                    resource = {
                        "@id": attachment_url,
                        "accessibility": "accessible"
                        if attachment.get("accessible")
                        else "not-declared-accessible",
                        "attachment_id": attachment_id,
                        "id": attachment_id,
                        "bytes": attachment.get("file_size"),
                        "content_type": attachment.get("content_type")
                        or "application/octet-stream",
                        "name": name,
                        "open": route,
                        "pages": attachment.get("number_of_pages"),
                        "parent": source_route,
                        "parent_content_id": dataset.get("canonical_content_id"),
                        "dataset": dataset["name"],
                        "record_type": "GOV.UK attachment",
                        "rights_status": "item-specific-review-required",
                        "confidence": "source-declared",
                        "evidence_url": evidence_url,
                        "evidence_sha256": evidence_sha256,
                        "evidence_locator": (
                            f"{base_locator.rstrip('/')}/details/attachments/{attachment_index}"
                        ),
                        "retrieved_at": source_observed_at,
                        "title": str(
                            attachment.get("title")
                            or attachment.get("filename")
                            or "Attachment"
                        ),
                        "url": attachment_url,
                    }
                    self._retain_entity(
                        "resources", resource, identity_fields=("url", "attachment_id")
                    )
                    self._retain_relationship(
                        relationship(
                            source_route,
                            route,
                            "has attachment",
                            evidence_url,
                            source_observed_at,
                            "details.attachments",
                            evidence_sha256=evidence_sha256,
                            evidence_locator=(
                                f"{base_locator.rstrip('/')}/details/attachments/{attachment_index}"
                            ),
                            snapshot_id=self.snapshot_id,
                        )
                    )

                redirects = record.get("redirects") or []
                for redirect in redirects:
                    if not isinstance(redirect, dict):
                        continue
                    destination = str(
                        redirect.get("destination") or redirect.get("path") or ""
                    )
                    if not destination:
                        continue
                    target_url = (
                        destination
                        if destination.startswith("http")
                        else "https://www.gov.uk" + destination
                    )
                    target = self._route_for_url(target_url) or (
                        "dataset/" + dataset_name(None, str(dataset["language"]), target_url)
                    )
                    self._insert_stub(
                        target,
                        {
                            "canonical_url": target_url,
                            "title": "Redirect destination",
                            "document_type": "redirect_destination",
                            "locale": dataset["language"],
                            "source_id": "redirect-destination",
                            "evidence_url": evidence_url,
                        },
                    )
                    self._retain_relationship(
                        relationship(
                            source_route,
                            target,
                            "redirects to",
                            evidence_url,
                            source_observed_at,
                            "redirects.destination",
                            evidence_sha256=evidence_sha256,
                            evidence_locator=f"{base_locator.rstrip('/')}/redirects",
                            snapshot_id=self.snapshot_id,
                        )
                    )
                self.connection.execute(
                    "UPDATE datasets SET row_json = ? WHERE seq = ?",
                    (_json_text(dataset), seq),
                )
            self.connection.commit()

    def _add_identifier(
        self, identifier: Any, entry: dict[str, Any], *, is_route: bool = False
    ) -> None:
        if identifier is None or str(identifier) == "":
            return
        key = str(identifier)
        if is_route:
            exact = self.connection.execute(
                "SELECT 1 FROM identifiers "
                "WHERE identifier=? AND kind=? AND ordinal=? AND open=?",
                (key, entry["kind"], entry["ordinal"], entry["open"]),
            ).fetchone()
            if exact:
                return
            if self.connection.execute(
                "SELECT 1 FROM identifiers WHERE identifier=? LIMIT 1", (key,)
            ).fetchone():
                raise PublicationError(f"runtime route collisions: {[key]}")
        self.connection.execute(
            "INSERT OR IGNORE INTO identifiers(bucket, identifier, kind, ordinal, open) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                adjacency_bucket(key),
                key,
                entry["kind"],
                entry["ordinal"],
                entry["open"],
            ),
        )

    def _index_row_identifiers(
        self, kind: str, ordinal: int, row: dict[str, Any]
    ) -> None:
        entry = {"kind": kind, "ordinal": ordinal, "open": row["open"]}
        self._add_identifier(row.get("open"), entry, is_route=True)
        for identifier in (
            row.get("url"),
            row.get("@id"),
            row.get("canonical_content_id"),
            row.get("content_id"),
            row.get("attachment_id"),
            row.get("id"),
            row.get("name"),
        ):
            self._add_identifier(identifier, entry)

    def _index_search_row(self, dataset: dict[str, Any]) -> None:
        fields = {
            "title": dataset["title"],
            "publisher": dataset.get("publisher_title", ""),
            "description": dataset.get("description", ""),
            "record_type": dataset.get("document_type", ""),
            "schema": dataset.get("schema_name", ""),
            "tags": " ".join(dataset.get("tags", [])),
            "url": dataset.get("url", ""),
            "language": dataset.get("language", ""),
            "lifecycle": dataset.get("lifecycle", ""),
        }
        combined: dict[str, tuple[int, int]] = {}
        for field, text in fields.items():
            for token in tokenise(str(text)):
                score, mask = combined.get(token, (0, 0))
                combined[token] = (
                    score + FIELD_WEIGHTS[field],
                    mask | FIELD_MASKS[field],
                )
        ordinal = int(dataset["ordinal"])
        self.connection.executemany(
            "INSERT INTO search_postings(token, score, ordinal, mask, shard) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                (token, score, ordinal, mask, search_shard(token))
                for token, (score, mask) in combined.items()
            ),
        )

    def _finalise_datasets(self) -> None:
        last: tuple[str, str, int] | None = None
        ordinal = 0
        while True:
            if last is None:
                rows = self.connection.execute(
                    "SELECT seq, open, url, row_json FROM datasets "
                    "ORDER BY open, url, seq LIMIT ?",
                    (FINALISE_BATCH_SIZE,),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    "SELECT seq, open, url, row_json FROM datasets "
                    "WHERE (open, url, seq) > (?, ?, ?) "
                    "ORDER BY open, url, seq LIMIT ?",
                    (*last, FINALISE_BATCH_SIZE),
                ).fetchall()
            if not rows:
                break
            for seq, route, url, row_json in rows:
                last = (str(route), str(url), int(seq))
                dataset = _json_value(str(row_json))
                dataset["ordinal"] = ordinal
                dataset.setdefault("publisher", "unknown")
                dataset.setdefault(
                    "publisher_title", "Publisher not available from admitted source"
                )
                self.connection.execute(
                    "UPDATE datasets SET output_index=?, row_json=? WHERE seq=?",
                    (ordinal, _json_text(dataset), seq),
                )
                self._index_row_identifiers("datasets", ordinal, dataset)
                self._index_search_row(dataset)
                ordinal += 1
            self.connection.commit()

    def _finalise_entities(self, table: str, kind: str) -> None:
        last_open: str | None = None
        ordinal = 0
        while True:
            if last_open is None:
                rows = self.connection.execute(
                    f"SELECT open, row_json FROM {table} ORDER BY open LIMIT ?",
                    (FINALISE_BATCH_SIZE,),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    f"SELECT open, row_json FROM {table} WHERE open > ? "
                    "ORDER BY open LIMIT ?",
                    (last_open, FINALISE_BATCH_SIZE),
                ).fetchall()
            if not rows:
                break
            for route, row_json in rows:
                last_open = str(route)
                row = _json_value(str(row_json))
                self.connection.execute(
                    f"UPDATE {table} SET output_index=? WHERE open=?",
                    (ordinal, route),
                )
                self._index_row_identifiers(kind, ordinal, row)
                ordinal += 1
            self.connection.commit()

    def _finalise_relationships(self) -> None:
        last: tuple[str, str, str, str] | None = None
        ordinal = 0
        while True:
            if last is None:
                rows = self.connection.execute(
                    "SELECT assertion_id, source, kind, target, row_json "
                    "FROM relationships ORDER BY source, kind, target, assertion_id LIMIT ?",
                    (FINALISE_BATCH_SIZE,),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    "SELECT assertion_id, source, kind, target, row_json "
                    "FROM relationships WHERE (source, kind, target, assertion_id) > (?, ?, ?, ?) "
                    "ORDER BY source, kind, target, assertion_id LIMIT ?",
                    (*last, FINALISE_BATCH_SIZE),
                ).fetchall()
            if not rows:
                break
            for assertion_id, source, kind, target, row_json in rows:
                last = (str(source), str(kind), str(target), str(assertion_id))
                self.connection.execute(
                    "UPDATE relationships SET output_index=? WHERE assertion_id=?",
                    (ordinal, assertion_id),
                )
                for route in {str(source), str(target)}:
                    self.connection.execute(
                        "INSERT INTO adjacency(bucket, route, kind, source, target, assertion_id, row_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            adjacency_bucket(route),
                            route,
                            kind,
                            source,
                            target,
                            assertion_id,
                            row_json,
                        ),
                    )
                ordinal += 1
            self.connection.commit()

    def _finalise_search(self) -> None:
        self.connection.execute(
            "CREATE INDEX search_shard_token_idx ON search_postings(shard, token)"
        )
        self.connection.execute(
            "INSERT INTO token_stats(shard, token, df) "
            "SELECT MIN(shard), token, COUNT(*) FROM search_postings GROUP BY token"
        )
        last_token: str | None = None
        while True:
            if last_token is None:
                rows = self.connection.execute(
                    "SELECT token, df FROM token_stats ORDER BY token LIMIT ?",
                    (INGEST_COMMIT_INTERVAL,),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    "SELECT token, df FROM token_stats WHERE token > ? "
                    "ORDER BY token LIMIT ?",
                    (last_token, INGEST_COMMIT_INTERVAL),
                ).fetchall()
            if not rows:
                break
            for token_value, df in rows:
                token = str(token_value)
                last_token = token
                for length in range(3, min(len(token), 12) + 1):
                    prefix = token[:length]
                    self.connection.execute(
                        "INSERT INTO prefixes(shard, prefix, token, df) VALUES (?, ?, ?, ?)",
                        (search_shard(prefix), prefix, token, df),
                    )
            self.connection.commit()

    def finalise(self) -> None:
        self._finalise_datasets()
        self._finalise_entities("publishers", "publishers")
        self._finalise_entities("resources", "resources")
        self._finalise_relationships()
        self._finalise_search()
        self.connection.execute(
            "CREATE INDEX identifiers_bucket_idx ON identifiers(bucket, identifier)"
        )
        self.connection.execute(
            "CREATE INDEX adjacency_bucket_idx ON adjacency(bucket, route)"
        )
        self.connection.commit()

    def compile(self, source: Path) -> None:
        self.ingest(source)
        self.transform()
        self.finalise()

    def sequences(
        self,
    ) -> tuple[
        SQLiteJSONSequence,
        SQLiteJSONSequence,
        SQLiteJSONSequence,
        SQLiteJSONSequence,
    ]:
        return tuple(
            SQLiteJSONSequence(self.connection, table)
            for table in ("datasets", "publishers", "resources", "relationships")
        )  # type: ignore[return-value]

    def write_search(
        self,
        output: Path,
        datasets: Sequence[dict[str, Any]],
        facets_path: str,
        *,
        snapshot_id: str,
        generated_at: str,
    ) -> dict[str, Any]:
        search_root = output / "data" / "search"
        search_root.mkdir(parents=True, exist_ok=True)
        result_paths: list[str] = []
        shard_rows: dict[str, list[dict[str, Any]]] = {
            "result_docs": [],
            "lexicon": [],
            "postings": [],
            "prefixes": [],
            "doc_map": [],
        }
        for start in range(0, len(datasets), RESULT_CHUNK_SIZE):
            result_docs = [
                {
                    key: value
                    for key, value in dataset.items()
                    if key not in {"evidence_url", "source_memberships"}
                }
                for dataset in datasets[start : start + RESULT_CHUNK_SIZE]
            ]
            path = search_root / f"results-{start // RESULT_CHUNK_SIZE}.json"
            path.write_text(pretty_json(result_docs), encoding="utf-8")
            result_paths.append(path.relative_to(output).as_posix())
            keys = [str(row["open"]) for row in result_docs]
            shard_rows["result_docs"].append(
                data_plane_shard_metadata(
                    output,
                    path,
                    schema="okf-search-result-shard.v1",
                    snapshot_id=snapshot_id,
                    count=len(result_docs),
                    first_key=keys[0] if keys else None,
                    last_key=keys[-1] if keys else None,
                    compression="identity",
                    extra={
                        "kind": "result_docs",
                        "ordinal": start // RESULT_CHUNK_SIZE,
                    },
                )
            )

        lexicon_paths: dict[str, str] = {}
        postings_paths: list[str] = []
        shards = [
            str(row[0])
            for row in self.connection.execute(
                "SELECT DISTINCT shard FROM token_stats ORDER BY shard"
            )
        ]
        for shard in shards:
            postings_relative = f"data/search/postings/{shard}.json"

            def lexicon_entries() -> Iterator[dict[str, Any]]:
                rows = self.connection.execute(
                    "SELECT token, df FROM token_stats WHERE shard=? ORDER BY token",
                    (shard,),
                )
                for token, df in rows:
                    yield {
                        "token": token,
                        "df": int(df),
                        "postings": postings_relative,
                    }

            lexicon_path = search_root / "lexicon" / f"{shard}.json"
            _write_pretty_sequence(lexicon_path, lexicon_entries())
            lexicon_paths[shard] = lexicon_path.relative_to(output).as_posix()
            token_count, first_token, last_token = self.connection.execute(
                "SELECT COUNT(*), MIN(token), MAX(token) FROM token_stats WHERE shard=?",
                (shard,),
            ).fetchone()
            shard_rows["lexicon"].append(
                data_plane_shard_metadata(
                    output,
                    lexicon_path,
                    schema="okf-search-lexicon-shard.v1",
                    snapshot_id=snapshot_id,
                    count=int(token_count),
                    first_key=str(first_token) if first_token is not None else None,
                    last_key=str(last_token) if last_token is not None else None,
                    compression="identity",
                    extra={"kind": "lexicon", "shard": shard},
                )
            )

            postings_path = search_root / "postings" / f"{shard}.json"
            postings_path.parent.mkdir(parents=True, exist_ok=True)
            tokens = self.connection.execute(
                "SELECT token FROM token_stats WHERE shard=? ORDER BY token", (shard,)
            )
            with postings_path.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write('{\n  "tokens": {')
                first = True
                for (token,) in tokens:
                    rows = [
                        [int(ordinal), int(score), int(mask)]
                        for ordinal, score, mask in self.connection.execute(
                            "SELECT ordinal, score, mask FROM search_postings "
                            "WHERE token=? ORDER BY score DESC, ordinal LIMIT ?",
                            (token, MAX_POSTINGS),
                        )
                    ]
                    stream.write("\n" if first else ",\n")
                    stream.write(_pretty_entry(str(token), rows, 4))
                    first = False
                if first:
                    stream.write("}\n}\n")
                else:
                    stream.write("\n  }\n}\n")
            postings_paths.append(postings_path.relative_to(output).as_posix())
            posting_count = int(
                self.connection.execute(
                    "SELECT COALESCE(SUM(CASE WHEN df > ? THEN ? ELSE df END), 0) "
                    "FROM token_stats WHERE shard=?",
                    (MAX_POSTINGS, MAX_POSTINGS, shard),
                ).fetchone()[0]
            )
            shard_rows["postings"].append(
                data_plane_shard_metadata(
                    output,
                    postings_path,
                    schema="okf-search-postings-shard.v1",
                    snapshot_id=snapshot_id,
                    count=int(token_count),
                    first_key=str(first_token) if first_token is not None else None,
                    last_key=str(last_token) if last_token is not None else None,
                    compression="identity",
                    extra={
                        "kind": "postings",
                        "shard": shard,
                        "posting_count": posting_count,
                    },
                )
            )

        prefix_paths: dict[str, str] = {}
        prefix_shards = [
            str(row[0])
            for row in self.connection.execute(
                "SELECT DISTINCT shard FROM prefixes ORDER BY shard"
            )
        ]
        for shard in prefix_shards:
            def prefix_entries() -> Iterator[tuple[str, object]]:
                prefix_rows = self.connection.execute(
                    "SELECT DISTINCT prefix FROM prefixes WHERE shard=? ORDER BY prefix",
                    (shard,),
                )
                for (prefix_value,) in prefix_rows:
                    prefix = str(prefix_value)
                    values = [
                        {"token": token, "df": int(df)}
                        for token, df in self.connection.execute(
                            "SELECT token, df FROM prefixes WHERE shard=? AND prefix=? "
                            "ORDER BY df DESC, token LIMIT 100",
                            (shard, prefix),
                        )
                    ]
                    yield prefix, values

            path = search_root / "prefixes" / f"{shard}.json"
            _write_pretty_mapping(path, prefix_entries())
            prefix_paths[shard] = path.relative_to(output).as_posix()
            prefix_count, first_prefix, last_prefix = self.connection.execute(
                "SELECT COUNT(DISTINCT prefix), MIN(prefix), MAX(prefix) "
                "FROM prefixes WHERE shard=?",
                (shard,),
            ).fetchone()
            shard_rows["prefixes"].append(
                data_plane_shard_metadata(
                    output,
                    path,
                    schema="okf-search-prefix-shard.v1",
                    snapshot_id=snapshot_id,
                    count=int(prefix_count),
                    first_key=(
                        str(first_prefix) if first_prefix is not None else None
                    ),
                    last_key=str(last_prefix) if last_prefix is not None else None,
                    compression="identity",
                    extra={"kind": "prefixes", "shard": shard},
                )
            )

        doc_map_path = search_root / "doc-map.json"

        def doc_map_entries() -> Iterator[tuple[str, object]]:
            rows = self.connection.execute(
                "SELECT CAST(output_index AS TEXT), row_json "
                "FROM datasets ORDER BY CAST(output_index AS TEXT)"
            )
            for ordinal, row_json in rows:
                yield str(ordinal), _json_value(str(row_json))["open"]

        _write_pretty_mapping(doc_map_path, doc_map_entries())
        shard_rows["doc_map"].append(
            data_plane_shard_metadata(
                output,
                doc_map_path,
                schema="okf-search-doc-map-shard.v1",
                snapshot_id=snapshot_id,
                count=len(datasets),
                first_key="0" if datasets else None,
                last_key=str(len(datasets) - 1) if datasets else None,
                compression="identity",
                extra={"kind": "doc_map"},
            )
        )
        token_count = int(
            self.connection.execute("SELECT COUNT(*) FROM token_stats").fetchone()[0]
        )
        uncapped = int(
            self.connection.execute("SELECT COUNT(*) FROM search_postings").fetchone()[0]
        )
        retained = int(
            self.connection.execute(
                "SELECT COALESCE(SUM(CASE WHEN df > ? THEN ? ELSE df END), 0) "
                "FROM token_stats",
                (MAX_POSTINGS, MAX_POSTINGS),
            ).fetchone()[0]
        )
        shard_metadata = {
            "schema": "okf-search-shard-manifest.v1",
            "schema_version": DATA_PLANE_SCHEMA_VERSION,
            "snapshot": snapshot_id,
            "generated_at": generated_at,
            "shards": shard_rows,
        }
        shard_metadata["shard_manifest_sha256"] = shard_manifest_sha256(
            shard_rows
        )
        shard_metadata_path = search_root / "shards.json"
        shard_metadata_path.write_text(
            pretty_json(shard_metadata), encoding="utf-8"
        )
        manifest = {
            "schema": "okf-static-search.v1",
            "schema_version": DATA_PLANE_SCHEMA_VERSION,
            "snapshot": snapshot_id,
            "generated_at": generated_at,
            "token_min_length": 2,
            "prefix_min_length": 3,
            "lexicon_shard_length": 2,
            "result_doc_chunk_size": RESULT_CHUNK_SIZE,
            "result_limit": 200,
            "field_masks": FIELD_MASKS,
            "weights": FIELD_WEIGHTS,
            "counts": {
                "documents": len(datasets),
                "tokens": token_count,
                "postings": retained,
                "uncapped_postings": uncapped,
                "max_postings_per_token": MAX_POSTINGS,
            },
            "entrypoints": {
                "doc_map": "data/search/doc-map.json",
                "facets": facets_path,
                "lexicon": lexicon_paths,
                "postings": postings_paths,
                "prefixes": prefix_paths,
                "result_docs": result_paths,
            },
            "budgets": DATA_PLANE_BUDGETS,
            "shard_metadata": shard_metadata_path.relative_to(output).as_posix(),
        }
        manifest["shard_manifest_sha256"] = shard_metadata[
            "shard_manifest_sha256"
        ]
        (search_root / "manifest.json").write_text(
            pretty_json(manifest), encoding="utf-8"
        )
        manifest["_compiled_shards"] = shard_rows
        return manifest

    def write_adjacency(
        self,
        output: Path,
        relationships: Sequence[dict[str, Any]],
        *,
        snapshot_id: str,
        generated_at: str,
    ) -> dict[str, Any]:
        mapping: dict[str, str] = {}
        shard_rows: list[dict[str, Any]] = []
        for number in range(256):
            bucket = f"{number:02x}"
            path = output / "data" / "adjacency" / f"{bucket}.json.gz"
            routes = self.connection.execute(
                "SELECT DISTINCT route FROM adjacency WHERE bucket=? ORDER BY route",
                (bucket,),
            )
            with _CanonicalGzipWriter(path) as stream:
                stream.write("{")
                first_route = True
                for (route,) in routes:
                    stream.write("" if first_route else ",")
                    stream.write(json.dumps(str(route), ensure_ascii=False) + ":[")
                    first_edge = True
                    edges = self.connection.execute(
                        "SELECT row_json FROM adjacency WHERE route=? "
                        "ORDER BY kind, source, target, assertion_id",
                        (route,),
                    )
                    for (row_json,) in edges:
                        stream.write("" if first_edge else ",")
                        stream.write(str(row_json))
                        first_edge = False
                    stream.write("]")
                    first_route = False
                stream.write("}\n")
            mapping[bucket] = path.relative_to(output).as_posix()
            route_count, first_route, last_route, occurrence_count = (
                self.connection.execute(
                    "SELECT COUNT(DISTINCT route), MIN(route), MAX(route), COUNT(*) "
                    "FROM adjacency WHERE bucket=?",
                    (bucket,),
                ).fetchone()
            )
            shard_rows.append(
                data_plane_shard_metadata(
                    output,
                    path,
                    schema="okf-adjacency-shard.v1",
                    snapshot_id=snapshot_id,
                    count=int(route_count),
                    first_key=str(first_route) if first_route is not None else None,
                    last_key=str(last_route) if last_route is not None else None,
                    compression="gzip",
                    extra={
                        "kind": "adjacency",
                        "bucket": bucket,
                        "relationship_occurrences": int(occurrence_count),
                    },
                )
            )
        route_count = int(
            self.connection.execute(
                "SELECT COUNT(DISTINCT route) FROM adjacency"
            ).fetchone()[0]
        )
        manifest = {
            "schema": "okf-relationship-adjacency.v1",
            "schema_version": DATA_PLANE_SCHEMA_VERSION,
            "snapshot": snapshot_id,
            "generated_at": generated_at,
            "algorithm": "fnv1a32-prefix-2",
            "relationships": len(relationships),
            "routes": route_count,
            "buckets": mapping,
            "budgets": DATA_PLANE_BUDGETS,
            "shards": shard_rows,
        }
        manifest["shard_manifest_sha256"] = shard_manifest_sha256(shard_rows)
        (output / "data" / "adjacency" / "manifest.json").write_text(
            pretty_json(manifest), encoding="utf-8"
        )
        return manifest

    def write_route_index(
        self,
        output: Path,
        datasets: Sequence[dict[str, Any]],
        publishers: Sequence[dict[str, Any]],
        resources: Sequence[dict[str, Any]],
        *,
        snapshot_id: str,
        generated_at: str,
    ) -> dict[str, Any]:
        mapping: dict[str, str] = {}
        shard_rows: list[dict[str, Any]] = []
        for number in range(256):
            bucket = f"{number:02x}"
            path = output / "data" / "routes" / f"{bucket}.json.gz"
            identifiers = self.connection.execute(
                "SELECT DISTINCT identifier FROM identifiers WHERE bucket=? "
                "ORDER BY identifier",
                (bucket,),
            )
            with _CanonicalGzipWriter(path) as stream:
                stream.write("{")
                first_identifier = True
                for (identifier,) in identifiers:
                    stream.write("" if first_identifier else ",")
                    stream.write(json.dumps(str(identifier), ensure_ascii=False) + ":[")
                    first_match = True
                    matches = self.connection.execute(
                        "SELECT kind, ordinal, open FROM identifiers WHERE identifier=? "
                        "ORDER BY kind, open, ordinal",
                        (identifier,),
                    )
                    for kind, ordinal, route in matches:
                        stream.write("" if first_match else ",")
                        stream.write(
                            _json_text(
                                {
                                    "kind": kind,
                                    "ordinal": int(ordinal),
                                    "open": route,
                                }
                            )
                        )
                        first_match = False
                    stream.write("]")
                    first_identifier = False
                stream.write("}\n")
            mapping[bucket] = path.relative_to(output).as_posix()
            identifier_count, first_identifier, last_identifier, bucket_entries = (
                self.connection.execute(
                    "SELECT COUNT(DISTINCT identifier), MIN(identifier), "
                    "MAX(identifier), COUNT(*) FROM identifiers WHERE bucket=?",
                    (bucket,),
                ).fetchone()
            )
            shard_rows.append(
                data_plane_shard_metadata(
                    output,
                    path,
                    schema="okf-route-shard.v1",
                    snapshot_id=snapshot_id,
                    count=int(identifier_count),
                    first_key=(
                        str(first_identifier)
                        if first_identifier is not None
                        else None
                    ),
                    last_key=(
                        str(last_identifier) if last_identifier is not None else None
                    ),
                    compression="gzip",
                    extra={
                        "kind": "routes",
                        "bucket": bucket,
                        "match_count": int(bucket_entries),
                    },
                )
            )
        identifier_count = int(
            self.connection.execute(
                "SELECT COUNT(DISTINCT identifier) FROM identifiers"
            ).fetchone()[0]
        )
        match_count = int(
            self.connection.execute("SELECT COUNT(*) FROM identifiers").fetchone()[0]
        )
        manifest = {
            "schema": "okf-route-index.v1",
            "schema_version": DATA_PLANE_SCHEMA_VERSION,
            "snapshot": snapshot_id,
            "generated_at": generated_at,
            "algorithm": "fnv1a32-prefix-2",
            "entry_shape": "identifier-to-typed-matches",
            "kinds": ["datasets", "publishers", "resources"],
            "chunk_size": RECORD_CHUNK_SIZE,
            "identifiers": identifier_count,
            "entries": match_count,
            "buckets": mapping,
            "budgets": DATA_PLANE_BUDGETS,
            "shards": shard_rows,
        }
        manifest["shard_manifest_sha256"] = shard_manifest_sha256(shard_rows)
        (output / "data" / "routes" / "manifest.json").write_text(
            pretty_json(manifest), encoding="utf-8"
        )
        return manifest

    def metrics(self) -> dict[str, Any]:
        tables = {}
        for table in (
            "source_records",
            "datasets",
            "publishers",
            "resources",
            "relationships",
            "identifiers",
            "adjacency",
            "search_postings",
            "token_stats",
            "prefixes",
        ):
            tables[table] = int(
                self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        self.connection.commit()
        return {
            "engine": "sqlite-bounded-v1",
            "sqlite_cache_kib": SQLITE_CACHE_KIB,
            "source_line_limit_bytes": MAX_SOURCE_LINE_BYTES,
            "database_bytes": self.database.stat().st_size,
            "tables": tables,
        }


def _validate_output(output: Path) -> Path:
    output = output.resolve()
    protected_root_children = {
        ".git",
        ".github",
        "src",
        "tests",
        "planning",
        "governance",
        "research",
        "semantic",
        "questions",
        "personas",
        "stories",
        "evaluation",
        "orchestration",
        "scripts",
    }
    if output in {Path("/").resolve(), Path.home().resolve(), ROOT.resolve()}:
        raise PublicationError(f"refusing destructive publication output: {output}")
    if ".git" in output.parts or (
        output.parent == ROOT.resolve() and output.name in protected_root_children
    ):
        raise PublicationError(f"refusing protected repository output: {output}")
    if output.exists() and not output.is_dir():
        raise PublicationError(
            f"publication output exists and is not a directory: {output}"
        )
    return output


def build_publication_from_path(
    source: Path,
    output: Path,
    generated_at: str,
    snapshot_id: str,
) -> dict[str, Any]:
    """Build atomically from streamed JSONL using the disk-backed compiler."""
    output = _validate_output(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    build_root = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.build-", dir=output.parent)
    )
    candidate = build_root / "publication"
    backup = build_root / "previous"
    database = build_root / "compiler.sqlite3"
    compiler: DiskCompiler | None = None
    moved_existing = False
    installed_candidate = False
    try:
        compiler = DiskCompiler(database, generated_at, snapshot_id)
        compiler.compile(source)
        candidate.mkdir(parents=True, exist_ok=False)
        datasets, publishers, resources, relationships = compiler.sequences()
        result = _emit_publication(
            datasets,
            publishers,
            resources,
            relationships,
            candidate,
            generated_at,
            snapshot_id,
            source_count=compiler.source_count,
            dispositions_close=(
                compiler.source_count > 0 and compiler.invalid_dispositions == 0
            ),
            search_writer=compiler.write_search,
            adjacency_writer=compiler.write_adjacency,
            route_index_writer=compiler.write_route_index,
        )
        result["compiler"] = compiler.metrics()
        for required in (
            "okf-bundle.yamlld",
            "okf-bundle.jsonld",
            "okf-explorer.json",
            "data/manifest.json",
        ):
            if not (candidate / required).is_file():
                raise PublicationError(f"candidate publication is missing {required}")
        compiler.close()
        compiler = None
        if output.exists():
            output.rename(backup)
            moved_existing = True
        candidate.rename(output)
        installed_candidate = True
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        if build_root.exists():
            shutil.rmtree(build_root, ignore_errors=True)
        return result
    except BaseException:
        if compiler is not None:
            compiler.close()
        if (
            moved_existing
            and not installed_candidate
            and not output.exists()
            and backup.exists()
        ):
            backup.rename(output)
        if build_root.exists():
            shutil.rmtree(build_root)
        raise
