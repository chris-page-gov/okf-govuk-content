"""Deterministic citation inventory, evidence, and release verification.

The module deliberately separates three different judgements:

* deterministic collection and transport/identity/locator checks;
* a manual semantic-support review bound to an exact claim and document hash;
* release policy, including narrowly-scoped non-dependent waivers.

The deterministic code never promotes token overlap to semantic entailment.
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import datetime as dt
import gzip
import hashlib
import html
from html.parser import HTMLParser
import io
import json
import mimetypes
from pathlib import Path
import re
import ssl
import subprocess
import tempfile
import time
from typing import Any, Iterable
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib


SCHEMA_VERSION = "1.0"
MARKDOWN_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\((https?://[^)\s]+)(?:\s+['\"][^)]*)?\)")
URL_VALUE = re.compile(r"^(?P<indent>\s*)url:\s*(?P<url>https?://\S+)\s*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
WORD = re.compile(r"[a-z0-9]+")
SPACE = re.compile(r"\s+")
DEFAULT_USER_AGENT = (
    "govuk-okf-citation-verifier/0.1 "
    "(+https://github.com/chris-page-gov/okf-govuk-content)"
)
AUDIT_PATHS = (
    "README.md",
    "CHANGELOG.md",
    "WHATS_ON_GOVUK_OKF.md",
    "planning",
    "research",
    "governance",
    "semantic",
    "docs",
    "reports",
)
GENERATED_MARKDOWN = {
    "research/bibliography.md",
    "reports/citation-verification.md",
}
EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "vendor",
}
STRUCTURED_SOURCE_FILES = {"research/source-registry.yaml"}
STRUCTURED_COMPARATOR_FILES = {
    "evaluation/govuk-chat/new-parent-multi-service.json",
    "evaluation/govuk-chat/official-published-example.json",
}
ALLOWED_VERDICTS = {"entailed", "partly_supported", "contradicted", "unrelated"}
PASS_VERDICTS = {"entailed", "partly_supported"}
STOP_WORDS = {
    "a",
    "an",
    "and",
    "api",
    "especially",
    "for",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "v1",
    "v2",
    "with",
}


class CitationError(RuntimeError):
    """Raised when a citation gate fails closed."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalise_space(value: str) -> str:
    return SPACE.sub(" ", html.unescape(value)).strip()


def strip_markdown(value: str) -> str:
    value = MARKDOWN_LINK.sub(lambda match: match.group(1), value)
    value = re.sub(r"[`*_>#]", "", value)
    return normalise_space(value)


