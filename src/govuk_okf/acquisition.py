"""Resumable public-source census and metadata acquisition."""

from __future__ import annotations

import collections
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import threading
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from .util import canonical_json_bytes, pretty_json, sha256_bytes, slugify
from .webprobe import Probe, fetch_probe

ROOT = Path(__file__).resolve().parents[2]
SEARCH_URL = "https://www.gov.uk/api/search.json"
SITEMAP_URL = "https://www.gov.uk/sitemap.xml"
ORGANISATIONS_URL = "https://www.gov.uk/api/organisations"
CONTENT_API_ROOT = "https://www.gov.uk/api/content"
SEARCH_PAGE_SIZE = 1500
SEARCH_FIELDS = [
    "title",
    "link",
    "description",
    "format",
    "content_store_document_type",
    "public_timestamp",
    "organisation_content_ids",
    "taxons",
    "world_locations",
]
NAVIGATION_LINKS = {
    "level_one_taxons",
    "child_taxons",
    "parent_taxons",
    "top_level_browse_pages",
    "second_level_browse_pages",
    "mainstream_browse_pages",
    "available_translations",
    "document_collections",
    "related_to_step_navs",
    "part_of_step_navs",
}
CURATED_CONTENT_PATHS = (
    "",
    "/browse",
    "/world/all",
    "/government",
    "/government/organisations",
    "/government/world",
    "/government/how-government-works",
    "/government/publications",
    "/government/statistics",
    "/government/consultations",
    "/government/announcements",
)
CREDENTIAL_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "key",
    "password",
    "secret",
    "sig",
    "signature",
    "token",
}
TRACKING_QUERY_PREFIXES = ("utm_",)


class AcquisitionError(RuntimeError):
    """Raised when a source snapshot cannot be closed safely."""


def search_partition_value(option: Any) -> str:
    """Return the exact filter value from a Search API aggregate option.

    Search API facet values can be strings or small objects.  In particular,
    ``content_store_document_type`` is currently returned as ``{"slug": ...}``.
    Stringifying the object creates a syntactically valid but false filter, so
    reject every object shape other than the documented single-slug form.
    """

    if not isinstance(option, dict) or "value" not in option:
        raise AcquisitionError("Search partition option has no value")
    raw = option["value"]
    if isinstance(raw, str):
        value = raw
    elif isinstance(raw, dict):
        if set(raw) != {"slug"} or not isinstance(raw.get("slug"), str):
            raise AcquisitionError("Search partition option has an ambiguous object value")
        value = raw["slug"]
    else:
        raise AcquisitionError("Search partition option value is not a string or slug object")
    if not value or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
        raise AcquisitionError("Search partition option has an unsafe filter value")
    return value


@dataclass
class HostLimiter:
    requests_per_second: float
    state_path: Path | None = None
    budget_path: Path | None = None
    max_requests: int | None = None
    last_request: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _reserve_budget(self) -> None:
        if self.max_requests is None:
            return
        if self.max_requests < 1 or self.budget_path is None:
            raise AcquisitionError("a positive request ceiling requires a shared budget ledger")
        self.budget_path.parent.mkdir(parents=True, exist_ok=True)
        with self.budget_path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.seek(0)
            try:
                consumed = int(stream.read().strip() or "0")
            except ValueError as exc:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                raise AcquisitionError(f"invalid shared request-budget ledger: {self.budget_path}") from exc
            if consumed >= self.max_requests:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                raise AcquisitionError(
                    f"official-source request ceiling exhausted: {consumed}/{self.max_requests}"
                )
            stream.seek(0)
            stream.truncate()
            stream.write(f"{consumed + 1}\n")
            stream.flush()
            os.fsync(stream.fileno())
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def wait(self) -> None:
        if self.requests_per_second <= 0:
            raise AcquisitionError("rate limit must be greater than zero")
        self._reserve_budget()
        interval = 1.0 / self.requests_per_second
        if self.state_path is None:
            with self._lock:
                delay = self.last_request + interval - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self.last_request = time.monotonic()
            return

        # Reserve a request slot under a cross-process file lock, then sleep
        # outside the lock. Every process and worker for a host shares this
        # single timestamp ledger, so concurrency cannot multiply the rate.
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.state_path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.seek(0)
            try:
                previous = float(stream.read().strip() or "0")
            except ValueError:
                previous = 0.0
            now = time.time()
            reserved = max(now, previous + interval)
            stream.seek(0)
            stream.truncate()
            stream.write(f"{reserved:.9f}\n")
            stream.flush()
            os.fsync(stream.fileno())
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        delay = reserved - time.time()
        if delay > 0:
            time.sleep(delay)


def request_observation(
    url: str,
    *,
    limiter: HostLimiter,
    max_bytes: int = 64 * 1024 * 1024,
    attempts: int = 5,
) -> tuple[bytes, dict[str, Any]]:
    """Fetch one public observation with every attempt passing the host bucket."""
    retryable = {0, 408, 429, 500, 502, 503, 504}
    final_body = b""
    final_result: dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        limiter.wait()
        result = fetch_probe(Probe("acquisition", url, "enumerator", max_bytes=max_bytes), attempts=1)
        body = result.pop("body", b"")
        result["acquisition_attempt"] = attempt
        final_body, final_result = body, result
        if result.get("ok") or int(result.get("status") or 0) not in retryable:
            break
        if attempt < attempts:
            retry_after = str((result.get("headers") or {}).get("retry-after") or "")
            try:
                server_delay = float(retry_after)
            except ValueError:
                server_delay = 0.0
            jitter_seed = hashlib.sha256(f"{url}\0{attempt}".encode("utf-8")).digest()[0] / 2550
            time.sleep(max(server_delay, min(8.0, 0.5 * (2 ** (attempt - 1))) + jitter_seed))
    return final_body, final_result


def request_bytes(url: str, *, limiter: HostLimiter, max_bytes: int = 64 * 1024 * 1024) -> tuple[bytes, dict[str, Any]]:
    body, result = request_observation(url, limiter=limiter, max_bytes=max_bytes)
    if not result.get("ok"):
        raise AcquisitionError(f"request failed for {url}: {result.get('status')} {result.get('error', '')}")
    if result.get("partial"):
        raise AcquisitionError(f"response exceeded {max_bytes} bytes for {url}")
    return body, result


