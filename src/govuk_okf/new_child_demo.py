"""Bounded, reproducible acquisition for the 69-record new-child demonstrator.

The live phase retains allowlisted public metadata envelopes only.  The
projection phase is network-free and emits exactly the frozen 69 Search API
seeds; typed targets outside that set are evidence-bearing boundary references
rather than recursively compiled content datasets.
"""

from __future__ import annotations

import collections
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import quote, urlencode, urlparse

from .acquisition import (
    AcquisitionError,
    HostLimiter,
    normalise_url,
    sanitise_content_api,
)
from .publication import LINK_KINDS
from .util import canonical_json_bytes, pretty_json, sha256_bytes
from .webprobe import Probe, fetch_probe

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT = ROOT / "demo" / "new-child-cohort.json"
ENVELOPE_SCHEMA = "govuk-okf-source-metadata-envelope.v1"
SNAPSHOT_SCHEMA = "govuk-okf-new-child-demo-snapshot.v1"
COHORT_MANIFEST_SCHEMA = "govuk-okf-new-child-demo-manifest.v1"
FORBIDDEN_BODY_KEYS = frozenset({"body", "govspeak", "html", "rendered_body"})
LINK_TARGET_SCALAR_FIELDS = frozenset(
    {
        "analytics_identifier",
        "api_path",
        "api_url",
        "base_path",
        "content_id",
        "description",
        "document_type",
        "link",
        "locale",
        "organisation_state",
        "public_updated_at",
        "schema_name",
        "slug",
        "title",
        "url",
        "web_url",
        "withdrawn",
    }
)
JSON_SCALAR_TYPES = (str, bool, int, float, type(None))
PROGRAMME_LEDGER_ID = "programme-official-source-request-counter"
MAX_RETRY_AFTER_SECONDS = 300.0
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)

Fetch = Callable[[str], tuple[bytes, dict[str, Any]]]


class NewChildDemoError(RuntimeError):
    """Raised when the bounded demonstrator cannot close exactly."""


def _bounded_retry_after_seconds(value: object) -> float:
    """Parse a Retry-After delta without allowing an unbounded process sleep."""

    if value in (None, ""):
        return 0.0
    try:
        delay = float(str(value))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(delay) or delay < 0:
        return 0.0
    if delay > MAX_RETRY_AFTER_SECONDS:
        raise NewChildDemoError(
            f"Retry-After exceeds bounded acquisition ceiling of {MAX_RETRY_AFTER_SECONDS:g} seconds"
        )
    return delay