def normalise_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise CitationError(f"invalid citation URL: {value!r}")
    if parsed.username or parsed.password:
        raise CitationError(f"userinfo is forbidden in citation URL: {value!r}")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    port = parsed.port
    netloc = host
    if port and not ((parsed.scheme.lower() == "https" and port == 443) or (parsed.scheme.lower() == "http" and port == 80)):
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def stable_id(prefix: str, *parts: str, length: int = 16) -> str:
    raw = "\x1f".join(parts)
    return f"{prefix}-{digest_text(raw)[:length].upper()}"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CitationError(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
        if not isinstance(value, dict):
            raise CitationError(f"{path}:{line_number}: JSONL record must be an object")
        records.append(value)
    return records


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(canonical_json(value) + "\n" for value in values)
    path.write_text(payload, encoding="utf-8")


def _iter_markdown(root: Path) -> Iterable[Path]:
    for item in AUDIT_PATHS:
        path = root / item
        candidates = [path] if path.is_file() else sorted(path.rglob("*.md")) if path.exists() else []
        for candidate in candidates:
            relative = candidate.relative_to(root).as_posix()
            relative_parts = candidate.relative_to(root).parts
            if relative not in GENERATED_MARKDOWN and not any(
                part in EXCLUDED_DIRECTORY_NAMES for part in relative_parts
            ):
                yield candidate


def _paragraph(lines: list[str], index: int) -> tuple[int, int, str]:
    list_marker = re.compile(r"^\s*(?:[-*]|\d+\.)\s")
    if lines[index].lstrip().startswith("|"):
        return index, index, lines[index]
    candidate_start = index
    while candidate_start > 0 and lines[candidate_start].strip() and not list_marker.match(lines[candidate_start]):
        if not lines[candidate_start - 1].strip() or lines[candidate_start - 1].lstrip().startswith(("#", "|", "```")):
            break
        candidate_start -= 1
    if list_marker.match(lines[candidate_start]):
        end = index
        while end + 1 < len(lines) and lines[end + 1].strip() and not list_marker.match(lines[end + 1]):
            end += 1
        return candidate_start, end, " ".join(lines[candidate_start : end + 1])
    start = index
    end = index
    while start > 0 and lines[start - 1].strip() and not lines[start - 1].lstrip().startswith(("#", "|", "```")):
        start -= 1
    while end + 1 < len(lines) and lines[end + 1].strip() and not lines[end + 1].lstrip().startswith(("#", "|", "```")):
        end += 1
    return start, end, " ".join(lines[start : end + 1])


def _claim_kind(relative: str, start_line: int, text: str) -> tuple[str, bool]:
    if relative == "planning/AFHF_GOVUK_OKF_SOTA_RESEARCH_IMPLEMENTATION_PLAN.md" and start_line >= 1078:
        return "bibliography_seed_summary", False
    if relative == "reports/comparators.md" and "Primary evidence entry points" in text:
        return "reference_entry", False
    if relative == "research/source-registry.yaml":
        return "source_contract", True
    return "narrative_claim", True


def _host_rule(policy: dict[str, Any], url: str) -> dict[str, Any]:
    host = urllib.parse.urlsplit(url).hostname or ""
    best: dict[str, Any] | None = None
    for rule in policy.get("authority_rules", []):
        suffix = str(rule.get("host_suffix", "")).lower()
        if host == suffix or host.endswith("." + suffix):
            if best is None or len(suffix) > len(str(best.get("host_suffix", ""))):
                best = rule
    if best is None:
        raise CitationError(f"no authority rule for released citation host {host!r}: {url}")
    if best.get("authority_class") not in policy.get("allowed_authority_classes", []):
        raise CitationError(f"released citation source class is not allowed: {url}")
    return best


def _apply_replacement(policy: dict[str, Any], url: str) -> str:
    replacement = policy.get("url_replacements", {}).get(url)
    return normalise_url(replacement or url)


def collect_citations(root: Path, policy: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    claims: dict[str, dict[str, Any]] = {}
    citations: dict[str, dict[str, Any]] = {}
    sources: dict[str, dict[str, Any]] = {}

    def register(
        *,
        relative: str,
        start_line: int,
        end_line: int,
        raw_claim: str,
        label: str,
        raw_url: str,
        structured: bool = False,
        json_pointer: str | None = None,
        source_metadata: dict[str, Any] | None = None,
    ) -> None:
        url = _apply_replacement(policy, raw_url)
        if any(url.startswith(prefix) for prefix in policy.get("non_citation_prefixes", [])):
            return
        if urllib.parse.urlsplit(url).scheme.casefold() != "https":
            raise CitationError(f"released citation must use HTTPS: {url}")
        claim_text = strip_markdown(raw_claim)
        kind, material = _claim_kind(relative, start_line, claim_text)
        claim_id = stable_id("CLM", relative, claim_text)
        source_location: dict[str, Any] = {
            "path": relative,
            "line_start": start_line,
            "line_end": end_line,
        }
        if json_pointer is not None:
            source_location["json_pointer"] = json_pointer
        claim = {
            "schema_version": SCHEMA_VERSION,
            "claim_id": claim_id,
            "claim_sha256": digest_text(claim_text),
            "text": claim_text,
            "source_location": source_location,
            "claim_kind": kind,
            "release_material": material,
        }
        previous = claims.get(claim_id)
        if previous and previous["claim_sha256"] != claim["claim_sha256"]:
            raise CitationError(f"claim ID collision: {claim_id}")
        claims[claim_id] = claim

        source_id = stable_id("SRC", url)
        rule = _host_rule(policy, url)
        source = sources.setdefault(
            source_id,
            {
                "schema_version": SCHEMA_VERSION,
                "source_id": source_id,
                "requested_url": url,
                "authority_class": rule["authority_class"],
                "publisher": rule["publisher"],
                "expected_hosts": rule.get("expected_hosts", [urllib.parse.urlsplit(url).hostname]),
                "labels": [],
                "version_or_commit": version_from_url(url),
            },
        )
        for key, value in (source_metadata or {}).items():
            previous_value = source.get(key)
            if previous_value is not None and previous_value != value:
                raise CitationError(f"conflicting structured citation source metadata: {url}: {key}")
            source[key] = value
        clean_label = strip_markdown(label)
        if clean_label and clean_label not in source["labels"]:
            source["labels"].append(clean_label)
        source["labels"].sort(key=lambda value: (-len(value), value.casefold()))

        citation_id = stable_id("CIT", claim_id, source_id)
        citations[citation_id] = {
            "schema_version": SCHEMA_VERSION,
            "citation_id": citation_id,
            "claim_id": claim_id,
            "source_id": source_id,
            "requested_url": url,
            "link_label": clean_label,
            "source_location": claim["source_location"],
            "structured_source": structured,
        }

    for path in _iter_markdown(root):
        relative = path.relative_to(root).as_posix()
        payload = path.read_text(encoding="utf-8")
        lines = payload.splitlines()
        line_offsets = [0]
        line_offsets.extend(match.end() for match in re.finditer(r"\n", payload))
        for match in MARKDOWN_LINK.finditer(payload):
            index = bisect.bisect_right(line_offsets, match.start()) - 1
            start, end, raw_claim = _paragraph(lines, index)
            register(
                relative=relative,
                start_line=start + 1,
                end_line=end + 1,
                raw_claim=raw_claim,
                label=match.group(1),
                raw_url=match.group(2),
            )

    for relative in sorted(STRUCTURED_SOURCE_FILES):
        path = root / relative
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        blocks: list[tuple[int, int, list[str]]] = []
        starts = [index for index, line in enumerate(lines) if re.match(r"\s{2}-\s+id:\s*", line)]
        for position, start in enumerate(starts):
            end = starts[position + 1] - 1 if position + 1 < len(starts) else len(lines) - 1
            blocks.append((start, end, lines[start : end + 1]))
        for start, end, block in blocks:
            joined = "\n".join(block)
            id_match = re.search(r"^\s*-\s+id:\s*(\S+)\s*$", joined, re.MULTILINE)
            url_match = re.search(r"^\s+url:\s*(https?://\S+)\s*$", joined, re.MULTILINE)
            role_match = re.search(r"^\s+role:\s*(.+?)\s*$", joined, re.MULTILINE)
            if not (id_match and url_match):
                continue
            current_id = id_match.group(1)
            current_role = role_match.group(1) if role_match else "Released structured source"
            raw_url = url_match.group(1)
            if "{" in raw_url:
                raw_url = raw_url.split("{")[0].rstrip("/")
            register(
                relative=relative,
                start_line=start + 1,
                end_line=end + 1,
                raw_claim=f"Source {current_id}: {current_role}",
                label=current_id,
                raw_url=raw_url,
                structured=True,
            )

    for relative in sorted(STRUCTURED_COMPARATOR_FILES):
        path = root / relative
        if not path.exists():
            continue
        try:
            document = _load_json(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CitationError(f"invalid structured comparator source {relative}: {exc}") from exc
        if not isinstance(document, dict):
            raise CitationError(f"structured comparator source must be an object: {relative}")
        lines = path.read_text(encoding="utf-8").splitlines()

        def url_line(url: str) -> int:
            encoded = json.dumps(url, ensure_ascii=False)
            for ordinal, line in enumerate(lines, 1):
                if encoded in line:
                    return ordinal
            raise CitationError(f"structured comparator URL has no exact source line: {relative}: {url}")

        def value_line(value: object) -> int:
            encoded = json.dumps(value, ensure_ascii=False)
            for ordinal, line in enumerate(lines, 1):
                if encoded in line:
                    return ordinal
            raise CitationError(
                f"structured comparator value has no exact source line: {relative}: {encoded}"
            )

        if relative.endswith("new-parent-multi-service.json"):
            if document.get("schema") != "govuk-chat-comparison-walkthrough.v1":
                raise CitationError(f"unsupported GOV.UK Chat walkthrough schema: {relative}")
            contexts = document.get("official_context")
            if not isinstance(contexts, list) or not contexts:
                raise CitationError(f"GOV.UK Chat walkthrough has no official context: {relative}")
            for index, context in enumerate(contexts):
                if not isinstance(context, dict):
                    raise CitationError(f"GOV.UK Chat official context row is not an object: {relative}")
                claim = context.get("claim")
                url = context.get("url")
                if not isinstance(claim, str) or not claim or not isinstance(url, str) or not url:
                    raise CitationError(f"GOV.UK Chat official context row is incomplete: {relative}")
                line = url_line(url)
                register(
                    relative=relative,
                    start_line=line,
                    end_line=line,
                    raw_claim=claim,
                    label=claim,
                    raw_url=url,
                    structured=True,
                    json_pointer=f"/official_context/{index}/url",
                )
            continue

        if document.get("schema") != "govuk-chat-published-observation.v1":
            raise CitationError(f"unsupported GOV.UK Chat published-observation schema: {relative}")
        page_url = document.get("source_page_url")
        image_url = document.get("source_image_url")
        source_cards = document.get("source_cards")
        question = document.get("question")
        answer = document.get("answer")
        if not isinstance(page_url, str) or not page_url or not isinstance(image_url, str) or not image_url:
            raise CitationError(f"GOV.UK Chat published observation lacks source URLs: {relative}")
        if not isinstance(source_cards, list) or not source_cards:
            raise CitationError(f"GOV.UK Chat published observation has no source cards: {relative}")
        if not isinstance(question, str) or not question or not isinstance(answer, dict):
            raise CitationError(f"GOV.UK Chat published observation lacks question/answer evidence: {relative}")
        excerpt = answer.get("short_verbatim_excerpt")
        summaries = answer.get("structured_summary")
        if (
            not isinstance(excerpt, str)
            or not excerpt
            or not isinstance(summaries, list)
            or not summaries
            or any(not isinstance(summary, str) or not summary for summary in summaries)
        ):
            raise CitationError(f"GOV.UK Chat published observation answer evidence is incomplete: {relative}")
        positions = [
            card.get("position") if isinstance(card, dict) else None for card in source_cards
        ]
        if positions != list(range(1, len(source_cards) + 1)):
            raise CitationError(
                f"GOV.UK Chat source-card positions must be unique, contiguous and match array order: {relative}"
            )
        structured_references: list[tuple[str, str, str, str]] = [
            (
                "The published GOV.UK Chat observation is sourced from this official GDS launch page.",
                "GOV.UK Chat launch source page",
                page_url,
                "/source_page_url",
            ),
            (
                "This official GDS image asset is the binary source referenced by the published comparator observation.",
                "GOV.UK Chat official published example image",
                image_url,
                "/source_image_url",
            ),
        ]
        asset_sha256 = (document.get("capture") or {}).get("asset_sha256")
        if not isinstance(asset_sha256, str) or not SHA256.fullmatch(asset_sha256):
            raise CitationError(f"GOV.UK Chat published observation has no valid asset SHA-256: {relative}")
        image_source_metadata = {
            "expected_document_sha256": asset_sha256,
            "locator": {"kind": "binary_sha256", "value": asset_sha256},
            "licence": "not_independently_verified_url_and_sha256_only",
        }

        observed_fields: list[tuple[str, str, str]] = [
            (
                f"The published GOV.UK Chat comparator question is: {question}",
                question,
                "/question",
            ),
            (
                f"The published GOV.UK Chat comparator contains this bounded verbatim excerpt: {excerpt}",
                excerpt,
                "/answer/short_verbatim_excerpt",
            ),
        ]
        observed_fields.extend(
            (
                f"The published GOV.UK Chat comparator answer is represented by this structured paraphrase: {summary}",
                summary,
                f"/answer/structured_summary/{index}",
            )
            for index, summary in enumerate(summaries)
        )
        for claim, field_value, pointer in observed_fields:
            line = value_line(field_value)
            register(
                relative=relative,
                start_line=line,
                end_line=line,
                raw_claim=claim,
                label="GOV.UK Chat official published example image",
                raw_url=image_url,
                structured=True,
                json_pointer=pointer,
                source_metadata=image_source_metadata,
            )

        for index, card in enumerate(source_cards):
            if not isinstance(card, dict):
                raise CitationError(f"GOV.UK Chat source card is not an object: {relative}")
            title = card.get("title")
            url = card.get("url")
            position = card.get("position")
            if (
                not isinstance(title, str)
                or not title
                or not isinstance(url, str)
                or not url
                or not isinstance(position, int)
                or isinstance(position, bool)
                or position < 1
            ):
                raise CitationError(f"GOV.UK Chat source card is incomplete: {relative}")
            card_claim = (
                f"In the official GOV.UK Chat screenshot, source card position {position} "
                f"is titled {title} and links to {url}."
            )
            line = url_line(url)
            pointer = f"/source_cards/{index}"
            register(
                relative=relative,
                start_line=line,
                end_line=line,
                raw_claim=card_claim,
                label="GOV.UK Chat official published example image",
                raw_url=image_url,
                structured=True,
                json_pointer=pointer,
                source_metadata=image_source_metadata,
            )
            register(
                relative=relative,
                start_line=line,
                end_line=line,
                raw_claim=card_claim,
                label=title,
                raw_url=url,
                structured=True,
                json_pointer=pointer,
            )
        for claim, label, url, pointer in structured_references:
            line = url_line(url)
            register(
                relative=relative,
                start_line=line,
                end_line=line,
                raw_claim=claim,
                label=label,
                raw_url=url,
                structured=True,
                json_pointer=pointer,
                source_metadata=image_source_metadata if pointer == "/source_image_url" else None,
            )

    for source in sources.values():
        override = policy.get("source_overrides", {}).get(source["requested_url"], {})
        source.update(override)
        source["expected_identity_terms"] = identity_terms(source)
        source["locator_hint"] = locator_hint(source)

    return {
        "claims": sorted(claims.values(), key=lambda value: value["claim_id"]),
        "citations": sorted(citations.values(), key=lambda value: value["citation_id"]),
        "sources": sorted(sources.values(), key=lambda value: value["source_id"]),
    }


def identity_terms(source: dict[str, Any]) -> list[str]:
    override = source.get("identity_terms")
    if override:
        return sorted({normalise_space(str(value)).casefold() for value in override if value})
    candidates: list[str] = []
    for label in source.get("labels", []):
        words = [word for word in WORD.findall(label.casefold()) if word not in STOP_WORDS and len(word) > 2]
        if words:
            candidates.append(" ".join(words[:8]))
    return candidates[:4]


def locator_hint(source: dict[str, Any]) -> dict[str, Any]:
    if "locator" in source:
        return source["locator"]
    url = source["requested_url"]
    if "github.com/" in url:
        parsed = parse_github_url(url)
        if parsed:
            return {
                "kind": "commit_path" if parsed[3] == "tree" else "commit_lines",
                "value": f"{parsed[1]}@{parsed[2]}:{parsed[4]}",
            }
    raw = parse_raw_github_url(url)
    if raw:
        return {
            "kind": "commit_lines",
            "value": f"{raw[1]}@{raw[2]}:{raw[3]}",
        }
    if urllib.parse.urlsplit(url).path.lower().endswith(".pdf"):
        return {"kind": "pdf_page_text", "value": source.get("labels", ["document"])[0]}
    if urllib.parse.urlsplit(url).path.lower().endswith(".json"):
        return {"kind": "json_pointer", "value": "/"}
    if url.endswith(".xml") or url.endswith(".atom"):
        return {"kind": "xml_root", "value": "/"}
    return {"kind": "heading_text_fingerprint", "value": source.get("labels", ["document"])[0]}


def version_from_url(url: str) -> str | None:
    parsed = parse_github_url(url)
    if parsed and COMMIT.fullmatch(parsed[2]):
        return parsed[2]
    raw = parse_raw_github_url(url)
    if raw and COMMIT.fullmatch(raw[2]):
        return raw[2]
    patterns = (
        r"/TR/(\d{4}/(?:REC|WD|CR|PR)-[^/]+/)",
        r"/specification/(\d{4}-\d{2}-\d{2})/",
        r"/releases/([0-9]+(?:\.[0-9]+)+)/",
        r"/(v[0-9]+(?:\.[0-9]+)+)(?:\.html|/)",
    )
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1).rstrip("/")
    return None


def parse_github_url(url: str) -> tuple[str, str, str, str, str] | None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 5 or parts[2] not in {"blob", "tree"}:
        return None
    owner, repo, kind, revision = parts[:4]
    return owner, repo, revision, kind, "/".join(parts[4:])


def parse_raw_github_url(url: str) -> tuple[str, str, str, str] | None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname != "raw.githubusercontent.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4:
        return None
    return parts[0], parts[1], parts[2], "/".join(parts[3:])


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.blocks: list[tuple[str, str]] = []
        self._tag: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"title", "h1", "h2", "h3", "h4", "p", "li", "dt", "dd"}:
            self._flush()
            self._tag = tag
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if self._tag == tag:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._tag:
            self._parts.append(data)

    def _flush(self) -> None:
        if self._tag:
            value = normalise_space(" ".join(self._parts))
            if value:
                if self._tag == "title":
                    self.title_parts.append(value)
                self.blocks.append((self._tag, value))
        self._tag = None
        self._parts = []

    def close(self) -> None:
        self._flush()
        super().close()


class _RecordingRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, require_https: bool = True) -> None:
        super().__init__()
        self.require_https = require_https
        self.chain: list[dict[str, Any]] = []

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> urllib.request.Request | None:
        self.chain.append({"status": code, "from": req.full_url, "to": newurl})
        if len(self.chain) > 8:
            raise CitationError(f"redirect chain exceeds 8 hops for {req.full_url}")
        if self.require_https and urllib.parse.urlsplit(newurl).scheme.casefold() != "https":
            raise CitationError(f"HTTPS citation redirect attempted a transport downgrade to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _decode_body(body: bytes, media_type: str, charset: str | None) -> tuple[str, str, list[tuple[str, str]]]:
    if media_type == "application/pdf" or body.startswith(b"%PDF-"):
        with tempfile.TemporaryDirectory(prefix="govuk-okf-citation-") as directory:
            pdf_path = Path(directory) / "source.pdf"
            text_path = Path(directory) / "source.txt"
            pdf_path.write_bytes(body)
            proc = subprocess.run(
                ["pdftotext", "-layout", str(pdf_path), str(text_path)],
                capture_output=True,
                check=False,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0:
                return "", "", []
            text = text_path.read_text(encoding="utf-8", errors="replace")
            pages = [normalise_space(page) for page in text.split("\f") if normalise_space(page)]
            title = pages[0][:300] if pages else ""
            return title, normalise_space(text), [(f"page:{index + 1}", page) for index, page in enumerate(pages)]
    encoding = charset or "utf-8"
    text = body.decode(encoding, errors="replace")
    if media_type in {"text/html", "application/xhtml+xml"} or "<html" in text[:1000].casefold():
        parser = _TextExtractor()
        parser.feed(text)
        parser.close()
        title = parser.title_parts[0] if parser.title_parts else ""
        return title, normalise_space(" ".join(value for _, value in parser.blocks)), parser.blocks
    return "", text, [("document", normalise_space(text))]


def _decode_content_encoding(body: bytes, encoding: str | None, max_bytes: int) -> bytes:
    value = (encoding or "identity").split(",", 1)[0].strip().casefold()
    if value in {"", "identity"}:
        decoded = body
    elif value == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                decoded = stream.read(max_bytes + 1)
        except (OSError, EOFError) as exc:
            raise CitationError(f"invalid gzip response: {exc}") from exc
    elif value == "deflate":
        try:
            decompressor = zlib.decompressobj()
            decoded = decompressor.decompress(body, max_bytes + 1)
            if len(decoded) <= max_bytes:
                decoded += decompressor.flush(max_bytes + 1 - len(decoded))
            if decompressor.unconsumed_tail or not decompressor.eof:
                raise CitationError("deflate response is truncated or exceeds the verification ceiling")
        except zlib.error as exc:
            raise CitationError(f"invalid deflate response: {exc}") from exc
    else:
        raise CitationError(f"unsupported content encoding {encoding!r}")
    if len(decoded) > max_bytes:
        raise CitationError(f"decoded source exceeds {max_bytes} byte verification ceiling")
    return decoded


def _terms(value: str) -> set[str]:
    return {word for word in WORD.findall(value.casefold()) if word not in STOP_WORDS and len(word) > 2}


def _resolve_json_pointer(text: str, pointer: str) -> tuple[Any, bool]:
    try:
        current: Any = json.loads(text)
    except json.JSONDecodeError:
        return None, False
    # The policy uses "/" as a readable root-document locator. Empty-string is
    # also accepted as the RFC 6901 root pointer.
    if pointer in {"", "/"}:
        return current, True
    if not pointer.startswith("/"):
        return None, False
    for encoded in pointer[1:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return None, False
    return current, True


def _best_locator(source: dict[str, Any], blocks: list[tuple[str, str]], text: str) -> tuple[dict[str, Any], str, bool]:
    hint = source.get("locator_hint", {})
    kind = hint.get("kind", "heading_text_fingerprint")
    value = normalise_space(str(hint.get("value", "")))
    search_value = normalise_space(str(source.get("locator_search_hint", value)))
    if kind == "commit_path":
        excerpt = blocks[0][1] if blocks else text[:400]
        return hint, " ".join(excerpt.split()[:24]), bool(excerpt)
    if kind == "commit_lines":
        wanted = _terms(search_value + " " + " ".join(source.get("expected_identity_terms", [])))
        lines = text.splitlines()
        ranked: list[tuple[float, int, str]] = []
        for index, line in enumerate(lines):
            clean = normalise_space(line)
            if not clean:
                continue
            found = _terms(clean)
            score = len(wanted & found) / max(1, len(wanted))
            ranked.append((score, -index, clean))
        if not ranked:
            return hint, "", False
        score, negative_index, excerpt = max(ranked)
        line_number = -negative_index + 1
        locator = {
            **hint,
            "line_start": line_number,
            "line_end": line_number,
            "line_sha256": digest_text(excerpt),
        }
        return locator, " ".join(excerpt.split()[:24]), score >= 0.1
    if kind == "json_pointer":
        resolved, found = _resolve_json_pointer(text, value)
        if not found:
            return hint, "", False
        serialised = canonical_json(resolved)
        locator = {**hint, "resolved_value_sha256": digest_text(serialised)}
        excerpt = " ".join(serialised.split()[:48])[:800]
        return locator, excerpt, True
    if kind == "xml_root":
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return hint, "", False
        root_name = root.tag.rsplit("}", 1)[-1]
        serialised = ET.tostring(root, encoding="utf-8")
        locator = {
            **hint,
            "resolved_root": root_name,
            "root_sha256": digest_bytes(serialised),
        }
        excerpt = normalise_space(" ".join(root.itertext()))[:800]
        return locator, excerpt, bool(root_name and excerpt)
    if kind == "text_lines":
        wanted = _terms(search_value)
        ranked: list[tuple[float, int, str]] = []
        for index, line in enumerate(text.splitlines()):
            clean = normalise_space(line)
            if not clean:
                continue
            found = _terms(clean)
            score = len(wanted & found) / max(1, len(wanted))
            ranked.append((score, -index, clean))
        if not ranked:
            return hint, "", False
        score, negative_index, excerpt = max(ranked)
        line_number = -negative_index + 1
        locator = {
            **hint,
            "line_start": line_number,
            "line_end": line_number,
            "line_sha256": digest_text(excerpt),
        }
        return locator, excerpt[:800], score >= 0.34
    if kind == "heading_set_fingerprint":
        values = [normalise_space(str(item)) for item in hint.get("values", []) if normalise_space(str(item))]
        members: list[dict[str, Any]] = []
        excerpts: list[str] = []
        all_found = bool(values)
        for wanted_value in values:
            wanted = _terms(wanted_value)
            ranked_members: list[tuple[float, int, str, str]] = []
            for index, (block_kind, block) in enumerate(blocks):
                found = _terms(block)
                score = len(wanted & found) / max(1, len(wanted))
                if wanted_value.casefold() in block.casefold():
                    score += 1.0
                ranked_members.append((score, -index, block_kind, block))
            if not ranked_members:
                all_found = False
                continue
            score, negative_index, block_kind, block = max(ranked_members)
            block_index = -negative_index
            heading_path: list[str] = []
            for prior_kind, prior_block in blocks[: block_index + 1]:
                if prior_kind in {"h1", "h2", "h3", "h4"}:
                    level = int(prior_kind[1])
                    heading_path = heading_path[: level - 1]
                    heading_path.append(prior_block)
            found_member = score >= 0.34
            all_found = all_found and found_member
            members.append(
                {
                    "value": wanted_value,
                    "resolved_block_kind": block_kind,
                    "block_ordinal": block_index + 1,
                    "heading_path": heading_path,
                    "text_fingerprint": digest_text(normalise_space(block)),
                    "found": found_member,
                }
            )
            excerpts.append(" ".join(block.split()[:24]))
        locator = {"kind": kind, "values": values, "members": members}
        return locator, " | ".join(excerpts)[:1600], all_found
    wanted = _terms(search_value)
    ranked: list[tuple[float, int, str, str]] = []
    for index, (block_kind, block) in enumerate(blocks):
        found = _terms(block)
        score = len(wanted & found) / max(1, len(wanted))
        if value and value.casefold() in block.casefold():
            score += 1.0
        ranked.append((score, -index, block_kind, block))
    if not ranked:
        return hint, "", False
    score, negative_index, block_kind, block = max(ranked)
    block_index = -negative_index
    heading_path: list[str] = []
    for prior_kind, prior_block in blocks[: block_index + 1]:
        if prior_kind in {"h1", "h2", "h3", "h4"}:
            level = int(prior_kind[1])
            heading_path = heading_path[: level - 1]
            heading_path.append(prior_block)
    words = block.split()
    excerpt = " ".join(words[:24])
    locator = {
        "kind": kind,
        "value": value,
        "resolved_block_kind": block_kind,
        "block_ordinal": block_index + 1,
        "heading_path": heading_path,
        "text_fingerprint": digest_text(normalise_space(block)),
    }
    minimum = 0.34 if wanted else 0.0
    return locator, excerpt, score >= minimum


def _identity_matches(source: dict[str, Any], title: str, text: str) -> tuple[bool, list[str]]:
    haystack = normalise_space(f"{title} {text[:50000]}").casefold()
    matched: list[str] = []
    for phrase in source.get("expected_identity_terms", []):
        phrase_terms = _terms(phrase)
        if phrase.casefold() in haystack or (phrase_terms and len(phrase_terms & _terms(haystack)) / len(phrase_terms) >= 0.6):
            matched.append(phrase)
    key_markers = [str(value) for value in source.get("identity_markers", [])]
    matched_markers = [value for value in key_markers if value.casefold() in haystack]
    if key_markers:
        return len(matched_markers) == len(key_markers), matched + matched_markers
    return bool(matched) or not source.get("expected_identity_terms"), matched


def fetch_evidence(
    source: dict[str, Any],
    *,
    citation_contexts: list[dict[str, Any]] | None = None,
    timeout: float,
    max_bytes: int,
    user_agent: str,
) -> dict[str, Any]:
    requested_url = source["requested_url"]
    fetch_url = source.get("verification_url", requested_url)
    if urllib.parse.urlsplit(fetch_url).scheme.casefold() != "https":
        raise CitationError(f"released citation verification URL must use HTTPS: {fetch_url}")
    redirect = _RecordingRedirect(require_https=True)
    strict_context = ssl.create_default_context()
    opener = urllib.request.build_opener(redirect, urllib.request.HTTPSHandler(context=strict_context))
    request = urllib.request.Request(
        fetch_url,
        headers={"User-Agent": user_agent, "Accept": "text/html,application/json,application/pdf,text/plain,application/xml;q=0.9,*/*;q=0.5"},
    )
    started = time.monotonic()
    try:
        response = opener.open(request, timeout=timeout)
        with contextlib.closing(response):
            status = int(getattr(response, "status", response.getcode()))
            final_url = normalise_url(response.geturl())
            headers = response.headers
            media_type = headers.get_content_type().lower()
            charset = headers.get_content_charset()
            transfer_body = response.read(max_bytes + 1)
            if len(transfer_body) > max_bytes:
                raise CitationError(f"source exceeds {max_bytes} byte verification ceiling: {requested_url}")
            body = _decode_content_encoding(transfer_body, headers.get("Content-Encoding"), max_bytes)
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_id": source["source_id"],
            "requested_url": requested_url,
            "verification_url": fetch_url,
            "retrieved_at": utc_now(),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "checks": {
                "reachable": "fail",
                "secure_transport": "fail",
                "redirect_source_identity": "not_run",
                "identity_matches": "not_run",
                "locator_found": "not_run",
                "excerpt_matches": "not_run",
            },
        }

    document_sha256 = digest_bytes(body)
    binary_sha256 = source.get("expected_document_sha256")
    if binary_sha256 is not None:
        if not isinstance(binary_sha256, str) or not SHA256.fullmatch(binary_sha256):
            raise CitationError(f"invalid expected binary SHA-256 for {requested_url}")
        title, text, blocks = "", "", []
        locator = {"kind": "binary_sha256", "value": binary_sha256, "observed_sha256": document_sha256}
        excerpt = f"sha256:{document_sha256}"
        locator_found = document_sha256 == binary_sha256
    else:
        title, text, blocks = _decode_body(body, media_type, charset)
        locator, excerpt, locator_found = _best_locator(source, blocks, text)
    citation_evidence: list[dict[str, Any]] = []
    for context in citation_contexts or []:
        contextual_source = dict(source)
        citation_override = source.get("citation_locator_overrides", {}).get(
            context.get("link_label") or ""
        )
        if citation_override:
            contextual_source["locator_hint"] = citation_override
        else:
            contextual_source["locator_search_hint"] = (
                context.get("link_label") or context.get("claim_text") or ""
            )
        if binary_sha256 is not None:
            contextual_locator = locator
            contextual_excerpt = excerpt
            contextual_found = locator_found
        else:
            contextual_locator, contextual_excerpt, contextual_found = _best_locator(contextual_source, blocks, text)
        locator_digest = digest_text(canonical_json(contextual_locator))
        citation_evidence.append(
            {
                "citation_id": context["citation_id"],
                "claim_sha256": context["claim_sha256"],
                "locator": contextual_locator,
                "locator_sha256": locator_digest,
                "evidence_excerpt": contextual_excerpt,
                "excerpt_sha256": digest_text(contextual_excerpt),
                "checks": {
                    "locator_found": "pass" if contextual_found else "fail",
                    "excerpt_matches": "pass" if contextual_excerpt else "fail",
                },
            }
        )
    if binary_sha256 is not None:
        identity_ok = document_sha256 == binary_sha256
        identity_matches = [f"sha256:{binary_sha256}"] if identity_ok else []
    else:
        identity_ok, identity_matches = _identity_matches(source, title, text)
    final_host = urllib.parse.urlsplit(final_url).hostname
    secure_transport = urllib.parse.urlsplit(final_url).scheme.casefold() == "https" and all(
        urllib.parse.urlsplit(hop.get("to", "")).scheme.casefold() == "https" for hop in redirect.chain
    )
    expected_hosts = set(source.get("expected_hosts", []))
    host_ok = not expected_hosts or final_host in expected_hosts or any(final_host and final_host.endswith("." + host) for host in expected_hosts)
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_id": stable_id("EVD", source["source_id"], document_sha256),
        "source_id": source["source_id"],
        "requested_url": requested_url,
        "verification_url": fetch_url,
        "final_url": final_url,
        "redirect_chain": redirect.chain,
        "retrieved_at": utc_now(),
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "http_status": status,
        "media_type": media_type,
        "charset": charset,
        "content_length": len(body),
        "transfer_length": len(transfer_body),
        "transfer_sha256": digest_bytes(transfer_body),
        "content_encoding": headers.get("Content-Encoding", "identity"),
        "document_sha256": document_sha256,
        "title": title[:500],
        "publisher": source["publisher"],
        "published_or_updated_at": headers.get("Last-Modified"),
        "version_or_commit": source.get("version_or_commit"),
        "http_metadata": {
            "etag": headers.get("ETag"),
            "last_modified": headers.get("Last-Modified"),
            "cache_control": headers.get("Cache-Control"),
            "content_language": headers.get("Content-Language"),
        },
        "locator": locator,
        "evidence_excerpt": excerpt,
        "excerpt_sha256": digest_text(excerpt),
        "licence": source.get("licence", "not_determined_short_excerpt_and_hash_only"),
        "citation_evidence": sorted(citation_evidence, key=lambda value: value["citation_id"]),
        "tls": {"verified": secure_transport, "policy": "python_default_strict_context"},
        "identity_terms_matched": identity_matches,
        "checks": {
            "reachable": "pass" if 200 <= status < 300 else "fail",
            "secure_transport": "pass" if secure_transport else "fail",
            "redirect_source_identity": "pass" if host_ok else "fail",
            "identity_matches": "pass" if identity_ok else "fail",
            "locator_found": "pass" if locator_found else "fail",
            "excerpt_matches": "pass" if excerpt and digest_text(excerpt) else "fail",
        },
    }


def fetch_all(
    inventory: dict[str, list[dict[str, Any]]],
    *,
    timeout: float = 30.0,
    max_bytes: int = 32 * 1024 * 1024,
    delay: float = 0.55,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    claims_by_id = {value["claim_id"]: value for value in inventory["claims"]}
    contexts_by_source: dict[str, list[dict[str, Any]]] = {}
    for citation in inventory["citations"]:
        claim = claims_by_id[citation["claim_id"]]
        contexts_by_source.setdefault(citation["source_id"], []).append(
            {
                "citation_id": citation["citation_id"],
                "claim_sha256": claim["claim_sha256"],
                "claim_text": claim["text"],
                "link_label": citation.get("link_label"),
            }
        )
    last_host: str | None = None
    last_time = 0.0
    for source in inventory["sources"]:
        host = urllib.parse.urlsplit(source["requested_url"]).hostname
        if host == last_host:
            remaining = delay - (time.monotonic() - last_time)
            if remaining > 0:
                time.sleep(remaining)
        result: dict[str, Any] = {}
        for attempt in range(1, 4):
            result = fetch_evidence(
                source,
                citation_contexts=contexts_by_source.get(source["source_id"], []),
                timeout=timeout,
                max_bytes=max_bytes,
                user_agent=user_agent,
            )
            result["attempts"] = attempt
            if result.get("checks", {}).get("reachable") == "pass":
                break
            if attempt < 3:
                time.sleep(2 ** (attempt - 1))
        results.append(result)
        last_host = host
        last_time = time.monotonic()
    return results


def _records_by(records: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        value = str(record.get(key, ""))
        if not value:
            raise CitationError(f"record missing {key}")
        if value in result:
            raise CitationError(f"duplicate {key}: {value}")
        result[value] = record
    return result


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _require_unique_nonempty(records: list[dict[str, Any]], field: str) -> None:
    seen: set[str] = set()
    for record in records:
        value = record.get(field)
        if value in (None, ""):
            continue
        serialised = str(value)
        if serialised in seen:
            raise CitationError(f"duplicate {field}: {serialised}")
        seen.add(serialised)


def _valid_waiver(waiver: dict[str, Any], claim: dict[str, Any], snapshot_id: str) -> tuple[bool, str]:
    required = {"waiver_id", "citation_id", "reason", "owner", "approved_at", "review_at", "evidence", "non_dependent"}
    missing = sorted(required - set(waiver))
    if missing:
        return False, f"waiver missing fields: {', '.join(missing)}"
    if waiver.get("non_dependent") is not True or waiver.get("dependent_conclusions") not in (None, []):
        return False, "waiver is not explicitly non-dependent"
    if claim.get("release_material"):
        return False, "material released claim cannot use a non-dependent waiver"
    if not all(isinstance(waiver.get(field), str) and waiver[field].strip() for field in ("reason", "owner", "evidence")):
        return False, "waiver reason, owner and evidence must be non-empty"
    if not _valid_timestamp(waiver.get("approved_at")) or not _valid_timestamp(waiver.get("review_at")):
        return False, "waiver approval and review dates must be timezone-qualified ISO timestamps"
    if waiver.get("snapshot_ids") not in (None, []) and snapshot_id not in waiver["snapshot_ids"]:
        return False, "waiver does not cover this snapshot"
    return True, "pass"


def verify_release(
    *,
    inventory: dict[str, list[dict[str, Any]]],
    evidence: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    claim_reviews: list[dict[str, Any]],
    waivers: list[dict[str, Any]],
    snapshot_id: str,
) -> dict[str, Any]:
    if not snapshot_id or snapshot_id in {"unknown", "latest"}:
        raise CitationError("citation release evidence requires an explicit immutable snapshot ID")
    claims = _records_by(inventory["claims"], "claim_id")
    sources = _records_by(inventory["sources"], "source_id")
    citations = _records_by(inventory["citations"], "citation_id")
    evidence_by_source = _records_by(evidence, "source_id")
    reviews_by_citation = _records_by(reviews, "citation_id")
    claim_reviews_by_claim = _records_by(claim_reviews, "claim_id")
    waivers_by_citation = _records_by(waivers, "citation_id")
    _require_unique_nonempty(evidence, "evidence_id")
    _require_unique_nonempty(reviews, "review_id")
    _require_unique_nonempty(claim_reviews, "claim_review_id")
    _require_unique_nonempty(waivers, "waiver_id")
    citations_by_claim: dict[str, list[str]] = {}
    citations_by_source: dict[str, list[str]] = {}
    for citation in citations.values():
        citations_by_claim.setdefault(citation["claim_id"], []).append(citation["citation_id"])
        citations_by_source.setdefault(citation["source_id"], []).append(citation["citation_id"])
    failures: list[dict[str, Any]] = []
    passed = 0
    waived = 0
    citation_failures = 0
    source_problems: dict[str, list[str]] = {}

    for source_id, source in sources.items():
        observation = evidence_by_source.get(source_id)
        if not observation:
            continue
        problems: list[str] = []
        document_hash = str(observation.get("document_sha256", ""))
        if observation.get("schema_version") != SCHEMA_VERSION:
            problems.append("unexpected evidence schema_version")
        if not _valid_timestamp(observation.get("retrieved_at")):
            problems.append("missing or invalid timezone-qualified retrieval timestamp")
        if SHA256.fullmatch(document_hash):
            expected_evidence_id = stable_id("EVD", source_id, document_hash)
            if observation.get("evidence_id") != expected_evidence_id:
                problems.append("evidence_id is not bound to source and document hash")
        if observation.get("verification_url") != source.get("verification_url", source["requested_url"]):
            problems.append("evidence verification_url does not match catalogue")
        try:
            final_url = normalise_url(str(observation.get("final_url", "")))
        except CitationError:
            final_url = ""
            problems.append("missing or invalid final_url")
        if final_url and urllib.parse.urlsplit(final_url).scheme.casefold() != "https":
            problems.append("final_url is not HTTPS")
        if any(
            urllib.parse.urlsplit(str(hop.get("to", ""))).scheme.casefold() != "https"
            for hop in observation.get("redirect_chain", [])
        ):
            problems.append("redirect chain contains a non-HTTPS target")
        if observation.get("tls", {}).get("verified") is not True:
            problems.append("strict TLS verification is not recorded")
        observed_citation_ids = [
            str(value.get("citation_id", "")) for value in observation.get("citation_evidence", [])
        ]
        if sorted(observed_citation_ids) != sorted(citations_by_source.get(source_id, [])):
            problems.append("claim-specific evidence set does not exactly match source citations")
        if problems:
            source_problems[source_id] = problems

    for citation_id, citation in citations.items():
        claim = claims[citation["claim_id"]]
        source = sources[citation["source_id"]]
        problems: list[str] = []
        observation = evidence_by_source.get(source["source_id"])
        citation_observation: dict[str, Any] | None = None
        if not observation:
            problems.append("missing evidence observation")
        else:
            problems.extend(f"source_evidence: {value}" for value in source_problems.get(source["source_id"], []))
            for check in (
                "reachable",
                "secure_transport",
                "redirect_source_identity",
                "identity_matches",
                "locator_found",
                "excerpt_matches",
            ):
                if observation.get("checks", {}).get(check) != "pass":
                    problems.append(f"{check}={observation.get('checks', {}).get(check, 'missing')}")
            if not SHA256.fullmatch(str(observation.get("document_sha256", ""))):
                problems.append("missing or invalid document_sha256")
            if observation.get("requested_url") != source["requested_url"]:
                problems.append("evidence requested_url does not match catalogue")
            matches = [value for value in observation.get("citation_evidence", []) if value.get("citation_id") == citation_id]
            if len(matches) != 1:
                problems.append("missing or duplicate claim-specific locator evidence")
            else:
                citation_observation = matches[0]
                if citation_observation.get("claim_sha256") != claim["claim_sha256"]:
                    problems.append("locator evidence is not bound to current claim hash")
                for check in ("locator_found", "excerpt_matches"):
                    if citation_observation.get("checks", {}).get(check) != "pass":
                        problems.append(f"claim_{check}={citation_observation.get('checks', {}).get(check, 'missing')}")
                for field in ("locator_sha256", "excerpt_sha256"):
                    if not SHA256.fullmatch(str(citation_observation.get(field, ""))):
                        problems.append(f"missing or invalid claim {field}")
                if citation_observation.get("locator_sha256") != digest_text(
                    canonical_json(citation_observation.get("locator"))
                ):
                    problems.append("claim locator hash does not match locator")
                if citation_observation.get("excerpt_sha256") != digest_text(
                    str(citation_observation.get("evidence_excerpt", ""))
                ):
                    problems.append("claim excerpt hash does not match evidence excerpt")
        review = reviews_by_citation.get(citation_id)
        if not review:
            problems.append("missing independent semantic-support review")
        else:
            required_review_fields = {
                "review_id",
                "reviewer_id",
                "reviewer_kind",
                "reviewed_at",
                "rationale",
                "independence_limitations",
            }
            missing_review_fields = sorted(required_review_fields - set(review))
            if missing_review_fields:
                problems.append(f"semantic review missing fields: {', '.join(missing_review_fields)}")
            if any(not isinstance(review.get(field), str) or not review[field].strip() for field in required_review_fields):
                problems.append("semantic review identity, timestamp, rationale and limitations must be non-empty")
            if not _valid_timestamp(review.get("reviewed_at")):
                problems.append("semantic review timestamp is not timezone-qualified ISO")
            if review.get("verdict") not in ALLOWED_VERDICTS:
                problems.append("invalid semantic-support verdict")
            elif review.get("verdict") not in PASS_VERDICTS:
                problems.append(f"semantic_support={review.get('verdict')}")
            elif review.get("verdict") == "partly_supported":
                if len(citations_by_claim.get(claim["claim_id"], [])) < 2:
                    problems.append("partly_supported is invalid for a single-source claim")
                spans = review.get("supported_claim_spans")
                if not isinstance(spans, list) or not spans:
                    problems.append("partly_supported review requires supported_claim_spans")
                elif any(not isinstance(span, str) or span not in claim["text"] for span in spans):
                    problems.append("supported_claim_spans must be exact non-empty substrings of the claim")
            if review.get("claim_sha256") != claim["claim_sha256"]:
                problems.append("semantic review is not bound to current claim hash")
            expected_document = observation.get("document_sha256") if observation else None
            if review.get("document_sha256") != expected_document:
                problems.append("semantic review is not bound to current document hash")
            if citation_observation:
                if review.get("locator_sha256") != citation_observation.get("locator_sha256"):
                    problems.append("semantic review is not bound to current locator hash")
                if review.get("excerpt_sha256") != citation_observation.get("excerpt_sha256"):
                    problems.append("semantic review is not bound to current excerpt hash")
            if review.get("reviewer_independent_from_claim_author") is not True:
                problems.append("semantic reviewer independence not declared")
            if review.get("reviewer_kind") not in {"human", "domain_expert", "independent_agent_configuration"}:
                problems.append("semantic reviewer kind is not recognised")
            if review.get("method") != "manual_locator_review":
                problems.append("semantic review must be manual_locator_review")
            if review.get("numbers_dates_named_entities_checked") is not True:
                problems.append("numbers, dates and named entities were not explicitly checked")
            if review.get("contrary_evidence_checked") is not True:
                problems.append("contrary evidence was not explicitly checked")

        if problems:
            waiver = waivers_by_citation.get(citation_id)
            if waiver:
                valid, reason = _valid_waiver(waiver, claim, snapshot_id)
                if valid:
                    waived += 1
                    continue
                problems.append(reason)
            failures.append({"citation_id": citation_id, "claim_id": claim["claim_id"], "source_id": source["source_id"], "problems": problems})
            citation_failures += 1
        else:
            passed += 1

    joint_required = 0
    joint_passed = 0
    joint_failed = 0
    for claim_id, citation_ids in citations_by_claim.items():
        if len(citation_ids) < 2:
            continue
        joint_required += 1
        claim = claims[claim_id]
        review = claim_reviews_by_claim.get(claim_id)
        problems: list[str] = []
        if not review:
            problems.append("missing joint semantic-support review for multi-source claim")
        else:
            required = {
                "claim_review_id",
                "reviewer_id",
                "reviewer_kind",
                "reviewed_at",
                "rationale",
                "independence_limitations",
                "method",
                "citation_ids",
                "citation_review_ids",
            }
            missing = sorted(required - set(review))
            if missing:
                problems.append(f"joint review missing fields: {', '.join(missing)}")
            if review.get("claim_sha256") != claim["claim_sha256"]:
                problems.append("joint review is not bound to current claim hash")
            if sorted(review.get("citation_ids", [])) != sorted(citation_ids):
                problems.append("joint review citation set does not match the claim citation set")
            expected_review_ids = sorted(
                str(reviews_by_citation.get(citation_id, {}).get("review_id", ""))
                for citation_id in citation_ids
            )
            if sorted(review.get("citation_review_ids", [])) != expected_review_ids:
                problems.append("joint review is not bound to the current per-citation review set")
            if review.get("verdict") != "entailed_jointly":
                problems.append("joint semantic-support verdict must be entailed_jointly")
            if review.get("method") != "manual_joint_support_review":
                problems.append("joint semantic-support review must be manual_joint_support_review")
            if review.get("reviewer_independent_from_claim_author") is not True:
                problems.append("joint semantic reviewer independence not declared")
            if review.get("reviewer_kind") not in {"human", "domain_expert", "independent_agent_configuration"}:
                problems.append("joint semantic reviewer kind is not recognised")
            if any(
                not isinstance(review.get(field), str) or not review[field].strip()
                for field in (
                    "claim_review_id",
                    "reviewer_id",
                    "reviewer_kind",
                    "reviewed_at",
                    "rationale",
                    "independence_limitations",
                    "method",
                )
            ):
                problems.append("joint review identity, timestamp, rationale and limitations must be non-empty")
            if not _valid_timestamp(review.get("reviewed_at")):
                problems.append("joint review timestamp is not timezone-qualified ISO")
            if review.get("coverage_complete") is not True or review.get("uncovered_claim_spans") not in ([], None):
                problems.append("joint review does not declare complete claim coverage")
            if review.get("numbers_dates_named_entities_checked") is not True:
                problems.append("joint review did not check numbers, dates and named entities")
            if review.get("contrary_evidence_checked") is not True:
                problems.append("joint review did not check contrary evidence")
        if problems:
            failures.append({"claim_id": claim_id, "problems": problems})
            joint_failed += 1
        else:
            joint_passed += 1

    orphan_evidence = sorted(set(evidence_by_source) - set(sources))
    orphan_reviews = sorted(set(reviews_by_citation) - set(citations))
    orphan_claim_reviews = sorted(set(claim_reviews_by_claim) - set(claims))
    orphan_waivers = sorted(set(waivers_by_citation) - set(citations))
    for source_id in orphan_evidence:
        failures.append({"source_id": source_id, "problems": ["orphan evidence observation"]})
    for citation_id in orphan_reviews:
        failures.append({"citation_id": citation_id, "problems": ["orphan semantic review"]})
    for claim_id in orphan_claim_reviews:
        failures.append({"claim_id": claim_id, "problems": ["orphan joint semantic review"]})
    unexpected_claim_reviews = sorted(
        claim_id
        for claim_id in set(claim_reviews_by_claim) & set(claims)
        if len(citations_by_claim.get(claim_id, [])) < 2
    )
    for claim_id in unexpected_claim_reviews:
        failures.append({"claim_id": claim_id, "problems": ["joint semantic review supplied for a single-source claim"]})
    for citation_id in orphan_waivers:
        failures.append({"citation_id": citation_id, "problems": ["orphan waiver"]})

    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "citation_verification_passed": not failures,
        "summary": {
            "released_claims": len(claims),
            "released_citations": len(citations),
            "unique_sources": len(sources),
            "citations_passed": passed,
            "citations_waived_non_dependent": waived,
            "citations_failed": citation_failures,
            "joint_claim_reviews_required": joint_required,
            "joint_claim_reviews_passed": joint_passed,
            "joint_claim_reviews_failed": joint_failed,
            "blocking_failures": len(failures),
            "material_claims": sum(bool(claim["release_material"]) for claim in claims.values()),
        },
        "failures": failures,
        "deterministic_boundary": (
            "Transport, redirect, identity marker, locator, excerpt, hash, coverage, and binding checks are deterministic. "
            "Semantic support is accepted only from a separately recorded manual review bound to the exact claim and fetched document hashes."
        ),
    }


def build_outputs(
    root: Path,
    policy: dict[str, Any],
    inventory: dict[str, list[dict[str, Any]]],
    evidence: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    claim_reviews: list[dict[str, Any]],
    waivers: list[dict[str, Any]],
    snapshot_id: str,
) -> dict[str, Any]:
    report = verify_release(
        inventory=inventory,
        evidence=evidence,
        reviews=reviews,
        claim_reviews=claim_reviews,
        waivers=waivers,
        snapshot_id=snapshot_id,
    )
    evidence_by_source = {record["source_id"]: record for record in evidence}
    claims_by_id = {record["claim_id"]: record for record in inventory["claims"]}
    reviews_by_id = {record["citation_id"]: record for record in reviews}
    claim_reviews_by_id = {record["claim_id"]: record for record in claim_reviews}
    waivers_by_id = {record["citation_id"]: record for record in waivers}
    citation_count_by_claim: dict[str, int] = {}
    for value in inventory["citations"]:
        citation_count_by_claim[value["claim_id"]] = citation_count_by_claim.get(value["claim_id"], 0) + 1

    write_jsonl(root / "research/claims.jsonl", inventory["claims"])
    write_jsonl(root / "research/evidence.jsonl", evidence)
    write_jsonl(root / "research/citations.jsonl", inventory["citations"])
    write_json(
        root / "research/bibliography.json",
        {
            "schema_version": SCHEMA_VERSION,
            "sources": inventory["sources"],
        },
    )

    bibliography_lines = [
        "# Release bibliography",
        "",
        f"Snapshot: `{snapshot_id}`",
        "",
        "This file is generated from the citation catalogue. Verification status is",
        "reported separately because a reachable source is not automatically evidence",
        "that a claim is semantically supported.",
        "",
    ]
    for source in sorted(inventory["sources"], key=lambda value: (value["publisher"], value["requested_url"])):
        observation = evidence_by_source.get(source["source_id"], {})
        title = observation.get("title") or (source.get("labels") or [source["requested_url"]])[0]
        locator = observation.get("locator", source.get("locator_hint", {}))
        bibliography_lines.extend(
            [
                f"- **{source['publisher']}**. [{title}]({source['requested_url']}).",
                f"  `{source['source_id']}`; {source['authority_class']}; retrieved",
                f"  `{observation.get('retrieved_at', 'not_fetched')}`; version/commit",
                f"  `{source.get('version_or_commit') or 'content-hash snapshot'}`; locator",
                f"  `{locator.get('kind', 'missing')}:{locator.get('value', 'missing')}`; document SHA-256",
                f"  `{observation.get('document_sha256', 'missing')}`.",
            ]
        )
    (root / "research/bibliography.md").write_text("\n".join(bibliography_lines) + "\n", encoding="utf-8")

    ledger: list[dict[str, Any]] = []
    for citation in inventory["citations"]:
        claim = claims_by_id[citation["claim_id"]]
        observation = evidence_by_source.get(citation["source_id"], {})
        citation_observation = next(
            (
                value
                for value in observation.get("citation_evidence", [])
                if value.get("citation_id") == citation["citation_id"]
            ),
            {},
        )
        review = reviews_by_id.get(citation["citation_id"])
        claim_review = claim_reviews_by_id.get(citation["claim_id"])
        waiver = waivers_by_id.get(citation["citation_id"])
        ledger.append(
            {
                "schema_version": SCHEMA_VERSION,
                "snapshot_id": snapshot_id,
                **citation,
                "claim_sha256": claim["claim_sha256"],
                "evidence_id": observation.get("evidence_id"),
                "document_sha256": observation.get("document_sha256"),
                "link_status": observation.get("checks", {}).get("reachable", "missing"),
                "source_identity_status": observation.get("checks", {}).get("identity_matches", "missing"),
                "redirect_status": observation.get("checks", {}).get("redirect_source_identity", "missing"),
                "locator": citation_observation.get("locator"),
                "locator_sha256": citation_observation.get("locator_sha256"),
                "excerpt_sha256": citation_observation.get("excerpt_sha256"),
                "locator_status": citation_observation.get("checks", {}).get("locator_found", "missing"),
                "support_status": review.get("verdict") if review else "missing",
                "support_review_id": review.get("review_id") if review else None,
                "joint_support_required": citation_count_by_claim.get(citation["claim_id"], 0) > 1,
                "joint_support_status": claim_review.get("verdict") if claim_review else None,
                "joint_support_review_id": claim_review.get("claim_review_id") if claim_review else None,
                "waiver_id": waiver.get("waiver_id") if waiver else None,
            }
        )
    write_jsonl(root / "provenance/claim-citation-ledger.jsonl", ledger)

    inputs = {
        "policy_sha256": digest_text(canonical_json(policy)),
        "claims_sha256": digest_text("".join(canonical_json(value) + "\n" for value in inventory["claims"])),
        "citations_sha256": digest_text("".join(canonical_json(value) + "\n" for value in inventory["citations"])),
        "evidence_sha256": digest_text("".join(canonical_json(value) + "\n" for value in evidence)),
        "reviews_sha256": digest_text("".join(canonical_json(value) + "\n" for value in reviews)),
        "claim_reviews_sha256": digest_text("".join(canonical_json(value) + "\n" for value in claim_reviews)),
        "waivers_sha256": digest_text(canonical_json(waivers)),
    }
    timestamps = [
        str(value)
        for value in (
            [record.get("retrieved_at") for record in evidence]
            + [record.get("reviewed_at") for record in reviews]
            + [record.get("reviewed_at") for record in claim_reviews]
            + [record.get("approved_at") for record in waivers]
        )
        if value
    ]
    report.update(
        {
            "verified_at": max(timestamps) if timestamps else None,
            "input_digests": inputs,
            "source_access_history": policy.get("source_access_history", []),
        }
    )
    write_json(root / "release/citation-verification.json", report)

    status = "PASS" if report["citation_verification_passed"] else "BLOCKED"
    report_lines = [
        "# Citation verification",
        "",
        f"Status: **{status}**",
        f"Snapshot: `{snapshot_id}`",
        "",
        f"- Released claims: {report['summary']['released_claims']}",
        f"- Citation links: {report['summary']['released_citations']}",
        f"- Unique sources: {report['summary']['unique_sources']}",
        f"- Passed: {report['summary']['citations_passed']}",
        f"- Non-dependent waivers: {report['summary']['citations_waived_non_dependent']}",
        f"- Per-citation failures: {report['summary']['citations_failed']}",
        f"- Joint claim reviews: {report['summary']['joint_claim_reviews_passed']}/"
        f"{report['summary']['joint_claim_reviews_required']} passed",
        f"- Blocking failures: {report['summary']['blocking_failures']}",
        "",
        "## Verification boundary",
        "",
        report["deterministic_boundary"],
        "",
        "A URL/title/token match never sets semantic support. The release verifier",
        "requires a separate manual locator review bound to both the claim hash and",
        "the fetched document hash. Any changed claim or source invalidates that review.",
    ]
    if report["failures"]:
        report_lines.extend(["", "## Blocking failures", ""])
        for failure in report["failures"]:
            identifier = failure.get("citation_id") or failure.get("source_id")
            report_lines.append(f"- `{identifier}`: {'; '.join(failure['problems'])}")
    report_path = root / "reports/citation-verification.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return report


def build_review_packet(
    inventory: dict[str, list[dict[str, Any]]], evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build a review queue without assigning or suggesting a semantic verdict."""

    claims = {value["claim_id"]: value for value in inventory["claims"]}
    sources = {value["source_id"]: value for value in inventory["sources"]}
    observations = {value["source_id"]: value for value in evidence}
    packet: list[dict[str, Any]] = []
    for citation in inventory["citations"]:
        claim = claims[citation["claim_id"]]
        source = sources[citation["source_id"]]
        observation = observations.get(citation["source_id"], {})
        located = next(
            (
                value
                for value in observation.get("citation_evidence", [])
                if value.get("citation_id") == citation["citation_id"]
            ),
            {},
        )
        packet.append(
            {
                "schema_version": SCHEMA_VERSION,
                "citation_id": citation["citation_id"],
                "claim_id": claim["claim_id"],
                "claim_sha256": claim["claim_sha256"],
                "claim_text": claim["text"],
                "release_material": claim["release_material"],
                "source_id": source["source_id"],
                "requested_url": source["requested_url"],
                "document_sha256": observation.get("document_sha256"),
                "locator": located.get("locator"),
                "locator_sha256": located.get("locator_sha256"),
                "evidence_excerpt": located.get("evidence_excerpt"),
                "excerpt_sha256": located.get("excerpt_sha256"),
                "allowed_verdicts": sorted(ALLOWED_VERDICTS),
                "verdict": "manual_review_required",
                "reviewer_instruction": (
                    "Read the exact located source context, check every number/date/entity and contrary evidence, "
                    "then create a separate hash-bound review record. Token overlap is not semantic support."
                ),
            }
        )
    return packet


def build_joint_review_packet(
    inventory: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    claims = {value["claim_id"]: value for value in inventory["claims"]}
    grouped: dict[str, list[str]] = {}
    for citation in inventory["citations"]:
        grouped.setdefault(citation["claim_id"], []).append(citation["citation_id"])
    packet: list[dict[str, Any]] = []
    for claim_id, citation_ids in sorted(grouped.items()):
        if len(citation_ids) < 2:
            continue
        claim = claims[claim_id]
        packet.append(
            {
                "schema_version": SCHEMA_VERSION,
                "claim_id": claim_id,
                "claim_sha256": claim["claim_sha256"],
                "claim_text": claim["text"],
                "citation_ids": sorted(citation_ids),
                "verdict": "manual_joint_review_required",
                "reviewer_instruction": (
                    "Check that the cited evidence, considered together, supports every factual part of the exact claim; "
                    "retain any unsupported span rather than smoothing it away."
                ),
            }
        )
    return packet


def _load_policy(root: Path) -> dict[str, Any]:
    policy = _load_json(root / "research/citation-policy.json")
    if policy.get("schema_version") != SCHEMA_VERSION:
        raise CitationError("unsupported citation policy schema")
    return policy


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect_parser = subparsers.add_parser("collect", help="collect released citation claims and sources")
    collect_parser.add_argument("--check", action="store_true")
    fetch_parser = subparsers.add_parser("fetch", help="fetch and hash citation evidence with strict TLS")
    fetch_parser.add_argument("--timeout", type=float, default=30.0)
    fetch_parser.add_argument("--max-bytes", type=int, default=32 * 1024 * 1024)
    fetch_parser.add_argument("--delay", type=float, default=0.55)
    verify_parser = subparsers.add_parser("verify", help="verify frozen evidence and semantic reviews offline")
    verify_parser.add_argument("--snapshot-id", required=True)
    verify_parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    policy = _load_policy(root)
    inventory = collect_citations(root, policy)

    if args.command == "collect":
        outputs = {
            root / "research/claims.jsonl": "".join(canonical_json(value) + "\n" for value in inventory["claims"]),
            root / "research/citations.jsonl": "".join(canonical_json(value) + "\n" for value in inventory["citations"]),
            root / "research/bibliography.json": json.dumps(
                {"schema_version": SCHEMA_VERSION, "sources": inventory["sources"]},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
        }
        stale: list[str] = []
        for path, payload in outputs.items():
            if args.check:
                if not path.exists() or path.read_text(encoding="utf-8") != payload:
                    stale.append(path.relative_to(root).as_posix())
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(payload, encoding="utf-8")
        if stale:
            raise CitationError(f"stale citation inventory: {', '.join(stale)}")
        print(canonical_json({"claims": len(inventory["claims"]), "citations": len(inventory["citations"]), "sources": len(inventory["sources"]), "check": args.check}))
        return 0

    if args.command == "fetch":
        evidence = fetch_all(
            inventory,
            timeout=args.timeout,
            max_bytes=args.max_bytes,
            delay=args.delay,
        )
        write_jsonl(root / "research/evidence.jsonl", evidence)
        write_jsonl(root / "research/citation-review-packet.jsonl", build_review_packet(inventory, evidence))
        write_jsonl(root / "research/claim-review-packet.jsonl", build_joint_review_packet(inventory))
        required_source_checks = {
            "reachable",
            "secure_transport",
            "redirect_source_identity",
            "identity_matches",
            "locator_found",
            "excerpt_matches",
        }
        source_checks_passed = sum(
            all(record.get("checks", {}).get(check) == "pass" for check in required_source_checks)
            for record in evidence
        )
        citation_observations = [
            item for record in evidence for item in record.get("citation_evidence", [])
        ]
        citation_locators_passed = sum(
            item.get("checks", {}).get("locator_found") == "pass"
            and item.get("checks", {}).get("excerpt_matches") == "pass"
            for item in citation_observations
        )
        fetch_passed = source_checks_passed == len(evidence) and citation_locators_passed == len(
            inventory["citations"]
        )
        print(
            canonical_json(
                {
                    "sources": len(evidence),
                    "source_checks_passed": source_checks_passed,
                    "citation_locators": len(citation_observations),
                    "citation_locators_passed": citation_locators_passed,
                    "fetch_passed": fetch_passed,
                }
            )
        )
        return 0 if fetch_passed else 1

    evidence = load_jsonl(root / "research/evidence.jsonl")
    reviews = load_jsonl(root / "research/citation-support-reviews.jsonl")
    claim_reviews = load_jsonl(root / "research/claim-support-reviews.jsonl")
    waiver_data = _load_json(root / "research/citation-waivers.json")
    waivers = waiver_data.get("waivers", [])
    if args.check:
        with tempfile.TemporaryDirectory(prefix="govuk-okf-citation-check-") as directory:
            temporary_root = Path(directory)
            report = build_outputs(
                temporary_root,
                policy,
                inventory,
                evidence,
                reviews,
                claim_reviews,
                waivers,
                args.snapshot_id,
            )
            generated_paths = (
                "research/claims.jsonl",
                "research/evidence.jsonl",
                "research/citations.jsonl",
                "research/bibliography.json",
                "research/bibliography.md",
                "provenance/claim-citation-ledger.jsonl",
                "release/citation-verification.json",
                "reports/citation-verification.md",
            )
            stale = [
                relative
                for relative in generated_paths
                if not (root / relative).exists()
                or (root / relative).read_bytes() != (temporary_root / relative).read_bytes()
            ]
            if stale:
                raise CitationError(f"stale citation release evidence: {', '.join(stale)}")
    else:
        report = build_outputs(
            root,
            policy,
            inventory,
            evidence,
            reviews,
            claim_reviews,
            waivers,
            args.snapshot_id,
        )
    print(canonical_json(report["summary"] | {"citation_verification_passed": report["citation_verification_passed"]}))
    return 0 if report["citation_verification_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(cli())
