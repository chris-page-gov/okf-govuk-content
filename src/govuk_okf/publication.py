"""Compile source-native GOV.UK metadata into the federated OKF publication."""

from __future__ import annotations

import collections
import gzip
import hashlib
import html
import json
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence
from urllib.parse import quote, urlparse

from .util import adjacency_bucket, canonical_json_bytes, chunks, pretty_json, sha256_text, slugify, write_gzip_json, yaml_dump

ROOT = Path(__file__).resolve().parents[2]
PROFILE_URL = "https://chris-page-gov.github.io/okf-explorer/profile/bundle-wiki/v1/"
EXPLORER_CONTEXT_URL = PROFILE_URL + "context.jsonld"
HOME_URL = "https://chris-page-gov.github.io/okf-govuk-content/"
GOVUK_CONTEXT_URL = HOME_URL + "context/govuk-okf-v1.jsonld"
REPOSITORY_URL = "https://github.com/chris-page-gov/okf-govuk-content"
OGL_URL = "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
RECORD_CHUNK_SIZE = 1000
RELATIONSHIP_CHUNK_SIZE = 1000
RESULT_CHUNK_SIZE = 1000
MAX_POSTINGS = 2000
SEMANTIC_ENTITY_CHUNK_SIZE = 500
SEMANTIC_ASSERTION_CHUNK_SIZE = 1000
SEMANTIC_PUBLICATION_DIRECTORIES = (
    "context",
    "crosswalks",
    "profile",
    "schemas",
    "shapes",
)
SEMANTIC_PUBLICATION_FILES = ("README.md",)
PUBLISHING_API_SCHEMA_COMMIT = "b1e987aa7b3e62c105ff2b2db87667f7638726f8"
DATA_PLANE_SCHEMA_VERSION = "1.0"
MAX_DATA_PLANE_SHARD_COMPRESSED_BYTES = 5 * 1024 * 1024
MAX_DATA_PLANE_SHARD_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
DATA_PLANE_BUDGETS = {
    "ordinary_shard_compressed_bytes": MAX_DATA_PLANE_SHARD_COMPRESSED_BYTES,
    "ordinary_shard_uncompressed_safety_bytes": MAX_DATA_PLANE_SHARD_UNCOMPRESSED_BYTES,
    "bootstrap_compressed_bytes": 2 * 1024 * 1024,
    "search_warm_p95_ms": 500,
    "search_cold_p95_ms": 1500,
    "first_useful_render_p75_ms": 2500,
    "steady_browser_heap_bytes": 512 * 1024 * 1024,
    "graph_expansion_nodes": 250,
    "graph_expansion_edges": 500,
}

LINK_KINDS = {
    "organisations": "associated with organisation",
    "primary_publishing_organisation": "published by",
    "original_primary_publishing_organisation": "originally published by",
    "owning_organisation": "owned by",
    "lead_organisations": "led by",
    "parent": "part of",
    "parents": "part of",
    "parent_taxons": "child of",
    "child_taxons": "parent of",
    "taxons": "classified under",
    "mainstream_browse_pages": "classified under",
    "top_level_browse_pages": "parent of",
    "second_level_browse_pages": "parent of",
    "document_collections": "part of collection",
    "related": "related to",
    "ordered_related_items": "related to",
    "related_to_step_navs": "part of step by step",
    "part_of_step_navs": "part of step by step",
    "available_translations": "available in language",
    "world_locations": "applies to world location",
    "topical_events": "part of topical event",
}
ORGANISATION_LINKS = {
    "organisations",
    "primary_publishing_organisation",
    "original_primary_publishing_organisation",
    "owning_organisation",
    "lead_organisations",
}
FIELD_MASKS = {
    "title": 1,
    "publisher": 2,
    "description": 4,
    "record_type": 8,
    "schema": 16,
    "tags": 32,
    "url": 64,
    "language": 128,
    "lifecycle": 256,
}
FIELD_WEIGHTS = {
    "title": 16,
    "publisher": 8,
    "description": 5,
    "record_type": 4,
    "schema": 4,
    "tags": 3,
    "url": 2,
    "language": 3,
    "lifecycle": 4,
}
STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


class PublicationError(RuntimeError):
    """Raised when source metadata cannot be compiled safely."""


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    opener = gzip.open if path.suffix == ".gz" else Path.open
    if path.suffix == ".gz":
        stream_context = gzip.open(path, "rt", encoding="utf-8")
    else:
        stream_context = path.open(encoding="utf-8")
    with stream_context as stream:
        for number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise PublicationError(f"{path}:{number}: record must be an object")
            records.append(value)
    return records


def canonical_url(record: dict[str, Any]) -> str:
    explicit = str(record.get("canonical_url") or record.get("url") or "")
    if explicit.startswith(("https://", "http://")):
        return explicit
    path = str(record.get("base_path") or record.get("link") or explicit or "/")
    if not path.startswith("/"):
        path = "/" + path
    return "https://www.gov.uk" + path


def dataset_name(content_id: str | None, locale: str, url: str) -> str:
    if content_id:
        base = re.sub(r"[^A-Za-z0-9-]", "", content_id).casefold()
    else:
        base = "route-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    locale_slug = slugify(locale or "en")
    return f"{base}-{locale_slug}"


def link_url(link: dict[str, Any]) -> str:
    candidate = str(link.get("api_path") or link.get("web_url") or link.get("base_path") or link.get("link") or link.get("url") or "")
    if candidate.startswith("/api/content"):
        candidate = candidate[len("/api/content") :] or "/"
    if candidate.startswith(("https://", "http://")):
        return candidate
    if not candidate.startswith("/"):
        candidate = "/" + candidate
    return "https://www.gov.uk" + candidate


def source_evidence_url(record: dict[str, Any]) -> str:
    if record.get("evidence_url"):
        return str(record["evidence_url"])
    url = canonical_url(record)
    parsed = urlparse(url)
    if record.get("source_id") == "content-api" and parsed.netloc == "www.gov.uk":
        return "https://www.gov.uk/api/content" + (parsed.path if parsed.path != "/" else "")
    return url


def normalise_source_record(record: dict[str, Any], observed_at: str) -> dict[str, Any]:
    url = canonical_url(record)
    locale = str(record.get("locale") or record.get("language") or "en")
    content_id = str(record["content_id"]) if record.get("content_id") else None
    name = dataset_name(content_id, locale, url)
    title = str(record.get("title") or record.get("name") or urlparse(url).path.rsplit("/", 1)[-1].replace("-", " ").title() or "GOV.UK")
    schema_name = str(record.get("schema_name") or record.get("schema") or record.get("content_store_document_type") or "unknown")
    document_type = str(record.get("document_type") or record.get("format") or "unknown")
    withdrawn = record.get("withdrawn_notice")
    redirects = record.get("redirects") or []
    lifecycle = "redirect" if document_type == "redirect" or redirects else "withdrawn" if withdrawn else "published"
    tags = sorted({schema_name, document_type, locale, lifecycle} - {"", "unknown"})
    source_memberships = sorted(set(record.get("source_memberships") or [str(record.get("source_id") or "fixture")]))
    return {
        "@id": url,
        "access_model": "anonymous",
        "base_path": urlparse(url).path or "/",
        "canonical_content_id": content_id,
        "confidence": "source-declared" if record.get("content_id") or record.get("title") else "source-observed-route",
        "description": str(record.get("description") or ""),
        "document_type": document_type,
        "first_published_at": record.get("first_published_at"),
        "jurisdiction": record.get("jurisdiction") or ["United Kingdom"],
        "language": locale,
        "lifecycle": lifecycle,
        "name": name,
        "notes": str(record.get("description") or ""),
        "open": f"dataset/{name}",
        "public_updated_at": record.get("public_updated_at") or record.get("updated_at") or record.get("lastmod"),
        "record_type": "GOV.UK content item",
        "schema_name": schema_name,
        "source_adapter": str(record.get("source_adapter") or "govuk_public_metadata"),
        "source_memberships": source_memberships,
        "source_tier": "official-public",
        "status": lifecycle,
        "tags": tags,
        "timestamp": record.get("public_updated_at") or record.get("updated_at") or record.get("lastmod") or observed_at,
        "title": title,
        "url": url,
        "evidence_url": source_evidence_url(record),
        "evidence_sha256": record.get("evidence_sha256") or hashlib.sha256(canonical_json_bytes(record)).hexdigest(),
        "evidence_locator": record.get("evidence_locator") or "/",
        "retrieved_at": record.get("retrieved_at") or observed_at,
        "_source": record,
    }


def publisher_from_link(
    link: dict[str, Any],
    *,
    evidence_url: str,
    evidence_sha256: str,
    evidence_locator: str,
    retrieved_at: str,
) -> dict[str, Any]:
    url = link_url(link)
    slug = str(link.get("slug") or urlparse(url).path.rsplit("/", 1)[-1] or link.get("content_id") or "organisation")
    name = slugify(slug)
    return {
        "@id": url,
        "name": name,
        "open": f"publisher/{name}",
        "record_type": "GOV.UK organisation",
        "title": str(link.get("title") or slug.replace("-", " ").title()),
        "url": url,
        "content_id": link.get("content_id"),
        "state": link.get("organisation_state") or "unknown",
        "source_tier": "official-public",
        "confidence": "source-declared",
        "evidence_url": evidence_url,
        "evidence_sha256": evidence_sha256,
        "evidence_locator": evidence_locator,
        "retrieved_at": retrieved_at,
    }


def link_dataset_route(link: dict[str, Any]) -> str:
    return "dataset/" + dataset_name(str(link["content_id"]) if link.get("content_id") else None, str(link.get("locale") or "en"), link_url(link))


def assertion_id(
    source: str,
    kind: str,
    target: str,
    evidence_url: str,
    native_predicate: str,
    evidence_locator: str,
) -> str:
    identity = (source, kind, target, evidence_url, native_predicate, evidence_locator)
    digest = hashlib.sha256("\0".join(identity).encode("utf-8")).hexdigest()
    return f"assertion-{digest}"