def write_jsonl_gzip(path: Path, records: Iterable[dict[str, Any]]) -> tuple[int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    count = 0
    digest = hashlib.sha256()
    with temporary.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as compressed:
            for record in records:
                line = canonical_json_bytes(record)
                digest.update(line)
                compressed.write(line)
                count += 1
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(temporary, path)
    return count, digest.hexdigest()


def read_jsonl_gzip(path: Path, *, max_record_bytes: int = 16 * 1024 * 1024) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rb") as stream:
        while True:
            line = stream.readline(max_record_bytes + 1)
            if not line:
                break
            if len(line) > max_record_bytes:
                raise AcquisitionError(f"gzip JSONL record exceeds {max_record_bytes} bytes: {path}")
            if line.strip():
                value = json.loads(line.decode("utf-8"))
                if not isinstance(value, dict):
                    raise AcquisitionError(f"gzip JSONL record is not an object: {path}")
                yield value


def write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def write_jsonl_gzip_shards(
    parent: Path,
    name: str,
    records: Iterable[dict[str, Any]],
    *,
    max_records: int = 10_000,
    max_uncompressed_bytes: int = 32 * 1024 * 1024,
    max_compressed_bytes: int = 50 * 1024 * 1024,
) -> dict[str, Any]:
    """Write immutable content-addressed JSONL.GZ shards below ``parent``."""
    if max_records < 1 or max_uncompressed_bytes < 1:
        raise AcquisitionError("JSONL shard limits must be positive")
    parent.mkdir(parents=True, exist_ok=True)
    build_root = Path(tempfile.mkdtemp(prefix=f".{name}.building-", dir=parent))
    aggregate = hashlib.sha256()
    total = 0
    shard_rows: list[dict[str, Any]] = []
    chunk: list[dict[str, Any]] = []
    chunk_bytes = 0

    def flush() -> None:
        nonlocal chunk, chunk_bytes
        if not chunk:
            return
        relative = f"part-{len(shard_rows):05d}.jsonl.gz"
        path = build_root / relative
        count, digest = write_jsonl_gzip(path, chunk)
        compressed_bytes = path.stat().st_size
        if compressed_bytes > max_compressed_bytes:
            raise AcquisitionError(f"compressed JSONL shard exceeds {max_compressed_bytes} bytes: {path}")
        shard_rows.append(
            {
                "path": relative,
                "records": count,
                "bytes": compressed_bytes,
                "canonical_sha256": digest,
                "file_sha256": sha256_bytes(path.read_bytes()),
            }
        )
        chunk = []
        chunk_bytes = 0

    try:
        for record in records:
            line = canonical_json_bytes(record)
            if len(line) > max_uncompressed_bytes:
                raise AcquisitionError("one JSONL record exceeds the uncompressed shard budget")
            if chunk and (len(chunk) >= max_records or chunk_bytes + len(line) > max_uncompressed_bytes):
                flush()
            aggregate.update(line)
            total += 1
            chunk.append(record)
            chunk_bytes += len(line)
        flush()
        digest = aggregate.hexdigest()
        index = {
            "schema": "govuk-okf-jsonl-shards.v1",
            "records": total,
            "canonical_sha256": digest,
            "max_records_per_shard": max_records,
            "max_uncompressed_bytes_per_shard": max_uncompressed_bytes,
            "max_compressed_bytes_per_shard": max_compressed_bytes,
            "shards": shard_rows,
        }
        write_text_atomic(build_root / "index.json", pretty_json(index))
        final_root = parent / f"{name}-{digest[:16]}"
        if final_root.exists():
            expected = {
                path.relative_to(build_root): sha256_bytes(path.read_bytes())
                for path in build_root.rglob("*")
                if path.is_file()
            }
            actual = {
                path.relative_to(final_root): sha256_bytes(path.read_bytes())
                for path in final_root.rglob("*")
                if path.is_file()
            }
            if actual != expected:
                raise AcquisitionError(f"content-addressed shard directory differs: {final_root}")
            shutil.rmtree(build_root)
        else:
            os.replace(build_root, final_root)
        return {**index, "root": final_root}
    except Exception:
        if build_root.exists():
            shutil.rmtree(build_root)
        raise


def parse_sitemap(body: bytes) -> list[dict[str, str | None]]:
    root = ET.fromstring(body)
    if root.tag.endswith("sitemapindex"):
        return [
            {
                "url": next((child.text for child in element if child.tag.endswith("loc")), None),
                "lastmod": next((child.text for child in element if child.tag.endswith("lastmod")), None),
            }
            for element in root
            if element.tag.endswith("sitemap")
        ]
    if root.tag.endswith("urlset"):
        return [
            {
                "url": next((child.text for child in element if child.tag.endswith("loc")), None),
                "lastmod": next((child.text for child in element if child.tag.endswith("lastmod")), None),
            }
            for element in root
            if element.tag.endswith("url")
        ]
    raise AcquisitionError(f"unexpected sitemap root: {root.tag}")


def normalise_url(value: str) -> str:
    if value.startswith("//"):
        raise AcquisitionError("candidate URL uses an ambiguous scheme-relative form")
    if value.startswith("/"):
        value = "https://www.gov.uk" + value
    if any(ord(character) < 32 for character in value) or "\\" in value:
        raise AcquisitionError("candidate URL contains unsafe characters")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise AcquisitionError("candidate URL uses an unsupported scheme")
    if parsed.username or parsed.password or not parsed.hostname:
        raise AcquisitionError("candidate URL contains userinfo or has no host")
    try:
        ascii_host = parsed.hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise AcquisitionError("candidate URL host is not valid IDNA") from exc
    scheme = "https" if ascii_host in {"gov.uk", "www.gov.uk"} else parsed.scheme
    host = "www.gov.uk" if ascii_host in {"gov.uk", "www.gov.uk"} else ascii_host
    if parsed.port and not ((scheme == "https" and parsed.port == 443) or (scheme == "http" and parsed.port == 80)):
        host = f"{host}:{parsed.port}"
    path = parsed.path or "/"
    query_pairs = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        folded = key.casefold()
        if folded in CREDENTIAL_QUERY_KEYS or any(folded.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        query_pairs.append((key, item))
    return urlunsplit((scheme, host, path, urlencode(query_pairs, doseq=True), ""))


def candidate_key(
    url: str, locale: str = "en", entity_class: str = "route", source_native_id: str | None = None
) -> str:
    identity = source_native_id or url
    return hashlib.sha256(f"{entity_class}\0{identity}\0{locale}\0{url}".encode("utf-8")).hexdigest()


def search_result_record(
    result: dict[str, Any], membership: str, evidence: dict[str, Any] | str, result_index: int = 0
) -> dict[str, Any]:
    if isinstance(evidence, str):
        evidence = {"retrieved_at": evidence, "requested_url": SEARCH_URL, "sha256": None}
    retrieved_at = str(evidence["retrieved_at"])
    url = normalise_url(str(result.get("link") or result.get("url") or result.get("_id") or ""))
    host = urlparse(url).netloc
    links: dict[str, list[dict[str, Any]]] = {}
    for predicate in ("taxons", "world_locations"):
        values = result.get(predicate) or []
        if isinstance(values, list):
            links[predicate] = [item for item in values if isinstance(item, dict)]
    document_type = str(result.get("content_store_document_type") or result.get("format") or "unknown")
    return {
        "candidate_key": candidate_key(url, "en", "external_boundary" if host != "www.gov.uk" else "route"),
        "entity_class": "external_boundary" if host != "www.gov.uk" else "route",
        "source_native_id": url,
        "source_id": "search-api-v1",
        "source_memberships": [membership],
        "coverage_disposition": "represented",
        "canonical_url": url,
        "base_path": urlparse(url).path,
        "title": str(result.get("title") or urlparse(url).path),
        "description": str(result.get("description") or ""),
        "document_type": "external_content" if host != "www.gov.uk" else document_type,
        "schema_name": document_type,
        "locale": "en",
        "public_updated_at": result.get("public_timestamp"),
        "links": links,
        "organisation_content_ids": [str(value) for value in result.get("organisation_content_ids", []) if value],
        "retrieved_at": retrieved_at,
        "evidence_url": evidence.get("requested_url") or SEARCH_URL,
        "evidence_sha256": evidence.get("sha256"),
        "evidence_locator": f"/results/{result_index}",
        "source_adapter": "govuk_search_api_v1",
    }


def sitemap_record(item: dict[str, str | None], evidence: dict[str, Any] | str, item_index: int = 0) -> dict[str, Any]:
    if isinstance(evidence, str):
        evidence = {"retrieved_at": evidence, "requested_url": SITEMAP_URL, "sha256": None}
    retrieved_at = str(evidence["retrieved_at"])
    url = normalise_url(str(item["url"]))
    title = urlparse(url).path.strip("/").rsplit("/", 1)[-1].replace("-", " ").strip().title() or "GOV.UK"
    return {
        "candidate_key": candidate_key(url),
        "entity_class": "route",
        "source_native_id": url,
        "source_id": "govuk-sitemap",
        "source_memberships": ["sitemap"],
        "coverage_disposition": "represented",
        "canonical_url": url,
        "base_path": urlparse(url).path,
        "title": title,
        "description": "",
        "document_type": "sitemap_route",
        "schema_name": "unknown",
        "locale": "en",
        "lastmod": item.get("lastmod"),
        "links": {},
        "retrieved_at": retrieved_at,
        "evidence_url": evidence.get("requested_url") or SITEMAP_URL,
        "evidence_sha256": evidence.get("sha256"),
        "evidence_locator": f"/urlset/url/{item_index}",
        "source_adapter": "govuk_sitemap",
    }


def merge_records(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    memberships = sorted(set(existing.get("source_memberships", [])) | set(incoming.get("source_memberships", [])))
    source_priority = {
        "content-api": 50,
        "organisations-api": 40,
        "search-api-v1": 30,
        "govuk-sitemap": 10,
    }
    preferred = max(
        (existing, incoming),
        key=lambda item: (
            item.get("coverage_disposition") != "exceptioned",
            source_priority.get(str(item.get("source_id")), 20),
            item.get("document_type") not in {"sitemap_route", "unknown"},
            bool(item.get("content_id")),
        ),
    )
    merged = {**existing, **preferred}
    for key, value in incoming.items():
        if key not in merged or merged[key] is None or merged[key] == "" or merged[key] == "unknown" or merged[key] == []:
            merged[key] = value
    merged["source_memberships"] = memberships
    merged["candidate_key"] = existing["candidate_key"]
    observations: dict[bytes, dict[str, Any]] = {}
    for item in (existing, incoming):
        supplied = item.get("evidence_observations")
        rows = supplied if isinstance(supplied, list) else []
        if item.get("evidence_url"):
            rows = [
                *rows,
                {
                    "source_id": item.get("source_id"),
                    "url": item.get("evidence_url"),
                    "sha256": item.get("evidence_sha256"),
                    "locator": item.get("evidence_locator") or "/",
                    "retrieved_at": item.get("retrieved_at"),
                },
            ]
        for row in rows:
            if isinstance(row, dict):
                observations[canonical_json_bytes(row)] = row
    merged["evidence_observations"] = [observations[key] for key in sorted(observations)]
    return merged


def sanitise_content_api(payload: dict[str, Any], evidence: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(evidence, str):
        evidence = {"retrieved_at": evidence, "requested_url": CONTENT_API_ROOT, "sha256": None}
    retrieved_at = str(evidence["retrieved_at"])
    base_path = str(payload.get("base_path") or "/")
    url = normalise_url(base_path)
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    attachments = details.get("attachments", []) if isinstance(details, dict) else []
    safe_attachments = []
    for attachment in attachments if isinstance(attachments, list) else []:
        if not isinstance(attachment, dict):
            continue
        safe_attachments.append(
            {
                key: attachment.get(key)
                for key in (
                    "id",
                    "title",
                    "attachment_type",
                    "content_type",
                    "filename",
                    "file_size",
                    "number_of_pages",
                    "accessible",
                    "alternative_format_contact_email",
                    "url",
                )
                if attachment.get(key) is not None
            }
        )
    return {
        "candidate_key": candidate_key(
            url,
            str(payload.get("locale") or "en"),
            "content_identity" if payload.get("content_id") else "route",
            str(payload.get("content_id")) if payload.get("content_id") else url,
        ),
        "entity_class": "content_identity" if payload.get("content_id") else "route",
        "source_native_id": payload.get("content_id") or url,
        "source_id": "content-api",
        "source_memberships": ["structured-content-api"],
        "coverage_disposition": "redirect_only" if payload.get("document_type") == "redirect" else "represented",
        "content_id": payload.get("content_id"),
        "canonical_url": url,
        "base_path": base_path,
        "title": str(payload.get("title") or base_path),
        "description": str(payload.get("description") or ""),
        "document_type": str(payload.get("document_type") or "unknown"),
        "schema_name": str(payload.get("schema_name") or "unknown"),
        "locale": str(payload.get("locale") or "en"),
        "first_published_at": payload.get("first_published_at"),
        "public_updated_at": payload.get("public_updated_at"),
        "updated_at": payload.get("updated_at"),
        "withdrawn_notice": payload.get("withdrawn_notice"),
        "redirects": payload.get("redirects") or [],
        "links": payload.get("links") or {},
        "details": {"attachments": safe_attachments} if safe_attachments else {},
        "retrieved_at": retrieved_at,
        "evidence_url": evidence.get("requested_url") or CONTENT_API_ROOT + base_path,
        "evidence_sha256": evidence.get("sha256"),
        "evidence_locator": "/",
        "source_adapter": "govuk_content_api",
    }


def _evidence_ids(record: dict[str, Any]) -> list[str]:
    observations = record.get("evidence_observations")
    rows = observations if isinstance(observations, list) else []
    if record.get("evidence_url"):
        rows = [
            *rows,
            {
                "source_id": record.get("source_id"),
                "url": record.get("evidence_url"),
                "sha256": record.get("evidence_sha256"),
                "locator": record.get("evidence_locator") or "/",
                "retrieved_at": record.get("retrieved_at"),
            },
        ]
    return sorted(
        {
            "evidence-" + hashlib.sha256(canonical_json_bytes(row)).hexdigest()
            for row in rows
            if isinstance(row, dict) and row.get("url")
        }
    )


def expand_candidate_records(record: dict[str, Any], snapshot_id: str) -> list[dict[str, Any]]:
    """Project one source envelope into disjoint, countable native entities."""
    url = normalise_url(str(record.get("canonical_url") or record.get("base_path") or "/"))
    locale = str(record.get("locale") or "en")
    content_id = str(record["content_id"]) if record.get("content_id") else None
    evidence_ids = _evidence_ids(record)
    base = {
        "snapshot_id": snapshot_id,
        "locale": locale,
        "route_or_resource_uri": url,
        "canonical_content_id": content_id,
        "source_memberships": sorted(set(record.get("source_memberships") or [record.get("source_id") or "unknown"])),
        "constraints": list(record.get("constraints") or []),
        "evidence_ids": evidence_ids,
        "title": record.get("title"),
        "document_type": record.get("document_type"),
        "schema_name": record.get("schema_name"),
    }
    original_disposition = str(record.get("coverage_disposition") or "represented")
    route_disposition = (
        "exceptioned"
        if original_disposition == "exceptioned"
        else "redirect_only"
        if record.get("document_type") == "redirect" or record.get("redirects")
        else "represented"
    )
    host = urlparse(url).netloc
    declared_class = str(record.get("entity_class") or "")
    route_class = (
        declared_class
        if declared_class in {"resource", "external_boundary"}
        else "external_boundary"
        if host != "www.gov.uk"
        else "route"
    )

    def candidate(entity_class: str, native_id: str, disposition: str = "represented") -> dict[str, Any]:
        return {
            **base,
            "candidate_key": candidate_key(url, locale, entity_class, native_id),
            "entity_class": entity_class,
            "source_native_id": native_id,
            "coverage_disposition": disposition,
            "disposition_target": None,
        }

    candidates = [candidate(route_class, url, route_disposition)]
    if content_id:
        candidates.append(candidate("content_identity", content_id))
        candidates.append(candidate("document", f"{content_id}:{locale}"))
        edition_time = record.get("public_updated_at") or record.get("updated_at") or record.get("lastmod")
        if edition_time:
            candidates.append(candidate("edition", f"{content_id}:{locale}:{edition_time}"))

    details = record.get("details") if isinstance(record.get("details"), dict) else {}
    attachments = details.get("attachments") if isinstance(details, dict) else []
    for attachment in attachments if isinstance(attachments, list) else []:
        if not isinstance(attachment, dict):
            continue
        resource_url = str(attachment.get("url") or "")
        if not resource_url.startswith(("https://", "http://")):
            continue
        resource_url = normalise_url(resource_url)
        native_id = str(attachment.get("id") or resource_url)
        resource_base = {
            **base,
            "route_or_resource_uri": resource_url,
            "parent_route": url,
            "title": attachment.get("title") or attachment.get("filename"),
            "content_type": attachment.get("content_type"),
            "bytes": attachment.get("file_size"),
        }
        candidates.append(
            {
                **resource_base,
                "candidate_key": candidate_key(resource_url, locale, "resource", native_id),
                "entity_class": "resource",
                "source_native_id": native_id,
                "coverage_disposition": "represented",
                "disposition_target": None,
            }
        )
    return candidates


def build_candidate_ledger(records: Iterable[dict[str, Any]], snapshot_id: str) -> dict[str, dict[str, Any]]:
    ledger: dict[str, dict[str, Any]] = {}
    for record in records:
        expanded = expand_candidate_records(record, snapshot_id)
        record["candidate_keys"] = sorted(candidate["candidate_key"] for candidate in expanded)
        for candidate in expanded:
            key = str(candidate["candidate_key"])
            if key not in ledger:
                ledger[key] = candidate
                continue
            existing = ledger[key]
            existing["source_memberships"] = sorted(
                set(existing.get("source_memberships", [])) | set(candidate.get("source_memberships", []))
            )
            constraint_rows = {
                canonical_json_bytes(value): value
                for value in [*existing.get("constraints", []), *candidate.get("constraints", [])]
            }
            existing["constraints"] = [constraint_rows[value] for value in sorted(constraint_rows)]
            existing["evidence_ids"] = sorted(
                set(existing.get("evidence_ids", [])) | set(candidate.get("evidence_ids", []))
            )

    identities: dict[tuple[str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for candidate in ledger.values():
        if candidate["entity_class"] in {"content_identity", "document", "edition"}:
            identities[
                (
                    str(candidate["entity_class"]),
                    str(candidate["source_native_id"]),
                    str(candidate.get("locale") or "en"),
                )
            ].append(candidate)
    for duplicates in identities.values():
        represented = sorted(duplicates, key=lambda item: str(item["candidate_key"]))[0]
        for candidate in duplicates:
            if candidate is represented:
                continue
            candidate["coverage_disposition"] = "alias_of_represented"
            candidate["disposition_target"] = represented["candidate_key"]
    return ledger


class SnapshotBuilder:
    def __init__(self, root: Path, label: str, *, search_rate: float = 2.0, content_rate: float = 8.0) -> None:
        self.root = root
        self.label = label
        self.cache = root / "corpus" / "cache" / label
        self.manifest_root = root / "corpus" / "source-manifests" / label
        self.inventory_root = root / "corpus" / "inventory"
        shared_rate = min(search_rate, content_rate)
        self.govuk_limiter = HostLimiter(
            shared_rate,
            state_path=root / ".tmp" / "rate-limits" / "www.gov.uk.timestamp",
            budget_path=root / ".tmp" / "request-budget" / "official-sources.count",
            max_requests=1_000_000,
        )
        self.search_limiter = self.govuk_limiter
        self.content_limiter = self.govuk_limiter
        self.source_events: list[dict[str, Any]] = []
        self.search_partition_proofs: list[dict[str, Any]] = []
        self.sitemap_stable = False
        self.sitemap_proof: dict[str, Any] = {}
        self.organisations_proof: dict[str, Any] = {}
        self.navigation_proof: dict[str, Any] = {}
        self.started_at = datetime.now(timezone.utc).isoformat()

    def cached_json(self, path: Path, url: str, limiter: HostLimiter) -> tuple[Any, dict[str, Any]]:
        if path.is_file():
            try:
                with gzip.open(path, "rb") as stream:
                    body = stream.read(64 * 1024 * 1024 + 1)
                if len(body) > 64 * 1024 * 1024:
                    raise AcquisitionError(f"cached response exceeds 64 MiB: {path}")
                return json.loads(body), {
                    "requested_url": url,
                    "final_url": url,
                    "status": 200,
                    "retrieved_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                    "bytes_retained": len(body),
                    "sha256": sha256_bytes(body),
                    "cache_hit": True,
                }
            except (gzip.BadGzipFile, EOFError, OSError, UnicodeDecodeError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
        body, evidence = request_bytes(url, limiter=limiter)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as compressed:
                compressed.write(body)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, path)
        return json.loads(body), evidence

    def enumerate_search(self, opposing: bool = True, record_limit: int | None = None) -> dict[str, dict[str, Any]]:
        aggregate_params = [("count", 0), ("aggregate_content_store_document_type", 1000)]
        count_url = SEARCH_URL + "?" + urlencode(aggregate_params)
        count_payload, count_evidence = self.cached_json(
            self.cache / "search" / "root-count.json.gz",
            count_url,
            self.search_limiter,
        )
        total_start = int(count_payload["total"])
        self.source_events.append({"source": "search-api-v1-count-start", **count_evidence, "reported_total": total_start})
        records: dict[str, dict[str, Any]] = {}
        orders = ["public_timestamp", "-public_timestamp"] if opposing else ["public_timestamp"]
        global_leaf_urls: set[str] = set()
        if record_limit is not None:
            leaves = [{"value": "__sample__", "expected": min(total_start, record_limit), "filter": None}]
        else:
            aggregate = (count_payload.get("aggregates") or {}).get("content_store_document_type") or {}
            options = aggregate.get("options") if isinstance(aggregate, dict) else None
            if not isinstance(options, list) or not options:
                raise AcquisitionError("Search did not return content_store_document_type partition options")
            leaves = []
            seen_partition_values: set[str] = set()
            for option in options:
                value = search_partition_value(option)
                if value in seen_partition_values:
                    raise AcquisitionError(f"Search returned duplicate partition option: {value}")
                seen_partition_values.add(value)
                expected = int(option.get("documents") if option.get("documents") is not None else option.get("count", 0))
                if expected < 0:
                    raise AcquisitionError(f"Search partition {value} has a negative document count")
                leaves.append({"value": value, "expected": expected, "filter": value})
            missing_url = SEARCH_URL + "?" + urlencode(
                [("count", 0), ("filter_content_store_document_type", "_MISSING")]
            )
            missing_payload, missing_evidence = self.cached_json(
                self.cache / "search" / "missing-content-store-document-type" / "count.json.gz",
                missing_url,
                self.search_limiter,
            )
            missing_count = int(missing_payload["total"])
            self.source_events.append(
                {
                    "source": "search-api-v1-leaf-count",
                    "partition": "_MISSING",
                    "reported_total": missing_count,
                    **missing_evidence,
                }
            )
            if missing_count:
                leaves.append({"value": "_MISSING", "expected": missing_count, "filter": "_MISSING"})
            if sum(int(leaf["expected"]) for leaf in leaves) != total_start:
                raise AcquisitionError("Search partition counts do not reconcile to the root total")

        for leaf in sorted(leaves, key=lambda item: str(item["value"])):
            value = str(leaf["value"])
            expected = int(leaf["expected"])
            filter_value = leaf["filter"]
            leaf_slug = slugify(value)[:60] + "-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
            if filter_value is not None:
                leaf_count_params = [("count", 0), ("filter_content_store_document_type", filter_value)]
                leaf_count_url = SEARCH_URL + "?" + urlencode(leaf_count_params)
                leaf_count_payload, leaf_count_evidence = self.cached_json(
                    self.cache / "search" / leaf_slug / "count.json.gz",
                    leaf_count_url,
                    self.search_limiter,
                )
                observed_leaf_total = int(leaf_count_payload["total"])
                if observed_leaf_total != expected:
                    raise AcquisitionError(
                        f"Search partition {value} changed between aggregate and leaf count: {expected} != {observed_leaf_total}"
                    )
                self.source_events.append(
                    {
                        "source": "search-api-v1-leaf-count",
                        "partition": value,
                        "reported_total": observed_leaf_total,
                        **leaf_count_evidence,
                    }
                )
            proof = {
                "partition": value,
                "predicate": {"filter_content_store_document_type": filter_value} if filter_value is not None else {},
                "expected": expected,
                "maximum_page_size": SEARCH_PAGE_SIZE,
                "passes": [],
            }
            opposing_sets: list[set[str]] = []
            for order in orders:
                pass_name = "ascending" if not order.startswith("-") else "descending"
                returned = 0
                pass_urls: set[str] = set()
                for start in range(0, expected, SEARCH_PAGE_SIZE):
                    page_count = min(SEARCH_PAGE_SIZE, expected - start)
                    params: list[tuple[str, Any]] = [("count", page_count), ("start", start), ("order", order)]
                    if filter_value is not None:
                        params.append(("filter_content_store_document_type", filter_value))
                    params.extend(("fields", field) for field in SEARCH_FIELDS)
                    url = SEARCH_URL + "?" + urlencode(params)
                    payload, evidence = self.cached_json(
                        self.cache / "search" / leaf_slug / f"{pass_name}-{start:09d}.json.gz",
                        url,
                        self.search_limiter,
                    )
                    rows = payload.get("results", [])
                    if not isinstance(rows, list):
                        raise AcquisitionError(f"Search partition {value} page {start} has no result list")
                    page_total = int(payload.get("total", expected))
                    if page_total != expected and record_limit is None:
                        raise AcquisitionError(
                            f"Search partition {value} page {start} total changed: {page_total} != {expected}"
                        )
                    returned += len(rows)
                    for result_index, row in enumerate(rows):
                        record = search_result_record(
                            row,
                            f"search-{value}-{pass_name}",
                            evidence,
                            result_index,
                        )
                        key = record["canonical_url"]
                        pass_urls.add(key)
                        records[key] = merge_records(records[key], record) if key in records else record
                    self.source_events.append(
                        {
                            "source": f"search-api-v1-{pass_name}",
                            "partition": value,
                            "page_start": start,
                            "returned": len(rows),
                            **evidence,
                        }
                    )
                    if len(rows) != page_count:
                        raise AcquisitionError(
                            f"Search partition {value} returned short page at {start}: {len(rows)} != {page_count}"
                        )
                if returned != expected:
                    raise AcquisitionError(f"Search partition {value} did not close: {returned} != {expected}")
                if len(pass_urls) != expected:
                    raise AcquisitionError(
                        f"Search partition {value} {pass_name} pass repeated identities: "
                        f"{len(pass_urls)} unique != {expected} returned"
                    )
                opposing_sets.append(pass_urls)
                proof["passes"].append(
                    {
                        "order": order,
                        "returned_rows": returned,
                        "unique_urls": len(pass_urls),
                        "identity_sha256": hashlib.sha256("\n".join(sorted(pass_urls)).encode("utf-8")).hexdigest(),
                        "closed": True,
                    }
                )
            if len(opposing_sets) == 2 and opposing_sets[0] != opposing_sets[1]:
                raise AcquisitionError(f"Search partition {value} opposing passes returned different identities")
            leaf_urls = opposing_sets[0] if opposing_sets else set()
            overlap = global_leaf_urls & leaf_urls
            if overlap:
                raise AcquisitionError(
                    f"Search partitions are not disjoint; {value} repeats {len(overlap)} identities"
                )
            global_leaf_urls.update(leaf_urls)
            proof["sibling_disjoint"] = True
            self.search_partition_proofs.append(proof)
        if record_limit is None and len(global_leaf_urls) != total_start:
            raise AcquisitionError(
                f"Search partition tree did not reconcile globally: {len(global_leaf_urls)} != {total_start}"
            )
        count_end_body, count_end_evidence = request_bytes(count_url, limiter=self.search_limiter)
        total_end = int(json.loads(count_end_body)["total"])
        self.source_events.append(
            {
                "source": "search-api-v1-count-end",
                **count_end_evidence,
                "reported_total": total_end,
                "delta_from_start": total_end - total_start,
            }
        )
        return records

    def enumerate_sitemap(self, verify_stable: bool = True, shard_limit: int | None = None) -> dict[str, dict[str, Any]]:
        index_body, index_evidence = request_bytes(SITEMAP_URL, limiter=self.search_limiter)
        index_rows = parse_sitemap(index_body)
        declared_shards = len(index_rows)
        self.source_events.append({"source": "sitemap-index", **index_evidence, "declared_shards": len(index_rows)})
        if shard_limit is not None:
            index_rows = index_rows[:shard_limit]
        records: dict[str, dict[str, Any]] = {}
        first_hashes: dict[str, str] = {}
        shard_proofs: list[dict[str, Any]] = []
        raw_entries = 0
        for ordinal, item in enumerate(index_rows):
            url = str(item["url"])
            body, evidence = request_bytes(url, limiter=self.search_limiter, max_bytes=64 * 1024 * 1024)
            first_hashes[url] = sha256_bytes(body)
            rows = parse_sitemap(body)
            raw_entries += len(rows)
            for item_index, row in enumerate(rows):
                if not row.get("url"):
                    continue
                record = sitemap_record(row, evidence, item_index)
                key = record["canonical_url"]
                records[key] = merge_records(records[key], record) if key in records else record
            self.source_events.append({"source": "sitemap-shard", "ordinal": ordinal, "entries": len(rows), **evidence})
            shard_proofs.append(
                {
                    "ordinal": ordinal,
                    "url": url,
                    "declared_lastmod": item.get("lastmod"),
                    "entries": len(rows),
                    "sha256": first_hashes[url],
                }
            )
        if verify_stable:
            for item in index_rows:
                url = str(item["url"])
                body, evidence = request_bytes(url, limiter=self.search_limiter, max_bytes=64 * 1024 * 1024)
                if sha256_bytes(body) != first_hashes[url]:
                    raise AcquisitionError(f"sitemap shard changed during snapshot: {url}")
                self.source_events.append({"source": "sitemap-shard-verification", **evidence})
            closing_index_body, closing_index_evidence = request_bytes(SITEMAP_URL, limiter=self.search_limiter)
            if sha256_bytes(closing_index_body) != sha256_bytes(index_body):
                raise AcquisitionError("sitemap index changed during snapshot")
            self.source_events.append({"source": "sitemap-index-verification", **closing_index_evidence})
            self.sitemap_stable = True
        self.sitemap_proof = {
            "declared_shards": declared_shards,
            "observed_shards": len(index_rows),
            "raw_entries": raw_entries,
            "unique_urls": len(records),
            "index_sha256": sha256_bytes(index_body),
            "shards": shard_proofs,
            "byte_stable": self.sitemap_stable,
            "closed": shard_limit is None and len(index_rows) == declared_shards and (self.sitemap_stable or not verify_stable),
        }
        return records

    def enumerate_organisations(self) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        page = 1
        returned_total = 0
        reported_total = 0
        reported_pages = 0
        while True:
            url = ORGANISATIONS_URL + "?" + urlencode({"page": page})
            payload, evidence = self.cached_json(self.cache / "organisations" / f"page-{page:04d}.json.gz", url, self.search_limiter)
            rows = payload.get("results", [])
            returned_total += len(rows)
            page_total = int(payload.get("total") or reported_total)
            page_pages = int(payload.get("pages") or 1)
            if reported_total and page_total != reported_total:
                raise AcquisitionError(f"Organisations API total changed: {reported_total} != {page_total}")
            if reported_pages and page_pages != reported_pages:
                raise AcquisitionError(f"Organisations API page count changed: {reported_pages} != {page_pages}")
            reported_total = page_total
            reported_pages = page_pages
            for result_index, item in enumerate(rows):
                url_value = normalise_url(str(item.get("link") or item.get("web_url") or ""))
                record = {
                    "candidate_key": candidate_key(
                        url_value,
                        "en",
                        "organisation",
                        str(item.get("content_id") or url_value),
                    ),
                    "entity_class": "organisation",
                    "source_native_id": item.get("content_id") or url_value,
                    "source_id": "organisations-api",
                    "source_memberships": ["organisations-api"],
                    "coverage_disposition": "represented",
                    "content_id": item.get("content_id"),
                    "canonical_url": url_value,
                    "base_path": urlparse(url_value).path,
                    "title": str(item.get("title") or item.get("name") or "Organisation"),
                    "description": str(item.get("details") or item.get("description") or ""),
                    "document_type": "organisation",
                    "schema_name": "organisation",
                    "locale": "en",
                    "public_updated_at": item.get("public_timestamp"),
                    "links": {},
                    "organisation_state": item.get("organisation_state"),
                    "retrieved_at": evidence["retrieved_at"],
                    "evidence_url": evidence.get("requested_url") or url,
                    "evidence_sha256": evidence.get("sha256"),
                    "evidence_locator": f"/results/{result_index}",
                    "source_adapter": "govuk_organisations_api",
                }
                records[url_value] = record
            self.source_events.append({"source": "organisations-api", "page": page, "returned": len(rows), **evidence})
            pages = page_pages
            if page >= pages:
                break
            page += 1
        if returned_total != reported_total:
            raise AcquisitionError(f"Organisations API did not close: {returned_total} != {reported_total}")
        if len(records) != returned_total:
            raise AcquisitionError(
                f"Organisations API repeated identities: {len(records)} unique != {returned_total} returned"
            )
        self.organisations_proof = {
            "reported_total": reported_total,
            "reported_pages": reported_pages,
            "returned_rows": returned_total,
            "unique_urls": len(records),
            "closed": returned_total == reported_total,
        }
        return records

    def enumerate_navigation(self, limit: int | None = None) -> dict[str, dict[str, Any]]:
        checkpoint_path = self.cache / "navigation-checkpoint.json"
        records_path = self.cache / "navigation-records.jsonl.gz"
        queue = collections.deque(CURATED_CONTENT_PATHS)
        visited: set[str] = set()
        records: dict[str, dict[str, Any]] = {}
        if checkpoint_path.is_file() and records_path.is_file():
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            queue = collections.deque(checkpoint.get("queue", []))
            visited = set(checkpoint.get("visited", []))
            records = {record["canonical_url"]: record for record in read_jsonl_gzip(records_path)}
        while queue and (limit is None or len(visited) < limit):
            path = queue.popleft()
            if path in visited:
                continue
            url = CONTENT_API_ROOT + path
            try:
                body, evidence = request_bytes(url, limiter=self.content_limiter)
            except AcquisitionError as exc:
                now = datetime.now(timezone.utc).isoformat()
                canonical = normalise_url(path or "/")
                exception_id = "source-exception-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
                records[canonical] = {
                    "candidate_key": candidate_key(canonical, "en", "route", canonical),
                    "entity_class": "route",
                    "source_native_id": canonical,
                    "source_id": "content-api",
                    "source_memberships": ["structured-content-api"],
                    "coverage_disposition": "exceptioned",
                    "canonical_url": canonical,
                    "base_path": path or "/",
                    "title": "Content API acquisition exception",
                    "description": "The known navigation route could not be hydrated after bounded retries.",
                    "document_type": "constraint_record",
                    "schema_name": "constraint_record",
                    "locale": "en",
                    "links": {},
                    "retrieved_at": now,
                    "evidence_url": url,
                    "evidence_locator": "/",
                    "exception": {
                        "id": exception_id,
                        "reason": str(exc),
                        "owner": "corpus-maintainer",
                        "retry_after": "next acquisition run",
                        "review_date": now[:10],
                    },
                    "source_adapter": "govuk_content_api",
                }
                self.source_events.append(
                    {"source": "content-api-navigation", "path": path, "error": str(exc), "exception_id": exception_id}
                )
                visited.add(path)
                continue
            payload = json.loads(body)
            record = sanitise_content_api(payload, evidence)
            records[record["canonical_url"]] = record
            visited.add(path)
            for predicate, values in (payload.get("links") or {}).items():
                if predicate not in NAVIGATION_LINKS or not isinstance(values, list):
                    continue
                for item in values:
                    if not isinstance(item, dict):
                        continue
                    candidate = str(item.get("base_path") or item.get("api_path") or "")
                    if candidate.startswith("/api/content"):
                        candidate = candidate[len("/api/content") :] or ""
                    if candidate.startswith("/") and candidate not in visited:
                        queue.append(candidate)
            self.source_events.append({"source": "content-api-navigation", "path": path, **evidence})
            if len(visited) % 100 == 0:
                checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                write_text_atomic(checkpoint_path, pretty_json({"queue": list(queue), "visited": sorted(visited)}))
                write_jsonl_gzip(records_path, (records[key] for key in sorted(records)))
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(checkpoint_path, pretty_json({"queue": list(queue), "visited": sorted(visited)}))
        write_jsonl_gzip(records_path, (records[key] for key in sorted(records)))
        self.navigation_proof = {
            "visited_paths": len(visited),
            "records": len(records),
            "remaining_paths": len(queue),
            "exceptioned": sum(record.get("coverage_disposition") == "exceptioned" for record in records.values()),
            "closed": not queue,
        }
        return records

    def build(
        self,
        *,
        opposing_search: bool = True,
        verify_sitemap: bool = True,
        navigation_limit: int | None = None,
        search_limit: int | None = None,
        sitemap_shard_limit: int | None = None,
    ) -> dict[str, Any]:
        search = self.enumerate_search(opposing=opposing_search, record_limit=search_limit)
        sitemap = self.enumerate_sitemap(verify_stable=verify_sitemap, shard_limit=sitemap_shard_limit)
        organisations = self.enumerate_organisations()
        navigation = self.enumerate_navigation(limit=navigation_limit)
        organisations_by_content_id = {
            str(record["content_id"]): record for record in organisations.values() if record.get("content_id")
        }
        for record in search.values():
            linked = []
            for content_id in record.pop("organisation_content_ids", []):
                organisation = organisations_by_content_id.get(content_id)
                if organisation:
                    linked.append(
                        {
                            "content_id": content_id,
                            "base_path": organisation["base_path"],
                            "title": organisation["title"],
                            "slug": organisation["base_path"].rsplit("/", 1)[-1],
                            "organisation_state": organisation.get("organisation_state"),
                        }
                    )
            if linked:
                record.setdefault("links", {})["organisations"] = linked
        source_sets = {
            "search": set(search),
            "sitemap": set(sitemap),
            "organisations": set(organisations),
            "navigation": set(navigation),
        }
        # Publication envelopes are merged by route and locale for downstream
        # compilation, while the accounting ledger below retains separate
        # content identity, document, edition, route and resource candidates.
        publication_records: dict[tuple[str, str], dict[str, Any]] = {}
        for collection in (sitemap, search, organisations, navigation):
            for record in collection.values():
                group_key = (str(record["canonical_url"]), str(record.get("locale") or "en"))
                publication_records[group_key] = (
                    merge_records(publication_records[group_key], record)
                    if group_key in publication_records
                    else dict(record)
                )

        # Multiple routes can be public aliases for the same source-native
        # content/document/edition identity. Alias only within one entity
        # class; never collapse a route into a content identity.
        candidate_ledger = build_candidate_ledger(publication_records.values(), self.label)

        snapshot_inventory_root = self.inventory_root / self.label
        candidate_output = write_jsonl_gzip_shards(
            snapshot_inventory_root,
            "candidates",
            (candidate_ledger[key] for key in sorted(candidate_ledger)),
        )
        inventory_output = write_jsonl_gzip_shards(
            snapshot_inventory_root,
            "source-records",
            (publication_records[key] for key in sorted(publication_records)),
        )
        candidate_count = int(candidate_output["records"])
        candidate_digest = str(candidate_output["canonical_sha256"])
        publication_count = int(inventory_output["records"])
        digest = str(inventory_output["canonical_sha256"])
        candidate_path = Path(candidate_output["root"]) / "index.json"
        inventory_path = Path(inventory_output["root"]) / "index.json"
        dispositions = collections.Counter(record["coverage_disposition"] for record in candidate_ledger.values())
        accounted = sum(dispositions[status] for status in ("represented", "alias_of_represented", "redirect_only", "tombstone_only", "exceptioned"))
        reconciliation = {
            "schema_version": 1,
            "snapshot": self.label,
            "sampled": search_limit is not None or sitemap_shard_limit is not None or navigation_limit is not None,
            "expected_candidate_keys": candidate_count,
            "represented": dispositions["represented"],
            "alias_of_represented": dispositions["alias_of_represented"],
            "redirect_only": dispositions["redirect_only"],
            "tombstone_only": dispositions["tombstone_only"],
            "exceptioned": dispositions["exceptioned"],
            "unexplained_omissions": candidate_count - accounted,
            "publication_records": publication_count,
            "entity_class_counts": dict(sorted(collections.Counter(str(record.get("entity_class")) for record in candidate_ledger.values()).items())),
            "source_counts": {key: len(value) for key, value in source_sets.items()},
            "set_differences": {
                "sitemap_only": len(source_sets["sitemap"] - source_sets["search"]),
                "search_only": len(source_sets["search"] - source_sets["sitemap"]),
                "both_sitemap_search": len(source_sets["search"] & source_sets["sitemap"]),
                "navigation_only": len(source_sets["navigation"] - source_sets["search"] - source_sets["sitemap"]),
            },
            "search_partition_proofs": self.search_partition_proofs,
            "search_partitions_closed": bool(self.search_partition_proofs)
            and all(
                all(pass_row.get("closed") for pass_row in proof.get("passes", []))
                for proof in self.search_partition_proofs
            ),
            "sitemap_byte_stable": self.sitemap_stable,
            "sitemap_proof": self.sitemap_proof,
            "organisations_proof": self.organisations_proof,
            "navigation_proof": self.navigation_proof,
            "inventory_path": inventory_path.relative_to(self.root).as_posix(),
            "inventory_shards": [
                (Path(inventory_output["root"]) / row["path"]).relative_to(self.root).as_posix()
                for row in inventory_output["shards"]
            ],
            "inventory_canonical_sha256": digest,
            "candidate_ledger_path": candidate_path.relative_to(self.root).as_posix(),
            "candidate_ledger_shards": [
                (Path(candidate_output["root"]) / row["path"]).relative_to(self.root).as_posix()
                for row in candidate_output["shards"]
            ],
            "candidate_ledger_canonical_sha256": candidate_digest,
        }
        if reconciliation["unexplained_omissions"] != 0:
            raise AcquisitionError("candidate reconciliation did not close")
        reconciliation_path = self.root / "corpus" / "reconciliation" / f"{self.label}.json"
        reconciliation_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(reconciliation_path, pretty_json(reconciliation))
        manifest = {
            "schema_version": 1,
            "snapshot": self.label,
            "started_at": self.started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "sources": self.source_events,
            "search_partition_proofs": self.search_partition_proofs,
            "reconciliation": reconciliation,
        }
        self.manifest_root.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self.manifest_root / "manifest.json", pretty_json(manifest))
        return manifest
