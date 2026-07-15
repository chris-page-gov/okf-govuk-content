"""Local-only GOV.UK Search part extracts for concept and relationship analysis."""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from .storage import StoragePolicyError, assert_minimum_free_disk
from .util import canonical_json_bytes


class SearchExtractError(RuntimeError):
    """Raised when a local extract database cannot be built safely."""


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._suppressed = 0
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "template"}:
            self._suppressed += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "template"} and self._suppressed:
            self._suppressed -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self.values.append(data)


def extract_plain_text(value: str) -> str:
    """Return bounded, whitespace-normalised text from a Search API extract."""

    if len(value.encode("utf-8")) > 4 * 1024 * 1024:
        raise SearchExtractError("one Search API part extract exceeds 4 MiB")
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = " ".join(parser.values) if "<" in value else html.unescape(value)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def part_metadata(part: dict[str, Any]) -> dict[str, Any]:
    """Return the source-native part fields permitted in the public metadata envelope."""

    return {
        key: part.get(key)
        for key in ("link", "title", "slug", "content_id", "document_type")
        if part.get(key) is not None
    }


def query_extract_database(path: Path, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    if limit < 1 or limit > 100:
        raise SearchExtractError("extract query limit must be between 1 and 100")
    if not query.strip():
        raise SearchExtractError("extract query cannot be empty")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT p.content_id, p.canonical_path, p.parent_title, p.part_link,
                   p.part_title, p.extract_text, p.source_body_sha256,
                   p.relations_json
            FROM search_parts_fts AS f
            JOIN search_parts AS p USING(part_key)
            WHERE search_parts_fts MATCH ?
            ORDER BY bm25(search_parts_fts), p.canonical_path, p.part_index
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
    except sqlite3.Error as exc:
        raise SearchExtractError(f"extract query failed: {exc}") from exc
    finally:
        connection.close()
    return [
        {
            "content_id": row[0],
            "canonical_path": row[1],
            "parent_title": row[2],
            "part_link": row[3],
            "part_title": row[4],
            "extract_text": row[5],
            "source_body_sha256": row[6],
            "relations": json.loads(row[7]),
        }
        for row in rows
    ]


class SearchExtractStore:
    """Resumable SQLite/FTS cache of Search API part snippets.

    Search v1 exposes short ``parts[].body`` extracts for some multi-part
    documents. They are useful for local concept/topic discovery but are not
    part of the release corpus. Only normalised text and its source hash are
    stored; raw HTML and complete GOV.UK page bodies are not retained.
    """

    def __init__(
        self,
        path: Path,
        snapshot: str,
        *,
        minimum_free_bytes: int | None = None,
    ) -> None:
        self.path = path
        self.snapshot = snapshot
        self.minimum_free_bytes = minimum_free_bytes
        if path.is_symlink() or path.parent.is_symlink():
            raise SearchExtractError("extract database path cannot be a symbolic link")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=60)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.fts_enabled = True
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS search_parts (
                part_key TEXT PRIMARY KEY,
                snapshot TEXT NOT NULL,
                search_index_id TEXT NOT NULL,
                content_id TEXT,
                canonical_path TEXT NOT NULL,
                parent_title TEXT NOT NULL,
                document_type TEXT NOT NULL,
                part_index INTEGER NOT NULL,
                part_link TEXT,
                part_slug TEXT,
                part_title TEXT NOT NULL,
                extract_text TEXT NOT NULL,
                source_body_sha256 TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                evidence_url TEXT NOT NULL,
                evidence_sha256 TEXT,
                is_historic INTEGER,
                content_purpose_supergroup TEXT,
                content_purpose_subgroup TEXT
            );
            CREATE INDEX IF NOT EXISTS search_parts_content_id ON search_parts(content_id);
            CREATE INDEX IF NOT EXISTS search_parts_path ON search_parts(canonical_path);
            CREATE INDEX IF NOT EXISTS search_parts_document_type ON search_parts(document_type);
            """
        )
        columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(search_parts)")
        }
        if "relations_json" not in columns:
            self.connection.execute(
                "ALTER TABLE search_parts ADD COLUMN relations_json TEXT NOT NULL DEFAULT '{}'"
            )
        try:
            self.connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS search_parts_fts "
                "USING fts5(part_key UNINDEXED, part_title, extract_text)"
            )
        except sqlite3.OperationalError:
            self.fts_enabled = False
        self.connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema', 'govuk-okf-search-extracts.v2')"
        )
        self.connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('snapshot', ?)",
            (self.snapshot,),
        )
        self.connection.commit()

    @staticmethod
    def _part_key(search_index_id: str, index: int, part: dict[str, Any]) -> str:
        identity = str(part.get("link") or part.get("slug") or index)
        return hashlib.sha256(f"{search_index_id}\0{index}\0{identity}".encode("utf-8")).hexdigest()

    def ingest_result(
        self,
        result: dict[str, Any],
        evidence: dict[str, Any],
    ) -> list[dict[str, Any]]:
        search_index_id = str(result.get("_id") or "")
        if not search_index_id:
            raise SearchExtractError("Search result lacks _id for extract storage")
        parts = result.get("parts")
        if parts is None:
            return []
        if not isinstance(parts, list):
            raise SearchExtractError("Search result parts field is not a list")
        safe_parts: list[dict[str, Any]] = []
        for index, part in enumerate(parts):
            if not isinstance(part, dict):
                raise SearchExtractError("Search result part is not an object")
            safe_parts.append(part_metadata(part))
            source_body = part.get("body")
            if not isinstance(source_body, str) or not source_body.strip():
                continue
            text = extract_plain_text(source_body)
            if not text:
                continue
            part_key = self._part_key(search_index_id, index, part)
            source_hash = hashlib.sha256(source_body.encode("utf-8")).hexdigest()
            retrieved_at = str(evidence.get("retrieved_at") or datetime.now(timezone.utc).isoformat())
            relations = {
                "organisation_content_ids": sorted(
                    {
                        str(value)
                        for value in result.get("organisation_content_ids", [])
                        if value
                    }
                ),
                "organisations": [
                    part_metadata(value)
                    for value in result.get("organisations", [])
                    if isinstance(value, dict)
                ],
                "taxons": [
                    part_metadata(value)
                    for value in result.get("taxons", [])
                    if isinstance(value, dict)
                ],
                "world_locations": [
                    part_metadata(value)
                    for value in result.get("world_locations", [])
                    if isinstance(value, dict)
                ],
            }
            row = (
                part_key,
                self.snapshot,
                search_index_id,
                str(result.get("content_id")) if result.get("content_id") else None,
                str(result.get("link") or search_index_id),
                str(result.get("title") or result.get("link") or search_index_id),
                str(result.get("content_store_document_type") or result.get("format") or "unknown"),
                index,
                str(part.get("link")) if part.get("link") else None,
                str(part.get("slug")) if part.get("slug") else None,
                str(part.get("title") or part.get("slug") or f"Part {index + 1}"),
                text,
                source_hash,
                retrieved_at,
                str(evidence.get("requested_url") or "https://www.gov.uk/api/search.json"),
                str(evidence.get("sha256")) if evidence.get("sha256") else None,
                int(bool(result.get("is_historic"))) if result.get("is_historic") is not None else None,
                str(result.get("content_purpose_supergroup"))
                if result.get("content_purpose_supergroup")
                else None,
                str(result.get("content_purpose_subgroup"))
                if result.get("content_purpose_subgroup")
                else None,
                canonical_json_bytes(relations).decode("utf-8"),
            )
            self.connection.execute(
                """
                INSERT INTO search_parts(
                  part_key, snapshot, search_index_id, content_id, canonical_path,
                  parent_title, document_type, part_index, part_link, part_slug,
                  part_title, extract_text, source_body_sha256, retrieved_at,
                  evidence_url, evidence_sha256, is_historic,
                  content_purpose_supergroup, content_purpose_subgroup, relations_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(part_key) DO UPDATE SET
                  snapshot=excluded.snapshot,
                  content_id=excluded.content_id,
                  canonical_path=excluded.canonical_path,
                  parent_title=excluded.parent_title,
                  document_type=excluded.document_type,
                  part_link=excluded.part_link,
                  part_slug=excluded.part_slug,
                  part_title=excluded.part_title,
                  extract_text=excluded.extract_text,
                  source_body_sha256=excluded.source_body_sha256,
                  retrieved_at=excluded.retrieved_at,
                  evidence_url=excluded.evidence_url,
                  evidence_sha256=excluded.evidence_sha256,
                  is_historic=excluded.is_historic,
                  content_purpose_supergroup=excluded.content_purpose_supergroup,
                  content_purpose_subgroup=excluded.content_purpose_subgroup,
                  relations_json=excluded.relations_json
                """,
                row,
            )
            if self.fts_enabled:
                self.connection.execute("DELETE FROM search_parts_fts WHERE part_key=?", (part_key,))
                self.connection.execute(
                    "INSERT INTO search_parts_fts(part_key, part_title, extract_text) VALUES (?, ?, ?)",
                    (part_key, row[10], text),
                )
        return safe_parts

    def ingest_results(
        self,
        results: Iterable[dict[str, Any]],
        evidence: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows = list(results)
        safe: list[dict[str, Any]] = []
        try:
            if self.minimum_free_bytes is not None:
                reserve = 8 * 1024 * 1024
                for result in rows:
                    parts = result.get("parts")
                    for part in parts if isinstance(parts, list) else []:
                        if isinstance(part, dict) and isinstance(part.get("body"), str):
                            reserve += len(part["body"].encode("utf-8")) * 2
                assert_minimum_free_disk(
                    self.path,
                    self.minimum_free_bytes,
                    reserve_bytes=reserve,
                    phase="Search extract database write",
                )
            self.connection.execute("BEGIN")
            for result in rows:
                clean = dict(result)
                clean["parts"] = self.ingest_result(result, evidence)
                safe.append(clean)
            self.connection.commit()
        except StoragePolicyError as exc:
            self.connection.rollback()
            raise SearchExtractError(str(exc)) from exc
        except Exception:
            self.connection.rollback()
            raise
        return safe

    def summary(self) -> dict[str, Any]:
        count, text_bytes, distinct_documents = self.connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(extract_text AS BLOB))), 0), "
            "COUNT(DISTINCT search_index_id) FROM search_parts"
        ).fetchone()
        digest = hashlib.sha256()
        for part_key, source_hash in self.connection.execute(
            "SELECT part_key, source_body_sha256 FROM search_parts ORDER BY part_key"
        ):
            digest.update(f"{part_key}\0{source_hash}\n".encode("utf-8"))
        return {
            "schema": "govuk-okf-search-extract-cache-summary.v1",
            "snapshot": self.snapshot,
            "local_only": True,
            "publication_dependency": False,
            "contains_complete_page_bodies": False,
            "source_field": "Search API v1 parts[].body extracts",
            "extract_rows": int(count),
            "documents_with_extracts": int(distinct_documents),
            "extract_text_bytes": int(text_bytes),
            "extract_hash_set_sha256": digest.hexdigest(),
            "fts5_enabled": self.fts_enabled,
            "database_filename": self.path.name,
        }

    def write_sidecar_manifest(self) -> Path:
        path = self.path.with_suffix(".manifest.json")
        document = json.dumps(self.summary(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if self.minimum_free_bytes is not None:
            try:
                assert_minimum_free_disk(
                    path,
                    self.minimum_free_bytes,
                    reserve_bytes=len(document.encode("utf-8")) * 2,
                    phase="Search extract sidecar write",
                )
            except StoragePolicyError as exc:
                raise SearchExtractError(str(exc)) from exc
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(document, encoding="utf-8")
        temporary.replace(path)
        return path

    def close(self) -> None:
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.connection.close()

    def __enter__(self) -> SearchExtractStore:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self.write_sidecar_manifest()
        self.close()