def relationship(
    source: str,
    target: str,
    kind: str,
    evidence_url: str,
    observed_at: str,
    native_predicate: str,
    *,
    evidence_sha256: str | None,
    evidence_locator: str,
    snapshot_id: str,
) -> dict[str, Any]:
    return {
        "assertion_id": assertion_id(source, kind, target, evidence_url, native_predicate, evidence_locator),
        "source": source,
        "target": target,
        "kind": kind,
        "source_native_predicate": native_predicate,
        "evidence_type": "official-source-field",
        "evidence_url": evidence_url,
        "evidence_sha256": evidence_sha256,
        "evidence_locator": evidence_locator,
        "observed_at": observed_at,
        "derivation_method": "deterministic-source-field-mapping-v1",
        "software_version": "govuk-okf/0.1.0",
        "snapshot_id": snapshot_id,
        "assertion_status": "source-declared",
        "confidence": 1.0,
    }


def compile_records(
    source_records: list[dict[str, Any]], observed_at: str, snapshot_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    datasets = [normalise_source_record(record, observed_at) for record in source_records]
    by_route = {item["open"]: item for item in datasets}
    route_by_url = {str(item["url"]).rstrip("/") or "/": item["open"] for item in datasets}
    publishers: dict[str, dict[str, Any]] = {}
    resources: dict[str, dict[str, Any]] = {}
    relationships: dict[str, dict[str, Any]] = {}

    def retain_entity(
        table: dict[str, dict[str, Any]],
        row: dict[str, Any],
        *,
        identity_fields: tuple[str, ...],
    ) -> dict[str, Any]:
        route = str(row["open"])
        existing = table.get(route)
        if existing is None:
            table[route] = row
            return row
        for field in identity_fields:
            left = existing.get(field)
            right = row.get(field)
            if left and right and left != right:
                raise PublicationError(f"conflicting {route} identity field {field}: {left!r} != {right!r}")
            if not left and right:
                existing[field] = right
        return existing

    for dataset in list(datasets):
        source = dataset["open"]
        record = dataset.pop("_source")
        evidence_url = dataset["evidence_url"]
        source_observed_at = str(record.get("retrieved_at") or observed_at)
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
                kind = LINK_KINDS.get(native_predicate, native_predicate.replace("_", " "))
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
                        publisher = retain_entity(
                            publishers,
                            publisher,
                            identity_fields=("url", "content_id"),
                        )
                        target = publisher["open"]
                        if native_predicate == "primary_publishing_organisation" and not dataset.get("publisher"):
                            dataset["publisher"] = publisher["name"]
                            dataset["publisher_title"] = publisher["title"]
                    else:
                        target_url = link_url(value)
                        target = route_by_url.get(target_url.rstrip("/") or "/") or link_dataset_route(value)
                        if target not in by_route:
                            stub_source = {
                                "content_id": value.get("content_id"),
                                "base_path": urlparse(link_url(value)).path,
                                "canonical_url": link_url(value),
                                "title": value.get("title") or "Referenced GOV.UK content",
                                "description": "Discovered through a typed Content API relationship.",
                                "document_type": value.get("document_type") or "linked_content",
                                "schema_name": value.get("schema_name") or "unknown",
                                "locale": value.get("locale") or "en",
                                "source_id": "structured-linked-content",
                                "evidence_url": evidence_url,
                            }
                            stub = normalise_source_record(stub_source, observed_at)
                            stub.pop("_source")
                            by_route[target] = stub
                            route_by_url[str(stub["url"]).rstrip("/") or "/"] = target
                            datasets.append(stub)
                    edge = relationship(
                        source,
                        target,
                        kind,
                        evidence_url,
                        source_observed_at,
                        native_predicate,
                        evidence_sha256=evidence_sha256,
                        evidence_locator=locator,
                        snapshot_id=snapshot_id,
                    )
                    relationships[edge["assertion_id"]] = edge

        attachments = (record.get("details") or {}).get("attachments", []) if isinstance(record.get("details") or {}, dict) else []
        for attachment_index, attachment in enumerate(attachments):
            if not isinstance(attachment, dict):
                continue
            attachment_url = str(attachment.get("url") or "")
            if not attachment_url.startswith(("https://", "http://")):
                continue
            attachment_id = str(attachment.get("id") or hashlib.sha256(attachment_url.encode("utf-8")).hexdigest()[:24])
            name = slugify(attachment_id)
            route = f"resource/{name}"
            resource = {
                "@id": attachment_url,
                "accessibility": "accessible" if attachment.get("accessible") else "not-declared-accessible",
                "attachment_id": attachment_id,
                "id": attachment_id,
                "bytes": attachment.get("file_size"),
                "content_type": attachment.get("content_type") or "application/octet-stream",
                "name": name,
                "open": route,
                "pages": attachment.get("number_of_pages"),
                "parent": source,
                "parent_content_id": dataset.get("canonical_content_id"),
                "dataset": dataset["name"],
                "record_type": "GOV.UK attachment",
                "rights_status": "item-specific-review-required",
                "confidence": "source-declared",
                "evidence_url": evidence_url,
                "evidence_sha256": evidence_sha256,
                "evidence_locator": f"{base_locator.rstrip('/')}/details/attachments/{attachment_index}",
                "retrieved_at": source_observed_at,
                "title": str(attachment.get("title") or attachment.get("filename") or "Attachment"),
                "url": attachment_url,
            }
            retain_entity(resources, resource, identity_fields=("url", "attachment_id"))
            edge = relationship(
                source,
                route,
                "has attachment",
                evidence_url,
                source_observed_at,
                "details.attachments",
                evidence_sha256=evidence_sha256,
                evidence_locator=f"{base_locator.rstrip('/')}/details/attachments/{attachment_index}",
                snapshot_id=snapshot_id,
            )
            relationships[edge["assertion_id"]] = edge

        redirects = record.get("redirects") or []
        for redirect in redirects:
            if not isinstance(redirect, dict):
                continue
            destination = str(redirect.get("destination") or redirect.get("path") or "")
            if not destination:
                continue
            target_url = destination if destination.startswith("http") else "https://www.gov.uk" + destination
            target = route_by_url.get(target_url.rstrip("/") or "/") or (
                "dataset/" + dataset_name(None, dataset["language"], target_url)
            )
            if target not in by_route:
                stub = normalise_source_record(
                    {
                        "canonical_url": target_url,
                        "title": "Redirect destination",
                        "document_type": "redirect_destination",
                        "locale": dataset["language"],
                        "source_id": "redirect-destination",
                        "evidence_url": evidence_url,
                    },
                    observed_at,
                )
                stub.pop("_source")
                by_route[target] = stub
                route_by_url[str(stub["url"]).rstrip("/") or "/"] = target
                datasets.append(stub)
            edge = relationship(
                source,
                target,
                "redirects to",
                evidence_url,
                source_observed_at,
                "redirects.destination",
                evidence_sha256=evidence_sha256,
                evidence_locator=f"{base_locator.rstrip('/')}/redirects",
                snapshot_id=snapshot_id,
            )
            relationships[edge["assertion_id"]] = edge

    datasets.sort(key=lambda item: (item["open"], item["url"]))
    for ordinal, dataset in enumerate(datasets):
        dataset["ordinal"] = ordinal
        dataset.setdefault("publisher", "unknown")
        dataset.setdefault("publisher_title", "Publisher not available from admitted source")
    publisher_rows = sorted(publishers.values(), key=lambda item: item["open"])
    resource_rows = sorted(resources.values(), key=lambda item: item["open"])
    relationship_rows = sorted(relationships.values(), key=lambda item: (item["source"], item["kind"], item["target"], item["assertion_id"]))
    return datasets, publisher_rows, resource_rows, relationship_rows


def tokenise(value: str) -> set[str]:
    normal = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").casefold()
    tokens = set()
    for match in re.finditer(r"[a-z0-9][a-z0-9._-]*", normal):
        token = match.group(0).strip("._-")
        if len(token) >= 2 and token not in STOP_WORDS:
            tokens.add(token)
    return tokens


def search_shard(value: str, length: int = 2) -> str:
    clean = re.sub(r"[^a-z0-9]", "", value.casefold())
    return clean[:length] or "_"


def _uncompressed_file_size(path: Path, compression: str) -> int:
    if compression == "identity":
        return path.stat().st_size
    if compression != "gzip":
        raise PublicationError(f"unsupported data-plane compression: {compression}")
    size = 0
    with gzip.open(path, "rb") as stream:
        while block := stream.read(1024 * 1024):
            size += len(block)
            if size > MAX_DATA_PLANE_SHARD_UNCOMPRESSED_BYTES:
                raise PublicationError(
                    "data-plane shard exceeds the uncompressed safety budget: "
                    f"{path} ({size} > {MAX_DATA_PLANE_SHARD_UNCOMPRESSED_BYTES})"
                )
    return size


def data_plane_shard_metadata(
    output: Path,
    path: Path,
    *,
    schema: str,
    snapshot_id: str,
    count: int,
    first_key: str | None,
    last_key: str | None,
    compression: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe and budget one immutable data-plane shard.

    The hash covers the distributed bytes, while ``uncompressed_bytes`` is
    counted through a bounded stream.  Build failure is intentional when an
    ordinary shard exceeds the frozen §9.6 budget; a release may not silently
    publish an oversized shard or relax the contract.
    """
    compressed_bytes = path.stat().st_size
    if compressed_bytes > MAX_DATA_PLANE_SHARD_COMPRESSED_BYTES:
        raise PublicationError(
            "data-plane shard exceeds the compressed budget: "
            f"{path} ({compressed_bytes} > {MAX_DATA_PLANE_SHARD_COMPRESSED_BYTES})"
        )
    uncompressed_bytes = _uncompressed_file_size(path, compression)
    if uncompressed_bytes > MAX_DATA_PLANE_SHARD_UNCOMPRESSED_BYTES:
        raise PublicationError(
            "data-plane shard exceeds the uncompressed safety budget: "
            f"{path} ({uncompressed_bytes} > "
            f"{MAX_DATA_PLANE_SHARD_UNCOMPRESSED_BYTES})"
        )
    if count < 0:
        raise PublicationError(f"negative data-plane shard count: {path}")
    if count == 0 and (first_key is not None or last_key is not None):
        raise PublicationError(f"empty data-plane shard has key bounds: {path}")
    if count > 0 and (first_key is None or last_key is None):
        raise PublicationError(f"non-empty data-plane shard lacks key bounds: {path}")
    row: dict[str, Any] = {
        "path": path.relative_to(output).as_posix(),
        "schema": schema,
        "schema_version": DATA_PLANE_SCHEMA_VERSION,
        "snapshot": snapshot_id,
        "count": count,
        "first_key": first_key,
        "last_key": last_key,
        "compression": compression,
        "compressed_bytes": compressed_bytes,
        "uncompressed_bytes": uncompressed_bytes,
        "sha256": _file_sha256(path),
    }
    if extra:
        row.update(extra)
    return row


def shard_manifest_sha256(shards: object) -> str:
    return hashlib.sha256(canonical_json_bytes(shards)).hexdigest()


def data_plane_manifest_root(shards: Iterable[dict[str, Any]]) -> str:
    """Return the canonical release root over sorted immutable shard leaves."""
    leaves = sorted(
        (
            {
                "path": str(row["path"]),
                "schema": str(row["schema"]),
                "schema_version": str(row["schema_version"]),
                "snapshot": str(row["snapshot"]),
                "count": int(row["count"]),
                "first_key": row.get("first_key"),
                "last_key": row.get("last_key"),
                "compression": str(row["compression"]),
                "compressed_bytes": int(row["compressed_bytes"]),
                "uncompressed_bytes": int(row["uncompressed_bytes"]),
                "sha256": str(row["sha256"]),
            }
            for row in shards
        ),
        key=lambda row: row["path"],
    )
    return hashlib.sha256(canonical_json_bytes(leaves)).hexdigest()


def write_search(
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
    result_docs: list[dict[str, Any]] = []
    postings: dict[str, dict[int, list[int]]] = collections.defaultdict(dict)
    for dataset in datasets:
        result = {key: value for key, value in dataset.items() if key not in {"evidence_url", "source_memberships"}}
        result_docs.append(result)
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
                combined[token] = (score + FIELD_WEIGHTS[field], mask | FIELD_MASKS[field])
        ordinal = dataset["ordinal"]
        for token, (score, mask) in combined.items():
            postings[token][ordinal] = [score, mask]

    for index, block in enumerate(chunks(result_docs, RESULT_CHUNK_SIZE)):
        path = search_root / f"results-{index}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pretty_json(list(block)), encoding="utf-8")
        result_paths.append(path.relative_to(output).as_posix())
        keys = [str(row["open"]) for row in block]
        shard_rows["result_docs"].append(
            data_plane_shard_metadata(
                output,
                path,
                schema="okf-search-result-shard.v1",
                snapshot_id=snapshot_id,
                count=len(block),
                first_key=keys[0] if keys else None,
                last_key=keys[-1] if keys else None,
                compression="identity",
                extra={"kind": "result_docs", "ordinal": index},
            )
        )

    lexicon_groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    posting_groups: dict[str, dict[str, list[list[int]]]] = collections.defaultdict(dict)
    prefix_groups: dict[str, dict[str, list[dict[str, Any]]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    uncapped = 0
    retained = 0
    for token in sorted(postings):
        shard = search_shard(token)
        ordered = sorted(
            ([ordinal, score_mask[0], score_mask[1]] for ordinal, score_mask in postings[token].items()),
            key=lambda row: (-row[1], row[0]),
        )
        uncapped += len(ordered)
        ordered = ordered[:MAX_POSTINGS]
        retained += len(ordered)
        postings_path = f"data/search/postings/{shard}.json"
        posting_groups[shard][token] = ordered
        lexicon_groups[shard].append({"token": token, "df": len(postings[token]), "postings": postings_path})
        for length in range(3, min(len(token), 12) + 1):
            prefix = token[:length]
            prefix_shard = search_shard(prefix)
            prefix_groups[prefix_shard][prefix].append({"token": token, "df": len(postings[token])})

    lexicon_paths: dict[str, str] = {}
    postings_paths: list[str] = []
    prefix_paths: dict[str, str] = {}
    for shard in sorted(lexicon_groups):
        path = search_root / "lexicon" / f"{shard}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        lexicon_rows = lexicon_groups[shard]
        path.write_text(pretty_json(lexicon_rows), encoding="utf-8")
        lexicon_paths[shard] = path.relative_to(output).as_posix()
        lexicon_keys = [str(row["token"]) for row in lexicon_rows]
        shard_rows["lexicon"].append(
            data_plane_shard_metadata(
                output,
                path,
                schema="okf-search-lexicon-shard.v1",
                snapshot_id=snapshot_id,
                count=len(lexicon_rows),
                first_key=lexicon_keys[0] if lexicon_keys else None,
                last_key=lexicon_keys[-1] if lexicon_keys else None,
                compression="identity",
                extra={"kind": "lexicon", "shard": shard},
            )
        )
        postings_path = search_root / "postings" / f"{shard}.json"
        postings_path.parent.mkdir(parents=True, exist_ok=True)
        posting_tokens = posting_groups[shard]
        postings_path.write_text(pretty_json({"tokens": posting_tokens}), encoding="utf-8")
        postings_paths.append(postings_path.relative_to(output).as_posix())
        posting_keys = sorted(posting_tokens)
        shard_rows["postings"].append(
            data_plane_shard_metadata(
                output,
                postings_path,
                schema="okf-search-postings-shard.v1",
                snapshot_id=snapshot_id,
                count=len(posting_keys),
                first_key=posting_keys[0] if posting_keys else None,
                last_key=posting_keys[-1] if posting_keys else None,
                compression="identity",
                extra={
                    "kind": "postings",
                    "shard": shard,
                    "posting_count": sum(len(rows) for rows in posting_tokens.values()),
                },
            )
        )
    for shard in sorted(prefix_groups):
        path = search_root / "prefixes" / f"{shard}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        serialised = {
            prefix: sorted(values, key=lambda item: (-item["df"], item["token"]))[:100]
            for prefix, values in sorted(prefix_groups[shard].items())
        }
        path.write_text(pretty_json(serialised), encoding="utf-8")
        prefix_paths[shard] = path.relative_to(output).as_posix()
        prefix_keys = list(serialised)
        shard_rows["prefixes"].append(
            data_plane_shard_metadata(
                output,
                path,
                schema="okf-search-prefix-shard.v1",
                snapshot_id=snapshot_id,
                count=len(prefix_keys),
                first_key=prefix_keys[0] if prefix_keys else None,
                last_key=prefix_keys[-1] if prefix_keys else None,
                compression="identity",
                extra={"kind": "prefixes", "shard": shard},
            )
        )

    doc_map = {str(dataset["ordinal"]): dataset["open"] for dataset in datasets}
    doc_map_path = search_root / "doc-map.json"
    doc_map_path.write_text(pretty_json(doc_map), encoding="utf-8")
    shard_rows["doc_map"].append(
        data_plane_shard_metadata(
            output,
            doc_map_path,
            schema="okf-search-doc-map-shard.v1",
            snapshot_id=snapshot_id,
            count=len(doc_map),
            first_key="0" if doc_map else None,
            last_key=str(len(doc_map) - 1) if doc_map else None,
            compression="identity",
            extra={"kind": "doc_map"},
        )
    )
    shard_metadata = {
        "schema": "okf-search-shard-manifest.v1",
        "schema_version": DATA_PLANE_SCHEMA_VERSION,
        "snapshot": snapshot_id,
        "generated_at": generated_at,
        "shards": shard_rows,
    }
    shard_metadata["shard_manifest_sha256"] = shard_manifest_sha256(shard_rows)
    shard_metadata_path = search_root / "shards.json"
    shard_metadata_path.write_text(pretty_json(shard_metadata), encoding="utf-8")
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
            "tokens": len(postings),
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
    manifest["shard_manifest_sha256"] = shard_metadata["shard_manifest_sha256"]
    (search_root / "manifest.json").write_text(pretty_json(manifest), encoding="utf-8")
    manifest["_compiled_shards"] = shard_rows
    return manifest


def write_adjacency(
    output: Path,
    relationships: Sequence[dict[str, Any]],
    *,
    snapshot_id: str,
    generated_at: str,
) -> dict[str, Any]:
    buckets: dict[str, dict[str, list[dict[str, Any]]]] = {
        f"{number:02x}": collections.defaultdict(list) for number in range(256)
    }
    routes: set[str] = set()
    for edge in relationships:
        for route in {edge["source"], edge["target"]}:
            routes.add(route)
            buckets[adjacency_bucket(route)][route].append(edge)
    mapping: dict[str, str] = {}
    shard_rows: list[dict[str, Any]] = []
    for bucket in sorted(buckets):
        path = output / "data" / "adjacency" / f"{bucket}.json.gz"
        payload = {
            route: sorted(rows, key=lambda item: (item["kind"], item["source"], item["target"], item["assertion_id"]))
            for route, rows in sorted(buckets[bucket].items())
        }
        write_gzip_json(path, payload)
        mapping[bucket] = path.relative_to(output).as_posix()
        keys = list(payload)
        shard_rows.append(
            data_plane_shard_metadata(
                output,
                path,
                schema="okf-adjacency-shard.v1",
                snapshot_id=snapshot_id,
                count=len(payload),
                first_key=keys[0] if keys else None,
                last_key=keys[-1] if keys else None,
                compression="gzip",
                extra={
                    "kind": "adjacency",
                    "bucket": bucket,
                    "relationship_occurrences": sum(
                        len(rows) for rows in payload.values()
                    ),
                },
            )
        )
    manifest = {
        "schema": "okf-relationship-adjacency.v1",
        "schema_version": DATA_PLANE_SCHEMA_VERSION,
        "snapshot": snapshot_id,
        "generated_at": generated_at,
        "algorithm": "fnv1a32-prefix-2",
        "relationships": len(relationships),
        "routes": len(routes),
        "buckets": mapping,
        "budgets": DATA_PLANE_BUDGETS,
        "shards": shard_rows,
    }
    manifest["shard_manifest_sha256"] = shard_manifest_sha256(shard_rows)
    (output / "data" / "adjacency" / "manifest.json").write_text(pretty_json(manifest), encoding="utf-8")
    return manifest


def write_route_index(
    output: Path,
    datasets: Sequence[dict[str, Any]],
    publishers: Sequence[dict[str, Any]],
    resources: Sequence[dict[str, Any]],
    *,
    snapshot_id: str,
    generated_at: str,
) -> dict[str, Any]:
    buckets: dict[str, dict[str, list[dict[str, Any]]]] = {f"{number:02x}": {} for number in range(256)}
    route_collisions: list[str] = []

    def add(identifier: Any, entry: dict[str, Any], *, is_route: bool = False) -> None:
        if identifier is None or str(identifier) == "":
            return
        key = str(identifier)
        bucket = adjacency_bucket(key)
        matches = buckets[bucket].setdefault(key, [])
        if entry in matches:
            return
        if is_route and matches:
            route_collisions.append(key)
            return
        matches.append(entry)

    for kind, rows in (("datasets", datasets), ("publishers", publishers), ("resources", resources)):
        for ordinal, row in enumerate(rows):
            entry = {"kind": kind, "ordinal": ordinal, "open": row["open"]}
            add(row.get("open"), entry, is_route=True)
            for identifier in (
                row.get("url"),
                row.get("@id"),
                row.get("canonical_content_id"),
                row.get("content_id"),
                row.get("attachment_id"),
                row.get("id"),
                row.get("name"),
            ):
                add(identifier, entry)
    if route_collisions:
        raise PublicationError(f"runtime route collisions: {sorted(set(route_collisions))[:10]}")
    mapping: dict[str, str] = {}
    shard_rows: list[dict[str, Any]] = []
    identifier_count = 0
    match_count = 0
    for bucket, entries in sorted(buckets.items()):
        path = output / "data" / "routes" / f"{bucket}.json.gz"
        payload = {
            identifier: sorted(matches, key=lambda item: (item["kind"], item["open"], item["ordinal"]))
            for identifier, matches in sorted(entries.items())
        }
        write_gzip_json(path, payload)
        mapping[bucket] = path.relative_to(output).as_posix()
        identifier_count += len(entries)
        match_count += sum(len(matches) for matches in entries.values())
        keys = list(payload)
        shard_rows.append(
            data_plane_shard_metadata(
                output,
                path,
                schema="okf-route-shard.v1",
                snapshot_id=snapshot_id,
                count=len(payload),
                first_key=keys[0] if keys else None,
                last_key=keys[-1] if keys else None,
                compression="gzip",
                extra={
                    "kind": "routes",
                    "bucket": bucket,
                    "match_count": sum(len(matches) for matches in payload.values()),
                },
            )
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
    (output / "data" / "routes" / "manifest.json").write_text(pretty_json(manifest), encoding="utf-8")
    return manifest


def count_values(rows: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    counts: collections.Counter[str] = collections.Counter()
    for row in rows:
        value = row.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item is not None and str(item):
                counts[str(item)] += 1
    return [{"value": value, "count": count} for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def semantic_descriptor(counts: dict[str, int], generated_at: str, snapshot_id: str) -> dict[str, Any]:
    return {
        "@context": GOVUK_CONTEXT_URL,
        "@id": HOME_URL + "okf-bundle.yamlld",
        "@type": "okf:Bundle",
        "title": "What’s on GOV.UK",
        "description": "Derived, non-authoritative, snapshot-bounded semantic catalogue of the public GOV.UK metadata estate.",
        "version": "0.1.0",
        "status": "preview",
        "generatedAt": generated_at,
        "snapshot": snapshot_id,
        "derivedNotice": "GOV.UK remains authoritative; this bundle does not reproduce complete page bodies or transactions.",
        "descriptor": {"@id": HOME_URL + "okf-explorer.json"},
        "semanticDescriptor": {"@id": HOME_URL + "okf-bundle.yamlld"},
        "home": {"@id": HOME_URL},
        "profile": {"@id": PROFILE_URL},
        "publisher": {"@id": "https://github.com/chris-page-gov"},
        "license": {"@id": OGL_URL},
        "dataManifest": {"@id": HOME_URL + "data/manifest.json"},
        "sourceRegistry": {"@id": REPOSITORY_URL + "/blob/main/research/source-registry.yaml"},
        "constraintLedger": {"@id": REPOSITORY_URL + "/blob/main/research/source-constraints.json"},
        "semanticProjectionManifest": {"@id": HOME_URL + "data/semantic/manifest.json"},
        "counts": dict(sorted(counts.items())),
        "extensions": [
            "govuk-okf-profile.v1",
            "govuk-okf-semantic-projection.v1",
            "okf-explorer-large-corpus.v1",
            "okf-static-search.v1",
            "okf-relationship-adjacency.v1",
            "okf-route-index.v1",
        ],
    }


def semantic_route_iri(route: str) -> str:
    return HOME_URL + "id/" + quote(route, safe="/-._~")


def semantic_content_item_iri(content_id: str) -> str:
    return f"urn:govuk:content-item:{content_id.casefold()}"


def semantic_content_type_iri(document_type: str) -> str:
    return "urn:govuk:content-type:" + quote(document_type, safe="-._~")


def semantic_schema_family_iri(schema_name: str) -> str:
    return (
        "urn:govuk:schema-family:"
        + PUBLISHING_API_SCHEMA_COMMIT
        + ":"
        + quote(schema_name, safe="-._~")
    )


def semantic_route_node_iri(path: str) -> str:
    return "urn:govuk:route:" + hashlib.sha256(path.encode("utf-8")).hexdigest()


def semantic_evidence_id(row: dict[str, Any], discriminator: str) -> str:
    identity = "\0".join(
        (
            discriminator,
            str(row.get("evidence_url") or ""),
            str(row.get("evidence_locator") or "/"),
            str(row.get("retrieved_at") or row.get("observed_at") or ""),
            str(row.get("evidence_sha256") or ""),
        )
    )
    return "urn:govuk:evidence:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def semantic_evidence_node(
    row: dict[str, Any],
    *,
    discriminator: str,
    snapshot_id: str,
    source_system: str,
) -> dict[str, Any]:
    locator = str(row.get("evidence_locator") or "/")
    return {
        "@id": semantic_evidence_id(row, discriminator),
        "@type": "govuk:Evidence",
        "title": "Official GOV.UK source metadata observation",
        "evidenceUrl": str(row["evidence_url"]),
        "sourceSystem": source_system or "govuk_public_metadata",
        "sourceAuthority": "official",
        "locator": {
            "locatorKind": "json_pointer" if locator.startswith("/") else "source_field",
            "locatorValue": locator,
        },
        "retrievedAt": str(row.get("retrieved_at") or row.get("observed_at")),
        "sha256": str(row["evidence_sha256"]),
        "mediaType": "application/json",
        "license": {"@id": OGL_URL},
        "rightsStatus": "ogl_v3_except_where_otherwise_stated",
        "snapshotId": snapshot_id,
        "authority": "source_native",
    }


def _semantic_common(
    row: dict[str, Any],
    *,
    identifier: str,
    entity_type: str,
    source_native_id: str,
    evidence_id: str,
    snapshot_id: str,
    source_system: str,
) -> dict[str, Any]:
    value = {
        "@id": identifier,
        "@type": entity_type,
        "title": str(row.get("title") or row.get("name") or source_native_id),
        "description": str(row.get("description") or ""),
        "sourceNativeId": source_native_id,
        "sourceSystem": source_system or "govuk_public_metadata",
        "snapshotId": snapshot_id,
        "authority": "source_native",
        "evidence": [{"@id": evidence_id}],
        "retrievedAt": str(row.get("retrieved_at")),
    }
    return value


def dataset_semantic_nodes(row: dict[str, Any], snapshot_id: str) -> list[dict[str, Any]]:
    evidence = semantic_evidence_node(
        row,
        discriminator=str(row["open"]),
        snapshot_id=snapshot_id,
        source_system=str(row.get("source_adapter") or "govuk_public_metadata"),
    )
    evidence_id = str(evidence["@id"])
    source_system = str(row.get("source_adapter") or "govuk_public_metadata")
    route = str(row["open"])
    route_iri = semantic_route_iri(route)
    url = str(row["url"])
    base_path = str(row.get("base_path") or urlparse(url).path or "/")
    content_id = str(row.get("canonical_content_id") or "")
    locale = str(row.get("language") or "en")
    nodes: list[dict[str, Any]] = [evidence]
    if re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
        content_id,
    ):
        content_item_id = semantic_content_item_iri(content_id)
        item = _semantic_common(
            row,
            identifier=content_item_id,
            entity_type="govuk:ContentItem",
            source_native_id=content_id,
            evidence_id=evidence_id,
            snapshot_id=snapshot_id,
            source_system=source_system,
        )
        item.update(
            {
                "contentId": content_id,
                "basePath": base_path,
                "canonicalUrl": url,
                "contentType": {
                    "@id": semantic_content_type_iri(str(row["document_type"]))
                },
                "schemaFamily": {
                    "@id": semantic_schema_family_iri(str(row["schema_name"]))
                },
            }
        )
        document = _semantic_common(
            row,
            identifier=route_iri,
            entity_type="govuk:Document",
            source_native_id=f"{content_id}:{locale}",
            evidence_id=evidence_id,
            snapshot_id=snapshot_id,
            source_system=source_system,
        )
        document.update(
            {
                "contentItem": {"@id": content_item_id},
                "locale": locale,
                "route": {"@id": semantic_route_node_iri(base_path)},
            }
        )
        route_node = _semantic_common(
            row,
            identifier=semantic_route_node_iri(base_path),
            entity_type="govuk:Route",
            source_native_id=base_path,
            evidence_id=evidence_id,
            snapshot_id=snapshot_id,
            source_system=source_system,
        )
        route_node.update(
            {
                "path": base_path,
                "canonicalUrl": url,
                "routeKind": "redirect"
                if row.get("lifecycle") == "redirect"
                else "exact",
            }
        )
        nodes.extend((item, document, route_node))
        specialised = dataset_specialised_semantic_node(
            row,
            content_item_id=content_item_id,
            evidence_id=evidence_id,
            snapshot_id=snapshot_id,
            source_system=source_system,
        )
        if specialised is not None:
            nodes.append(specialised)
    else:
        route_node = _semantic_common(
            row,
            identifier=route_iri,
            entity_type="govuk:Route",
            source_native_id=base_path,
            evidence_id=evidence_id,
            snapshot_id=snapshot_id,
            source_system=source_system,
        )
        route_node.update(
            {
                "path": base_path,
                "canonicalUrl": url,
                "routeKind": "redirect"
                if row.get("lifecycle") == "redirect"
                else "unknown",
            }
        )
        nodes.append(route_node)
    return nodes


def dataset_specialised_semantic_node(
    row: dict[str, Any],
    *,
    content_item_id: str,
    evidence_id: str,
    snapshot_id: str,
    source_system: str,
) -> dict[str, Any] | None:
    document_type = str(row.get("document_type") or "unknown")
    schema_name = str(row.get("schema_name") or "unknown")
    base_path = str(row.get("base_path") or "/")
    entity_type: str | None = None
    attributes: dict[str, Any] = {}
    if document_type == "taxon" or schema_name == "taxon":
        if base_path.startswith("/world/"):
            entity_type = "govuk:WorldTaxon"
            attributes = {
                "basePath": base_path,
                "canonicalUrl": row["url"],
                "locale": row.get("language") or "en",
            }
        else:
            entity_type = "govuk:Taxon"
            attributes = {
                "basePath": base_path,
                "canonicalUrl": row["url"],
                "taxonomyKind": "topic",
            }
    elif document_type == "world_location" and base_path.startswith("/world/"):
        entity_type = "govuk:WorldTaxon"
        attributes = {
            "basePath": base_path,
            "canonicalUrl": row["url"],
            "locale": row.get("language") or "en",
        }
    elif document_type == "mainstream_browse_page":
        entity_type = "govuk:MainstreamBrowsePage"
        attributes = {"basePath": base_path, "canonicalUrl": row["url"]}
    elif document_type in {"document_collection", "collection"}:
        entity_type = "govuk:Collection"
        attributes = {"canonicalUrl": row["url"], "collectionKind": "collection"}
    if entity_type is None:
        return None
    node = _semantic_common(
        row,
        identifier=content_item_id + ":classification:" + entity_type.rsplit(":", 1)[-1],
        entity_type=entity_type,
        source_native_id=str(row.get("canonical_content_id")),
        evidence_id=evidence_id,
        snapshot_id=snapshot_id,
        source_system=source_system,
    )
    node.update(attributes)
    node["contentItem"] = {"@id": content_item_id}
    return node


def publisher_semantic_nodes(row: dict[str, Any], snapshot_id: str) -> list[dict[str, Any]]:
    evidence = semantic_evidence_node(
        row,
        discriminator=str(row["open"]),
        snapshot_id=snapshot_id,
        source_system="govuk_organisations_metadata",
    )
    state = str(row.get("state") or "unknown")
    if state not in {"live", "closed", "exempt", "unknown"}:
        state = "unknown"
    node = _semantic_common(
        row,
        identifier=semantic_route_iri(str(row["open"])),
        entity_type="govuk:Organisation",
        source_native_id=str(row.get("content_id") or row.get("name") or row["url"]),
        evidence_id=str(evidence["@id"]),
        snapshot_id=snapshot_id,
        source_system="govuk_organisations_metadata",
    )
    node.update(
        {
            "slug": str(row["name"]),
            "canonicalUrl": row["url"],
            "organisationStatus": state,
        }
    )
    return [evidence, node]


def resource_semantic_nodes(row: dict[str, Any], snapshot_id: str) -> list[dict[str, Any]]:
    evidence = semantic_evidence_node(
        row,
        discriminator=str(row["open"]),
        snapshot_id=snapshot_id,
        source_system="govuk_content_api_attachment",
    )
    filename = urlparse(str(row["url"])).path.rsplit("/", 1)[-1] or str(row["title"])
    parent_content_id = str(row.get("parent_content_id") or "")
    if not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
        parent_content_id,
    ):
        raise PublicationError(
            f"attachment {row['open']} has no source-native ContentItem parent; "
            "route-only attachment semantics must be exceptioned before publication"
        )
    node = _semantic_common(
        row,
        identifier=semantic_route_iri(str(row["open"])),
        entity_type="govuk:Attachment",
        source_native_id=str(row.get("attachment_id") or row["url"]),
        evidence_id=str(evidence["@id"]),
        snapshot_id=snapshot_id,
        source_system="govuk_content_api_attachment",
    )
    node.update(
        {
            "contentItem": {"@id": semantic_content_item_iri(parent_content_id)},
            "canonicalUrl": row["url"],
            "mimeType": row.get("content_type") or "application/octet-stream",
            "filename": filename,
            "accessibility": row.get("accessibility") or "unknown",
            "rightsStatus": "review_required"
            if row.get("rights_status") == "item-specific-review-required"
            else "unknown",
        }
    )
    if isinstance(row.get("bytes"), int) and row["bytes"] >= 0:
        node["byteSize"] = row["bytes"]
    if isinstance(row.get("pages"), int) and row["pages"] >= 0:
        node["pageCount"] = row["pages"]
    return [evidence, node]


SEMANTIC_PREDICATES = {
    "published by": "https://chris-page-gov.github.io/okf-govuk-content/ns#publishedBy",
    "originally published by": "https://chris-page-gov.github.io/okf-govuk-content/ns#publishedBy",
    "associated with organisation": "https://chris-page-gov.github.io/okf-govuk-content/ns#associatedWithOrganisation",
    "owned by": "https://chris-page-gov.github.io/okf-govuk-content/ns#ownedBy",
    "led by": "https://chris-page-gov.github.io/okf-govuk-content/ns#ledBy",
    "part of": "http://purl.org/dc/terms/isPartOf",
    "part of collection": "http://purl.org/dc/terms/isPartOf",
    "part of step by step": "http://purl.org/dc/terms/isPartOf",
    "part of topical event": "http://purl.org/dc/terms/isPartOf",
    "parent of": "https://chris-page-gov.github.io/okf-govuk-content/ns#parentOf",
    "child of": "https://chris-page-gov.github.io/okf-govuk-content/ns#childOf",
    "classified under": "https://chris-page-gov.github.io/okf-govuk-content/ns#classifiedUnder",
    "related to": "http://purl.org/dc/terms/relation",
    "redirects to": "https://chris-page-gov.github.io/okf-govuk-content/ns#redirectsTo",
    "has attachment": "https://chris-page-gov.github.io/okf-govuk-content/ns#hasAttachment",
    "available in language": "https://chris-page-gov.github.io/okf-govuk-content/ns#availableInLanguage",
    "applies to world location": "https://chris-page-gov.github.io/okf-govuk-content/ns#appliesToWorldLocation",
}


def assertion_semantic_nodes(
    edge: dict[str, Any], snapshot_id: str, activity_id: str
) -> list[dict[str, Any]]:
    evidence_row = {
        "evidence_url": edge["evidence_url"],
        "evidence_locator": edge["evidence_locator"],
        "retrieved_at": edge["observed_at"],
        "evidence_sha256": edge["evidence_sha256"],
    }
    evidence = semantic_evidence_node(
        evidence_row,
        discriminator=str(edge["assertion_id"]),
        snapshot_id=snapshot_id,
        source_system=str(edge.get("evidence_type") or "official_source_field"),
    )
    predicate = SEMANTIC_PREDICATES.get(
        str(edge["kind"]),
        "https://chris-page-gov.github.io/okf-govuk-content/ns#"
        + quote(slugify(str(edge["kind"])), safe="-"),
    )
    assertion = {
        "@id": "urn:govuk:" + str(edge["assertion_id"]),
        "@type": "govuk:Assertion",
        "subject": {"@id": semantic_route_iri(str(edge["source"]))},
        "predicate": {"@id": predicate},
        "object": {"@id": semantic_route_iri(str(edge["target"]))},
        "sourceNativePredicate": str(edge["source_native_predicate"]),
        "evidence": [{"@id": evidence["@id"]}],
        "retrievedAt": str(edge["observed_at"]),
        "observedAt": str(edge["observed_at"]),
        "generatedBy": {"@id": activity_id},
        "derivationMethod": str(edge["derivation_method"]),
        "assertionStatus": "source_native",
        "authority": "source_native",
        "confidence": float(edge["confidence"]),
        "snapshotId": snapshot_id,
    }
    return [evidence, assertion]


def _semantic_graph(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"@context": GOVUK_CONTEXT_URL, "@graph": nodes}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def write_semantic_projection(
    output: Path,
    datasets: Sequence[dict[str, Any]],
    publishers: Sequence[dict[str, Any]],
    resources: Sequence[dict[str, Any]],
    relationships: Sequence[dict[str, Any]],
    facets: dict[str, Any],
    generated_at: str,
    snapshot_id: str,
) -> dict[str, Any]:
    root = output / "data" / "semantic"
    root.mkdir(parents=True, exist_ok=True)
    paths: dict[str, list[str]] = {
        "entities": [],
        "publishers": [],
        "resources": [],
        "assertions": [],
    }
    shards: dict[str, list[dict[str, Any]]] = {
        "entities": [],
        "publishers": [],
        "resources": [],
        "assertions": [],
        "vocabulary": [],
    }
    by_type: collections.Counter[str] = collections.Counter()

    def emit_chunks(
        name: str,
        rows: Sequence[dict[str, Any]],
        size: int,
        transform: Callable[[dict[str, Any]], list[dict[str, Any]]],
    ) -> None:
        for index, block in enumerate(chunks(rows, size)):
            nodes: list[dict[str, Any]] = []
            for row in block:
                projected = transform(row)
                nodes.extend(projected)
                by_type.update(str(node["@type"]) for node in projected)
            path = root / f"{name}-{index}.jsonld.gz"
            document = _semantic_graph(nodes)
            write_gzip_json(path, document)
            relative = path.relative_to(output).as_posix()
            paths[name].append(relative)
            node_keys = sorted(str(node["@id"]) for node in nodes)
            shards[name].append(
                {
                    "path": relative,
                    "schema": "govuk-okf-semantic-shard.v1",
                    "snapshot": snapshot_id,
                    "count": len(nodes),
                    "source_row_count": len(block),
                    "compressed_bytes": path.stat().st_size,
                    "uncompressed_bytes": len(canonical_json_bytes(document)),
                    "sha256": _file_sha256(path),
                    "first_key": node_keys[0] if node_keys else None,
                    "last_key": node_keys[-1] if node_keys else None,
                    "compression": "gzip",
                }
            )

    emit_chunks(
        "entities",
        datasets,
        SEMANTIC_ENTITY_CHUNK_SIZE,
        lambda row: dataset_semantic_nodes(row, snapshot_id),
    )
    emit_chunks(
        "publishers",
        publishers,
        SEMANTIC_ENTITY_CHUNK_SIZE,
        lambda row: publisher_semantic_nodes(row, snapshot_id),
    )
    emit_chunks(
        "resources",
        resources,
        SEMANTIC_ENTITY_CHUNK_SIZE,
        lambda row: resource_semantic_nodes(row, snapshot_id),
    )

    profile_path = ROOT / "semantic" / "profile" / "govuk-okf-profile-v1.yamlld"
    profile_digest = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    profile_evidence_id = "urn:govuk:evidence:profile:" + profile_digest
    activity_id = "urn:govuk:inference:compile:" + hashlib.sha256(
        snapshot_id.encode("utf-8")
    ).hexdigest()
    vocabulary_evidence = {
        "@id": profile_evidence_id,
        "@type": "govuk:Evidence",
        "title": "GOV.UK OKF profile and pinned Publishing API schema crosswalk",
        "evidenceUrl": REPOSITORY_URL
        + "/blob/main/semantic/profile/govuk-okf-profile-v1.yamlld",
        "sourceSystem": "govuk_okf_profile",
        "sourceAuthority": "unknown",
        "locator": {
            "locatorKind": "schema_path",
            "locatorValue": "semantic/profile/govuk-okf-profile-v1.yamlld",
        },
        "retrievedAt": generated_at,
        "sha256": profile_digest,
        "mediaType": "application/yaml",
        "license": {"@id": REPOSITORY_URL + "/blob/main/LICENSE.md"},
        "rightsStatus": "source_declared",
        "snapshotId": snapshot_id,
        "authority": "source_native",
    }
    activity = {
        "@id": activity_id,
        "@type": "govuk:InferenceActivity",
        "title": "Deterministic GOV.UK OKF semantic projection",
        "description": "Deterministic projection of validated Explorer records and relationships.",
        "sourceNativeId": snapshot_id,
        "sourceSystem": "govuk-okf-compiler",
        "snapshotId": snapshot_id,
        "authority": "normalized",
        "evidence": [{"@id": profile_evidence_id}],
        "retrievedAt": generated_at,
        "startedAt": generated_at,
        "endedAt": generated_at,
        "derivationMethod": "deterministic-semantic-projection-v1",
    }
    vocabulary_nodes: list[dict[str, Any]] = [vocabulary_evidence, activity]
    for entry in facets["document_type"]:
        value = str(entry["value"])
        vocabulary_nodes.append(
            {
                "@id": semantic_content_type_iri(value),
                "@type": "govuk:ContentType",
                "title": value.replace("_", " "),
                "description": "Source-native GOV.UK document type.",
                "sourceNativeId": value,
                "sourceSystem": "govuk_content_metadata",
                "snapshotId": snapshot_id,
                "authority": "source_native",
                "evidence": [{"@id": profile_evidence_id}],
                "retrievedAt": generated_at,
                "sourceName": value,
            }
        )
    schema_base = (
        "https://github.com/alphagov/publishing-api/tree/"
        + PUBLISHING_API_SCHEMA_COMMIT
        + "/content_schemas"
    )
    for entry in facets["schema_name"]:
        value = str(entry["value"])
        vocabulary_nodes.append(
            {
                "@id": semantic_schema_family_iri(value),
                "@type": "govuk:SchemaFamily",
                "title": value.replace("_", " "),
                "description": "Version-pinned GOV.UK Publishing API schema family.",
                "sourceNativeId": value,
                "sourceSystem": "alphagov_publishing_api",
                "snapshotId": snapshot_id,
                "authority": "source_native",
                "evidence": [{"@id": profile_evidence_id}],
                "retrievedAt": generated_at,
                "schemaName": value,
                "schemaUri": schema_base,
                "sourceCommit": PUBLISHING_API_SCHEMA_COMMIT,
            }
        )
    by_type.update(str(node["@type"]) for node in vocabulary_nodes)
    vocabulary_path = root / "vocabulary.jsonld"
    vocabulary_path.write_text(pretty_json(_semantic_graph(vocabulary_nodes)), encoding="utf-8")
    vocabulary_keys = sorted(str(node["@id"]) for node in vocabulary_nodes)
    shards["vocabulary"].append(
        {
            "path": vocabulary_path.relative_to(output).as_posix(),
            "schema": "govuk-okf-semantic-shard.v1",
            "snapshot": snapshot_id,
            "count": len(vocabulary_nodes),
            "source_row_count": len(vocabulary_nodes),
            "compressed_bytes": vocabulary_path.stat().st_size,
            "uncompressed_bytes": vocabulary_path.stat().st_size,
            "sha256": _file_sha256(vocabulary_path),
            "first_key": vocabulary_keys[0] if vocabulary_keys else None,
            "last_key": vocabulary_keys[-1] if vocabulary_keys else None,
            "compression": "identity",
        }
    )

    emit_chunks(
        "assertions",
        relationships,
        SEMANTIC_ASSERTION_CHUNK_SIZE,
        lambda row: assertion_semantic_nodes(row, snapshot_id, activity_id),
    )
    counts = {
        "source_datasets": len(datasets),
        "source_publishers": len(publishers),
        "source_resources": len(resources),
        "source_relationships": len(relationships),
        "entity_nodes": sum(
            count
            for entity_type, count in by_type.items()
            if entity_type
            not in {"govuk:Evidence", "govuk:Assertion", "govuk:InferenceActivity"}
        ),
        "evidence_nodes": by_type["govuk:Evidence"],
        "assertion_nodes": by_type["govuk:Assertion"],
        "activity_nodes": by_type["govuk:InferenceActivity"],
        "total_nodes": sum(by_type.values()),
    }
    manifest = {
        "schema": "govuk-okf-semantic-projection.v1",
        "context": GOVUK_CONTEXT_URL,
        "profile": "semantic/profile/govuk-okf-profile-v1.yamlld",
        "snapshot": snapshot_id,
        "generated_at": generated_at,
        "startup": "lazy",
        "counts": counts,
        "by_type": dict(sorted(by_type.items())),
        "chunks": paths,
        "shards": shards,
        "entrypoints": {
            "vocabulary": vocabulary_path.relative_to(output).as_posix(),
            "entity_schema": "semantic/schemas/entity.schema.json",
            "evidence_schema": "semantic/schemas/evidence.schema.json",
            "assertion_schema": "semantic/schemas/assertion.schema.json",
            "profile": "semantic/profile/govuk-okf-profile-v1.yamlld",
            "entity_crosswalk": "semantic/crosswalks/entity-crosswalk.yamlld",
            "relationship_crosswalk": "semantic/crosswalks/relationship-crosswalk.yamlld",
            "shapes": "semantic/shapes/govuk-okf-shapes.ttl",
        },
        "identity": {
            "explorer_route": HOME_URL + "id/{route}",
            "content_item": "urn:govuk:content-item:{content_id}",
            "assertion": "urn:govuk:assertion-{sha256}",
        },
    }
    manifest["shard_manifest_sha256"] = hashlib.sha256(
        canonical_json_bytes(shards)
    ).hexdigest()
    (root / "manifest.json").write_text(pretty_json(manifest), encoding="utf-8")
    return manifest


def copy_static_app(output: Path) -> None:
    source = ROOT / "explorer" / "src"
    if source.is_dir() and (source / "index.html").is_file():
        for path in source.rglob("*"):
            if path.is_file() and path.name != ".DS_Store" and not path.name.startswith("._"):
                destination = output / path.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
        return
    fallback = """<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\"><title>What’s on GOV.UK</title><main><h1>What’s on GOV.UK</h1><p>This is a derived, non-authoritative metadata catalogue. <a href=\"https://www.gov.uk/\">Use GOV.UK for authoritative guidance and services.</a></p><p><a href=\"okf-explorer.json\">Open the Explorer descriptor</a></p></main></html>\n"""
    (output / "index.html").write_text(fallback, encoding="utf-8")


def _emit_publication(
    datasets: Sequence[dict[str, Any]],
    publishers: Sequence[dict[str, Any]],
    resources: Sequence[dict[str, Any]],
    relationships: Sequence[dict[str, Any]],
    output: Path,
    generated_at: str,
    snapshot_id: str,
    *,
    source_count: int,
    dispositions_close: bool,
    search_writer: Callable[..., dict[str, Any]] = write_search,
    adjacency_writer: Callable[..., dict[str, Any]] = write_adjacency,
    route_index_writer: Callable[..., dict[str, Any]] = write_route_index,
) -> dict[str, Any]:
    """Emit one publication from deterministic, repeatable row sequences.

    The ordinary compiler supplies lists.  The disk-backed compiler supplies
    SQLite-backed sequences and streaming index writers, keeping this single
    descriptor/manifest implementation as the bundle contract.
    """
    counts = {
        "datasets": len(datasets),
        "records": len(datasets),
        "publishers": len(publishers),
        "resources": len(resources),
        "relationships": len(relationships),
        "document_types": len({item["document_type"] for item in datasets}),
        "schema_families": len({item["schema_name"] for item in datasets}),
        "languages": len({item["language"] for item in datasets}),
    }

    record_shards: dict[str, list[dict[str, Any]]] = {
        "datasets": [],
        "publishers": [],
        "resources": [],
        "relationships": [],
    }
    dataset_paths: list[str] = []
    for index, block in enumerate(chunks(datasets, RECORD_CHUNK_SIZE)):
        block_rows = list(block)
        path = output / "data" / f"records-{index}.json.gz"
        write_gzip_json(path, block_rows)
        dataset_paths.append(path.relative_to(output).as_posix())
        keys = [str(row["open"]) for row in block_rows]
        record_shards["datasets"].append(
            data_plane_shard_metadata(
                output,
                path,
                schema="okf-record-shard.v1",
                snapshot_id=snapshot_id,
                count=len(block_rows),
                first_key=keys[0] if keys else None,
                last_key=keys[-1] if keys else None,
                compression="gzip",
                extra={"kind": "datasets", "ordinal": index},
            )
        )
    publisher_paths: list[str] = []
    for index, block in enumerate(chunks(publishers, RECORD_CHUNK_SIZE)):
        block_rows = list(block)
        path = output / "data" / f"publishers-{index}.json.gz"
        write_gzip_json(path, block_rows)
        publisher_paths.append(path.relative_to(output).as_posix())
        keys = [str(row["open"]) for row in block_rows]
        record_shards["publishers"].append(
            data_plane_shard_metadata(
                output,
                path,
                schema="okf-publisher-shard.v1",
                snapshot_id=snapshot_id,
                count=len(block_rows),
                first_key=keys[0] if keys else None,
                last_key=keys[-1] if keys else None,
                compression="gzip",
                extra={"kind": "publishers", "ordinal": index},
            )
        )
    if not publisher_paths:
        path = output / "data" / "publishers-0.json.gz"
        write_gzip_json(path, [])
        publisher_paths.append(path.relative_to(output).as_posix())
        record_shards["publishers"].append(
            data_plane_shard_metadata(
                output,
                path,
                schema="okf-publisher-shard.v1",
                snapshot_id=snapshot_id,
                count=0,
                first_key=None,
                last_key=None,
                compression="gzip",
                extra={"kind": "publishers", "ordinal": 0},
            )
        )
    resource_paths: list[str] = []
    for index, block in enumerate(chunks(resources, RECORD_CHUNK_SIZE)):
        block_rows = list(block)
        path = output / "data" / f"resources-{index}.json.gz"
        write_gzip_json(path, block_rows)
        resource_paths.append(path.relative_to(output).as_posix())
        keys = [str(row["open"]) for row in block_rows]
        record_shards["resources"].append(
            data_plane_shard_metadata(
                output,
                path,
                schema="okf-resource-shard.v1",
                snapshot_id=snapshot_id,
                count=len(block_rows),
                first_key=keys[0] if keys else None,
                last_key=keys[-1] if keys else None,
                compression="gzip",
                extra={"kind": "resources", "ordinal": index},
            )
        )
    if not resource_paths:
        path = output / "data" / "resources-0.json.gz"
        write_gzip_json(path, [])
        resource_paths.append(path.relative_to(output).as_posix())
        record_shards["resources"].append(
            data_plane_shard_metadata(
                output,
                path,
                schema="okf-resource-shard.v1",
                snapshot_id=snapshot_id,
                count=0,
                first_key=None,
                last_key=None,
                compression="gzip",
                extra={"kind": "resources", "ordinal": 0},
            )
        )
    relationship_paths: list[str] = []
    for index, block in enumerate(chunks(relationships, RELATIONSHIP_CHUNK_SIZE)):
        block_rows = list(block)
        path = output / "data" / f"relationships-{index}.json.gz"
        write_gzip_json(path, block_rows)
        relationship_paths.append(path.relative_to(output).as_posix())
        keys = [
            "\0".join(
                (
                    str(row["source"]),
                    str(row["kind"]),
                    str(row["target"]),
                    str(row["assertion_id"]),
                )
            )
            for row in block_rows
        ]
        record_shards["relationships"].append(
            data_plane_shard_metadata(
                output,
                path,
                schema="okf-relationship-shard.v1",
                snapshot_id=snapshot_id,
                count=len(block_rows),
                first_key=keys[0] if keys else None,
                last_key=keys[-1] if keys else None,
                compression="gzip",
                extra={"kind": "relationships", "ordinal": index},
            )
        )

    facets = {
        "schema": "okf-facets.v1",
        "document_type": count_values(datasets, "document_type"),
        "schema_name": count_values(datasets, "schema_name"),
        "language": count_values(datasets, "language"),
        "lifecycle": count_values(datasets, "lifecycle"),
        "publisher": count_values(datasets, "publisher_title"),
        "source_membership": count_values(datasets, "source_memberships"),
    }
    facets_path = output / "data" / "facets.json"
    facets_path.write_text(pretty_json(facets), encoding="utf-8")
    graph = {
        "schema": "okf-graph-summary.v1",
        "nodes": {"datasets": len(datasets), "publishers": len(publishers), "resources": len(resources)},
        "relationships_by_kind": count_values(relationships, "kind"),
    }
    (output / "data" / "graph.json").write_text(pretty_json(graph), encoding="utf-8")
    semantic_projection_manifest = write_semantic_projection(
        output,
        datasets,
        publishers,
        resources,
        relationships,
        facets,
        generated_at,
        snapshot_id,
    )
    search_manifest = search_writer(
        output,
        datasets,
        "data/facets.json",
        snapshot_id=snapshot_id,
        generated_at=generated_at,
    )
    adjacency_manifest = adjacency_writer(
        output,
        relationships,
        snapshot_id=snapshot_id,
        generated_at=generated_at,
    )
    route_manifest = route_index_writer(
        output,
        datasets,
        publishers,
        resources,
        snapshot_id=snapshot_id,
        generated_at=generated_at,
    )

    overview = {
        "schema": "okf-large-overview.v1",
        "title": "What’s on GOV.UK public metadata corpus",
        "generated_at": generated_at,
        "snapshot": snapshot_id,
        "counts": counts,
        "coverage": {
            "boundary": "frozen union of verified official public enumerators",
            "page_bodies": "not mirrored",
            "unexplained_omissions": 0 if dispositions_close else None,
            "authoritative_destination": "https://www.gov.uk/",
        },
        "facets": facets,
        "sample_records": datasets[:10],
    }
    (output / "data" / "overview.json").write_text(pretty_json(overview), encoding="utf-8")
    analysis = {
        "schema": "okf-explorer-analysis.v1",
        "generated_at": generated_at,
        "snapshot": snapshot_id,
        "cardinality": counts,
        "long_tail": {
            "document_types": len(facets["document_type"]),
            "schema_families": len(facets["schema_name"]),
            "publishers": len(facets["publisher"]),
        },
        "coverage": overview["coverage"],
        "performance": {
            "bootstrap_compressed_budget_bytes": 2097152,
            "ordinary_shard_compressed_budget_bytes": 5242880,
            "search_warm_p95_budget_ms": 500,
            "search_cold_p95_budget_ms": 1500,
            "graph_expansion_nodes": 250,
            "graph_expansion_edges": 500,
        },
    }
    analysis_path = output / "data" / "analysis" / "overview.json"
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    analysis_path.write_text(pretty_json(analysis), encoding="utf-8")

    search_shard_groups = search_manifest.pop("_compiled_shards", None)
    if not isinstance(search_shard_groups, dict):
        raise PublicationError("search writer did not return compiled shard metadata")
    search_shards = [
        row
        for group in search_shard_groups.values()
        for row in group
    ]
    all_data_plane_shards = [
        *(
            row
            for group in record_shards.values()
            for row in group
        ),
        *search_shards,
        *adjacency_manifest["shards"],
        *route_manifest["shards"],
    ]
    shard_paths = [str(row["path"]) for row in all_data_plane_shards]
    if len(shard_paths) != len(set(shard_paths)):
        raise PublicationError("data-plane manifest contains duplicate shard paths")
    record_shard_digest = shard_manifest_sha256(record_shards)
    manifest_root = data_plane_manifest_root(all_data_plane_shards)
    manifest = {
        "schema": "okf-data-manifest.v1",
        "schema_version": DATA_PLANE_SCHEMA_VERSION,
        "title": "What’s on GOV.UK static metadata index",
        "generated_at": generated_at,
        "snapshot": snapshot_id,
        "counts": counts,
        "chunks": {
            "datasets": dataset_paths,
            "resources": resource_paths,
            "publishers": publisher_paths,
            "relationships": relationship_paths,
        },
        "shards": record_shards,
        "budgets": DATA_PLANE_BUDGETS,
        "integrity": {
            "schema": "okf-data-plane-integrity.v1",
            "schema_version": DATA_PLANE_SCHEMA_VERSION,
            "algorithm": "sha256-canonical-shard-leaves-v1",
            "leaf_count": len(all_data_plane_shards),
            "record_shard_manifest_sha256": record_shard_digest,
            "search_shard_manifest_sha256": search_manifest[
                "shard_manifest_sha256"
            ],
            "adjacency_shard_manifest_sha256": adjacency_manifest[
                "shard_manifest_sha256"
            ],
            "route_shard_manifest_sha256": route_manifest[
                "shard_manifest_sha256"
            ],
            "manifest_root_sha256": manifest_root,
        },
        "indexes": {
            "overview": "data/overview.json",
            "analysis": "data/analysis/overview.json",
            "facets": "data/facets.json",
            "graph": "data/graph.json",
            "search": "data/search/manifest.json",
            "relationship_adjacency": "data/adjacency/manifest.json",
            "route_index": "data/routes/manifest.json",
            "semantic_projection": "data/semantic/manifest.json",
        },
        "search": {"schema": search_manifest["schema"], "documents": len(datasets), "tokens": search_manifest["counts"]["tokens"], "result_limit": 200},
        "semantic": {
            "schema": semantic_projection_manifest["schema"],
            "entity_nodes": semantic_projection_manifest["counts"]["entity_nodes"],
            "assertion_nodes": semantic_projection_manifest["counts"]["assertion_nodes"],
            "startup": "lazy",
        },
        "performance": {
            "startup_mode": "overview-first",
            "full_record_hydration": "lazy",
            "relationship_hydration": "lazy",
            "route_relationship_hydration": "hash-sharded adjacency",
            "search": "static worker-compatible shards",
            "semantic_projection": "lazy JSON-LD entity/evidence/assertion shards",
        },
    }
    (output / "data" / "manifest.json").write_text(pretty_json(manifest), encoding="utf-8")

    semantic = semantic_descriptor(counts, generated_at, snapshot_id)
    local_context = ROOT / "semantic" / "context" / "govuk-okf-v1.jsonld"
    if local_context.is_file():
        context_target = output / "context" / "govuk-okf-v1.jsonld"
        context_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_context, context_target)
    semantic_source = ROOT / "semantic"
    semantic_publication_paths = [
        semantic_source / relative
        for relative in (*SEMANTIC_PUBLICATION_DIRECTORIES, *SEMANTIC_PUBLICATION_FILES)
    ]
    for publication_path in semantic_publication_paths:
        candidates = (
            sorted(publication_path.rglob("*"))
            if publication_path.is_dir()
            else [publication_path]
        )
        for semantic_path in candidates:
            if (
                semantic_path.is_file()
                and semantic_path.name != ".DS_Store"
                and not semantic_path.name.startswith("._")
            ):
                semantic_target = output / "semantic" / semantic_path.relative_to(
                    semantic_source
                )
                semantic_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(semantic_path, semantic_target)
    (output / "okf-bundle.yamlld").write_text(yaml_dump(semantic) + "\n", encoding="utf-8")
    (output / "okf-bundle.jsonld").write_text(pretty_json(semantic), encoding="utf-8")
    semantic_projection_digest = hashlib.sha256(canonical_json_bytes(semantic)).hexdigest()
    descriptor = {
        "@context": EXPLORER_CONTEXT_URL,
        "@id": HOME_URL + "okf-explorer.json",
        "schema": "okf-explorer-large-corpus.v1",
        "kind": "okf-large-corpus",
        "title": "What’s on GOV.UK",
        "description": "Derived, non-authoritative semantic catalogue of GOV.UK content, navigation, organisations, taxonomies and relationships.",
        "version": "0.1.0",
        "status": "preview",
        "generated_at": generated_at,
        "snapshot": snapshot_id,
        "profile": PROFILE_URL,
        "publisher": "https://github.com/chris-page-gov",
        "license": OGL_URL,
        "semantic_descriptor": HOME_URL + "okf-bundle.yamlld",
        "semantic_projection_sha256": semantic_projection_digest,
        "data_plane_manifest_root_sha256": manifest_root,
        "counts": counts,
        "entrypoints": {
            "viewer": "https://chris-page-gov.github.io/okf-explorer/",
            "data_manifest": {
                "path": "data/manifest.json",
                "sha256": _file_sha256(output / "data" / "manifest.json"),
            },
            "overview_index": {
                "path": "data/overview.json",
                "sha256": _file_sha256(output / "data" / "overview.json"),
            },
            "analysis_overview": {
                "path": "data/analysis/overview.json",
                "sha256": _file_sha256(output / "data" / "analysis" / "overview.json"),
            },
            "search_manifest": {
                "path": "data/search/manifest.json",
                "sha256": _file_sha256(output / "data" / "search" / "manifest.json"),
            },
            "relationship_adjacency": {
                "path": "data/adjacency/manifest.json",
                "sha256": _file_sha256(output / "data" / "adjacency" / "manifest.json"),
            },
            "route_index": {
                "path": "data/routes/manifest.json",
                "sha256": _file_sha256(output / "data" / "routes" / "manifest.json"),
            },
            "semantic_projection": "data/semantic/manifest.json",
            "markdown_index": "index.md",
        },
        "performance": manifest["performance"],
        "vocabulary": {
            "record_singular": "GOV.UK content item",
            "record_plural": "GOV.UK content items",
            "publisher_singular": "organisation",
            "publisher_plural": "organisations",
            "resource_singular": "attachment",
            "resource_plural": "attachments",
            "search_placeholder": "Search GOV.UK titles, summaries, types and organisations",
        },
        "extensions": {
            "govuk-okf-profile.v1": {"provenance_required": True, "source_native_before_inference": True},
            "okf-explorer-analysis.v1": {"entrypoint": "analysis_overview", "mode": "external"},
        },
    }
    (output / "okf-explorer.json").write_text(pretty_json(descriptor), encoding="utf-8")
    (output / "index.md").write_text(
        "# What’s on GOV.UK\n\nThis derived, non-authoritative OKF Bundle Wiki maps the public GOV.UK metadata estate. GOV.UK remains authoritative.\n\n"
        f"- Snapshot: `{snapshot_id}`\n- Records: {len(datasets):,}\n- Relationships: {len(relationships):,}\n- Attachments: {len(resources):,}\n",
        encoding="utf-8",
    )
    (output / "log.md").write_text(
        f"# Build log\n\n- Generated: {generated_at}\n- Snapshot: `{snapshot_id}`\n- Semantic projection SHA-256: `{semantic_projection_digest}`\n- Source records: {source_count}\n- Published records including relationship stubs: {len(datasets)}\n",
        encoding="utf-8",
    )
    copy_static_app(output)
    (output / ".nojekyll").write_text("", encoding="utf-8")
    return {"counts": counts, "semantic_projection_sha256": semantic_projection_digest, "manifest": manifest}


def _build_publication_into(source_records: list[dict[str, Any]], output: Path, generated_at: str, snapshot_id: str) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    datasets, publishers, resources, relationships = compile_records(source_records, generated_at, snapshot_id)
    allowed_dispositions = {
        "represented",
        "alias_of_represented",
        "redirect_only",
        "tombstone_only",
        "exceptioned",
    }
    dispositions_close = bool(source_records) and all(
        item.get("coverage_disposition") in allowed_dispositions for item in source_records
    )
    return _emit_publication(
        datasets,
        publishers,
        resources,
        relationships,
        output,
        generated_at,
        snapshot_id,
        source_count=len(source_records),
        dispositions_close=dispositions_close,
    )


def build_publication(source_records: list[dict[str, Any]], output: Path, generated_at: str, snapshot_id: str) -> dict[str, Any]:
    """Build in a temporary sibling and atomically replace only an approved output."""
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
    if ".git" in output.parts or (output.parent == ROOT.resolve() and output.name in protected_root_children):
        raise PublicationError(f"refusing protected repository output: {output}")
    if output.exists() and not output.is_dir():
        raise PublicationError(f"publication output exists and is not a directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    build_root = Path(tempfile.mkdtemp(prefix=f".{output.name}.build-", dir=output.parent))
    candidate = build_root / "publication"
    backup = build_root / "previous"
    moved_existing = False
    installed_candidate = False
    try:
        result = _build_publication_into(source_records, candidate, generated_at, snapshot_id)
        for required in ("okf-bundle.yamlld", "okf-bundle.jsonld", "okf-explorer.json", "data/manifest.json"):
            if not (candidate / required).is_file():
                raise PublicationError(f"candidate publication is missing {required}")
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
        if moved_existing and not installed_candidate and not output.exists() and backup.exists():
            backup.rename(output)
        if build_root.exists():
            shutil.rmtree(build_root)
        raise


def compare_trees(actual: Path, expected: Path) -> list[str]:
    ignored = {Path("checksums.json")}
    actual_files = {
        path.relative_to(actual)
        for path in actual.rglob("*")
        if path.is_file() and path.name != ".DS_Store" and not path.name.startswith("._")
    } - ignored
    expected_files = {
        path.relative_to(expected)
        for path in expected.rglob("*")
        if path.is_file() and path.name != ".DS_Store" and not path.name.startswith("._")
    } - ignored
    errors = [f"unexpected generated file: {path}" for path in sorted(actual_files - expected_files)]
    errors.extend(f"missing generated file: {path}" for path in sorted(expected_files - actual_files))
    for relative in sorted(actual_files & expected_files):
        if (actual / relative).read_bytes() != (expected / relative).read_bytes():
            errors.append(f"generated file is out of date: {relative}")
    return errors


def select_compiler(source_path: Path, requested: str = "auto") -> str:
    """Select the fixture or disk compiler without reading the corpus."""
    if requested not in {"auto", "memory", "disk"}:
        raise PublicationError(f"unknown publication compiler: {requested}")
    if requested != "auto":
        return requested
    if (
        source_path.is_dir()
        or source_path.name.endswith(".gz")
        or source_path.suffix == ".json"
    ):
        return "disk"
    if source_path.is_file() and source_path.stat().st_size >= 32 * 1024 * 1024:
        return "disk"
    return "memory"


def synchronize(
    source_path: Path,
    output: Path,
    generated_at: str,
    snapshot_id: str,
    check: bool = False,
    compiler: str = "auto",
) -> list[str]:
    selected = select_compiler(source_path, compiler)
    if selected == "disk":
        from .publication_disk import build_publication_from_path

        build = lambda target: build_publication_from_path(  # noqa: E731
            source_path, target, generated_at, snapshot_id
        )
    else:
        records = load_jsonl(source_path)
        build = lambda target: build_publication(  # noqa: E731
            records, target, generated_at, snapshot_id
        )
    if check:
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "bundle"
            build(expected)
            return compare_trees(output, expected)
    build(output)
    return []
