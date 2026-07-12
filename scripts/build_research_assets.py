#!/usr/bin/env python3
"""Build the deterministic persona, story, question and evaluation assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
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

SEED_PATH = ROOT / "personas" / "seed.json"
EVIDENCE_PATH = ROOT / "personas" / "evidence.json"
PINNED_CONTENT_SCHEMA_COMMIT = "b1e987aa7b3e62c105ff2b2db87667f7638726f8"

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
)

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


def _persona_id(seed: dict[str, Any]) -> str:
    prefixes = {
        "public_life_event": "public",
        "business_organisation": "business",
        "professional_intermediary": "professional",
        "agent_system": "agent",
    }
    return f"persona-{prefixes[seed['class']]}-{seed['slug']}"


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
                    "persona_id": _persona_id(seed),
                    "persona_kind": "primary_archetype",
                    "title": seed["title"],
                    "archetype_class": seed["class"],
                    "goal": seed["goal"],
                    "context": seed["context"],
                    "risk_level": seed["risk_level"],
                    "jurisdiction": seed["jurisdiction"],
                    "channel_class": seed["channel_class"],
                    "overlay_ids": seed["overlays"],
                    "evidence_ids": seed["evidence_ids"],
                    "evidence_status": "research_hypothesis_not_human_validated",
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
        "coverage_status": "seeded_hypotheses_complete; human validation not authorised",
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
    output[overlay_path] = _json(
        {
            "schema_version": 1,
            "catalogue_id": "govuk-persona-overlays-v1",
            "status": "cross-cutting test dimensions; not primary personas or prevalence claims",
            "overlays": list(OVERLAYS),
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
            "dimensions": [
                "goal and information need",
                "task and journey stage",
                "jurisdiction and language",
                "channel and access constraints",
                "risk and consequence of error",
                "provenance and structured-output requirements",
            ],
            "evidenceRule": "Every profile and story cites evidence IDs and keeps unvalidated assumptions explicit.",
        }
    )
    persona_paths.append(ontology_path)
    coverage_path = ROOT / "personas" / "coverage.json"
    output[coverage_path] = _json(persona_coverage)
    persona_paths.append(coverage_path)
    persona_manifest_path = ROOT / "personas" / "manifest.json"
    output[persona_manifest_path] = _json(
        _manifest(
            "personas",
            output,
            persona_paths,
            {"primary_personas": len(profiles), "overlays": len(OVERLAYS)},
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
        question_path = ROOT / "questions" / "bindings" / f"{story['story_id']}.jsonl"
        output[question_path] = _jsonl(questions)
        question_paths.append(question_path)
        all_questions.extend(questions)

        suite = curate_persona_suite(persona, questions)
        suite_path = ROOT / "questions" / "persona-suites" / f"{persona['persona_id']}.jsonl"
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
            "schema_version": 1,
            "status": "implementations_and_empirical_runs_pending_verified_t0",
            "baselines": [
                {"baseline_id": "canonical-url-exact", "family": "known-item rule", "run_status": "not_run"},
                {"baseline_id": "metadata-keyword", "family": "lexical retrieval", "run_status": "not_run"},
                {"baseline_id": "typed-relationship-walk", "family": "graph traversal", "run_status": "not_run"},
                {"baseline_id": "static-browser-search", "family": "OKF Explorer static search", "run_status": "not_run"},
            ],
            "claim_constraint": "No quality, latency or cost comparison may be published until all baselines run under the preregistered protocol.",
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
    evaluation_paths = [preregistration_path, contract_path, release_contract_path, baseline_path, status_path, usage_path]
    evaluation_manifest_path = ROOT / "evaluation" / "manifest.json"
    output[evaluation_manifest_path] = _json(
        _manifest(
            "evaluation",
            output,
            evaluation_paths,
            {"protocols": 3, "baselines": 4, "empirical_runs": 0, "model_calls": 0, "human_sessions": 0},
        )
    )
    return output


def synchronize(check: bool = False) -> list[str]:
    errors: list[str] = []
    for path, expected in render().items():
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
