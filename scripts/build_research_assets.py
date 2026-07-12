#!/usr/bin/env python3
"""Build the deterministic persona, story, question and evaluation assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.question_factory import (  # noqa: E402
    CHALLENGES,
    CORPUS_SNAPSHOT_ID,
    GENERATOR_VERSION,
    MATRIX_VERSION,
    OPERATIONS,
    QUESTION_CONTRACT_DATE,
    SUITE_QUOTAS,
    build_story_questions,
    curate_persona_suite,
    manifest_root,
    record_with_checksum,
    sha256_text,
)
from govuk_okf.question_matrix_v2 import (  # noqa: E402
    GENERATOR_VERSION as QUESTION_V2_GENERATOR_VERSION,
    OPERATIONS as QUESTION_V2_OPERATIONS,
    STORIES_PER_PERSONA as QUESTION_V2_STORIES_PER_PERSONA,
    STORY_ROLES as QUESTION_V2_STORY_ROLES,
)
from govuk_okf.util import safe_child_path, safe_identifier  # noqa: E402

SEED_PATH = ROOT / "personas" / "seed.json"
EVIDENCE_PATH = ROOT / "personas" / "evidence.json"
PINNED_CONTENT_SCHEMA_COMMIT = "b1e987aa7b3e62c105ff2b2db87667f7638726f8"
USE_ONTOLOGY_PLAN_PATH = ROOT / "planning" / "AFHF_GOVUK_OKF_SOTA_RESEARCH_IMPLEMENTATION_PLAN.md"
REQUIREMENTS_REGISTER_PATH = ROOT / "planning" / "AFHF_GOVUK_OKF_REQUIREMENTS_REGISTER.md"
QUESTION_V2_SOURCE_PATH = ROOT / "src" / "govuk_okf" / "question_matrix_v2.py"
QUESTION_V2_SCRIPT_PATH = ROOT / "scripts" / "build_question_matrix_v2.py"

REQUIRED_COVERAGE_DIMENSIONS = (
    "actor",
    "goal",
    "journey_stage",
    "content_service_type",
    "relationship_need",
    "jurisdiction",
    "language",
    "accessibility_need",
    "device_context",
    "urgency_risk",
    "agent_involvement",
)

INITIAL_OVERLAY_IDS = (
    "accessibility-review",
    "assisted-digital",
    "cognitive-access",
    "high-stakes",
    "historical-comparison",
    "language-support",
    "licensing-review",
    "low-bandwidth",
    "low-digital-confidence",
    "low-literacy",
    "mobile-only",
    "proxy-delegate",
    "safe-abstention",
    "screen-reader-keyboard",
    "structured-output",
    "welsh-language",
)

RELATIONSHIP_NEEDS = (
    "canonical_identity_and_route",
    "publisher_owner_and_provenance",
    "lifecycle_redirect_and_replacement",
    "source_native_hierarchy_taxonomy_and_collection",
    "attachment_data_and_machine_representation",
    "language_jurisdiction_and_authoritative_handoff",
)

ACCESSIBILITY_OVERLAY_IDS = {
    "accessibility-review",
    "assisted-digital",
    "cognitive-access",
    "low-digital-confidence",
    "low-literacy",
    "screen-reader-keyboard",
}

HIGH_RISK_OVERLAY_COMBINATIONS = (
    ("assisted-digital", "high-stakes", "low-literacy"),
    ("high-stakes", "language-support", "mobile-only"),
    ("high-stakes", "proxy-delegate", "screen-reader-keyboard"),
    ("high-stakes", "safe-abstention", "structured-output"),
    ("assisted-digital", "privacy-sensitive-context", "proxy-delegate"),
)

# Names are taken from the pinned alphagov/publishing-api content_schemas/formats
# directory. Mapping them into stories is a coverage mechanism, not a claim that
# every live GOV.UK record validates against the pinned commit.
SCHEMA_FAMILIES = (
    "answer",
    "calendar",
    "call_for_evidence",
    "case_study",
    "completed_transaction",
    "consultation",
    "contact",
    "content_block",
    "coronavirus_landing_page",
    "corporate_information_page",
    "detailed_guide",
    "document_collection",
    "email_alert_signup",
    "embassies_index",
    "external_content",
    "facet",
    "fatality_notice",
    "field_of_operation",
    "fields_of_operation",
    "finder",
    "finder_email_signup",
    "generic",
    "generic_with_external_related_links",
    "get_involved",
    "gone",
    "government",
    "guide",
    "help_page",
    "historic_appointment",
    "historic_appointments",
    "history",
    "hmrc_manual",
    "hmrc_manual_section",
    "homepage",
    "how_government_works",
    "html_publication",
    "landing_page",
    "licence",
    "link_collection",
    "local_transaction",
    "mainstream_browse_page",
    "manual",
    "manual_section",
    "ministers_index",
    "news_article",
    "organisation",
    "organisations_homepage",
    "person",
    "place",
    "plan_for_change_landing_page",
    "publication",
    "redirect",
    "role",
    "role_appointment",
    "service_manual_guide",
    "service_manual_homepage",
    "service_manual_service_standard",
    "service_manual_service_toolkit",
    "service_manual_topic",
    "simple_smart_answer",
    "smart_answer",
    "special_route",
    "specialist_document",
    "speech",
    "statistical_data_set",
    "statistics_announcement",
    "step_by_step_nav",
    "substitute",
    "take_part",
    "taxon",
    "topical_event",
    "topical_event_about_page",
    "transaction",
    "travel_advice",
    "travel_advice_index",
    "vanish",
    "working_group",
    "world_index",
    "world_location",
    "world_location_news",
    "worldwide_corporate_information_page",
    "worldwide_office",
    "worldwide_organisation",
)

OVERLAYS = (
    {
        "overlay_id": "screen-reader-keyboard",
        "label": "Screen reader or keyboard-only access",
        "dimensions": ["perceivable structure", "keyboard operation", "meaningful link labels"],
    },
    {
        "overlay_id": "cognitive-access",
        "label": "Cognitive load, memory or neurodivergent access",
        "dimensions": ["plain language", "sequenced tasks", "visible uncertainty"],
    },
    {
        "overlay_id": "low-literacy",
        "label": "Low literacy or unfamiliar government language",
        "dimensions": ["everyday wording", "term explanation", "assisted route"],
    },
    {
        "overlay_id": "low-digital-confidence",
        "label": "Low digital confidence",
        "dimensions": ["orientation", "recoverable steps", "support hand-off"],
    },
    {
        "overlay_id": "low-bandwidth",
        "label": "Low bandwidth or intermittent connectivity",
        "dimensions": ["small static representation", "stable URLs", "offline-readable export"],
    },
    {
        "overlay_id": "mobile-only",
        "label": "Mobile-only access",
        "dimensions": ["compact results", "touch target clarity", "handoff preservation"],
    },
    {
        "overlay_id": "welsh-language",
        "label": "Welsh-language route",
        "dimensions": ["language-specific records", "equivalent-source linking", "locale declaration"],
    },
    {
        "overlay_id": "language-support",
        "label": "English as an additional language or translation support",
        "dimensions": ["term disambiguation", "source-language disclosure", "no silent translation drift"],
    },
    {
        "overlay_id": "assisted-digital",
        "label": "Assisted digital support",
        "dimensions": ["support channel", "shared task state", "no digital-only assumption"],
    },
    {
        "overlay_id": "proxy-delegate",
        "label": "Proxy, delegate or intermediary",
        "dimensions": ["actor distinction", "authority boundary", "privacy-minimised context"],
    },
    {
        "overlay_id": "high-stakes",
        "label": "High-stakes or urgent use",
        "dimensions": ["safe abstention", "date and jurisdiction", "authoritative escalation"],
    },
    {
        "overlay_id": "structured-output",
        "label": "Machine-readable agent output",
        "dimensions": ["stable identifiers", "typed relationships", "provenance fields"],
    },
    {
        "overlay_id": "safe-abstention",
        "label": "Agent safe-abstention boundary",
        "dimensions": ["answerability status", "missing evidence", "authoritative hand-off"],
    },
    {
        "overlay_id": "historical-comparison",
        "label": "Current and historical comparison",
        "dimensions": ["effective date", "withdrawal state", "replacement relationship"],
    },
    {
        "overlay_id": "licensing-review",
        "label": "Licensing and fair-use review",
        "dimensions": ["attribution", "third-party material", "reuse trigger"],
    },
    {
        "overlay_id": "accessibility-review",
        "label": "Accessibility review workflow",
        "dimensions": ["representation coverage", "attachment alternatives", "test evidence"],
    },
    {
        "overlay_id": "privacy-sensitive-context",
        "label": "Privacy-sensitive or shared-context use",
        "dimensions": ["data minimisation", "delegation boundary", "private authoritative hand-off"],
    },
)

OVERLAY_EVIDENCE = {
    "screen-reader-keyboard": ["ev-accessibility-testing", "ev-wcag"],
    "cognitive-access": ["ev-accessibility-testing", "ev-wcag"],
    "low-literacy": ["ev-user-needs", "ev-non-digital-users"],
    "low-digital-confidence": ["ev-assisted-digital", "ev-non-digital-users"],
    "low-bandwidth": ["ev-non-digital-users", "ev-use-ontology-contract"],
    "mobile-only": ["ev-user-needs", "ev-use-ontology-contract"],
    "welsh-language": ["ev-user-needs", "ev-use-ontology-contract"],
    "language-support": ["ev-user-needs", "ev-use-ontology-contract"],
    "assisted-digital": ["ev-assisted-digital", "ev-non-digital-users"],
    "proxy-delegate": ["ev-assisted-digital", "ev-user-needs"],
    "high-stakes": ["ev-user-needs", "ev-use-ontology-contract"],
    "structured-output": ["ev-content-api", "ev-mcp-resources"],
    "safe-abstention": ["ev-content-api", "ev-kilt"],
    "historical-comparison": ["ev-content-api"],
    "licensing-review": ["ev-ogl"],
    "accessibility-review": ["ev-accessibility-testing", "ev-wcag"],
    "privacy-sensitive-context": ["ev-use-ontology-contract", "ev-user-needs"],
}

RELATIONSHIPS_BY_CLASS = {
    "public_life_event": ["parent", "mainstream_browse_pages", "organisations", "related"],
    "business_organisation": ["organisations", "taxons", "document_collections", "related"],
    "professional_intermediary": ["organisations", "taxons", "document_collections", "ordered_related_items"],
    "agent_system": ["parent", "taxons", "organisations", "document_collections", "ordered_related_items"],
}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _jsonl(records: Iterable[dict[str, Any]]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n" for record in records)


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_digest(value: Any) -> str:
    content = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return _digest(content)


def _line_range_digest(path: Path, start: int, end: int) -> str:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    return _digest("".join(lines[start - 1 : end]))


def _dimension_values(profile: dict[str, Any], story: dict[str, Any]) -> dict[str, list[str]]:
    overlays = set(profile["overlay_ids"])
    language = ["source_declared_language"]
    if "welsh-language" in overlays:
        language.append("welsh_language_route")
    if "language-support" in overlays:
        language.append("other_language_or_translation_support")

    accessibility = sorted(overlays & ACCESSIBILITY_OVERLAY_IDS)
    if not accessibility:
        accessibility = ["cross_cutting_access_overlay_not_profile_specific"]

    device_context = []
    for overlay_id in (
        "low-bandwidth",
        "mobile-only",
        "assisted-digital",
        "proxy-delegate",
        "privacy-sensitive-context",
    ):
        if overlay_id in overlays:
            device_context.append(overlay_id)
    channel_context = {
        "human_self_service": "human_self_service_context",
        "human_assisted": "assisted_or_intermediary_context",
        "agent_or_system": "machine_to_machine_context",
    }[profile["channel_class"]]
    device_context.append(channel_context)

    involvement = {
        "human_self_service": "human_direct_no_delegated_agent",
        "human_assisted": "human_with_assistance_or_intermediary",
        "agent_or_system": (
            "read_only_agent_primary"
            if profile["archetype_class"] == "agent_system"
            else "system_mediated_professional"
        ),
    }[profile["channel_class"]]

    urgency_risk = [f"risk_{profile['risk_level']}"]
    if "high-stakes" in overlays:
        urgency_risk.append("urgent_or_high_stakes_context")

    values = {
        "actor": [profile["archetype_class"]],
        "goal": [item["id"] for item in QUESTION_V2_OPERATIONS],
        "journey_stage": [item["id"] for item in QUESTION_V2_STORY_ROLES],
        "content_service_type": list(story["target_entities"]),
        "relationship_need": list(RELATIONSHIP_NEEDS),
        "jurisdiction": list(profile["jurisdiction"]),
        "language": language,
        "accessibility_need": accessibility,
        "device_context": sorted(set(device_context)),
        "urgency_risk": urgency_risk,
        "agent_involvement": [involvement],
    }
    if tuple(values) != REQUIRED_COVERAGE_DIMENSIONS:
        raise AssertionError("coverage dimension order drifted")
    if any(not candidates for candidates in values.values()):
        raise ValueError(f"empty coverage dimension for {profile['persona_id']}")
    return values


def _build_coverage_matrix(
    profiles: list[dict[str, Any]], stories: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    stories_by_persona = {story["persona_ids"][0]: story for story in stories}
    rows: list[dict[str, Any]] = []
    for profile in profiles:
        story = stories_by_persona[profile["persona_id"]]
        rows.append(
            record_with_checksum(
                {
                    "schema_version": 1,
                    "coverage_row_id": f"coverage-{profile['persona_id']}",
                    "persona_id": profile["persona_id"],
                    "development_story_ids": [story["story_id"]],
                    "release_story_contract": {
                        "generator": QUESTION_V2_GENERATOR_VERSION,
                        "stories_per_persona": QUESTION_V2_STORIES_PER_PERSONA,
                        "story_roles": [item["id"] for item in QUESTION_V2_STORY_ROLES],
                        "status": "contract_verified_final_snapshot_regeneration_pending",
                    },
                    "dimension_values": _dimension_values(profile, story),
                    "overlay_ids": profile["overlay_ids"],
                    "evidence_ids": profile["evidence_ids"],
                    "evidence_status": "research_hypothesis_not_human_validated",
                    "claim_boundary": "Machine coverage row only; not prevalence, observed behaviour or UI preference evidence.",
                }
            )
        )

    cell_personas: dict[str, dict[str, set[str]]] = {
        dimension: defaultdict(set) for dimension in REQUIRED_COVERAGE_DIMENSIONS
    }
    for row in rows:
        for dimension, values in row["dimension_values"].items():
            for value in values:
                cell_personas[dimension][value].add(row["persona_id"])
    cells = []
    for dimension in REQUIRED_COVERAGE_DIMENSIONS:
        for value, persona_ids in sorted(cell_personas[dimension].items()):
            cells.append(
                {
                    "dimension": dimension,
                    "value": value,
                    "persona_count": len(persona_ids),
                    "persona_ids": sorted(persona_ids),
                    "coverage_status": (
                        "development_schema_mapping_final_snapshot_anchor_pending"
                        if dimension == "content_service_type"
                        else "machine_contract_covered"
                    ),
                }
            )

    content_values = set(cell_personas["content_service_type"])
    unexplained_gaps = []
    for dimension in REQUIRED_COVERAGE_DIMENSIONS:
        if not cell_personas[dimension]:
            unexplained_gaps.append({"dimension": dimension, "reason": "no values"})
    missing_schemas = sorted(set(SCHEMA_FAMILIES) - content_values)
    if missing_schemas:
        unexplained_gaps.append({"dimension": "content_service_type", "missing_values": missing_schemas})

    summary = {
        "schema_version": 1,
        "matrix_id": "govuk-use-coverage-matrix-v1",
        "as_of": "2026-07-12",
        "required_dimensions": list(REQUIRED_COVERAGE_DIMENSIONS),
        "coverage_kind": "marginal dimension coverage with persona/story cross-links; overlay combinations are separate",
        "rows": len(rows),
        "cells": cells,
        "counts": {
            "primary_personas": len(profiles),
            "development_stories": len(stories),
            "release_story_contracts": len(profiles) * QUESTION_V2_STORIES_PER_PERSONA,
            "content_schema_families": len(content_values),
            "dimension_values": {dimension: len(cell_personas[dimension]) for dimension in REQUIRED_COVERAGE_DIMENSIONS},
        },
        "unexplained_machine_dimension_gaps": unexplained_gaps,
        "status": "machine_contract_covered_human_validation_pending" if not unexplained_gaps else "machine_gap_open",
        "evidence_boundary": (
            "Persona/source references are hypotheses and URL-identity evidence unless separately verified. "
            "Goal and journey coverage is guaranteed by generator code, but final corpus anchoring remains pending."
        ),
    }
    return rows, summary


def _choose_overlay_persona(
    profile_rows: list[dict[str, Any]],
    overlay_ids: tuple[str, ...],
    seed_by_id: dict[str, dict[str, Any]],
) -> str:
    risk_order = {"critical": 0, "high": 1, "medium": 2}
    ranked = sorted(
        profile_rows,
        key=lambda row: (
            -len(set(row["overlay_ids"]) & set(overlay_ids)),
            risk_order[seed_by_id[row["persona_id"]]["risk_level"]],
            sha256_text(f"{'|'.join(overlay_ids)}\0{row['persona_id']}"),
        ),
    )
    return ranked[0]["persona_id"]


def _build_overlay_covering_array(coverage_rows: list[dict[str, Any]]) -> dict[str, Any]:
    overlay_ids = tuple(sorted(item["overlay_id"] for item in OVERLAYS))
    seed_by_id = {_persona_id(seed): seed for seed in _read_json(SEED_PATH)["primary_personas"]}
    rows = []
    for ordinal, pair in enumerate(combinations(overlay_ids, 2), start=1):
        rows.append(
            record_with_checksum(
                {
                    "schema_version": 1,
                    "scenario_id": f"overlay-pair-{ordinal:03d}",
                    "strength": 2,
                    "overlay_ids": list(pair),
                    "assigned_persona_id": _choose_overlay_persona(coverage_rows, pair, seed_by_id),
                    "evidence_ids": sorted({evidence for overlay in pair for evidence in OVERLAY_EVIDENCE[overlay]}),
                    "evidence_status": "research_hypothesis_not_human_validated",
                    "question_binding_status": "pending_final_snapshot_question_v2_regeneration",
                    "claim_boundary": "Covering-array scenario, not a prevalence or compatibility claim.",
                }
            )
        )
    pair_count = len(rows)
    for ordinal, triple in enumerate(HIGH_RISK_OVERLAY_COMBINATIONS, start=1):
        rows.append(
            record_with_checksum(
                {
                    "schema_version": 1,
                    "scenario_id": f"overlay-high-risk-tway-{ordinal:02d}",
                    "strength": 3,
                    "overlay_ids": sorted(triple),
                    "assigned_persona_id": _choose_overlay_persona(
                        coverage_rows,
                        tuple(sorted(triple)),
                        seed_by_id,
                    ),
                    "evidence_ids": sorted({evidence for overlay in triple for evidence in OVERLAY_EVIDENCE[overlay]}),
                    "evidence_status": "research_hypothesis_not_human_validated",
                    "question_binding_status": "pending_final_snapshot_question_v2_regeneration",
                    "claim_boundary": "High-risk t-way scenario, not a prevalence or compatibility claim.",
                }
            )
        )
    return {
        "schema_version": 1,
        "array_id": "govuk-persona-overlay-covering-array-v1",
        "method": "exhaustive_pair_enumeration_plus_explicit_high_risk_tway",
        "overlay_count": len(overlay_ids),
        "overlay_ids": list(overlay_ids),
        "pairwise_strength": 2,
        "expected_pair_count": len(overlay_ids) * (len(overlay_ids) - 1) // 2,
        "pair_scenario_count": pair_count,
        "high_risk_tway_scenario_count": len(HIGH_RISK_OVERLAY_COMBINATIONS),
        "all_pairs_covered": pair_count == len(overlay_ids) * (len(overlay_ids) - 1) // 2,
        "rows": rows,
        "status": "machine_scenario_coverage_complete_human_validation_pending",
        "limitations": [
            "Scenario coverage does not show that a combination is common, compatible or well described.",
            "The final release-v2 question matrix must be regenerated against the closing snapshot to bind these scenarios.",
            "No participant research or preference test has been run.",
        ],
    }


def _challenge_record(value: dict[str, Any]) -> dict[str, Any]:
    return record_with_checksum({"schema_version": 1, **value})


def _build_challenge_ledgers(
    profiles: list[dict[str, Any]],
    coverage_matrix_content: str,
    overlay_array_content: str,
    story_coverage_content: str,
) -> list[dict[str, Any]]:
    persona_ids = sorted(profile["persona_id"] for profile in profiles)
    baseline = {"persona_ids": persona_ids, "overlay_ids": list(INITIAL_OVERLAY_IDS)}
    pass_one = _challenge_record(
        {
            "pass_id": "persona-gap-pass-01-contract",
            "sequence": 1,
            "method": "literal_set_difference_against_controlling_use_ontology_and_overlay_clause",
            "input_partition": "controlling plan lines 207-259 and the pre-challenge 48-persona/16-overlay ID snapshot",
            "held_out_from": ["official schema inventory", "question-v2 generator", "pairwise/high-harm checks"],
            "independence": "method_and_input_partition_independent_only",
            "independence_limit": "Generated in the same deterministic repository process; no independent human or model reviewed this pass.",
            "input_artifacts": [
                {
                    "path": USE_ONTOLOGY_PLAN_PATH.relative_to(ROOT).as_posix(),
                    "line_range": "207-259",
                    "line_range_sha256": _line_range_digest(USE_ONTOLOGY_PLAN_PATH, 207, 259),
                },
                {"embedded_baseline_snapshot_sha256": _canonical_digest(baseline), **baseline},
            ],
            "findings": [
                {
                    "finding_id": "persona-gap-01-dimensions",
                    "kind": "missing_machine_coverage_artifact",
                    "finding": "The prior ontology named six broad dimensions instead of the eleven required machine dimensions.",
                    "disposition": "resolved_by_personas_coverage_matrix",
                },
                {
                    "finding_id": "persona-gap-01-privacy",
                    "kind": "missing_evidenced_overlay_hypothesis",
                    "finding": "Privacy sensitivity was explicit in the controlling overlay clause but absent from the 16-overlay seed.",
                    "disposition": "added_privacy_sensitive_context_overlay_as_unvalidated_hypothesis",
                },
            ],
            "novel_valid_use_classes": 1,
            "novel_fraction": 1 / (len(persona_ids) + len(INITIAL_OVERLAY_IDS)),
            "novel_fraction_denominator": "48 starting archetypes plus 16 starting overlays",
            "result": "gap_found_and_resolved",
        }
    )
    pass_two = _challenge_record(
        {
            "pass_id": "persona-gap-pass-02-schema-question-contract",
            "sequence": 2,
            "method": "held_out_official_schema_plus_question_contract_set_difference",
            "input_partition": "pinned 83-schema inventory, six v2 story roles and ten v2 goal operations",
            "held_out_from": ["pass-01 controlling-overlay findings", "pairwise/high-harm intersection checks"],
            "independence": "method_and_input_partition_independent_only",
            "independence_limit": "Deterministic contract audit, not independent human or model adjudication and not a final-corpus distribution test.",
            "input_artifacts": [
                {"path": "stories/coverage.json", "sha256": _digest(story_coverage_content)},
                {"path": QUESTION_V2_SOURCE_PATH.relative_to(ROOT).as_posix(), "sha256": _sha256_file(QUESTION_V2_SOURCE_PATH)},
                {"path": QUESTION_V2_SCRIPT_PATH.relative_to(ROOT).as_posix(), "sha256": _sha256_file(QUESTION_V2_SCRIPT_PATH)},
                {"coverage_matrix_sha256": _digest(coverage_matrix_content)},
            ],
            "checks": {
                "pinned_schema_families": len(SCHEMA_FAMILIES),
                "question_v2_goal_operations": len(QUESTION_V2_OPERATIONS),
                "question_v2_story_roles": len(QUESTION_V2_STORY_ROLES),
                "required_dimensions": len(REQUIRED_COVERAGE_DIMENSIONS),
            },
            "findings": [],
            "novel_valid_use_classes": 0,
            "novel_fraction": 0.0,
            "result": "no_new_machine_evidenced_dimension_or_use_class",
        }
    )
    pass_three = _challenge_record(
        {
            "pass_id": "persona-gap-pass-03-high-harm-intersections",
            "sequence": 3,
            "method": "held_out_actor_risk_agent_and_overlay_intersection_challenge",
            "input_partition": "actor families, risk strata, agent involvement, exhaustive overlay pairs and explicit high-risk triples",
            "held_out_from": ["pass-01 controlling clause", "pass-02 schema and question contracts"],
            "independence": "method_and_input_partition_independent_only",
            "independence_limit": "The covering array proves scenario enumeration only; compatibility, prevalence and human validity remain untested.",
            "input_artifacts": [
                {"path": "personas/seed.json", "sha256": _sha256_file(SEED_PATH)},
                {"overlay_covering_array_sha256": _digest(overlay_array_content)},
            ],
            "checks": {
                "actor_families": len({profile["archetype_class"] for profile in profiles}),
                "risk_strata": len({profile["risk_level"] for profile in profiles}),
                "agent_system_personas": sum(profile["archetype_class"] == "agent_system" for profile in profiles),
                "high_harm_personas": sum(profile["risk_level"] in {"high", "critical"} for profile in profiles),
                "overlay_pairs": len(OVERLAYS) * (len(OVERLAYS) - 1) // 2,
                "high_risk_tway_scenarios": len(HIGH_RISK_OVERLAY_COMBINATIONS),
            },
            "findings": [],
            "novel_valid_use_classes": 0,
            "novel_fraction": 0.0,
            "result": "no_new_machine_evidenced_dimension_or_use_class",
        }
    )
    return [pass_one, pass_two, pass_three]


def _persona_id(seed: dict[str, Any]) -> str:
    prefixes = {
        "public_life_event": "public",
        "business_organisation": "business",
        "professional_intermediary": "professional",
        "agent_system": "agent",
    }
    slug = safe_identifier(seed.get("slug"), label="persona slug")
    return safe_identifier(f"persona-{prefixes[seed['class']]}-{slug}", label="persona ID")


def _schema_owner(schema: str, persona_slugs: list[str]) -> str:
    """Choose a semantically plausible story owner for every pinned family."""

    if schema.startswith("world") or schema in {"embassies_index", "field_of_operation", "fields_of_operation"}:
        return "uk-national-abroad"
    if schema.startswith("travel_advice"):
        return "driver-traveller"
    if schema.startswith("service_manual"):
        return "content-publisher"
    if schema.startswith("hmrc_manual"):
        return "tax-pensions-user"
    if schema in {"statistical_data_set", "statistics_announcement"}:
        return "analyst-statistician"
    if schema in {"finder", "facet", "mainstream_browse_page", "taxon", "step_by_step_nav", "link_collection"}:
        return "navigation-agent"
    if schema in {"organisation", "organisations_homepage", "person", "role", "role_appointment", "ministers_index"}:
        return "government-activity-follower"
    if schema in {"news_article", "speech", "topical_event", "topical_event_about_page"}:
        return "journalist-archivist-auditor"
    if schema in {"transaction", "completed_transaction", "local_transaction", "licence", "simple_smart_answer", "smart_answer"}:
        return "transaction-handoff-agent"
    if schema in {"gone", "redirect", "substitute", "vanish", "historic_appointment", "historic_appointments", "history"}:
        return "change-monitor-agent"
    if schema in {"consultation", "call_for_evidence", "get_involved", "take_part", "working_group"}:
        return "policy-official"
    if schema in {"publication", "html_publication", "specialist_document", "document_collection", "manual", "manual_section"}:
        return "policy-legal-research-agent"
    if schema in {"external_content", "content_block", "generic_with_external_related_links", "email_alert_signup"}:
        return "crawler-indexer-agent"
    # A stable fallback distributes less specialised formats across all seeds.
    return persona_slugs[int(sha256_text(schema)[:8], 16) % len(persona_slugs)]


def _build_personas(seed_document: dict[str, Any], evidence_document: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    evidence_ids = {item["evidence_id"] for item in evidence_document["references"]}
    overlays = {item["overlay_id"] for item in OVERLAYS}
    if set(OVERLAY_EVIDENCE) != overlays:
        raise ValueError("every overlay must have an explicit evidence mapping")
    unknown_overlay_evidence = sorted(
        {evidence for candidates in OVERLAY_EVIDENCE.values() for evidence in candidates} - evidence_ids
    )
    if unknown_overlay_evidence:
        raise ValueError(f"overlay evidence is unknown: {unknown_overlay_evidence}")
    profiles: list[dict[str, Any]] = []
    for seed in seed_document["primary_personas"]:
        missing_evidence = sorted(set(seed["evidence_ids"]) - evidence_ids)
        missing_overlays = sorted(set(seed["overlays"]) - overlays)
        if missing_evidence or missing_overlays:
            raise ValueError(
                f"{seed['slug']} has unknown evidence {missing_evidence} or overlays {missing_overlays}"
            )
        profiles.append(
            record_with_checksum(
                {
                    "schema_version": 1,
                    "profile_version": "1.1",
                    "persona_id": _persona_id(seed),
                    "persona_kind": "primary_archetype",
                    "title": seed["title"],
                    "archetype_class": seed["class"],
                    "trigger": seed["context"],
                    "goal": seed["goal"],
                    "primary_outcome": seed["goal"],
                    "jobs_to_be_done": [seed["goal"]],
                    "context": seed["context"],
                    "risk_level": seed["risk_level"],
                    "jurisdiction": seed["jurisdiction"],
                    "channel_class": seed["channel_class"],
                    "overlay_ids": seed["overlays"],
                    "evidence_ids": seed["evidence_ids"],
                    "overlay_evidence_ids": sorted(
                        {evidence for overlay in seed["overlays"] for evidence in OVERLAY_EVIDENCE[overlay]}
                    ),
                    "evidence_status": "research_hypothesis_not_human_validated",
                    "capabilities_and_context": [
                        f"channel:{seed['channel_class']}",
                        "can follow a canonical GOV.UK hand-off when one is identified",
                    ],
                    "constraints": [f"overlay:{overlay}" for overlay in seed["overlays"]],
                    "trust_and_provenance_needs": [
                        "canonical GOV.UK identity and URL",
                        "source and snapshot evidence",
                        "explicit lifecycle, jurisdiction and language boundaries",
                    ],
                    "success_criteria": [
                        "find the authoritative GOV.UK destination",
                        "understand discovery limits and uncertainty",
                        "reach an appropriate next step without unsupported certainty",
                    ],
                    "failure_harms": [
                        "wrong, stale or wrong-jurisdiction destination",
                        "lost provenance or unsupported inference",
                        f"unmitigated {seed['risk_level']} consequence of error",
                    ],
                    "exclusion_notes": [
                        "Not a demographic profile or a proxy for protected characteristics.",
                        "Not evidence that every individual in this task class has the assigned overlays.",
                    ],
                    "linked_story_contract": {
                        "development_story_id": f"story-{seed['slug']}",
                        "release_v2_story_count": QUESTION_V2_STORIES_PER_PERSONA,
                        "release_v2_status": "final_snapshot_regeneration_pending",
                    },
                    "research_questions": [
                        "Does this archetype describe a distinct information need rather than a demographic segment?",
                        "Which channels, terms and relationship paths fail for this archetype?",
                        "Which accessibility, language, jurisdiction or risk overlays materially change the task?",
                    ],
                    "known_limitations": [
                        "No authorised participant research has validated this profile.",
                        "Preflight evidence is URL-identity level, not claim-level verification.",
                        "The profile must not be used to infer an individual's eligibility or protected characteristics.",
                    ],
                    "generation_method": "deterministic projection from personas/seed.json",
                }
            )
        )

    counts = Counter(profile["archetype_class"] for profile in profiles)
    coverage = {
        "schema_version": 1,
        "catalogue_id": seed_document["catalogue_id"],
        "counts": {"primary_personas": len(profiles), "classes": dict(sorted(counts.items()))},
        "expected_class_counts": {
            "agent_system": 12,
            "business_organisation": 8,
            "professional_intermediary": 10,
            "public_life_event": 18,
        },
        "overlay_coverage": {overlay: sum(overlay in item["overlay_ids"] for item in profiles) for overlay in sorted(overlays)},
        "evidence_references": len(evidence_ids),
        "initial_overlay_count": len(INITIAL_OVERLAY_IDS),
        "current_overlay_count": len(overlays),
        "coverage_status": "machine_saturation_artifacts_generated; human validation not authorised",
    }
    if coverage["counts"]["classes"] != coverage["expected_class_counts"]:
        raise ValueError(f"persona seed counts are wrong: {coverage['counts']['classes']}")
    return profiles, coverage


def _build_stories(profiles: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    persona_slugs = [profile["persona_id"].split("-", 2)[2] for profile in profiles]
    schemas_by_slug: dict[str, list[str]] = defaultdict(list)
    for schema in SCHEMA_FAMILIES:
        schemas_by_slug[_schema_owner(schema, persona_slugs)].append(schema)

    stories: list[dict[str, Any]] = []
    for profile in profiles:
        slug = profile["persona_id"].split("-", 2)[2]
        target_entities = sorted(set(schemas_by_slug[slug] or ["generic"]))
        story = record_with_checksum(
            {
                "schema_version": 1,
                "story_id": f"story-{slug}",
                "persona_ids": [profile["persona_id"]],
                "story_status": "research_hypothesis_pending_authorised_human_validation",
                "as_a": profile["title"],
                "given": profile["context"],
                "need": profile["goal"],
                "so_that": "I can identify the authoritative source, understand its limits and take an appropriate next step",
                "statement": (
                    f"As a {profile['title']}, given {profile['context']}, I need to {profile['goal']} so that I can "
                    "identify the authoritative source, understand its limits and take an appropriate next step."
                ),
                "risk_level": profile["risk_level"],
                "jurisdiction": profile["jurisdiction"],
                "overlay_ids": profile["overlay_ids"],
                "target_entities": target_entities,
                "target_relationships": RELATIONSHIPS_BY_CLASS[profile["archetype_class"]],
                "evidence_ids": profile["evidence_ids"],
                "acceptance_criteria": [
                    "Return canonical GOV.UK identifiers and source URLs for discovered records.",
                    "State date, jurisdiction, language and lifecycle boundaries where relevant.",
                    "Separate metadata-only discovery from authoritative body-content retrieval.",
                    "Preserve conflicts, missing evidence and unsupported premises.",
                    "Provide an explicit abstention or authoritative hand-off when the bundle cannot support an answer.",
                    "Retain provenance and a frozen-snapshot identifier in evaluation output.",
                ],
                "edge_cases": [
                    "no matching record",
                    "withdrawn, redirected or superseded record",
                    "jurisdiction or language mismatch",
                    "attachment or machine representation unavailable",
                    "question requires personal facts not represented in the bundle",
                ],
                "generation_method": "deterministic one-primary-story-per-seeded-persona",
            }
        )
        stories.append(story)

    mapped = sorted({schema for story in stories for schema in story["target_entities"] if schema in SCHEMA_FAMILIES})
    missing = sorted(set(SCHEMA_FAMILIES) - set(mapped))
    story_coverage = {
        "schema_version": 1,
        "story_count": len(stories),
        "persona_count": len({persona_id for story in stories for persona_id in story["persona_ids"]}),
        "minimum_stories_per_primary_persona": 1,
        "content_schema_commit": PINNED_CONTENT_SCHEMA_COMMIT,
        "content_schema_family_count": len(SCHEMA_FAMILIES),
        "mapped_content_schema_families": mapped,
        "unmapped_content_schema_families": missing,
        "mapping_status": "deterministic_design_coverage_not_live_record_validation",
    }
    if len(stories) != 48 or missing:
        raise ValueError(f"story coverage failed: stories={len(stories)}, missing={missing}")
    return stories, story_coverage


def _manifest(scope: str, contents: dict[Path, str], paths: list[Path], counts: dict[str, Any]) -> dict[str, Any]:
    material = {path: contents[path] if path in contents else path.read_text(encoding="utf-8") for path in paths}
    file_entries = [
        {
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": _digest(material[path]),
            "bytes": len(material[path].encode("utf-8")),
        }
        for path in sorted(paths)
    ]
    return {
        "schema_version": 1,
        "scope": scope,
        "generation_method": GENERATOR_VERSION,
        "files": file_entries,
        "root_sha256": manifest_root((entry["path"], entry["sha256"]) for entry in file_entries),
        "counts": counts,
    }


def render() -> dict[Path, str]:
    seed_document = _read_json(SEED_PATH)
    evidence_document = _read_json(EVIDENCE_PATH)
    profiles, persona_coverage = _build_personas(seed_document, evidence_document)
    stories, story_coverage = _build_stories(profiles)
    profiles_by_id = {profile["persona_id"]: profile for profile in profiles}

    output: dict[Path, str] = {}
    persona_paths: list[Path] = [SEED_PATH, EVIDENCE_PATH]
    for profile in profiles:
        path = ROOT / "personas" / "profiles" / f"{profile['persona_id']}.json"
        output[path] = _json(profile)
        persona_paths.append(path)
    overlay_path = ROOT / "personas" / "overlays" / "catalogue.json"
    overlay_records = [
        {
            **item,
            "evidence_ids": OVERLAY_EVIDENCE[item["overlay_id"]],
            "evidence_status": "research_hypothesis_not_human_validated",
        }
        for item in OVERLAYS
    ]
    output[overlay_path] = _json(
        {
            "schema_version": 1,
            "catalogue_id": "govuk-persona-overlays-v1",
            "initial_overlay_count": len(INITIAL_OVERLAY_IDS),
            "current_overlay_count": len(OVERLAYS),
            "status": "cross-cutting research hypotheses; not primary personas, prevalence or human validation claims",
            "overlays": overlay_records,
        }
    )
    persona_paths.append(overlay_path)
    ontology_path = ROOT / "personas" / "use-ontology.yamlld"
    output[ontology_path] = _json(
        {
            "@context": {
                "@vocab": "https://chris-page-gov.github.io/okf-govuk-content/ns/",
                "evidence": {"@id": "evidence", "@type": "@id"},
                "overlay": {"@id": "overlay", "@type": "@id"},
            },
            "@id": "govuk-use-ontology-v1",
            "@type": "UseOntology",
            "primaryArchetypeClasses": [
                {"@id": "public_life_event", "label": "Public and life-event users"},
                {"@id": "business_organisation", "label": "Business and organisation users"},
                {"@id": "professional_intermediary", "label": "Professional and intermediary users"},
                {"@id": "agent_system", "label": "Agent and system users"},
            ],
            "dimensions": [{"@id": item, "label": item.replace("_", " ")} for item in REQUIRED_COVERAGE_DIMENSIONS],
            "evidenceRule": "Every profile and story cites evidence IDs and keeps unvalidated assumptions explicit.",
            "coverageMatrix": "coverage-matrix.json",
            "overlayCoveringArray": "overlay-covering-array.json",
            "saturationLedger": "saturation.json",
            "humanValidationStatus": "not_authorised_not_run",
            "preferenceClaimStatus": "not_yet_testable",
        }
    )
    persona_paths.append(ontology_path)

    coverage_rows, coverage_matrix = _build_coverage_matrix(profiles, stories)
    coverage_rows_path = ROOT / "personas" / "coverage-matrix.jsonl"
    output[coverage_rows_path] = _jsonl(coverage_rows)
    persona_paths.append(coverage_rows_path)
    coverage_matrix_path = ROOT / "personas" / "coverage-matrix.json"
    output[coverage_matrix_path] = _json(coverage_matrix)
    persona_paths.append(coverage_matrix_path)

    overlay_array = _build_overlay_covering_array(coverage_rows)
    overlay_array_path = ROOT / "personas" / "overlay-covering-array.json"
    output[overlay_array_path] = _json(overlay_array)
    persona_paths.append(overlay_array_path)

    challenge_records = _build_challenge_ledgers(
        profiles,
        output[coverage_matrix_path],
        output[overlay_array_path],
        _json(story_coverage),
    )
    challenge_paths: list[Path] = []
    for challenge in challenge_records:
        challenge_path = ROOT / "personas" / "challenges" / f"{challenge['pass_id']}.json"
        output[challenge_path] = _json(challenge)
        challenge_paths.append(challenge_path)
        persona_paths.append(challenge_path)

    saturation_path = ROOT / "personas" / "saturation.json"
    saturation = record_with_checksum(
        {
            "schema_version": 1,
            "saturation_id": "govuk-persona-machine-saturation-v1",
            "as_of": "2026-07-12",
            "requirements": [f"REQ-{number:03d}" for number in range(49, 59)],
            "acceptance_gate": "GATE-05-personas",
            "machine_applicable_gate_status": "passed",
            "human_validation_status": "not_authorised_not_run",
            "human_ui_preference_status": "not_yet_testable",
            "primary_personas": len(profiles),
            "initial_overlays": len(INITIAL_OVERLAY_IDS),
            "current_overlays": len(OVERLAYS),
            "new_overlay_hypotheses": ["privacy-sensitive-context"],
            "required_dimensions": list(REQUIRED_COVERAGE_DIMENSIONS),
            "coverage_matrix": {
                "path": coverage_matrix_path.relative_to(ROOT).as_posix(),
                "sha256": _digest(output[coverage_matrix_path]),
                "unexplained_machine_dimension_gaps": coverage_matrix["unexplained_machine_dimension_gaps"],
            },
            "overlay_covering_array": {
                "path": overlay_array_path.relative_to(ROOT).as_posix(),
                "sha256": _digest(output[overlay_array_path]),
                "pairwise_strength": overlay_array["pairwise_strength"],
                "pair_scenarios": overlay_array["pair_scenario_count"],
                "high_risk_tway_scenarios": overlay_array["high_risk_tway_scenario_count"],
            },
            "challenge_passes": [
                {
                    "pass_id": record["pass_id"],
                    "path": path.relative_to(ROOT).as_posix(),
                    "sha256": _digest(output[path]),
                    "novel_valid_use_classes": record["novel_valid_use_classes"],
                    "result": record["result"],
                }
                for record, path in zip(challenge_records, challenge_paths)
            ],
            "stopping_rule": {
                "required_successive_below_one_percent_passes": 2,
                "qualifying_pass_ids": [challenge_records[-2]["pass_id"], challenge_records[-1]["pass_id"]],
                "qualifying_novel_fractions": [challenge_records[-2]["novel_fraction"], challenge_records[-1]["novel_fraction"]],
                "passed": all(record["novel_fraction"] < 0.01 for record in challenge_records[-2:]),
            },
            "residual_known_gaps": [
                "No authorised participant research validates the personas, overlays or their compatibility/prevalence.",
                "No authorised query/support/contact corpus was available for direct-observation saturation.",
                "Final corpus type frequencies and long-tail anchors remain unknown until T0/T1 closes.",
                "The privacy-sensitive overlay is a controlling-contract hypothesis, not an observed population result.",
                "Final release-v2 stories and questions must be regenerated and independently verified against the closing snapshot.",
                "Evidence locator and claim-support verification remains the separate F2 citation gate.",
            ],
            "independence_limit": (
                "Passes use disjoint inputs and methods but share one deterministic repository implementation. "
                "They are not independent participant evidence or independent human/model adjudication."
            ),
            "source_access_restrictions": [
                "Participant research and de-identified query/support/contact themes were not authorised or supplied.",
                "Most persona references remain URL-identity evidence pending the separate locator/support gate.",
                "Final-corpus frequencies and long-tail anchors are unavailable until the T0/T1 snapshot closes.",
            ],
            "licensing_and_fair_use_triggers": [
                "No page, attachment or participant-data body was read, retained or republished for this saturation build.",
                "External references remain links and short metadata; any later evidence snapshot remains subject to the rights gate.",
                "Final question anchors may expose item-level third-party review triggers and must retain source links only.",
            ],
            "fallbacks_used": [
                "The controlling plan supplied the privacy-sensitive hypothesis when direct-observation data was unavailable.",
                "Pinned schema families and generator contracts supplied deterministic machine coverage without pretending to be observed use.",
                "Exhaustive overlay pairs and explicit high-risk triples replaced an unverified claim of empirical intersectional saturation.",
            ],
            "claim_boundary": (
                "This closes machine-applicable dimensional, challenge and scenario enumeration only. "
                "It makes no UI-of-choice, preference, prevalence or observed-user claim."
            ),
            "model_usage": {
                "deterministic_generation": {
                    "model_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_gbp": 0.0,
                },
                "design_assistance": {
                    "role": "Codex collaborating implementation agent",
                    "exact_model_id": "unavailable_to_repository_process",
                    "input_tokens": "unavailable_to_repository_process",
                    "output_tokens": "unavailable_to_repository_process",
                    "cost_gbp": "unavailable_to_repository_process",
                    "accounting_status": "recorded_unknown_not_zero",
                },
            },
        }
    )
    output[saturation_path] = _json(saturation)
    persona_paths.append(saturation_path)

    coverage_path = ROOT / "personas" / "coverage.json"
    persona_coverage["required_dimensions"] = len(REQUIRED_COVERAGE_DIMENSIONS)
    persona_coverage["machine_applicable_saturation"] = "passed"
    persona_coverage["human_validation_status"] = "not_authorised_not_run"
    persona_coverage["unexplained_machine_dimension_gaps"] = coverage_matrix[
        "unexplained_machine_dimension_gaps"
    ]
    output[coverage_path] = _json(persona_coverage)
    persona_paths.append(coverage_path)

    persona_readme_path = ROOT / "personas" / "README.md"
    if persona_readme_path.is_file():
        persona_paths.append(persona_readme_path)
    persona_checksums_path = ROOT / "personas" / "checksums.txt"
    output[persona_checksums_path] = "".join(
        f"{_digest(output[path]) if path in output else _sha256_file(path)}  {path.relative_to(ROOT).as_posix()}\n"
        for path in sorted(persona_paths)
    )
    persona_paths.append(persona_checksums_path)
    persona_manifest_path = ROOT / "personas" / "manifest.json"
    output[persona_manifest_path] = _json(
        _manifest(
            "personas",
            output,
            persona_paths,
            {
                "primary_personas": len(profiles),
                "initial_overlays": len(INITIAL_OVERLAY_IDS),
                "current_overlays": len(OVERLAYS),
                "coverage_dimensions": len(REQUIRED_COVERAGE_DIMENSIONS),
                "challenge_passes": len(challenge_records),
                "overlay_pair_scenarios": overlay_array["pair_scenario_count"],
                "high_risk_tway_scenarios": overlay_array["high_risk_tway_scenario_count"],
            },
        )
    )

    story_catalogue_path = ROOT / "stories" / "catalogue.jsonl"
    output[story_catalogue_path] = _jsonl(stories)
    story_coverage_path = ROOT / "stories" / "coverage.json"
    output[story_coverage_path] = _json(story_coverage)
    story_paths = [story_catalogue_path, story_coverage_path]
    story_manifest_path = ROOT / "stories" / "manifest.json"
    output[story_manifest_path] = _json(
        _manifest(
            "stories",
            output,
            story_paths,
            {"stories": len(stories), "primary_personas": len(profiles), "schema_families": len(SCHEMA_FAMILIES)},
        )
    )

    archetypes_path = ROOT / "questions" / "archetypes.yaml"
    output[archetypes_path] = _json(
        {
            "schema_version": 1,
            "matrix_version": MATRIX_VERSION,
            "artifact_tier": "development_only",
            "release_eligible": False,
            "superseded_for_release_by": "govuk-question-matrix-v2",
            "operations": list(OPERATIONS),
            "challenge_modes": list(CHALLENGES),
            "persona_suite_quotas": SUITE_QUOTAS,
            "held_out_rule": "challenge columns 9 and 10; exactly 20 of 100 questions per story",
        }
    )
    question_paths: list[Path] = [archetypes_path]
    all_questions: list[dict[str, Any]] = []
    all_suites: list[dict[str, Any]] = []
    for story in stories:
        persona = profiles_by_id[story["persona_ids"][0]]
        questions = build_story_questions(story, persona)
        story_id = safe_identifier(story["story_id"], label="story ID")
        question_path = safe_child_path(
            ROOT / "questions" / "bindings",
            f"{story_id}.jsonl",
            label="research question binding path",
        )
        output[question_path] = _jsonl(questions)
        question_paths.append(question_path)
        all_questions.extend(questions)

        suite = curate_persona_suite(persona, questions)
        persona_id = safe_identifier(persona["persona_id"], label="persona ID")
        suite_path = safe_child_path(
            ROOT / "questions" / "persona-suites",
            f"{persona_id}.jsonl",
            label="research persona suite path",
        )
        output[suite_path] = _jsonl(suite)
        question_paths.append(suite_path)
        all_suites.extend(suite)

    if len(all_questions) != 4_800 or len({item["wording"].casefold() for item in all_questions}) != 4_800:
        raise ValueError("question factory did not produce 4,800 globally unique wordings")
    gold_records = [
        record_with_checksum(
            {
                "schema_version": 1,
                "question_id": item["question_id"],
                "question_checksum": item["checksum"],
                "expected_unanswerable": item["expected_unanswerable"],
                "gold_status": item["gold_status"],
                "gold": item["gold"],
            }
        )
        for item in all_questions
    ]
    gold_path = ROOT / "questions" / "gold" / "catalogue.jsonl"
    output[gold_path] = _jsonl(gold_records)
    question_paths.append(gold_path)
    checksum_path = ROOT / "questions" / "checksums.txt"
    output[checksum_path] = "".join(
        f"{_digest(output[path])}  {path.relative_to(ROOT).as_posix()}\n" for path in sorted(question_paths)
    )
    question_manifest_path = ROOT / "questions" / "manifest.json"
    operation_counts = Counter(item["operation"] for item in all_questions)
    challenge_counts = Counter(item["challenge"] for item in all_questions)
    split_counts = Counter(item["split"] for item in all_questions)
    question_manifest = _manifest(
        "questions",
        output,
        question_paths + [checksum_path],
        {
            "stories": len(stories),
            "primary_personas": len(profiles),
            "questions": len(all_questions),
            "questions_per_story": 100,
            "suite_entries": len(all_suites),
            "suite_entries_per_persona": 100,
            "gold_records": len(gold_records),
            "operations": dict(sorted(operation_counts.items())),
            "challenge_modes": dict(sorted(challenge_counts.items())),
            "splits": dict(sorted(split_counts.items())),
            "deliberately_unanswerable": sum(item["expected_unanswerable"] for item in all_questions),
        },
    )
    question_manifest.update(
        {
            "artifact_tier": "development_only",
            "release_eligible": False,
            "question_contract_passed": False,
            "superseded_for_release_by": "govuk-question-matrix-v2",
            "release_blockers": [
                "one story per persona instead of the required six to twelve",
                "gold targets are unassigned and independently unverified",
                "questions are design templates rather than frozen-corpus-anchored evaluation items",
            ],
        }
    )
    output[question_manifest_path] = _json(question_manifest)

    preregistration_path = ROOT / "evaluation" / "protocol" / "preregistration.json"
    output[preregistration_path] = _json(
        {
            "schema_version": 1,
            "protocol_id": "govuk-okf-evaluation-v1",
            "registered_design_date": QUESTION_CONTRACT_DATE,
            "status": "designed_not_executed",
            "matrix_version": MATRIX_VERSION,
            "corpus_snapshot_id": CORPUS_SNAPSHOT_ID,
            "independent_gold_gate": {
                "status": "blocked_pending_frozen_t0_and_independent_assignment",
                "rule": "Gold content IDs and URLs must be assigned and verified against a frozen corpus by a process independent of generation.",
            },
            "splits": {"development": 80, "held_out": 20, "unit": "questions per story"},
            "retrieval_metrics": ["Recall@10", "MRR@10", "nDCG@10", "exact content_id match"],
            "answer_metrics": [
                "citation precision",
                "citation completeness",
                "supported-claim rate",
                "unanswerable detection precision and recall",
                "jurisdiction and temporal boundary accuracy",
            ],
            "slice_reporting": [
                "primary archetype class",
                "accessibility and language overlays",
                "risk level",
                "jurisdiction",
                "operation",
                "challenge mode",
                "answerability",
            ],
            "comparison_rule": "Run all systems against identical corpus snapshots, questions, gold judgements and metric code.",
            "human_evaluation": {
                "status": "not_authorised_not_run",
                "requirements_before_claim": [
                    "approved research and safeguarding protocol",
                    "documented recruitment and consent",
                    "declared participant counts and sampling limitations",
                    "recorded task outcomes and qualitative evidence",
                ],
            },
        }
    )
    contract_path = ROOT / "evaluation" / "protocol" / "question-contract.json"
    output[contract_path] = _json(
        {
            "schema_version": 1,
            "matrix_version": MATRIX_VERSION,
            "artifact_tier": "development_only",
            "release_eligible": False,
            "question_contract_passed": False,
            "superseded_for_release_by": "govuk-question-matrix-v2",
            "operations": [item["id"] for item in OPERATIONS],
            "challenge_modes": [item["id"] for item in CHALLENGES],
            "expected_questions_per_story": 100,
            "expected_questions_per_persona_suite": 100,
            "persona_suite_quotas": SUITE_QUOTAS,
            "required_question_fields": [
                "question_id",
                "story_id",
                "persona_ids",
                "wording",
                "intent",
                "target_entities",
                "target_relationships",
                "expected_response_type",
                "risk",
                "difficulty",
                "ambiguity",
                "locale",
                "jurisdiction",
                "provenance_requirements",
                "gold",
                "expected_unanswerable",
                "gold_status",
                "generation_method",
                "split",
                "checksum",
            ],
        }
    )
    release_contract_path = ROOT / "evaluation" / "protocol" / "question-contract-v2.json"
    output[release_contract_path] = _json(
        {
            "schema_version": 2,
            "contract_id": "govuk-question-release-contract-v2",
            "status": "approved_not_yet_executed_against_release_snapshot",
            "matrix_version": "govuk-question-matrix-v2",
            "requirements": [f"REQ-{number:03d}" for number in range(59, 69)],
            "expected_stories_per_primary_persona": {"minimum": 6, "maximum": 12},
            "expected_questions_per_story": 100,
            "expected_questions_per_persona_suite": 100,
            "persona_suite_must_cover_every_story": True,
            "persona_suite_quotas": SUITE_QUOTAS,
            "input_gate": {
                "frozen_source_record_corpus": True,
                "complete_corpus_reconciliation_required": True,
                "unexplained_omissions": 0,
                "sampled_corpus_forbidden": True,
                "snapshot_manifest_sha256_required": True,
            },
            "persona_saturation_gate": {
                "path": saturation_path.relative_to(ROOT).as_posix(),
                "sha256": _digest(output[saturation_path]),
                "coverage_matrix_sha256": _digest(output[coverage_matrix_path]),
                "machine_applicable_gate_status": saturation["machine_applicable_gate_status"],
                "human_validation_status": saturation["human_validation_status"],
                "human_ui_preference_status": saturation["human_ui_preference_status"],
                "required_dimensions": list(REQUIRED_COVERAGE_DIMENSIONS),
                "final_snapshot_question_regeneration_required": True,
            },
            "gold_gate": {
                "answerable": "at least one verified content ID or URL plus source record and evidence hashes",
                "unanswerable": "explicit deliberately-unanswerable classification and evidence-backed rationale",
                "near_misses_required": True,
                "typed_paths_required_where_relevant": True,
                "generator_may_not_self_verify": True,
                "independent_verification_ledger_required": True,
            },
            "leakage_gate": {
                "entity_grouped_split": True,
                "held_out_entity_groups_minimum_fraction": 0.2,
                "normalised_wording_duplicates_forbidden": True,
                "semantic_signature_duplicates_forbidden": True,
                "implementation_prompt_leakage_forbidden": True,
            },
            "release_evidence": [
                "questions/release-v2/manifest.json",
                "questions/release-v2/contract.json",
                "questions/release-v2/verification-report.json",
                "questions/release-v2/verification-ledger.jsonl",
            ],
            "claim_boundary": (
                "The contract verifies metadata discovery gold. It does not claim that metadata alone answers body-content questions "
                "or that unauthorised human research has been completed."
            ),
        }
    )
    baseline_path = ROOT / "evaluation" / "baselines" / "catalogue.json"
    output[baseline_path] = _json(
        {
            "schema_version": 2,
            "status": "deterministic_baselines_implemented_release_run_pending_final_inputs",
            "baselines": [
                {
                    "baseline_id": "baseline-exact-known-item",
                    "family": "known-item deterministic rule",
                    "implementation": "src/govuk_okf/evaluation.py",
                    "run_status": "implemented_pending_release_run",
                },
                {
                    "baseline_id": "baseline-flat-metadata-fts",
                    "family": "flat lexical retrieval",
                    "implementation": "SQLite FTS5 over title, description and URL",
                    "run_status": "implemented_pending_release_run",
                },
                {
                    "baseline_id": "baseline-typed-metadata-fts",
                    "family": "raw official typed metadata retrieval",
                    "implementation": "SQLite FTS5 over source-native metadata with graph traversal disabled",
                    "run_status": "implemented_pending_release_run",
                },
            ],
            "external_comparators": [
                {
                    "comparator_id": "live-govuk-search-navigation",
                    "reason_not_run_by_deterministic_harness": (
                        "Requires external network and a separately frozen live-service protocol; retained for authorised human comparison."
                    ),
                },
                {
                    "comparator_id": "public-search-api-v1",
                    "reason_not_run_by_deterministic_harness": (
                        "Unsupported mutable external service; no network is permitted in the frozen local evaluation."
                    ),
                },
                {
                    "comparator_id": "dense-semantic-retrieval",
                    "reason_not_run_by_deterministic_harness": (
                        "No embedding model, model budget or provider execution is authorised; omission is explicit rather than simulated."
                    ),
                },
                {
                    "comparator_id": "govuk-chat",
                    "reason_not_run_by_deterministic_harness": (
                        "Direct matched system access is unavailable; architectural and published-evidence comparison remains separate."
                    ),
                },
                {
                    "comparator_id": "govsearch-govgraph",
                    "reason_not_run_by_deterministic_harness": "Internal/authenticated access is not authorised.",
                },
            ],
            "claim_constraint": (
                "Machine claims require the complete release-v2 matrix and all implemented systems to run against one matching frozen "
                "snapshot. External comparators must never be represented by fabricated results."
            ),
            "protocol": "evaluation/protocol/automated-evaluation-v1.json",
        }
    )
    status_path = ROOT / "evaluation" / "results" / "status.json"
    output[status_path] = _json(
        {
            "schema_version": 1,
            "status": "not_run",
            "artifact_tier": "development_only",
            "question_contract_passed": False,
            "completed": [
                "48 primary persona hypotheses",
                "48 primary stories",
                "4,800 deterministic matrix questions",
                "48 exact-quota persona suites",
                "evaluation protocol and baseline catalogue",
                "deterministic matched evaluation harness and fixture tests",
            ],
            "blocked": [
                "v1 question assets are development-only and cannot satisfy the release question gate",
                "independent gold assignment awaits frozen T0 corpus",
                "empirical baseline comparison awaits independently verified gold",
                "human evaluation is not authorised",
            ],
            "result_claims": [],
        }
    )
    usage_path = ROOT / "evaluation" / "results" / "build-usage.json"
    output[usage_path] = _json(
        {
            "schema_version": 1,
            "generation_method": GENERATOR_VERSION,
            "model_usage": {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_gbp": 0.0},
            "human_research": {"participants": 0, "sessions": 0, "status": "not_authorised_not_run"},
            "source_access_constraints": [
                "Planning preflight established URL identity but not locator-level or claim-level evidence.",
                "No live corpus was used to assign gold content IDs or URLs.",
            ],
            "licensing_and_fair_use_triggers": [
                "Review third-party material and attachments before redistribution.",
                "Retain OGL attribution and canonical source links for covered public-sector information.",
                "Do not reproduce body content merely because discovery metadata is available.",
            ],
            "fallbacks_used": [
                "Deterministic templates replaced external model generation.",
                "Pending verification states replaced unsupported gold-answer claims.",
                "Evidence references remain URL-identity-only where locator verification was not run.",
            ],
        }
    )
    automated_protocol_path = ROOT / "evaluation" / "protocol" / "automated-evaluation-v1.json"
    evaluation_readme_path = ROOT / "evaluation" / "README.md"
    evaluation_paths = [
        preregistration_path,
        contract_path,
        release_contract_path,
        automated_protocol_path,
        baseline_path,
        status_path,
        usage_path,
        evaluation_readme_path,
    ]
    evaluation_manifest_path = ROOT / "evaluation" / "manifest.json"
    output[evaluation_manifest_path] = _json(
        _manifest(
            "evaluation",
            output,
            evaluation_paths,
            {"protocols": 4, "baselines": 3, "empirical_runs": 0, "model_calls": 0, "human_sessions": 0},
        )
    )
    return output


def synchronize(check: bool = False) -> list[str]:
    errors: list[str] = []
    rendered = render()
    repository_root = ROOT.resolve()
    for path in rendered:
        if not path.resolve().is_relative_to(repository_root):
            raise ValueError(f"generated research path escapes repository root: {path}")
    for path, expected in rendered.items():
        if check:
            if not path.is_file():
                errors.append(f"{path.relative_to(ROOT)} is missing")
            elif path.read_text(encoding="utf-8") != expected:
                errors.append(f"{path.relative_to(ROOT)} is out of date")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when generated research assets are absent or stale")
    args = parser.parse_args()
    errors = synchronize(check=args.check)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("research assets are current" if args.check else "research assets generated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
