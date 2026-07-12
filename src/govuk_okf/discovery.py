"""Read-only search, fetch and relationship traversal over a built bundle."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .publication import FIELD_MASKS, search_shard, tokenise
from .util import adjacency_bucket, read_gzip_json


class DiscoveryError(RuntimeError):
    """Raised for an invalid identifier or bundle contract."""


KIND_ALIASES = {
    "dataset": "datasets",
    "datasets": "datasets",
    "publisher": "publishers",
    "publishers": "publishers",
    "resource": "resources",
    "resources": "resources",
}
ROUTE_KIND = {"dataset": "datasets", "publisher": "publishers", "resource": "resources"}
CACHE_ENTRY_LIMIT = 64


class DiscoveryIndex:
    def __init__(self, bundle: Path) -> None:
        self.bundle = bundle.resolve()
        if not self.bundle.is_dir():
            raise DiscoveryError(f"bundle directory does not exist: {bundle}")
        self.descriptor = self._load_json("okf-explorer.json")
        self.manifest = self._load_json(self.descriptor["entrypoints"]["data_manifest"])
        self.search_manifest = self._load_json(self.descriptor["entrypoints"]["search_manifest"])
        self.adjacency_manifest = self._load_json(self.descriptor["entrypoints"]["relationship_adjacency"])
        route_entrypoint = self.descriptor["entrypoints"].get("route_index") or self.manifest.get("indexes", {}).get("route_index")
        if not route_entrypoint:
            raise DiscoveryError("bundle has no route index")
        self.route_manifest = self._load_json(route_entrypoint)
        if self.descriptor.get("schema") != "okf-explorer-large-corpus.v1":
            raise DiscoveryError("unsupported Explorer descriptor schema")
        if self.search_manifest.get("schema") != "okf-static-search.v1":
            raise DiscoveryError("unsupported static-search schema")
        if self.adjacency_manifest.get("schema") != "okf-relationship-adjacency.v1":
            raise DiscoveryError("unsupported relationship-adjacency schema")
        if self.route_manifest.get("schema") != "okf-route-index.v1":
            raise DiscoveryError("unsupported route-index schema")
        if self.route_manifest.get("entry_shape") != "identifier-to-typed-matches":
            raise DiscoveryError("unsupported route-index entry shape")
        if self.route_manifest.get("algorithm") != "fnv1a32-prefix-2":
            raise DiscoveryError("unsupported route-index algorithm")
        self.snapshot = self.manifest.get("snapshot")
        self._json_cache: OrderedDict[str, Any] = OrderedDict()
        self._gzip_cache: OrderedDict[str, Any] = OrderedDict()
        self._result_docs: list[dict[str, Any]] | None = None

    def _resolve(self, relative: object) -> Path:
        value = relative.get("path") if isinstance(relative, dict) else relative
        path = Path(str(value or ""))
        if not str(value or ""):
            raise DiscoveryError(f"bundle reference has no path: {relative}")
        if path.is_absolute() or ".." in path.parts:
            raise DiscoveryError(f"unsafe bundle path: {relative}")
        resolved = (self.bundle / path).resolve()
        if not resolved.is_relative_to(self.bundle):
            raise DiscoveryError(f"bundle path escapes root: {relative}")
        return resolved

    def _load_json(self, relative: object) -> Any:
        path = self._resolve(relative)
        try:
            if path.stat().st_size > 64 * 1024 * 1024:
                raise DiscoveryError(f"JSON entrypoint exceeds 64 MiB: {relative}")
            if isinstance(relative, dict):
                expected = str(relative.get("sha256") or "").lower()
                if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
                    raise DiscoveryError(f"bundle entrypoint has no valid SHA-256: {relative}")
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise DiscoveryError(f"bundle entrypoint SHA-256 differs: {relative}")
            return json.loads(path.read_text(encoding="utf-8"))
        except DiscoveryError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DiscoveryError(f"invalid JSON entrypoint {relative}: {exc}") from exc

    @staticmethod
    def _remember(cache: OrderedDict[str, Any], relative: str, value: Any) -> Any:
        cache[relative] = value
        cache.move_to_end(relative)
        while len(cache) > CACHE_ENTRY_LIMIT:
            cache.popitem(last=False)
        return value

    def _cached_json(self, relative: str) -> Any:
        if relative not in self._json_cache:
            return self._remember(self._json_cache, relative, self._load_json(relative))
        self._json_cache.move_to_end(relative)
        return self._json_cache[relative]

    def _cached_gzip(self, relative: str) -> Any:
        if relative not in self._gzip_cache:
            try:
                value = read_gzip_json(self._resolve(relative))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise DiscoveryError(f"unsafe or invalid gzip entrypoint {relative}: {exc}") from exc
            return self._remember(self._gzip_cache, relative, value)
        self._gzip_cache.move_to_end(relative)
        return self._gzip_cache[relative]

    def result_docs(self) -> list[dict[str, Any]]:
        if self._result_docs is None:
            rows: list[dict[str, Any]] = []
            for path in self.search_manifest["entrypoints"]["result_docs"]:
                rows.extend(self._cached_json(path))
            self._result_docs = rows
        return self._result_docs

    def _result_doc(self, ordinal: int) -> dict[str, Any]:
        chunk_size = int(self.search_manifest.get("result_doc_chunk_size", 1000))
        if chunk_size < 1:
            raise DiscoveryError("search manifest has an invalid result chunk size")
        paths = self.search_manifest["entrypoints"]["result_docs"]
        chunk_index = ordinal // chunk_size
        if chunk_index >= len(paths):
            raise DiscoveryError(f"search ordinal {ordinal} points outside result chunks")
        rows = self._cached_json(paths[chunk_index])
        if not isinstance(rows, list) or ordinal % chunk_size >= len(rows):
            raise DiscoveryError(f"search ordinal {ordinal} is absent from its result chunk")
        row = rows[ordinal % chunk_size]
        if not isinstance(row, dict):
            raise DiscoveryError(f"search result at ordinal {ordinal} is not an object")
        if int(row.get("ordinal", -1)) != ordinal:
            raise DiscoveryError(f"search result chunk ordinal mismatch at {ordinal}")
        return row

    @staticmethod
    def _normalise_kind(kind: str | None) -> str | None:
        if kind is None:
            return None
        normalised = KIND_ALIASES.get(str(kind).casefold())
        if normalised is None:
            raise DiscoveryError(f"unsupported entity kind: {kind}")
        return normalised

    @staticmethod
    def _route_kind(identifier: str) -> str | None:
        prefix = identifier.split("/", 1)[0]
        return ROUTE_KIND.get(prefix)

    def resolve(self, identifier: str, *, kind: str | None = None) -> dict[str, Any]:
        if not isinstance(identifier, str) or not identifier:
            raise DiscoveryError("content identifier must be a non-empty string")
        requested_kind = self._normalise_kind(kind)
        inferred_kind = self._route_kind(identifier)
        if requested_kind and inferred_kind and requested_kind != inferred_kind:
            raise DiscoveryError(
                f"identifier route kind {inferred_kind} conflicts with requested kind {requested_kind}"
            )
        effective_kind = requested_kind or inferred_kind
        candidates = [identifier]
        if identifier.startswith("https://www.gov.uk") and identifier != identifier.rstrip("/"):
            candidates.append(identifier.rstrip("/"))
        for candidate in candidates:
            bucket = adjacency_bucket(candidate)
            relative = self.route_manifest["buckets"].get(bucket)
            if relative:
                raw_matches = self._cached_gzip(relative).get(candidate, [])
                if not isinstance(raw_matches, list):
                    raise DiscoveryError(f"route-index entry is not a typed match list: {candidate}")
                unique_matches: dict[tuple[str, int, str], dict[str, Any]] = {}
                for entry in raw_matches:
                    if not isinstance(entry, dict):
                        raise DiscoveryError(f"route-index match is not an object: {candidate}")
                    entry_kind = str(entry.get("kind", ""))
                    ordinal = entry.get("ordinal")
                    route = entry.get("open")
                    if (
                        entry_kind not in KIND_ALIASES.values()
                        or not isinstance(ordinal, int)
                        or isinstance(ordinal, bool)
                        or ordinal < 0
                        or not isinstance(route, str)
                        or not route
                    ):
                        raise DiscoveryError(f"route-index match is malformed: {candidate}")
                    if effective_kind is None or entry_kind == effective_kind:
                        unique_matches[(entry_kind, ordinal, route)] = entry
                if len(unique_matches) == 1:
                    return next(iter(unique_matches.values()))
                if len(unique_matches) > 1:
                    routes = sorted(key[2] for key in unique_matches)
                    qualifier = f" for kind {effective_kind}" if effective_kind else " across entity kinds"
                    raise DiscoveryError(
                        f"ambiguous content identifier{qualifier}: {candidate}; use an exact route from {routes[:10]}"
                    )
        raise DiscoveryError(f"unknown content identifier: {identifier}")

    def search(self, query: str, *, limit: int = 20, filters: dict[str, str] | None = None) -> dict[str, Any]:
        if not isinstance(query, str):
            raise DiscoveryError("search query must be a string")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise DiscoveryError("search limit must be a positive integer")
        if filters is not None and (
            not isinstance(filters, dict)
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in filters.items())
        ):
            raise DiscoveryError("search filters must be a string-to-string mapping")
        tokens = sorted(tokenise(query))
        if not tokens:
            return {
                "query": query,
                "tokens": [],
                "results": [],
                "answerability": "no_supported_query",
                "snapshot": self.snapshot,
            }
        scores: dict[int, int] = {}
        matched_tokens: dict[int, int] = {}
        posting_sets: list[tuple[set[int], bool, int]] = []
        lexicon_paths = self.search_manifest["entrypoints"]["lexicon"]
        for token in tokens:
            shard = search_shard(token)
            lexicon_path = lexicon_paths.get(shard)
            if not lexicon_path:
                continue
            entries = self._cached_json(lexicon_path)
            if not isinstance(entries, list):
                raise DiscoveryError(f"search lexicon shard is not a list: {lexicon_path}")
            entry = next((item for item in entries if isinstance(item, dict) and item.get("token") == token), None)
            if not entry:
                continue
            postings_payload = self._cached_json(str(entry.get("postings", "")))
            if not isinstance(postings_payload, dict) or not isinstance(postings_payload.get("tokens"), dict):
                raise DiscoveryError(f"search postings shard is malformed for token: {token}")
            postings = postings_payload["tokens"].get(token, [])
            if not isinstance(postings, list):
                raise DiscoveryError(f"search postings are not a list for token: {token}")
            token_ordinals: set[int] = set()
            for posting in postings:
                if not isinstance(posting, list) or len(posting) != 3:
                    raise DiscoveryError(f"search posting is malformed for token: {token}")
                ordinal, score, mask = posting
                if (
                    not isinstance(ordinal, int)
                    or isinstance(ordinal, bool)
                    or ordinal < 0
                    or not isinstance(score, int)
                    or isinstance(score, bool)
                    or not isinstance(mask, int)
                    or isinstance(mask, bool)
                ):
                    raise DiscoveryError(f"search posting has invalid values for token: {token}")
                token_ordinals.add(int(ordinal))
                scores[ordinal] = scores.get(ordinal, 0) + int(score) + (4 if int(mask) & FIELD_MASKS["title"] else 0)
                matched_tokens[ordinal] = matched_tokens.get(ordinal, 0) + 1
            cap = int(self.search_manifest.get("counts", {}).get("max_postings_per_token", 2**63 - 1))
            df = int(entry.get("df", 0))
            posting_sets.append((token_ordinals, df <= cap, df))
        posting_sets.sort(key=lambda item: item[2])
        complete_sets = [values for values, complete, _df in posting_sets if complete]
        candidate_sets = complete_sets or ([posting_sets[0][0]] if posting_sets else [])
        eligible: set[int] = set()
        if candidate_sets:
            eligible = set(candidate_sets[0])
            for values in candidate_sets[1:]:
                eligible.intersection_update(values)
            if not eligible and len(candidate_sets) > 1:
                eligible = set().union(*candidate_sets)
        ranked = []
        for ordinal in eligible:
            score = scores.get(ordinal, 0)
            row = self._result_doc(ordinal)
            if filters and any(str(row.get(key, "")) != value for key, value in filters.items()):
                continue
            ranked.append(
                {
                    **row,
                    "search_score": score,
                    "matched_tokens": matched_tokens[ordinal],
                    "why_this_result": f"matched {matched_tokens[ordinal]} of {len(tokens)} query tokens in weighted metadata fields",
                }
            )
        ranked.sort(key=lambda item: (-item["search_score"], item["ordinal"]))
        return {
            "query": query,
            "tokens": tokens,
            "results": ranked[: min(limit, self.search_manifest["result_limit"])],
            "answerability": "metadata_discovery" if ranked else "no_supported_result",
            "snapshot": self.snapshot,
        }

    def fetch(self, identifier: str, *, kind: str | None = None) -> dict[str, Any]:
        entry = self.resolve(identifier, kind=kind)
        ordinal = int(entry["ordinal"])
        kind = str(entry["kind"])
        chunk_size = int(self.route_manifest.get("chunk_size", 1000))
        if chunk_size < 1:
            raise DiscoveryError("route index has an invalid chunk size")
        chunk_index = ordinal // chunk_size
        paths = self.manifest["chunks"].get(kind)
        if not paths:
            raise DiscoveryError(f"route index points to unsupported kind: {kind}")
        if chunk_index >= len(paths):
            raise DiscoveryError(f"{kind} ordinal {ordinal} points outside the manifest")
        rows = self._cached_gzip(paths[chunk_index])
        if not isinstance(rows, list) or ordinal % chunk_size >= len(rows):
            raise DiscoveryError(f"{kind} ordinal {ordinal} is absent from its record chunk")
        row = rows[ordinal % chunk_size]
        if not isinstance(row, dict):
            raise DiscoveryError(f"{kind} ordinal {ordinal} is not a record object")
        if kind == "datasets" and int(row.get("ordinal", -1)) != ordinal:
            raise DiscoveryError(f"dataset chunk ordinal mismatch at {ordinal}")
        if row.get("open") != entry.get("open"):
            raise DiscoveryError(f"route index target mismatch for {identifier}")
        return row

    def traverse(
        self,
        identifier: str,
        *,
        kind: str | None = None,
        predicates: set[str] | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise DiscoveryError("traversal limit must be a positive integer")
        route = identifier
        if not route.startswith(("dataset/", "publisher/", "resource/")):
            route = self.fetch(identifier, kind=kind)["open"]
        bucket = adjacency_bucket(route)
        relative = self.adjacency_manifest["buckets"].get(bucket)
        if not relative:
            return {"route": route, "relationships": [], "truncated": False, "snapshot": self.snapshot}
        rows = self._cached_gzip(relative).get(route, [])
        if predicates:
            rows = [row for row in rows if row["kind"] in predicates]
        return {
            "route": route,
            "relationships": rows[:limit],
            "truncated": len(rows) > limit,
            "snapshot": self.snapshot,
        }

    def citation(self, identifier: str, *, kind: str | None = None) -> dict[str, Any]:
        row = self.fetch(identifier, kind=kind)
        return {
            "title": row["title"],
            "canonical_govuk_url": row["url"],
            "content_id": row.get("canonical_content_id"),
            "snapshot": self.snapshot,
            "bundle_route": row["open"],
            "evidence_url": row.get("evidence_url"),
            "evidence_sha256": row.get("evidence_sha256"),
            "evidence_locator": row.get("evidence_locator"),
            "derived_non_authoritative": True,
        }
