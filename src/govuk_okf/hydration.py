"""Checkpointed, bounded-memory Content API hydration and linked closure."""

from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
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
        self.records_root = self.root / "corpus" / "records" / label
        self.inventory_root = self.root / "corpus" / "inventory"
        self.reconciliation_root = self.root / "corpus" / "reconciliation"
        self.limiter = HostLimiter(
            requests_per_second,
            state_path=self.root / ".tmp" / "rate-limits" / "content-api.timestamp",
            budget_path=self.root / ".tmp" / "request-budget" / "official-sources.count",
            max_requests=1_000_000,
        )

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
            CREATE TABLE IF NOT EXISTS queue (
                url TEXT NOT NULL,
                locale TEXT NOT NULL,
                input_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending', 'complete')),
                hydration_status TEXT,
                record_json TEXT,
                PRIMARY KEY (url, locale)
            );
            CREATE INDEX IF NOT EXISTS queue_state_url ON queue(state, url, locale);
            CREATE TABLE IF NOT EXISTS candidates (
                candidate_key TEXT PRIMARY KEY,
                entity_class TEXT NOT NULL,
                source_native_id TEXT NOT NULL,
                locale TEXT NOT NULL,
                candidate_json TEXT NOT NULL
            );
            """
        )
        return connection

    def prepare(self, connection: sqlite3.Connection) -> int:
        digest = hashlib.sha256()
        count = 0
        insert_batch: list[tuple[str, str, str]] = []
        connection.execute("BEGIN")
        try:
            for record in read_source_records(self.source_path):
                url, locale = _record_key(record)
                record["canonical_url"] = url
                payload = canonical_json_bytes(record)
                digest.update(payload)
                insert_batch.append((url, locale, payload.decode("utf-8")))
                if len(insert_batch) >= 10_000:
                    connection.executemany(
                        "INSERT OR IGNORE INTO queue(url, locale, input_json, state) "
                        "VALUES (?, ?, ?, 'pending')",
                        insert_batch,
                    )
                    insert_batch.clear()
                count += 1
            if insert_batch:
                connection.executemany(
                    "INSERT OR IGNORE INTO queue(url, locale, input_json, state) "
                    "VALUES (?, ?, ?, 'pending')",
                    insert_batch,
                )
            source_digest = digest.hexdigest()
            previous = connection.execute("SELECT value FROM meta WHERE key='source_sha256'").fetchone()
            if previous and previous[0] != source_digest:
                completed = connection.execute("SELECT COUNT(*) FROM queue WHERE state='complete'").fetchone()[0]
                if completed:
                    raise HydrationError("source inventory changed after hydration began; use a new snapshot label")
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('source_sha256', ?)",
                (source_digest,),
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('source_records', ?)",
                (str(count),),
            )
            connection.commit()
        except Exception:
            connection.rollback()
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
            source_count = self.prepare(connection)
            processed_this_run = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
                while request_limit is None or processed_this_run < request_limit:
                    remaining = self.batch_size
                    if request_limit is not None:
                        remaining = min(remaining, request_limit - processed_this_run)
                    rows = connection.execute(
                        "SELECT url, locale, input_json FROM queue WHERE state='pending' ORDER BY url, locale LIMIT ?",
                        (remaining,),
                    ).fetchall()
                    if not rows:
                        break
                    futures = {
                        pool.submit(self._hydrate_one, input_json): (url, locale)
                        for url, locale, input_json in rows
                    }
                    completed_rows: list[tuple[str, str, dict[str, Any], str, list[dict[str, Any]]]] = []
                    for future in concurrent.futures.as_completed(futures):
                        url, locale = futures[future]
                        record, status, linked = future.result()
                        completed_rows.append((url, locale, record, status, linked))
                    connection.execute("BEGIN")
                    try:
                        for url, locale, record, status, linked in sorted(completed_rows):
                            connection.execute(
                                "UPDATE queue SET state='complete', hydration_status=?, record_json=? "
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

            source_output = write_jsonl_gzip_shards(self.records_root, "source-records", records())
            source_target = Path(source_output["root"]) / "index.json"
            record_count = int(source_output["records"])
            record_digest = str(source_output["canonical_sha256"])

            connection.execute("DELETE FROM candidates")
            connection.commit()
            candidate_insertions = 0
            for record in records():
                for candidate in expand_candidate_records(record, self.label):
                    key = str(candidate["candidate_key"])
                    existing = connection.execute(
                        "SELECT candidate_json FROM candidates WHERE candidate_key=?", (key,)
                    ).fetchone()
                    if existing:
                        current = json.loads(existing[0])
                        current["source_memberships"] = sorted(
                            set(current.get("source_memberships", []))
                            | set(candidate.get("source_memberships", []))
                        )
                        current["evidence_ids"] = sorted(
                            set(current.get("evidence_ids", [])) | set(candidate.get("evidence_ids", []))
                        )
                        candidate = current
                    connection.execute(
                        "INSERT OR REPLACE INTO candidates"
                        "(candidate_key, entity_class, source_native_id, locale, candidate_json) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            key,
                            candidate["entity_class"],
                            str(candidate["source_native_id"]),
                            str(candidate.get("locale") or "en"),
                            canonical_json_bytes(candidate).decode("utf-8"),
                        ),
                    )
                    candidate_insertions += 1
                    if candidate_insertions % 10_000 == 0:
                        connection.commit()
            connection.commit()

            previous_identity: tuple[str, str, str] | None = None
            represented_key = ""
            alias_updates: list[tuple[str, str]] = []
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
                alias_updates.append((canonical_json_bytes(candidate).decode("utf-8"), key))
                if len(alias_updates) >= 10_000:
                    connection.executemany(
                        "UPDATE candidates SET candidate_json=? WHERE candidate_key=?",
                        alias_updates,
                    )
                    connection.commit()
                    alias_updates.clear()
            connection.executemany(
                "UPDATE candidates SET candidate_json=? WHERE candidate_key=?",
                alias_updates,
            )
            connection.commit()

            def candidates() -> Iterator[dict[str, Any]]:
                for (candidate_json,) in connection.execute(
                    "SELECT candidate_json FROM candidates ORDER BY candidate_key"
                ):
                    yield json.loads(candidate_json)

            candidate_output = write_jsonl_gzip_shards(
                self.inventory_root / self.label,
                "hydrated-candidates",
                candidates(),
            )
            candidate_target = Path(candidate_output["root"]) / "index.json"
            candidate_count = int(candidate_output["records"])
            candidate_digest = str(candidate_output["canonical_sha256"])
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
            reconciliation_target = self.reconciliation_root / f"{self.label}-hydrated.json"
            write_text_atomic(reconciliation_target, pretty_json(reconciliation))
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
            }
            write_text_atomic(self.records_root / "manifest.json", pretty_json(manifest))
            return reconciliation
        finally:
            connection.close()
