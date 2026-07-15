"""Deterministic bulk-first selection for expensive Content API enrichment."""

from __future__ import annotations

import collections
import hashlib
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse

from .acquisition import normalise_url
from .util import canonical_json_bytes


POLICY_VERSION = "govuk-okf-selective-hydration.v2"
AUDIT_SAMPLE_MODULUS = 100

# Source-native navigation classes whose typed links are material to the graph.
STRUCTURAL_DOCUMENT_TYPES = frozenset(
    {
        "collection",
        "document_collection",
        "homepage",
        "mainstream_browse_page",
        "organisation",
        "service_manual_guide",
        "service_manual_topic",
        "step_by_step_nav",
        "taxon",
        "topical_event",
        "world_index",
        "world_location",
        "worldwide_organisation",
    }
)

# These public document families can expose downloadable resources or lifecycle
# detail not represented by Search v1's metadata/HTML-part fields. They remain
# an explicit deferred-enrichment population until an authoritative bulk source
# is admitted or a targeted resource pass is scheduled; the stable audit sample
# still measures the quality of that decision.
ATTACHMENT_OR_LIFECYCLE_DOCUMENT_TYPES = frozenset(
    {
        "asylum_support_tribunal_decision",
        "authored_article",
        "case_study",
        "closed_consultation",
        "consultation",
        "corporate_report",
        "decision",
        "detailed_guide",
        "employment_appeal_tribunal_decision",
        "employment_tribunal_decision",
        "foi_release",
        "form",
        "guidance",
        "impact_assessment",
        "independent_report",
        "international_treaty",
        "medical_safety_alert",
        "national_statistics",
        "national_statistics_announcement",
        "notice",
        "official_statistics",
        "official_statistics_announcement",
        "open_consultation",
        "policy_paper",
        "publication",
        "publication_scheme",
        "research",
        "research_for_development_output",
        "statistical_data_set",
        "statistics",
        "statistics_announcement",
        "tax_tribunal_decision",
        "utaac_decision",
    }
)


@dataclass(frozen=True)
class HydrationDecision:
    selected: bool
    reasons: tuple[str, ...]
    disposition: str

    def record(self, source: dict[str, Any]) -> dict[str, Any]:
        url = normalise_url(str(source.get("canonical_url") or source.get("base_path") or "/"))
        return {
            "policy": POLICY_VERSION,
            "candidate_key": source.get("candidate_key"),
            "canonical_url": url,
            "locale": str(source.get("locale") or "en"),
            "document_type": str(source.get("document_type") or "unknown"),
            "selected": self.selected,
            "reasons": list(self.reasons),
            "disposition": self.disposition,
        }


def _search_membership(record: dict[str, Any]) -> bool:
    return any(
        str(value).startswith("search-")
        for value in record.get("source_memberships", [])
    )


def _stable_audit_sample(record: dict[str, Any]) -> bool:
    identity = str(
        record.get("content_id")
        or record.get("candidate_key")
        or record.get("canonical_url")
        or record.get("base_path")
        or ""
    )
    if not identity:
        return False
    value = int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest()[:8], "big")
    return value % AUDIT_SAMPLE_MODULUS == 0


def hydration_decision(record: dict[str, Any]) -> HydrationDecision:
    url = normalise_url(str(record.get("canonical_url") or record.get("base_path") or "/"))
    if urlparse(url).netloc != "www.gov.uk":
        return HydrationDecision(False, ("external_boundary",), "external_boundary")

    source_id = str(record.get("source_id") or "")
    memberships = {str(value) for value in record.get("source_memberships", [])}
    document_type = str(record.get("document_type") or "unknown")

    if source_id == "content-api" and record.get("content_id"):
        return HydrationDecision(False, ("already_content_api_enriched",), "already_enriched")

    reasons: list[str] = []
    deferred_reasons: list[str] = []
    if "sitemap" in memberships and not _search_membership(record):
        reasons.append("sitemap_only")
    if not record.get("content_id"):
        reasons.append("missing_content_id")
    if source_id == "structured-linked-content":
        reasons.append("structured_link_closure")
    if document_type in STRUCTURAL_DOCUMENT_TYPES:
        reasons.append("structural_relationships")
    if document_type in ATTACHMENT_OR_LIFECYCLE_DOCUMENT_TYPES:
        deferred_reasons.append("deferred_attachments_or_lifecycle")
    if record.get("coverage_disposition") in {"redirect_only", "tombstone_only"}:
        reasons.append("lifecycle_disposition")
    if record.get("is_historic") is True:
        deferred_reasons.append("deferred_historic_content")
    if not reasons and _stable_audit_sample(record):
        reasons.append("deterministic_one_percent_audit")

    if reasons:
        return HydrationDecision(True, tuple(sorted(set(reasons))), "content_api_selected")
    if deferred_reasons:
        return HydrationDecision(
            False,
            tuple(sorted(set(deferred_reasons))),
            "deferred_bulk_source_or_targeted_enrichment",
        )
    return HydrationDecision(False, (), "bulk_metadata_represented")


def apply_bulk_metadata_disposition(
    record: dict[str, Any], decision: HydrationDecision
) -> dict[str, Any]:
    result = dict(record)
    result["enrichment_policy"] = POLICY_VERSION
    result["enrichment_selected"] = decision.selected
    result["enrichment_selection_reasons"] = list(decision.reasons)
    if decision.disposition == "external_boundary":
        result["hydration_status"] = "external_boundary"
        result["enrichment_status"] = "external_boundary"
    elif decision.disposition == "already_enriched":
        result["hydration_status"] = "content_api_already_represented"
        result["enrichment_status"] = "content_api_enriched"
    elif decision.disposition == "deferred_bulk_source_or_targeted_enrichment":
        result["hydration_status"] = "deferred_enrichment"
        result["enrichment_status"] = "bulk_metadata_only_deferred"
    elif not decision.selected:
        result["hydration_status"] = "bulk_metadata_represented"
        result["enrichment_status"] = "bulk_metadata_only"
    return result


def selection_manifest(records: Iterable[dict[str, Any]], snapshot: str) -> dict[str, Any]:
    total = 0
    selected = 0
    deferred = 0
    reasons: collections.Counter[str] = collections.Counter()
    dispositions: collections.Counter[str] = collections.Counter()
    document_types: collections.Counter[str] = collections.Counter()
    digest = hashlib.sha256()
    for record in records:
        decision = hydration_decision(record)
        row = decision.record(record)
        digest.update(canonical_json_bytes(row))
        total += 1
        dispositions[decision.disposition] += 1
        if decision.selected:
            selected += 1
            document_types[str(record.get("document_type") or "unknown")] += 1
            reasons.update(decision.reasons)
        elif decision.disposition == "deferred_bulk_source_or_targeted_enrichment":
            deferred += 1
            reasons.update(decision.reasons)
    return {
        "schema": "govuk-okf-hydration-selection-manifest.v1",
        "snapshot": snapshot,
        "policy": POLICY_VERSION,
        "audit_sample_modulus": AUDIT_SAMPLE_MODULUS,
        "source_records": total,
        "selected_records": selected,
        "deferred_enrichment_records": deferred,
        "bulk_or_already_represented_records": total - selected,
        "selected_fraction": selected / total if total else 0,
        "selection_reasons": dict(sorted(reasons.items())),
        "dispositions": dict(sorted(dispositions.items())),
        "selected_document_types": dict(sorted(document_types.items())),
        "decision_set_sha256": digest.hexdigest(),
    }
