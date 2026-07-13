#!/usr/bin/env python3
"""Run and validate the fresh official-source and plan-URL preflight."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.webprobe import Probe, fetch_probe, public_result  # noqa: E402

OUTPUT = ROOT / "research" / "source-preflight.json"
PLAN_PREFLIGHT = ROOT / "planning" / "PLAN_SOURCE_PREFLIGHT.json"
PLAN = ROOT / "planning" / "AFHF_GOVUK_OKF_SOTA_RESEARCH_IMPLEMENTATION_PLAN.md"

OFFICIAL_SOURCE_HOSTS = (
    "api.github.com",
    "content-api.publishing.service.gov.uk",
    "docs.publishing.service.gov.uk",
    "www.gov.uk",
    "www.nationalarchives.gov.uk",
)
PLAN_SOURCE_HOSTS = (
    "a2a-protocol.org",
    "aclanthology.org",
    "api.crossref.org",
    "arxiv.org",
    "blog.canada.ca",
    "content-api.publishing.service.gov.uk",
    "cormack.uwaterloo.ca",
    "data.europa.eu",
    "design-system.service.gov.uk",
    "design.canada.ca",
    "developers.openai.com",
    "digital.nhs.uk",
    "docs.publishing.service.gov.uk",
    "github.com",
    "insidegovuk.blog.gov.uk",
    "learn.chatgpt.com",
    "modelcontextprotocol.io",
    "open.gsa.gov",
    "openai.com",
    "pages.gseis.ucla.edu",
    "proceedings.iclr.cc",
    "proceedings.neurips.cc",
    "schema.org",
    "semiceu.github.io",
    "spec.openapis.org",
    "userresearch.blog.gov.uk",
    "www.gov.uk",
    "www.nationalarchives.gov.uk",
    "www.nist.gov",
    "www.researchobject.org",
    "www.usa.gov",
    "www.w3.org",
)


def official_probe(identifier: str, url: str, family: str) -> Probe:
    return Probe(identifier, url, family, allowed_hosts=OFFICIAL_SOURCE_HOSTS)


OFFICIAL_PROBES = [
    official_probe("content-api-home", "https://content-api.publishing.service.gov.uk/", "contract"),
    official_probe("content-api-reference", "https://content-api.publishing.service.gov.uk/reference.html", "contract"),
    official_probe("content-api-root", "https://www.gov.uk/api/content", "enumerator"),
    official_probe("content-api-sample", "https://www.gov.uk/api/content/take-pet-abroad", "metadata"),
    official_probe("content-api-redirect", "https://www.gov.uk/api/content/dfe", "lifecycle"),
    official_probe("search-api-root-count", "https://www.gov.uk/api/search.json?count=0", "enumerator"),
    official_probe("search-api-sample", "https://www.gov.uk/api/search.json?count=1", "enumerator"),
    official_probe("sitemap-index", "https://www.gov.uk/sitemap.xml", "enumerator"),
    official_probe("organisations-api", "https://www.gov.uk/api/organisations?page=1", "enumerator"),
    official_probe("organisations-content-index", "https://www.gov.uk/api/content/government/organisations", "navigation"),
    official_probe("world-taxonomy-root", "https://www.gov.uk/api/content/world/all", "enumerator"),
    official_probe("mainstream-browse-root", "https://www.gov.uk/api/content/browse", "enumerator"),
    official_probe("attachment-sample", "https://www.gov.uk/api/content/government/publications/ukho-1825-archives-catalogue", "resource"),
    official_probe("gds-atom-feed", "https://www.gov.uk/government/organisations/government-digital-service.atom", "gap-detector"),
    official_probe("search-atom-feed", "https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D=government-digital-service", "gap-detector"),
    official_probe("robots", "https://www.gov.uk/robots.txt", "policy"),
    official_probe("reuse", "https://www.gov.uk/help/reuse-govuk-content", "rights"),
    official_probe("terms", "https://www.gov.uk/help/terms-conditions", "rights"),
    official_probe("ogl-v3", "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/", "rights"),
    official_probe("ogl-exceptions", "https://www.nationalarchives.gov.uk/information-management/re-using-public-sector-information/uk-government-licensing-framework/open-government-licence/exceptions-to-ogl/", "rights"),
    official_probe("publishing-api-docs", "https://docs.publishing.service.gov.uk/repos/publishing-api/api.html", "contract"),
    official_probe("document-types", "https://docs.publishing.service.gov.uk/document-types.html", "contract"),
    official_probe("search-api-v1-docs", "https://docs.publishing.service.gov.uk/repos/search-api/using-the-search-api.html", "contract"),
    official_probe("search-api-v2-docs", "https://docs.publishing.service.gov.uk/repos/search-api-v2.html", "comparator"),
    official_probe("search-architecture", "https://docs.publishing.service.gov.uk/manual/govuk-search.html", "comparator"),
    official_probe("sitemap-docs", "https://docs.publishing.service.gov.uk/manual/govuk-sitemap.html", "contract"),
    official_probe("taxonomy-docs", "https://docs.publishing.service.gov.uk/manual/taxonomy.html", "contract"),
    official_probe("world-taxonomy-docs", "https://docs.publishing.service.gov.uk/manual/world-taxonomy.html", "contract"),
    official_probe("collections-docs", "https://docs.publishing.service.gov.uk/repos/collections.html", "contract"),
    official_probe("organisations-docs", "https://docs.publishing.service.gov.uk/manual/organisations-api.html", "contract"),
    official_probe("govsearch-model", "https://docs.publishing.service.gov.uk/repos/govuk-knowledge-graph-gcp/how-to-write-queries.html", "comparator"),
    official_probe("publishing-api-main-commit", "https://api.github.com/repos/alphagov/publishing-api/commits/main", "version"),
]


def extract_facts(result: dict[str, object], body: bytes) -> dict[str, object]:
    facts: dict[str, object] = {}
    content_type = str(result.get("headers", {}).get("content-type", ""))
    if "json" in content_type or body.lstrip().startswith((b"{", b"[")):
        try:
            payload = json.loads(body.decode("utf-8"))
            if result["id"] == "search-api-root-count":
                facts["reported_total"] = payload.get("total")
            elif result["id"] == "search-api-sample":
                facts["reported_total"] = payload.get("total")
                facts["result_count"] = len(payload.get("results", []))
                facts["top_level_keys"] = sorted(payload)
            elif str(result["id"]).startswith("content-api-") or result["id"] in {
                "organisations-content-index",
                "world-taxonomy-root",
                "mainstream-browse-root",
                "attachment-sample",
            }:
                for key in ("content_id", "base_path", "document_type", "schema_name", "locale", "public_updated_at"):
                    facts[key] = payload.get(key)
                facts["top_level_keys"] = sorted(payload)
                facts["link_types"] = sorted((payload.get("links") or {}).keys())
                facts["link_counts"] = {
                    key: len(value) for key, value in sorted((payload.get("links") or {}).items()) if isinstance(value, list)
                }
                attachments = (payload.get("details") or {}).get("attachments", [])
                if attachments:
                    facts["attachment_count"] = len(attachments)
                    facts["attachment_fields"] = sorted(attachments[0])
                    facts["attachment_url"] = attachments[0].get("url")
            elif result["id"] == "organisations-api":
                facts["total"] = payload.get("total")
                facts["pages"] = payload.get("pages")
                facts["page_size"] = payload.get("page_size")
                facts["result_count"] = len(payload.get("results", []))
            elif result["id"] == "publishing-api-main-commit":
                facts["commit_sha"] = payload.get("sha")
                facts["commit_url"] = payload.get("html_url")
        except (UnicodeDecodeError, json.JSONDecodeError):
            facts["parse_error"] = "invalid JSON"
    if result["id"] == "sitemap-index" and body:
        try:
            root = ET.fromstring(body)
            locations = [element.text for element in root.iter() if element.tag.endswith("loc")]
            facts["root_element"] = root.tag
            facts["declared_locations"] = len(locations)
            facts["first_location"] = locations[0] if locations else None
            facts["last_location"] = locations[-1] if locations else None
        except ET.ParseError as exc:
            facts["parse_error"] = str(exc)
    if result["id"] == "robots" and body:
        text = body.decode("utf-8", errors="replace")
        facts["line_count"] = len(text.splitlines())
        facts["sitemap_directives"] = [line.strip() for line in text.splitlines() if line.lower().startswith("sitemap:")]
        facts["disallow_count"] = sum(1 for line in text.splitlines() if line.lower().startswith("disallow:"))
    if result["id"] in {"gds-atom-feed", "search-atom-feed"} and body:
        try:
            root = ET.fromstring(body)
            facts["entry_count"] = sum(1 for element in root.iter() if element.tag.endswith("entry"))
            facts["next_links"] = [
                element.attrib.get("href")
                for element in root.iter()
                if element.tag.endswith("link") and element.attrib.get("rel") == "next"
            ]
        except ET.ParseError as exc:
            facts["parse_error"] = str(exc)
    return facts


def run_live(include_plan: bool) -> dict[str, object]:
    started = datetime.now(timezone.utc)
    previous = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.is_file() else {}
    official = []
    last_host = ""
    for probe in OFFICIAL_PROBES:
        host = urlparse(probe.url).netloc
        if host == last_host:
            time.sleep(0.5)
        result = fetch_probe(probe)
        body = result.get("body", b"")
        result["facts"] = extract_facts(result, body)
        official.append(public_result(result))
        last_host = host

    plan_results = []
    if include_plan:
        preflight = json.loads(PLAN_PREFLIGHT.read_text(encoding="utf-8"))
        for index, source in enumerate(preflight["sources"], start=1):
            probe = Probe(
                f"plan-{index:03d}",
                source["url"],
                "plan-citation",
                partial=True,
                max_bytes=131072,
                allowed_hosts=PLAN_SOURCE_HOSTS,
            )
            result = public_result(fetch_probe(probe, attempts=2))
            result["expected_identity_status"] = source["url_identity_preflight"]
            plan_results.append(result)
    elif previous:
        plan_results = previous.get("plan_sources", [])

    ended = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "started_at": started.isoformat(),
        "completed_at": ended.isoformat(),
        "method": "bounded GET with redirects, identified user agent, retries and retained response hashes",
        "official_sources": official,
        "plan_source_history": previous.get("plan_source_history", {}),
        "plan_sources": plan_results,
        "summary": {
            "official_total": len(official),
            "official_ok": sum(bool(item["ok"]) for item in official),
            "official_failed": sum(not bool(item["ok"]) for item in official),
            "plan_total": len(plan_results),
            "plan_ok": sum(bool(item["ok"]) for item in plan_results),
            "plan_failed": sum(not bool(item["ok"]) for item in plan_results),
        },
    }


def validate(document: dict[str, object]) -> list[str]:
    errors: list[str] = []
    official = document.get("official_sources", [])
    plan = document.get("plan_sources", [])
    if len(official) != len(OFFICIAL_PROBES):
        errors.append(f"official probe count is {len(official)}, expected {len(OFFICIAL_PROBES)}")
    plan_preflight = json.loads(PLAN_PREFLIGHT.read_text(encoding="utf-8"))
    expected_sources = plan_preflight["sources"]
    expected_plan = len(expected_sources)
    if len(plan) != expected_plan:
        errors.append(f"plan probe count is {len(plan)}, expected {expected_plan}")
    if plan_preflight.get("plan_sha256") != hashlib.sha256(PLAN.read_bytes()).hexdigest():
        errors.append("plan-source preflight hash does not match the implementation plan")
    expected_urls = [source["url"] for source in expected_sources]
    if [item.get("requested_url") for item in plan if isinstance(item, dict)] != expected_urls:
        errors.append("active plan probe URLs do not match PLAN_SOURCE_PREFLIGHT.json")
    ids = {item.get("id") for item in official if isinstance(item, dict)}
    missing = {probe.id for probe in OFFICIAL_PROBES} - ids
    if missing:
        errors.append(f"missing official probes: {sorted(missing)}")
    for item in [*official, *plan]:
        if not isinstance(item, dict) or not item.get("retrieved_at") or len(str(item.get("sha256", ""))) != 64:
            errors.append(f"invalid probe record: {item.get('id') if isinstance(item, dict) else '<non-object>'}")
    history = document.get("plan_source_history", {})
    if not isinstance(history, dict) or history.get("preserved_original_result_count") != 93:
        errors.append("the original 93-result plan preflight history is not preserved")
    else:
        superseded = history.get("superseded_results", [])
        if len(superseded) != 1 or "DH_KEY_TOO_SMALL" not in superseded[0].get("error", ""):
            errors.append("the superseded CMU strict-TLS failure is not preserved")
        else:
            superseded_by_id = {item["id"]: item for item in superseded}
            reconstructed = [superseded_by_id.get(item.get("id"), item) for item in plan]
            original_digest = hashlib.sha256(
                json.dumps(
                    reconstructed,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if original_digest != history.get("original_results_sha256"):
                errors.append("the reconstructed original 93 plan results do not match their frozen digest")
        restrictions = history.get("access_restrictions", [])
        if not any(
            "researchgate.net" in item.get("url", "")
            and item.get("publication_record_result") == "HTTP 403 Forbidden"
            and item.get("exact_author_pdf_result") == "HTTP 403 Forbidden"
            for item in restrictions
        ):
            errors.append("the ResearchGate publication/PDF access restriction is not preserved")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="run public HTTP probes and replace the result")
    parser.add_argument("--official-only", action="store_true", help="skip the 93 plan-citation URL probes")
    parser.add_argument("--check", action="store_true", help="validate the saved preflight without network access")
    args = parser.parse_args()
    if args.live == args.check:
        parser.error("choose exactly one of --live or --check")
    if args.live:
        document = run_live(include_plan=not args.official_only)
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        if not OUTPUT.is_file():
            print(f"missing {OUTPUT.relative_to(ROOT)}", file=sys.stderr)
            return 1
        document = json.loads(OUTPUT.read_text(encoding="utf-8"))
    errors = validate(document)
    if errors:
        print("source preflight validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(json.dumps(document["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
