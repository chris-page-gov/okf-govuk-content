"""Transient rendered-page link and schema.org gap detection.

The detector retains only typed targets, compact page metadata and response
hashes. HTML and JSON-LD bodies are parsed in memory and never written.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

from .acquisition import AcquisitionError, candidate_key, normalise_url

RESOURCE_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".epub",
    ".geojson",
    ".ics",
    ".json",
    ".ods",
    ".odt",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rdf",
    ".rtf",
    ".tsv",
    ".txt",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}
DENIED_ROUTE_PREFIXES = ("/api/", "/search", "/email-signup", "/print/")
SCHEMA_URL_KEYS = {"url", "sameAs", "contentUrl", "mainEntityOfPage"}


@dataclass(frozen=True)
class RobotsRule:
    allow: bool
    pattern: str

    def matches(self, path: str) -> bool:
        expression = re.escape(self.pattern).replace(r"\*", ".*")
        if expression.endswith(r"\$"):
            expression = expression[:-2] + "$"
        return re.match(expression, path) is not None


@dataclass(frozen=True)
class RobotsPolicy:
    rules: tuple[RobotsRule, ...]
    sha256: str
    retrieved_at: str
    source_url: str

    def allows(self, url: str) -> bool:
        parsed = urlsplit(url)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        matches = [rule for rule in self.rules if rule.matches(path)]
        if not matches:
            return True
        winner = max(matches, key=lambda rule: (len(rule.pattern), rule.allow))
        return winner.allow


def parse_robots(body: bytes, evidence: dict[str, Any]) -> RobotsPolicy:
    text = body.decode("utf-8", errors="replace")
    groups: list[tuple[list[str], list[RobotsRule]]] = []
    agents: list[str] = []
    rules: list[RobotsRule] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, value = (part.strip() for part in line.split(":", 1))
        folded = field.casefold()
        if folded == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.casefold())
        elif folded in {"allow", "disallow"} and agents:
            if value:
                rules.append(RobotsRule(folded == "allow", value))
    if agents or rules:
        groups.append((agents, rules))
    selected = [rule for group_agents, group_rules in groups if "*" in group_agents for rule in group_rules]
    return RobotsPolicy(
        rules=tuple(selected),
        sha256=str(evidence.get("sha256") or hashlib.sha256(body).hexdigest()),
        retrieved_at=str(evidence.get("retrieved_at") or ""),
        source_url=str(evidence.get("requested_url") or "https://www.gov.uk/robots.txt"),
    )


class RenderedMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self.language = ""
        self._jsonld = False
        self._jsonld_parts: list[str] = []
        self.jsonld_documents: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        folded = tag.casefold()
        if folded == "html" and values.get("lang"):
            self.language = values["lang"]
        if folded in {"a", "link"} and values.get("href"):
            self.links.append(
                {
                    "tag": folded,
                    "href": values["href"],
                    "rel": values.get("rel", ""),
                    "type": values.get("type", ""),
                }
            )
        if folded == "script" and values.get("type", "").split(";", 1)[0].strip().casefold() == "application/ld+json":
            self._jsonld = True
            self._jsonld_parts = []

    def handle_data(self, data: str) -> None:
        if self._jsonld and sum(len(part) for part in self._jsonld_parts) < 1024 * 1024:
            self._jsonld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._jsonld:
            self.jsonld_documents.append("".join(self._jsonld_parts)[: 1024 * 1024])
            self._jsonld = False
            self._jsonld_parts = []


def _schema_values(value: Any) -> tuple[set[str], set[str]]:
    types: set[str] = set()
    urls: set[str] = set()

    def visit(item: Any, key: str = "") -> None:
        if isinstance(item, dict):
            item_type = item.get("@type")
            for value_type in item_type if isinstance(item_type, list) else [item_type]:
                if value_type:
                    types.add(str(value_type))
            for child_key, child in item.items():
                if child_key in SCHEMA_URL_KEYS:
                    for url in child if isinstance(child, list) else [child]:
                        if isinstance(url, str):
                            urls.add(url)
                        elif isinstance(url, dict) and isinstance(url.get("@id"), str):
                            urls.add(url["@id"])
                if child_key not in {"articleBody", "description", "text"}:
                    visit(child, child_key)
        elif isinstance(item, list):
            for child in item:
                visit(child, key)

    visit(value)
    return types, urls


def _canonical_rendered_target(base_url: str, raw: str) -> str | None:
    joined = urljoin(base_url, raw.strip())
    try:
        value = normalise_url(joined)
    except (AcquisitionError, ValueError):
        return None
    parsed = urlsplit(value)
    if parsed.hostname == "www.gov.uk":
        value = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))
    return value


def _is_resource(url: str, declared_type: str = "") -> bool:
    parsed = urlparse(url)
    suffix = "." + parsed.path.rsplit(".", 1)[-1].casefold() if "." in parsed.path.rsplit("/", 1)[-1] else ""
    return (
        parsed.netloc == "assets.publishing.service.gov.uk"
        or suffix in RESOURCE_EXTENSIONS
        or declared_type.casefold() not in {"", "text/html", "application/xhtml+xml"}
    )


def rendered_observation(
    parent: dict[str, Any],
    body: bytes,
    evidence: dict[str, Any],
    policy: RobotsPolicy,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    parent_url = normalise_url(str(parent.get("canonical_url") or parent.get("base_path") or "/"))
    parser = RenderedMetadataParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    schema_types: set[str] = set()
    schema_urls: set[str] = set()
    invalid_jsonld = 0
    for document in parser.jsonld_documents:
        try:
            types, urls = _schema_values(json.loads(document))
            schema_types.update(types)
            schema_urls.update(urls)
        except json.JSONDecodeError:
            invalid_jsonld += 1

    link_rows = list(parser.links)
    for url in sorted(schema_urls):
        link_rows.append({"tag": "script", "href": url, "rel": "schema.org", "type": ""})
    records: dict[tuple[str, str], dict[str, Any]] = {}
    same_host = 0
    external = 0
    resources = 0
    blocked = 0
    for index, link in enumerate(link_rows):
        target = _canonical_rendered_target(parent_url, link["href"])
        if not target:
            continue
        parsed = urlparse(target)
        if parsed.netloc == "www.gov.uk" and (
            any(parsed.path.startswith(prefix) for prefix in DENIED_ROUTE_PREFIXES) or not policy.allows(target)
        ):
            blocked += 1
            continue
        resource = _is_resource(target, link.get("type", ""))
        entity_class = "resource" if resource else "route" if parsed.netloc == "www.gov.uk" else "external_boundary"
        if resource:
            resources += 1
        elif entity_class == "route":
            same_host += 1
        else:
            external += 1
        locale = str(parent.get("locale") or parser.language or "en")
        key = (target, entity_class)
        records.setdefault(
            key,
            {
                "candidate_key": candidate_key(target, locale, entity_class, target),
                "entity_class": entity_class,
                "source_native_id": target,
                "source_id": "rendered-link-gap-detector",
                "source_memberships": ["rendered-links"],
                "coverage_disposition": "represented",
                "canonical_url": target,
                "base_path": parsed.path or "/",
                "title": parsed.path.rsplit("/", 1)[-1].replace("-", " ").strip().title() or parsed.netloc,
                "description": "Discovered by the bounded rendered-link gap detector; body content was not retained.",
                "document_type": "rendered_resource" if resource else "rendered_link_boundary" if entity_class == "external_boundary" else "rendered_route",
                "schema_name": "rendered_link_observation",
                "locale": locale,
                "links": {},
                "retrieved_at": evidence.get("retrieved_at"),
                "evidence_url": evidence.get("requested_url") or parent_url,
                "evidence_sha256": evidence.get("sha256") or hashlib.sha256(body).hexdigest(),
                "evidence_locator": f"/html/{link['tag']}/{index}/@href",
                "source_adapter": "govuk_rendered_link_gap_detector",
                "rendered_link_rel": link.get("rel") or None,
            },
        )
    canonical = next(
        (
            _canonical_rendered_target(parent_url, link["href"])
            for link in parser.links
            if "canonical" in link.get("rel", "").casefold().split()
        ),
        None,
    )
    metadata = {
        "body_sha256": evidence.get("sha256") or hashlib.sha256(body).hexdigest(),
        "retrieved_at": evidence.get("retrieved_at"),
        "final_url": evidence.get("final_url"),
        "canonical_url": canonical,
        "language": parser.language or None,
        "schema_org_types": sorted(schema_types),
        "jsonld_blocks": len(parser.jsonld_documents),
        "invalid_jsonld_blocks": invalid_jsonld,
        "retained_body_bytes": 0,
        "discovered": {
            "same_host_routes": same_host,
            "resources": resources,
            "external_boundaries": external,
            "robots_or_route_blocked": blocked,
        },
    }
    return metadata, [records[key] for key in sorted(records)]