def read_request_counter(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        promoted = False
        try:
            return int(stream.read().strip() or "0")
        except ValueError as exc:
            raise NewChildDemoError(f"invalid shared request ledger: {path}") from exc
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def increment_request_counter(path: Path, *, ceiling: int = 1_000_000) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.seek(0)
        try:
            current = int(stream.read().strip() or "0")
        except ValueError as exc:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            raise NewChildDemoError(f"invalid shared request ledger: {path}") from exc
        if current >= ceiling:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            raise NewChildDemoError(f"shared official-source request ceiling exhausted: {current}/{ceiling}")
        current += 1
        stream.seek(0)
        stream.truncate()
        stream.write(f"{current}\n")
        stream.flush()
        os.fsync(stream.fileno())
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return current


class _AuditedLimiter:
    """Apply the local cap/rate and separately increment the programme ledger."""

    def __init__(self, local: HostLimiter, audit_ledger: Path | None) -> None:
        self.local = local
        self.audit_ledger = audit_ledger

    def reserve(self) -> int | None:
        self.local.wait()
        if self.audit_ledger is not None:
            return increment_request_counter(self.audit_ledger)
        return None

    def reserve_without_rate_wait(self) -> int | None:
        """Test seam: preserve both ledgers without simulating wall-clock delay."""
        self.local._reserve_budget()
        if self.audit_ledger is not None:
            return increment_request_counter(self.audit_ledger)
        return None

    def wait(self) -> None:
        self.reserve()


def load_contract(path: Path = DEFAULT_CONTRACT) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != "govuk-okf-new-child-cohort-contract.v1":
        raise NewChildDemoError(f"unsupported new-child cohort contract: {path}")
    if int(value.get("search", {}).get("expected_seed_denominator", 0)) != 69:
        raise NewChildDemoError("new-child cohort contract must fail closed at exactly 69 seeds")
    if int(value.get("content_api", {}).get("retained_record_ceiling", 0)) != 250:
        raise NewChildDemoError("new-child retained-record ceiling must be 250")
    if int(value.get("content_api", {}).get("official_request_attempt_ceiling", 0)) != 500:
        raise NewChildDemoError("new-child official-attempt ceiling must be 500")
    return value


def combined_search_params(contract: dict[str, Any], *, count: int) -> list[tuple[str, Any]]:
    search = contract["search"]
    params: list[tuple[str, Any]] = [("count", count)]
    params.extend((str(search["filter_name"]), path) for path in search["browse_paths"])
    if count:
        params.extend(("fields", field) for field in search["fields"])
    return params


def group_search_params(contract: dict[str, Any], browse_path: str) -> list[tuple[str, Any]]:
    search = contract["search"]
    params: list[tuple[str, Any]] = [
        ("count", int(search["maximum_page_size"])),
        (str(search["filter_name"]), browse_path),
    ]
    params.extend(("fields", field) for field in search["fields"])
    return params


def search_url(endpoint: str, params: Iterable[tuple[str, Any]]) -> str:
    return endpoint + "?" + urlencode(list(params))


def canonical_search_link(row: dict[str, Any]) -> str:
    raw = row.get("link") or row.get("url")
    if not isinstance(raw, str) or not raw:
        raise NewChildDemoError("Search seed has no canonical link")
    return normalise_url(raw)


def search_content_id(row: dict[str, Any]) -> str | None:
    value = row.get("content_id")
    if not isinstance(value, str) or not value:
        candidate = row.get("_id")
        value = candidate if isinstance(candidate, str) and UUID_RE.fullmatch(candidate) else None
    return value


def seed_identity(row: dict[str, Any]) -> str:
    content_id = search_content_id(row)
    return "content-id:" + content_id if content_id else "canonical-link:" + canonical_search_link(row)


def dedupe_search_seeds(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe by content ID first and canonical link second, fail on conflicts."""

    by_link: dict[str, dict[str, Any]] = {}
    id_to_link: dict[str, str] = {}
    link_to_id: dict[str, str | None] = {}
    for source in rows:
        if not isinstance(source, dict):
            raise NewChildDemoError("Search result is not an object")
        row = dict(source)
        link = canonical_search_link(row)
        content_id = search_content_id(row)
        if content_id:
            existing_link = id_to_link.get(content_id)
            if existing_link is not None and existing_link != link:
                raise NewChildDemoError(f"content ID maps to conflicting canonical links: {content_id}")
            existing_id = link_to_id.get(link)
            if existing_id is not None and existing_id != content_id:
                raise NewChildDemoError(f"canonical link maps to conflicting content IDs: {link}")
            id_to_link[content_id] = link
            link_to_id[link] = content_id
        else:
            link_to_id.setdefault(link, None)
        existing = by_link.get(link)
        if existing is None or (search_content_id(existing) is None and content_id is not None):
            # Upgrade an earlier ID-less observation so a later duplicate by ID
            # cannot silently split the same identity across two routes.
            by_link[link] = row
    return sorted(by_link.values(), key=lambda row: (search_content_id(row) or "", canonical_search_link(row)))


def _without_body_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _without_body_fields(child)
            for key, child in sorted(value.items())
            if str(key).casefold() not in FORBIDDEN_BODY_KEYS
        }
    if isinstance(value, list):
        return [_without_body_fields(child) for child in value]
    return value


def _contains_forbidden_body_field(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).casefold() in FORBIDDEN_BODY_KEYS or _contains_forbidden_body_field(child)
            for key, child in value.items()
        )
    return isinstance(value, list) and any(_contains_forbidden_body_field(child) for child in value)


def safe_search_payload(payload: dict[str, Any], allowed_fields: Iterable[str]) -> dict[str, Any]:
    """Retain only the exact requested Search fields and shallow JSON values."""

    results = payload.get("results")
    if not isinstance(results, list):
        raise NewChildDemoError("Search response has no result list")
    fields = {"_id", *(str(value) for value in allowed_fields)}
    safe_results: list[dict[str, Any]] = []
    for ordinal, source in enumerate(results):
        if not isinstance(source, dict):
            raise NewChildDemoError(f"Search result {ordinal} is not an object")
        for key, value in source.items():
            if key not in fields and isinstance(value, (dict, list)):
                raise NewChildDemoError(
                    f"Search result {ordinal} contains undeclared structured field: {key}"
                )
        safe_row: dict[str, Any] = {}
        for key in sorted(fields):
            if key not in source:
                continue
            value = source[key]
            if isinstance(value, JSON_SCALAR_TYPES):
                safe_row[key] = value
            elif isinstance(value, list) and all(isinstance(item, JSON_SCALAR_TYPES) for item in value):
                safe_row[key] = list(value)
            else:
                raise NewChildDemoError(
                    f"Search result {ordinal} requested field is not shallow metadata: {key}"
                )
        safe_results.append(safe_row)
    safe: dict[str, Any] = {"results": safe_results}
    for key in ("start", "total"):
        if key in payload:
            if not isinstance(payload[key], (int, float)):
                raise NewChildDemoError(f"Search response {key} is not numeric")
            safe[key] = payload[key]
    return safe


def _safe_step_navigation(details: dict[str, Any]) -> dict[str, Any] | None:
    """Retain ordered step titles and link targets, never narrative step body."""

    source = details.get("step_by_step_nav")
    if not isinstance(source, dict):
        return None
    result: dict[str, Any] = {}
    if isinstance(source.get("title"), str):
        result["title"] = source["title"]
    steps: list[dict[str, Any]] = []
    for step in source.get("steps", []) if isinstance(source.get("steps"), list) else []:
        if not isinstance(step, dict):
            continue
        safe_step = {
            key: step[key]
            for key in ("title", "logic", "optional")
            if isinstance(step.get(key), (str, bool, int, float))
        }
        links: list[dict[str, str]] = []
        contents = step.get("contents") if isinstance(step.get("contents"), list) else []
        for item in contents:
            if not isinstance(item, dict) or str(item.get("type") or "") != "link":
                continue
            href = item.get("href") or item.get("url")
            if not isinstance(href, str) or not href:
                continue
            links.append(
                {
                    "title": str(item.get("text") or item.get("title") or href),
                    "url": href,
                }
            )
        if links:
            safe_step["links"] = links
        if safe_step:
            steps.append(safe_step)
    if steps:
        result["steps"] = steps
    return result or None


def _safe_content_links(value: Any) -> dict[str, list[dict[str, Any]]]:
    """Project Content API links to one shallow, typed metadata layer."""

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise NewChildDemoError("Content API links value is not an object")
    result: dict[str, list[dict[str, Any]]] = {}
    for predicate, targets in sorted(value.items()):
        if not isinstance(targets, list):
            raise NewChildDemoError(f"Content API link predicate is not a list: {predicate}")
        safe_targets: list[dict[str, Any]] = []
        for ordinal, target in enumerate(targets):
            if not isinstance(target, dict):
                raise NewChildDemoError(
                    f"Content API link target is not an object: {predicate}/{ordinal}"
                )
            safe_target = {
                key: target[key]
                for key in sorted(LINK_TARGET_SCALAR_FIELDS)
                if key in target and isinstance(target[key], JSON_SCALAR_TYPES)
            }
            safe_targets.append(safe_target)
        result[str(predicate)] = safe_targets
    return result


def _assert_shallow_content_links(payload: dict[str, Any], *, context: str) -> None:
    links = payload.get("links")
    if links is None:
        return
    if not isinstance(links, dict):
        raise NewChildDemoError(f"{context}: Content API links value is not an object")
    for predicate, targets in links.items():
        if not isinstance(targets, list):
            raise NewChildDemoError(f"{context}: link predicate is not a list: {predicate}")
        for ordinal, target in enumerate(targets):
            if not isinstance(target, dict):
                raise NewChildDemoError(
                    f"{context}: link target is not an object: {predicate}/{ordinal}"
                )
            unexpected = set(target) - LINK_TARGET_SCALAR_FIELDS
            if unexpected:
                raise NewChildDemoError(
                    f"{context}: link target retains undeclared fields at {predicate}/{ordinal}: "
                    + ", ".join(sorted(unexpected))
                )
            if any(not isinstance(child, JSON_SCALAR_TYPES) for child in target.values()):
                raise NewChildDemoError(
                    f"{context}: link target is not shallow at {predicate}/{ordinal}"
                )


def safe_content_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "analytics_identifier",
        "base_path",
        "content_id",
        "description",
        "document_type",
        "first_published_at",
        "links",
        "locale",
        "phase",
        "public_updated_at",
        "publishing_app",
        "redirects",
        "rendering_app",
        "schema_name",
        "title",
        "updated_at",
        "withdrawn_notice",
    }
    safe = {key: payload[key] for key in sorted(allowed - {"links"}) if key in payload}
    if "links" in payload:
        safe["links"] = _safe_content_links(payload["links"])
    details = payload.get("details")
    safe_details: dict[str, Any] = {}
    if isinstance(details, dict) and isinstance(details.get("attachments"), list):
        # Reuse the established attachment-metadata allowlist without keeping
        # any narrative body or rendered fragment.
        projected = sanitise_content_api(payload, "1970-01-01T00:00:00+00:00")
        if projected.get("details"):
            safe_details.update(projected["details"])
    if isinstance(details, dict):
        step_navigation = _safe_step_navigation(details)
        if step_navigation:
            safe_details["step_by_step_nav"] = step_navigation
    if safe_details:
        safe["details"] = safe_details
    safe = _without_body_fields(safe)
    if _contains_forbidden_body_field(safe):
        raise NewChildDemoError("content metadata allowlist retained a forbidden body field")
    _assert_shallow_content_links(safe, context="sanitised Content API metadata")
    return safe


def _observation(evidence: dict[str, Any], body: bytes) -> dict[str, Any]:
    return {
        "requested_url": str(evidence.get("requested_url") or ""),
        "final_url": str(evidence.get("final_url") or evidence.get("requested_url") or ""),
        "status": int(evidence.get("status") or 0),
        "ok": bool(evidence.get("ok")),
        "retrieved_at": str(evidence.get("retrieved_at") or datetime.now(timezone.utc).isoformat()),
        "transfer_bytes": len(body),
        "transfer_sha256": str(evidence.get("sha256") or sha256_bytes(body)),
        "attempts": int(evidence.get("acquisition_attempt") or evidence.get("attempts") or 1),
        "media_type": str(evidence.get("media_type") or (evidence.get("headers") or {}).get("content-type") or ""),
    }


def metadata_envelope(
    role: str,
    url: str,
    body: bytes,
    evidence: dict[str, Any],
    *,
    kind: str,
    search_fields: Iterable[str] = (),
) -> dict[str, Any]:
    observation = _observation({**evidence, "requested_url": evidence.get("requested_url") or url}, body)
    metadata: dict[str, Any] | None = None
    if observation["ok"]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NewChildDemoError(f"official JSON response is invalid for {url}: {exc}") from exc
        if not isinstance(payload, dict):
            raise NewChildDemoError(f"official JSON response is not an object for {url}")
        metadata = (
            safe_search_payload(payload, search_fields)
            if kind == "search"
            else safe_content_payload(payload)
        )
    envelope = {
        "schema": ENVELOPE_SCHEMA,
        "role": role,
        "kind": kind,
        "source": "official-public-govuk",
        "observation": observation,
        "metadata": metadata,
        "retention": {
            "complete_page_body_retained": False,
            "rendered_page_retained": False,
            "transfer_retained": False,
            "transfer_hash_retained": True,
        },
    }
    if _contains_forbidden_body_field(envelope):
        raise NewChildDemoError(f"forbidden body field remained in metadata envelope for {url}")
    return envelope


def write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def append_jsonl_event(path: Path, value: dict[str, Any]) -> None:
    """Append and fsync one audit event without rewriting earlier receipts."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.write(canonical_json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _sequence_ranges(sequences: Iterable[int]) -> list[list[int]]:
    values = sorted(set(int(value) for value in sequences))
    if not values:
        return []
    ranges: list[list[int]] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append([start, previous])
        start = previous = value
    ranges.append([start, previous])
    return ranges


def _receipt_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise NewChildDemoError(f"request receipt ledger is missing: {path}")
    events: list[dict[str, Any]] = []
    for ordinal, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise NewChildDemoError(f"request receipt {ordinal} is invalid JSON") from exc
        if not isinstance(value, dict):
            raise NewChildDemoError(f"request receipt {ordinal} is not an object")
        events.append(value)
    return events


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> tuple[int, str]:
    values = list(rows)
    payload = b"".join(canonical_json_bytes(row) for row in values)
    write_text_atomic(path, payload.decode("utf-8"))
    return len(values), sha256_bytes(payload)


def retain_envelope(root: Path, envelope: dict[str, Any]) -> tuple[str, str]:
    payload = canonical_json_bytes(envelope)
    digest = sha256_bytes(payload)
    relative = f"frozen/envelopes/{digest}.json"
    path = root / relative
    if path.is_file() and path.read_bytes() != payload:
        raise NewChildDemoError(f"content-addressed envelope collision: {digest}")
    if not path.exists():
        write_text_atomic(path, payload.decode("utf-8"))
    return relative, digest


def _envelope_evidence(envelope: dict[str, Any]) -> dict[str, Any]:
    observation = envelope["observation"]
    return {
        "requested_url": observation["requested_url"],
        "final_url": observation["final_url"],
        "status": observation["status"],
        "ok": observation["ok"],
        "retrieved_at": observation["retrieved_at"],
        "sha256": observation["transfer_sha256"],
    }


def _search_rows(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = envelope.get("metadata")
    rows = metadata.get("results") if isinstance(metadata, dict) else None
    if not isinstance(rows, list):
        raise NewChildDemoError(f"{envelope.get('role')}: frozen Search envelope has no results")
    return [row for row in rows if isinstance(row, dict)]


def _target_url(value: dict[str, Any]) -> str | None:
    raw = value.get("web_url") or value.get("base_path") or value.get("link") or value.get("url")
    if not raw and isinstance(value.get("api_path"), str):
        raw = str(value["api_path"])
        if raw.startswith("/api/content"):
            raw = raw[len("/api/content") :] or "/"
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return normalise_url(raw)
    except Exception:
        return None


def content_api_url(target_url: str, prefix: str) -> str | None:
    parsed = urlparse(target_url)
    if (parsed.hostname or "").casefold() != "www.gov.uk" or parsed.query:
        return None
    return prefix.rstrip("/") + quote(parsed.path or "/", safe="/-._~")


def _link_rows(payload: dict[str, Any]) -> Iterator[tuple[str, int, dict[str, Any]]]:
    links = payload.get("links")
    if not isinstance(links, dict):
        return
    for predicate, values in sorted(links.items()):
        if not isinstance(values, list):
            continue
        for ordinal, value in enumerate(values):
            if isinstance(value, dict):
                yield str(predicate), ordinal, value


def _is_seed_target(value: dict[str, Any], seed_ids: set[str], seed_urls: set[str]) -> bool:
    content_id = value.get("content_id")
    if isinstance(content_id, str) and content_id in seed_ids:
        return True
    target = _target_url(value)
    return target in seed_urls if target else False


def boundary_reference(
    *,
    source_url: str,
    predicate: str,
    ordinal: int,
    value: dict[str, Any],
    evidence: dict[str, Any],
    boundary_class: str,
    closure_observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = _target_url(value)
    return {
        "source_url": source_url,
        "target_url": target or "",
        "base_path": str(value.get("base_path") or (urlparse(target).path if target else "")),
        "content_id": value.get("content_id"),
        "title": str(value.get("title") or value.get("slug") or target or "Unresolved typed target"),
        "document_type": str(value.get("document_type") or "unknown"),
        "predicate": predicate,
        "relationship": LINK_KINDS.get(predicate, predicate.replace("_", " ")),
        "boundary_class": boundary_class,
        "evidence_url": str(evidence["requested_url"]),
        "evidence_sha256": str(evidence["sha256"]),
        "evidence_locator": f"/links/{predicate}/{ordinal}",
        "retrieved_at": str(evidence["retrieved_at"]),
        "closure_observation": closure_observation,
    }


def _identity_map(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {seed_identity(row): row for row in dedupe_search_seeds(rows)}


def _load_envelope(snapshot: Path, row: dict[str, Any]) -> dict[str, Any]:
    relative = Path(str(row["path"]))
    path = (snapshot / relative).resolve()
    root = snapshot.resolve()
    if relative.is_absolute() or ".." in relative.parts or root not in path.parents:
        raise NewChildDemoError(f"unsafe frozen envelope path: {relative}")
    payload = path.read_bytes()
    if sha256_bytes(payload) != row.get("sha256"):
        raise NewChildDemoError(f"frozen envelope hash failed: {relative}")
    value = json.loads(payload)
    if not isinstance(value, dict) or value.get("schema") != ENVELOPE_SCHEMA:
        raise NewChildDemoError(f"invalid frozen envelope: {relative}")
    if _contains_forbidden_body_field(value):
        raise NewChildDemoError(f"frozen envelope retains a forbidden body field: {relative}")
    if value.get("kind") == "content" and isinstance(value.get("metadata"), dict):
        _assert_shallow_content_links(value["metadata"], context=f"frozen envelope {relative}")
    return value


def _source_observation(source_id: str, envelope: dict[str, Any], locator: str) -> dict[str, Any]:
    evidence = _envelope_evidence(envelope)
    return {
        "source_id": source_id,
        "url": evidence["requested_url"],
        "sha256": evidence["sha256"],
        "locator": locator,
        "retrieved_at": evidence["retrieved_at"],
    }


def build_projection(snapshot: Path, output: Path) -> dict[str, Any]:
    """Rebuild deterministic publication inputs from frozen envelopes only."""

    snapshot = snapshot.resolve()
    index = json.loads((snapshot / "frozen" / "index.json").read_text(encoding="utf-8"))
    contract_path = snapshot / "contract.json"
    contract = load_contract(contract_path)
    if index.get("contract_sha256") != sha256_bytes(contract_path.read_bytes()):
        raise NewChildDemoError("frozen index does not bind the embedded cohort contract")
    expected = int(contract["search"]["expected_seed_denominator"])
    acquisition = index.get("acquisition") if isinstance(index.get("acquisition"), dict) else {}
    attempts = int(acquisition.get("official_request_attempts") or 0)
    if attempts < 1 or attempts > int(contract["content_api"]["official_request_attempt_ceiling"]):
        raise NewChildDemoError("frozen acquisition attempt count is outside the bounded contract")
    if acquisition.get("local_request_attempt_interval") != [1, attempts]:
        raise NewChildDemoError("frozen local request interval does not match the attempt count")
    receipt_relative = Path(str(acquisition.get("request_receipts_path") or ""))
    receipt_path = (snapshot / receipt_relative).resolve()
    if (
        not str(receipt_relative)
        or receipt_relative.is_absolute()
        or ".." in receipt_relative.parts
        or snapshot not in receipt_path.parents
    ):
        raise NewChildDemoError("frozen request receipt path is unsafe")
    receipt_payload = receipt_path.read_bytes()
    if sha256_bytes(receipt_payload) != acquisition.get("request_receipts_sha256"):
        raise NewChildDemoError("frozen request receipt hash failed")
    receipt_events = _receipt_events(receipt_path)
    reservations = [event for event in receipt_events if event.get("event") == "request-reserved"]
    results = [event for event in receipt_events if event.get("event") == "request-result"]
    local_sequences = [event.get("local_sequence") for event in reservations]
    if local_sequences != list(range(1, attempts + 1)):
        raise NewChildDemoError("request receipts do not contain the exact local reservation sequence")
    result_sequences = [event.get("local_sequence") for event in results]
    if result_sequences != list(range(1, attempts + 1)):
        raise NewChildDemoError("request receipts do not contain one result for every reservation")
    programme_sequences = [
        int(event["programme_sequence"])
        for event in reservations
        if isinstance(event.get("programme_sequence"), int)
    ]
    if acquisition.get("programme_request_ledger"):
        if acquisition.get("programme_request_ledger") != PROGRAMME_LEDGER_ID:
            raise NewChildDemoError("frozen programme request ledger exposes an unsupported identifier")
        if len(programme_sequences) != attempts or len(set(programme_sequences)) != attempts:
            raise NewChildDemoError("request receipts do not uniquely attribute every programme sequence")
        if acquisition.get("programme_request_sequences") != programme_sequences:
            raise NewChildDemoError("frozen programme sequence attribution differs from the receipts")
        if acquisition.get("programme_request_sequence_ranges") != _sequence_ranges(programme_sequences):
            raise NewChildDemoError("frozen programme sequence ranges differ from the receipts")
    elif programme_sequences or acquisition.get("programme_request_sequences"):
        raise NewChildDemoError("unconfigured programme ledger has attributed sequence numbers")
    envelope_rows = index.get("envelopes")
    if not isinstance(envelope_rows, list):
        raise NewChildDemoError("frozen index has no envelope list")
    loaded = [(row, _load_envelope(snapshot, row)) for row in envelope_rows if isinstance(row, dict)]
    roles: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = collections.defaultdict(list)
    for row, envelope in loaded:
        roles[str(row.get("role"))].append((row, envelope))

    combined_pairs = roles.get("search-combined-open")
    closing_pairs = roles.get("search-combined-close")
    count_pairs = roles.get("search-combined-count")
    if not combined_pairs or not closing_pairs or not count_pairs:
        raise NewChildDemoError("frozen snapshot lacks opening or closing combined Search query")
    if len(combined_pairs) != 1 or len(closing_pairs) != 1 or len(count_pairs) != 1:
        raise NewChildDemoError("frozen snapshot repeats a combined Search observation role")
    count_metadata = count_pairs[0][1].get("metadata")
    if not isinstance(count_metadata, dict) or int(count_metadata.get("total", -1)) != expected:
        raise NewChildDemoError("frozen combined Search count does not equal the 69-seed denominator")
    opening_envelope = combined_pairs[0][1]
    closing_envelope = closing_pairs[0][1]
    opening = _identity_map(_search_rows(opening_envelope))
    closing = _identity_map(_search_rows(closing_envelope))
    opening_total = int((opening_envelope.get("metadata") or {}).get("total", -1))
    closing_total = int((closing_envelope.get("metadata") or {}).get("total", -1))
    if (
        opening_total != expected
        or closing_total != expected
        or len(opening) != expected
        or set(opening) != set(closing)
    ):
        raise NewChildDemoError("combined Search seed set is not the same frozen 69 identities at open and close")

    memberships: dict[str, set[str]] = {identity: set() for identity in opening}
    group_envelopes: dict[str, dict[str, Any]] = {}
    for browse_path in contract["search"]["browse_paths"]:
        role = "search-group:" + browse_path
        pairs = roles.get(role)
        if not pairs:
            raise NewChildDemoError(f"frozen snapshot lacks group query: {browse_path}")
        if len(pairs) != 1:
            raise NewChildDemoError(f"frozen snapshot repeats a group-query role: {browse_path}")
        envelope = pairs[0][1]
        group_envelopes[browse_path] = envelope
        group_rows = _search_rows(envelope)
        group = _identity_map(group_rows)
        group_total = int((envelope.get("metadata") or {}).get("total", -1))
        if group_total != len(group_rows) or len(group) != len(group_rows):
            raise NewChildDemoError(f"group query does not close exactly without duplicates: {browse_path}")
        closing_group_pairs = roles.get("search-group-close:" + browse_path)
        if not closing_group_pairs or len(closing_group_pairs) != 1:
            raise NewChildDemoError(f"frozen snapshot lacks one closing group query: {browse_path}")
        closing_group_envelope = closing_group_pairs[0][1]
        closing_group_rows = _search_rows(closing_group_envelope)
        closing_group = _identity_map(closing_group_rows)
        closing_group_total = int((closing_group_envelope.get("metadata") or {}).get("total", -1))
        if (
            closing_group_total != group_total
            or len(closing_group_rows) != group_total
            or len(closing_group) != group_total
            or set(closing_group) != set(group)
        ):
            raise NewChildDemoError(f"group membership changed during acquisition: {browse_path}")
        unknown = set(group) - set(opening)
        if unknown:
            raise NewChildDemoError(f"group query has {len(unknown)} identities outside the combined seed set")
        for identity in group:
            memberships[identity].add(browse_path)
    if {identity for identity, values in memberships.items() if values} != set(opening):
        raise NewChildDemoError("group-query union does not reconcile to the exact combined seed set")

    content_by_url: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    content_by_id: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    closure_by_url: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    closure_envelopes: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for row, envelope in loaded:
        if envelope.get("kind") != "content":
            continue
        target_url = row.get("target_url")
        if str(row.get("scope")) == "one-hop" and isinstance(target_url, str) and target_url:
            closure_envelopes[normalise_url(target_url)] = (row, envelope)
        if not isinstance(envelope.get("metadata"), dict):
            continue
        metadata = envelope["metadata"]
        url = normalise_url(str(metadata.get("base_path") or "/"))
        pair = (row, envelope)
        if str(row.get("scope")) == "seed":
            content_by_url[url] = pair
            content_id = metadata.get("content_id")
            if isinstance(content_id, str) and content_id:
                content_by_id[content_id] = pair
        else:
            closure_by_url[url] = pair
    if len(content_by_url) != expected:
        raise NewChildDemoError(f"frozen seed Content API records are not exactly {expected}: {len(content_by_url)}")
    retained_content = sum(1 for row in envelope_rows if row.get("kind") == "content")
    if retained_content > int(contract["content_api"]["retained_record_ceiling"]):
        raise NewChildDemoError("frozen Content API metadata exceeds the retained-record ceiling")

    seed_ids = {value for value in content_by_id}
    seed_urls = set(content_by_url)
    journey_lookup = {
        str(group["membership"]): str(group["id"])
        for group in contract["journey_groups"]
        if group.get("membership") != "all-seeds"
    }
    overview_group = next(
        str(group["id"]) for group in contract["journey_groups"] if group.get("membership") == "all-seeds"
    )
    publisher_predicates = set(contract["content_api"]["publisher_link_predicates"])
    hydrate_predicates = set(contract["content_api"]["hydrate_predicates"])

    records: list[dict[str, Any]] = []
    all_boundaries: list[dict[str, Any]] = []
    for identity, search_row in sorted(opening.items()):
        search_id = search_content_id(search_row)
        search_link = canonical_search_link(search_row)
        pair = content_by_id.get(search_id) if search_id else None
        pair = pair or content_by_url.get(search_link)
        if pair is None:
            raise NewChildDemoError(f"seed has no frozen Content API metadata: {identity}")
        _index_row, envelope = pair
        metadata = envelope["metadata"]
        evidence = _envelope_evidence(envelope)
        record = sanitise_content_api(metadata, evidence)
        if search_id and record.get("content_id") != search_id:
            raise NewChildDemoError(f"Search and Content API content IDs differ for {search_link}")

        identity_memberships = sorted(memberships[identity])
        group_ids = [overview_group, *sorted(journey_lookup[path] for path in identity_memberships)]
        combined_index = _search_rows(opening_envelope).index(search_row)
        observations = [
            _source_observation("content-api", envelope, "/"),
            _source_observation("search-api-v1-combined", opening_envelope, f"/results/{combined_index}"),
        ]
        for browse_path in identity_memberships:
            group_envelope = group_envelopes[browse_path]
            group_rows = _search_rows(group_envelope)
            group_map = {seed_identity(row): index for index, row in enumerate(group_rows)}
            observations.append(
                _source_observation(
                    "search-api-v1-group",
                    group_envelope,
                    f"/results/{group_map[identity]}",
                )
            )

        kept_links: dict[str, list[dict[str, Any]]] = {}
        boundaries: list[dict[str, Any]] = []
        for predicate, ordinal, value in _link_rows(metadata):
            if _is_seed_target(value, seed_ids, seed_urls):
                kept_links.setdefault(predicate, []).append(value)
                continue
            target = _target_url(value)
            closure = closure_by_url.get(target) if target else None
            if target and (urlparse(target).hostname or "").casefold() != "www.gov.uk":
                boundary_class = "external-host"
            elif predicate not in hydrate_predicates:
                boundary_class = "predicate-outside-declared-closure"
            elif closure is None:
                boundary_class = "content-api-unavailable-or-non-content-route"
            else:
                boundary_class = "typed-one-hop-metadata"
            observed_closure = closure_envelopes.get(target) if target else None
            closure_observation = _envelope_evidence(observed_closure[1]) if observed_closure else None
            boundary = boundary_reference(
                source_url=str(record["canonical_url"]),
                predicate=predicate,
                ordinal=ordinal,
                value=value,
                evidence=evidence,
                boundary_class=boundary_class,
                closure_observation=closure_observation,
            )
            boundaries.append(boundary)
            all_boundaries.append(boundary)
            if predicate in publisher_predicates:
                kept_links.setdefault(predicate, []).append(value)
        record["links"] = kept_links
        record["source_memberships"] = sorted(
            set(record.get("source_memberships", []))
            | {"new-child-demo-search-seed"}
            | {"mainstream-browse:" + path for path in identity_memberships}
        )
        record["evidence_observations"] = observations
        record["search_index_id"] = search_row.get("_id")
        record["search_index_ids"] = [search_row.get("_id")] if search_row.get("_id") else []
        record["demo"] = {
            "is_seed": True,
            "cohort_id": str(contract["cohort_id"]),
            "seed_memberships": identity_memberships,
            "journey_groups": group_ids,
            "search_link": search_link,
        }
        details = metadata.get("details")
        if isinstance(details, dict) and isinstance(details.get("step_by_step_nav"), dict):
            record["demo"]["step_navigation"] = details["step_by_step_nav"]
        record["boundary_references"] = sorted(
            boundaries,
            key=lambda row: (row["predicate"], row["target_url"], row["evidence_locator"]),
        )
        record["rights_status"] = str(contract["rights"]["status"])
        record["licence_url"] = str(contract["rights"]["licence_url"])
        _assert_shallow_content_links(record, context=f"publication record {record['canonical_url']}")
        records.append(record)

    records.sort(key=lambda row: (str(row.get("content_id") or ""), str(row["canonical_url"])))
    if len(records) != expected or len({row["canonical_url"] for row in records}) != expected:
        raise NewChildDemoError("publication projection does not contain exactly 69 unique seed records")

    depth_boundaries: list[dict[str, Any]] = []
    for _target, (_row, envelope) in sorted(closure_by_url.items()):
        metadata = envelope["metadata"]
        evidence = _envelope_evidence(envelope)
        source_url = normalise_url(str(metadata.get("base_path") or "/"))
        for predicate, ordinal, value in _link_rows(metadata):
            depth_boundaries.append(
                boundary_reference(
                    source_url=source_url,
                    predicate=predicate,
                    ordinal=ordinal,
                    value=value,
                    evidence=evidence,
                    boundary_class="depth-ceiling",
                )
            )
    all_boundaries.extend(depth_boundaries)
    all_boundaries.sort(
        key=lambda row: (row["source_url"], row["predicate"], row["target_url"], row["evidence_locator"])
    )

    output.mkdir(parents=True, exist_ok=True)
    record_count, record_digest = write_jsonl(output / "source-records.jsonl", records)
    boundary_count, boundary_digest = write_jsonl(output / "boundary-references.jsonl", all_boundaries)
    group_counts = collections.Counter(path for values in memberships.values() for path in values)
    type_counts = collections.Counter(str(row.get("document_type") or "unknown") for row in records)
    boundary_counts = collections.Counter(str(row["boundary_class"]) for row in all_boundaries)
    raw_root = hashlib.sha256(
        "\n".join(sorted(str(row.get("sha256")) for row in envelope_rows)).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schema": COHORT_MANIFEST_SCHEMA,
        "snapshot_id": index["snapshot_id"],
        "cohort_id": contract["cohort_id"],
        "scope": contract["scope_statement"],
        "derived_non_authoritative": True,
        "source_queries": index["source_queries"],
        "counts": {
            "seed_denominator": expected,
            "seed_records": record_count,
            "unexplained_seed_omissions": expected - record_count,
            "retained_content_metadata_records": retained_content,
            "direct_boundary_references": boundary_count - len(depth_boundaries),
            "depth_ceiling_boundary_references": len(depth_boundaries),
            "boundary_references": boundary_count,
        },
        "retrieval": {
            "started_at": acquisition.get("started_at"),
            "ended_at": acquisition.get("ended_at"),
            "request_attempt_interval": acquisition.get("request_attempt_interval"),
            "local_request_attempt_interval": acquisition.get("local_request_attempt_interval"),
            "global_request_attempt_interval": acquisition.get("global_request_attempt_interval"),
            "official_request_attempts": acquisition.get("official_request_attempts"),
            "programme_request_ledger": acquisition.get("programme_request_ledger"),
            "programme_request_count_before": acquisition.get("programme_request_count_before"),
            "programme_request_count_after": acquisition.get("programme_request_count_after"),
            "programme_request_sequences": acquisition.get("programme_request_sequences"),
            "programme_request_sequence_ranges": acquisition.get("programme_request_sequence_ranges"),
            "request_receipts_path": acquisition.get("request_receipts_path"),
            "request_receipts_sha256": acquisition.get("request_receipts_sha256"),
            "official_request_attempt_ceiling": contract["content_api"]["official_request_attempt_ceiling"],
            "zero_model_calls": True,
        },
        "raw_metadata": {
            "envelopes": len(envelope_rows),
            "envelope_set_sha256": raw_root,
            "complete_page_bodies_retained": False,
            "rendered_pages_retained": False,
            "attachments_downloaded": False,
            "nested_link_targets_retained": False,
        },
        "classifications": {
            "document_types": dict(sorted(type_counts.items())),
            "seed_memberships": dict(sorted(group_counts.items())),
            "boundary_classes": dict(sorted(boundary_counts.items())),
        },
        "journey_groups": contract["journey_groups"],
        "closure": {
            "depth": contract["content_api"]["closure_depth"],
            "hydrate_predicates": contract["content_api"]["hydrate_predicates"],
            "retained_record_ceiling": contract["content_api"]["retained_record_ceiling"],
            "recursed_from_one_hop_records": False,
        },
        "rights": contract["rights"],
        "artifacts": {
            "source_records": {"path": "source-records.jsonl", "records": record_count, "sha256": record_digest},
            "boundary_references": {
                "path": "boundary-references.jsonl",
                "records": boundary_count,
                "sha256": boundary_digest,
            },
        },
    }
    write_text_atomic(output / "cohort-manifest.json", pretty_json(manifest))
    checksums = []
    for path in sorted(output.iterdir(), key=lambda item: item.name):
        if path.is_file() and path.name != "checksums.txt":
            checksums.append(f"{sha256_bytes(path.read_bytes())}  {path.name}")
    write_text_atomic(output / "checksums.txt", "\n".join(checksums) + "\n")
    return manifest


def _file_manifest(root: Path, *, exclude: set[str] | None = None) -> list[dict[str, Any]]:
    excluded = exclude or set()
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise NewChildDemoError(f"snapshot tree cannot contain symbolic links: {path}")
        relative = path.relative_to(root).as_posix()
        if path.is_file() and relative not in excluded:
            rows.append(
                {
                    "path": relative,
                    "bytes": path.stat().st_size,
                    "sha256": sha256_bytes(path.read_bytes()),
                }
            )
    return rows


def validate_snapshot(snapshot: Path) -> dict[str, Any]:
    snapshot = snapshot.resolve()
    manifest_path = snapshot / "snapshot-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != SNAPSHOT_SCHEMA:
        raise NewChildDemoError("unsupported new-child snapshot manifest")
    expected_files = manifest.get("files")
    actual_files = _file_manifest(snapshot, exclude={"snapshot-manifest.json"})
    if expected_files != actual_files:
        raise NewChildDemoError("snapshot file manifest differs from frozen bytes")
    checked_root = Path(tempfile.mkdtemp(prefix="new-child-check-"))
    try:
        checked = build_projection(snapshot, checked_root)
        publication = snapshot / "publication"
        rebuilt = Path(tempfile.mkdtemp(prefix="new-child-rebuild-"))
        try:
            build_projection(snapshot, rebuilt)
            original_files = _file_manifest(publication)
            rebuilt_files = _file_manifest(rebuilt)
            original_compare = [{**row, "path": row["path"]} for row in original_files]
            if original_compare != rebuilt_files:
                raise NewChildDemoError("network-free rebuild differs from acquired publication projection")
        finally:
            shutil.rmtree(rebuilt, ignore_errors=True)
    finally:
        shutil.rmtree(checked_root, ignore_errors=True)
    attempts = int(checked["retrieval"]["official_request_attempts"] or 0)
    if attempts > 500:
        raise NewChildDemoError(f"official request-attempt ceiling exceeded: {attempts}/500")
    return {
        "schema": "govuk-okf-new-child-demo-check.v1",
        "snapshot_id": manifest["snapshot_id"],
        "seed_records": checked["counts"]["seed_records"],
        "unexplained_seed_omissions": checked["counts"]["unexplained_seed_omissions"],
        "official_request_attempts": checked["retrieval"]["official_request_attempts"],
        "frozen_rebuild_identical": True,
        "complete_page_bodies_retained": False,
        "status": "pass",
    }


class NewChildDemoAcquirer:
    """Acquire one immutable bounded snapshot with an injectable fetcher."""

    def __init__(
        self,
        contract_path: Path = DEFAULT_CONTRACT,
        *,
        fetcher: Fetch | None = None,
        rate_state_path: Path | None = None,
        request_ledger_path: Path | None = None,
    ) -> None:
        self.contract_path = contract_path.resolve()
        self.contract = load_contract(self.contract_path)
        self._external_fetcher = fetcher
        self.rate_state_path = rate_state_path
        self.request_ledger_path = request_ledger_path.resolve() if request_ledger_path else None

    def acquire(self, snapshot_id: str, output: Path) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,80}", snapshot_id):
            raise NewChildDemoError("snapshot ID is not a safe bounded identifier")
        output = output.resolve()
        if output.exists():
            raise NewChildDemoError(f"immutable snapshot output already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}.", dir=output.parent))
        started_at = datetime.now(timezone.utc).isoformat()
        run_id = snapshot_id + ":" + hashlib.sha256(
            f"{snapshot_id}\0{stage.name}\0{started_at}".encode("utf-8")
        ).hexdigest()[:16]
        request_ledger = stage / "frozen" / "official-request-attempts.count"
        receipt_ledger = stage / "frozen" / "request-attempt-receipts.jsonl"
        local_limiter = HostLimiter(
            float(self.contract["content_api"]["requests_per_second"]),
            state_path=self.rate_state_path or ROOT / ".tmp" / "rate-limits" / "www.gov.uk.timestamp",
            budget_path=request_ledger,
            max_requests=int(self.contract["content_api"]["official_request_attempt_ceiling"]),
        )
        if self.request_ledger_path and self.request_ledger_path == request_ledger.resolve():
            raise NewChildDemoError("programme and stage-local request ledgers must be distinct")
        programme_before = read_request_counter(self.request_ledger_path) if self.request_ledger_path else None
        limiter = _AuditedLimiter(local_limiter, self.request_ledger_path)
        programme_sequences: list[int] = []
        reservation_count = 0

        def reserve(url: str, url_attempt: int, *, wait_for_rate: bool) -> int:
            nonlocal reservation_count
            programme_sequence = (
                limiter.reserve() if wait_for_rate else limiter.reserve_without_rate_wait()
            )
            reservation_count += 1
            local_sequence = reservation_count
            if read_request_counter(request_ledger) != local_sequence:
                raise NewChildDemoError("stage-local request ledger diverged from receipt sequence")
            if programme_sequence is not None:
                programme_sequences.append(programme_sequence)
            append_jsonl_event(
                receipt_ledger,
                {
                    "schema": "govuk-okf-request-attempt-receipt.v1",
                    "event": "request-reserved",
                    "run_id": run_id,
                    "local_sequence": local_sequence,
                    "programme_sequence": programme_sequence,
                    "requested_url": url,
                    "url_attempt": url_attempt,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return local_sequence

        def record_result(
            *,
            local_sequence: int,
            url: str,
            url_attempt: int,
            evidence: dict[str, Any] | None = None,
            error: Exception | None = None,
        ) -> None:
            value = evidence or {}
            append_jsonl_event(
                receipt_ledger,
                {
                    "schema": "govuk-okf-request-attempt-receipt.v1",
                    "event": "request-result",
                    "run_id": run_id,
                    "local_sequence": local_sequence,
                    "programme_sequence": (
                        programme_sequences[local_sequence - 1]
                        if len(programme_sequences) >= local_sequence
                        else None
                    ),
                    "requested_url": url,
                    "url_attempt": url_attempt,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "retrieved_at": value.get("retrieved_at"),
                    "status": int(value.get("status") or 0),
                    "ok": bool(value.get("ok")),
                    "error_type": type(error).__name__ if error else None,
                    "error": str(error) if error else str(value.get("error") or ""),
                    "transfer_sha256": value.get("sha256"),
                },
            )

        def fetch(url: str) -> tuple[bytes, dict[str, Any]]:
            if self._external_fetcher is not None:
                local_sequence = reserve(url, 1, wait_for_rate=False)
                try:
                    body, evidence = self._external_fetcher(url)
                except Exception as exc:
                    record_result(
                        local_sequence=local_sequence,
                        url=url,
                        url_attempt=1,
                        error=exc,
                    )
                    raise
                record_result(
                    local_sequence=local_sequence,
                    url=url,
                    url_attempt=1,
                    evidence=evidence,
                )
                return body, evidence

            retryable = {0, 408, 429, 500, 502, 503, 504}
            final_body = b""
            final_result: dict[str, Any] = {}
            for url_attempt in range(1, 6):
                local_sequence = reserve(url, url_attempt, wait_for_rate=True)
                try:
                    result = fetch_probe(
                        Probe(
                            "new-child-demonstrator",
                            url,
                            "metadata-enumerator",
                            max_bytes=64 * 1024 * 1024,
                            allowed_hosts=("www.gov.uk",),
                        ),
                        attempts=1,
                    )
                except Exception as exc:
                    record_result(
                        local_sequence=local_sequence,
                        url=url,
                        url_attempt=url_attempt,
                        error=exc,
                    )
                    raise
                body = result.pop("body", b"")
                result["acquisition_attempt"] = url_attempt
                record_result(
                    local_sequence=local_sequence,
                    url=url,
                    url_attempt=url_attempt,
                    evidence=result,
                )
                final_body, final_result = body, result
                if result.get("ok") or int(result.get("status") or 0) not in retryable:
                    break
                if url_attempt < 5:
                    server_delay = _bounded_retry_after_seconds(
                        (result.get("headers") or {}).get("retry-after")
                    )
                    jitter_seed = hashlib.sha256(f"{url}\0{url_attempt}".encode("utf-8")).digest()[0] / 2550
                    time.sleep(
                        max(
                            server_delay,
                            min(8.0, 0.5 * (2 ** (url_attempt - 1))) + jitter_seed,
                        )
                    )
            return final_body, final_result

        rows: list[dict[str, Any]] = []

        def observe(
            role: str,
            url: str,
            *,
            kind: str,
            scope: str | None = None,
            target_url: str | None = None,
        ) -> dict[str, Any]:
            body, evidence = fetch(url)
            envelope = metadata_envelope(
                role,
                url,
                body,
                evidence,
                kind=kind,
                search_fields=self.contract["search"]["fields"],
            )
            relative, digest = retain_envelope(stage, envelope)
            rows.append(
                {
                    "role": role,
                    "kind": kind,
                    "scope": scope,
                    "target_url": target_url,
                    "path": relative,
                    "sha256": digest,
                    "requested_url": url,
                }
            )
            return envelope

        try:
            write_text_atomic(stage / "contract.json", pretty_json(self.contract))
            endpoint = str(self.contract["search"]["endpoint"])
            count_url = search_url(endpoint, combined_search_params(self.contract, count=0))
            count_envelope = observe("search-combined-count", count_url, kind="search")
            count_metadata = count_envelope.get("metadata") or {}
            expected = int(self.contract["search"]["expected_seed_denominator"])
            if not count_envelope["observation"]["ok"] or int(count_metadata.get("total", -1)) != expected:
                raise NewChildDemoError(
                    f"combined Search denominator changed: {count_metadata.get('total')} != {expected}"
                )

            combined_url = search_url(
                endpoint,
                combined_search_params(self.contract, count=int(self.contract["search"]["maximum_page_size"])),
            )
            opening_envelope = observe("search-combined-open", combined_url, kind="search")
            opening_rows = _search_rows(opening_envelope)
            opening = _identity_map(opening_rows)
            if int(opening_envelope["metadata"].get("total", -1)) != expected or len(opening) != expected:
                raise NewChildDemoError(
                    f"combined Search query did not close to exactly {expected} unique seeds"
                )

            group_sets: dict[str, set[str]] = {}
            for browse_path in self.contract["search"]["browse_paths"]:
                url = search_url(endpoint, group_search_params(self.contract, browse_path))
                envelope = observe("search-group:" + browse_path, url, kind="search")
                group_sets[browse_path] = set(_identity_map(_search_rows(envelope)))
            group_union = set().union(*group_sets.values()) if group_sets else set()
            if group_union != set(opening):
                raise NewChildDemoError("three group-query sets do not reconcile to the combined OR query")

            seed_content: dict[str, dict[str, Any]] = {}
            for identity, search_row in sorted(opening.items()):
                target = canonical_search_link(search_row)
                api_url = content_api_url(target, str(self.contract["content_api"]["endpoint_prefix"]))
                if not api_url:
                    raise NewChildDemoError(f"seed is not a public www.gov.uk Content API route: {target}")
                envelope = observe(
                    "content-seed:" + identity,
                    api_url,
                    kind="content",
                    scope="seed",
                    target_url=target,
                )
                if not envelope["observation"]["ok"] or not isinstance(envelope.get("metadata"), dict):
                    raise NewChildDemoError(f"seed Content API metadata unavailable: {target}")
                metadata = envelope["metadata"]
                source_id = search_content_id(search_row)
                if source_id and metadata.get("content_id") != source_id:
                    raise NewChildDemoError(f"seed Content API identity differs from Search: {target}")
                seed_content[target] = metadata

            seed_ids = {
                str(payload["content_id"])
                for payload in seed_content.values()
                if isinstance(payload.get("content_id"), str)
            }
            seed_urls = {normalise_url(str(payload.get("base_path") or "/")) for payload in seed_content.values()}
            hydrate_predicates = set(self.contract["content_api"]["hydrate_predicates"])
            closure_targets: set[str] = set()
            for payload in seed_content.values():
                for predicate, _ordinal, value in _link_rows(payload):
                    if predicate not in hydrate_predicates or _is_seed_target(value, seed_ids, seed_urls):
                        continue
                    target = _target_url(value)
                    if target and content_api_url(target, str(self.contract["content_api"]["endpoint_prefix"])):
                        closure_targets.add(target)
            retained = expected + len(closure_targets)
            ceiling = int(self.contract["content_api"]["retained_record_ceiling"])
            if retained > ceiling:
                raise NewChildDemoError(
                    f"declared one-hop closure exceeds retained-record ceiling: {retained}/{ceiling}"
                )

            for target in sorted(closure_targets):
                api_url = content_api_url(target, str(self.contract["content_api"]["endpoint_prefix"]))
                if api_url is None:
                    continue
                observe(
                    "content-closure:" + hashlib.sha256(target.encode("utf-8")).hexdigest()[:24],
                    api_url,
                    kind="content",
                    scope="one-hop",
                    target_url=target,
                )

            closing_envelope = observe("search-combined-close", combined_url, kind="search")
            closing = _identity_map(_search_rows(closing_envelope))
            if int(closing_envelope["metadata"].get("total", -1)) != expected or set(closing) != set(opening):
                raise NewChildDemoError("combined Search result changed during the bounded acquisition window")

            for browse_path in self.contract["search"]["browse_paths"]:
                url = search_url(endpoint, group_search_params(self.contract, browse_path))
                envelope = observe("search-group-close:" + browse_path, url, kind="search")
                closing_group_rows = _search_rows(envelope)
                closing_group = set(_identity_map(closing_group_rows))
                if (
                    int(envelope["metadata"].get("total", -1)) != len(closing_group_rows)
                    or len(closing_group) != len(closing_group_rows)
                    or closing_group != group_sets[browse_path]
                ):
                    raise NewChildDemoError(
                        f"Search group membership changed during acquisition: {browse_path}"
                    )

            attempts = int(request_ledger.read_text(encoding="utf-8").strip() or "0")
            programme_after = read_request_counter(self.request_ledger_path) if self.request_ledger_path else None
            if attempts != reservation_count or len(_receipt_events(receipt_ledger)) != attempts * 2:
                raise NewChildDemoError("request reservation and result receipts do not reconcile")
            ended_at = datetime.now(timezone.utc).isoformat()
            source_queries = {
                "combined_count": count_url,
                "combined_records_open": combined_url,
                "combined_records_close": combined_url,
                "groups": {
                    browse_path: search_url(endpoint, group_search_params(self.contract, browse_path))
                    for browse_path in self.contract["search"]["browse_paths"]
                },
                "groups_close": {
                    browse_path: search_url(endpoint, group_search_params(self.contract, browse_path))
                    for browse_path in self.contract["search"]["browse_paths"]
                },
            }
            sequence_ranges = _sequence_ranges(programme_sequences)
            global_interval = sequence_ranges[0] if len(sequence_ranges) == 1 else None
            receipt_relative = receipt_ledger.relative_to(stage).as_posix()
            receipt_sha256 = sha256_bytes(receipt_ledger.read_bytes())
            frozen_index = {
                "schema": "govuk-okf-new-child-demo-frozen-index.v1",
                "snapshot_id": snapshot_id,
                "contract_sha256": sha256_bytes((stage / "contract.json").read_bytes()),
                "source_queries": source_queries,
                "acquisition": {
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "request_attempt_interval": (
                        global_interval
                        if global_interval is not None
                        else [1, attempts]
                        if attempts
                        else []
                    ),
                    "local_request_attempt_interval": [1, attempts] if attempts else [],
                    "global_request_attempt_interval": global_interval,
                    "official_request_attempts": attempts,
                    "programme_request_ledger": PROGRAMME_LEDGER_ID if self.request_ledger_path else None,
                    "programme_request_count_before": programme_before,
                    "programme_request_count_after": programme_after,
                    "programme_request_sequences": programme_sequences,
                    "programme_request_sequence_ranges": sequence_ranges,
                    "request_receipts_path": receipt_relative,
                    "request_receipts_sha256": receipt_sha256,
                },
                "envelopes": sorted(rows, key=lambda row: (str(row["role"]), str(row["requested_url"]))),
            }
            write_text_atomic(stage / "frozen" / "index.json", pretty_json(frozen_index))
            publication = build_projection(stage, stage / "publication")
            snapshot_manifest = {
                "schema": SNAPSHOT_SCHEMA,
                "snapshot_id": snapshot_id,
                "created_at": ended_at,
                "contract": "contract.json",
                "frozen_index": "frozen/index.json",
                "publication": "publication/cohort-manifest.json",
                "seed_records": publication["counts"]["seed_records"],
                "files": _file_manifest(stage, exclude={"snapshot-manifest.json"}),
            }
            write_text_atomic(stage / "snapshot-manifest.json", pretty_json(snapshot_manifest))
            # Validate the complete staged tree before the atomic immutable
            # promotion. A bad tree never occupies the requested snapshot ID.
            validate_snapshot(stage)
            os.replace(stage, output)
            promoted = True
            try:
                result = validate_snapshot(output)
            except Exception:
                # Preserve an unexpected post-promotion readback failure while
                # freeing the immutable destination for a corrected attempt.
                quarantine = stage.with_name(stage.name + ".invalid")
                os.replace(output, quarantine)
                stage = quarantine
                promoted = False
                raise
            return {**result, "path": str(output)}
        except Exception as exc:
            # Preserve the bounded attempt directory and its request counter for
            # audit rather than silently deleting evidence of failed access.
            local_attempts = (
                int(request_ledger.read_text(encoding="utf-8").strip() or "0")
                if request_ledger.is_file()
                else 0
            )
            programme_current = (
                read_request_counter(self.request_ledger_path) if self.request_ledger_path else None
            )
            failure = {
                "schema": "govuk-okf-new-child-demo-failed-attempt.v1",
                "snapshot_id": snapshot_id,
                "started_at": started_at,
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "run_id": run_id,
                "local_official_request_attempts": local_attempts,
                "local_request_attempt_interval": [1, local_attempts] if local_attempts else [],
                "programme_request_ledger": PROGRAMME_LEDGER_ID if self.request_ledger_path else None,
                "programme_request_count_before": programme_before,
                "programme_request_count_after": programme_current,
                "programme_request_sequences": programme_sequences,
                "programme_request_sequence_ranges": _sequence_ranges(programme_sequences),
                "global_request_attempt_interval": (
                    _sequence_ranges(programme_sequences)[0]
                    if len(_sequence_ranges(programme_sequences)) == 1
                    else None
                ),
                "request_receipts_path": (
                    receipt_ledger.relative_to(stage).as_posix()
                    if receipt_ledger.is_file() and stage in receipt_ledger.parents
                    else "frozen/request-attempt-receipts.jsonl"
                ),
            }
            if stage.exists():
                write_text_atomic(stage / "failure.json", pretty_json(failure))
            failed = stage.with_name(stage.name + ".failed")
            if stage.exists() and not failed.exists():
                os.replace(stage, failed)
            raise


def rebuild_snapshot(snapshot: Path, output: Path) -> dict[str, Any]:
    snapshot = snapshot.resolve()
    output = output.resolve()
    if output == snapshot or snapshot in output.parents:
        raise NewChildDemoError("rebuild output must be outside the immutable source snapshot")
    if output.exists():
        raise NewChildDemoError(f"rebuild output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent.resolve()))
    try:
        manifest = build_projection(snapshot, temporary)
        os.replace(temporary, output)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
