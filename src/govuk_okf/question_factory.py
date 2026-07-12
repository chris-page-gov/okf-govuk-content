"""Deterministic persona-suite and 10 x 10 story-question construction."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any, Iterable

MATRIX_VERSION = "govuk-question-matrix-v1"
GENERATOR_VERSION = "deterministic-question-factory-v1"
CORPUS_SNAPSHOT_ID = "pending-corpus-t0"
QUESTION_CONTRACT_DATE = "2026-07-12"

OPERATIONS: tuple[dict[str, str], ...] = (
    {
        "id": "locate_known_item",
        "label": "locate a known item",
        "intent": "lookup",
        "answer_shape": "ranked canonical records",
        "stage": "metadata_only_discovery",
        "prompt": "which authoritative GOV.UK record should support the need to {need}",
    },
    {
        "id": "explain_define",
        "label": "explain or define",
        "intent": "explanation",
        "answer_shape": "record plus authoritative-source hand-off",
        "stage": "discovery_plus_authoritative_retrieval",
        "prompt": "how should the relevant GOV.UK material be found and explained so someone can {need}",
    },
    {
        "id": "determine_applicability",
        "label": "determine applicability",
        "intent": "applicability",
        "answer_shape": "applicability boundary with cited source",
        "stage": "discovery_plus_authoritative_retrieval",
        "prompt": "which official records establish whether the information needed to {need} applies in this context",
    },
    {
        "id": "follow_process",
        "label": "follow a process",
        "intent": "procedure",
        "answer_shape": "ordered source-backed next steps",
        "stage": "discovery_plus_authoritative_retrieval",
        "prompt": "what authoritative sequence of GOV.UK records should be followed to {need}",
    },
    {
        "id": "compare_decide",
        "label": "compare or decide",
        "intent": "comparison",
        "answer_shape": "comparison with decision boundaries",
        "stage": "discovery_plus_authoritative_retrieval",
        "prompt": "which GOV.UK records should be compared before deciding how to {need}",
    },
    {
        "id": "traverse_relationships",
        "label": "traverse hierarchy or relationships",
        "intent": "relationship_traversal",
        "answer_shape": "typed path of records and relationships",
        "stage": "metadata_only_discovery",
        "prompt": "what typed path through topics, organisations, collections or parts leads to material needed to {need}",
    },
    {
        "id": "verify_provenance",
        "label": "verify provenance or authority",
        "intent": "provenance",
        "answer_shape": "citation and provenance object",
        "stage": "metadata_only_discovery",
        "prompt": "how can the publisher, owning body and source evidence be verified before using material to {need}",
    },
    {
        "id": "check_lifecycle",
        "label": "check lifecycle, redirect or freshness",
        "intent": "lifecycle",
        "answer_shape": "current and historical lifecycle disposition",
        "stage": "metadata_only_discovery",
        "prompt": "which lifecycle, update, withdrawal, redirect or replacement evidence matters when trying to {need}",
    },
    {
        "id": "obtain_resource",
        "label": "obtain attachment, data or API representation",
        "intent": "resource_discovery",
        "answer_shape": "resource metadata and authoritative URL",
        "stage": "metadata_only_discovery",
        "prompt": "which attachment, dataset or machine representation is authoritative for the need to {need}",
    },
    {
        "id": "handle_ambiguity",
        "label": "handle ambiguity, delegation or no answer",
        "intent": "abstention_and_clarification",
        "answer_shape": "clarification, bounded result or explicit abstention",
        "stage": "metadata_only_discovery",
        "prompt": "how should the system clarify, narrow or abstain when asked to {need}",
    },
)

CHALLENGES: tuple[dict[str, Any], ...] = (
    {
        "id": "direct",
        "label": "direct, well formed",
        "clause": "Use a direct, well-formed request and return the current canonical result.",
        "difficulty": "easy",
        "ambiguous": False,
    },
    {
        "id": "novice_wording",
        "label": "novice or colloquial wording",
        "clause": "Use everyday wording and explain unfamiliar government labels without changing their meaning.",
        "difficulty": "medium",
        "ambiguous": False,
    },
    {
        "id": "noisy_wording",
        "label": "synonym, spelling or noisy wording",
        "clause": "Treat one imprecise synonym or spelling error as a search clue, and make the correction explicit.",
        "difficulty": "medium",
        "ambiguous": True,
    },
    {
        "id": "temporal",
        "label": "current versus historical",
        "clause": "Separate current material from historical, withdrawn or superseded material and state the date basis.",
        "difficulty": "hard",
        "ambiguous": False,
    },
    {
        "id": "jurisdiction",
        "label": "geography or jurisdiction",
        "clause": "Make the applicable nation, locality or overseas boundary explicit and do not generalise across it.",
        "difficulty": "hard",
        "ambiguous": False,
    },
    {
        "id": "language_access",
        "label": "language, access or connectivity",
        "clause": "Request a Welsh-language or accessible route where available and retain a low-bandwidth fallback.",
        "difficulty": "hard",
        "ambiguous": False,
    },
    {
        "id": "multi_hop",
        "label": "multi-hop",
        "clause": "Require at least two typed relationships and show every intermediate record.",
        "difficulty": "hard",
        "ambiguous": False,
    },
    {
        "id": "conflicting_evidence",
        "label": "conflicting or missing evidence",
        "clause": "Preserve conflicting or missing evidence, rank source authority and do not smooth the conflict away.",
        "difficulty": "hard",
        "ambiguous": True,
    },
    {
        "id": "structured_agent",
        "label": "structured agent request",
        "clause": "Return stable identifiers, typed relationships, provenance fields and an explicit answerability status.",
        "difficulty": "hard",
        "ambiguous": False,
    },
    {
        "id": "unsupported_premise",
        "label": "safety, high-stakes or unanswerable",
        "clause": (
            "The request asserts that GOV.UK guarantees a personal outcome without the facts required; treat that "
            "premise as deliberately unanswerable, abstain and state what evidence is missing."
        ),
        "difficulty": "adversarial",
        "ambiguous": True,
        "expected_unanswerable": True,
    },
)

SUITE_QUOTAS: dict[str, int] = {
    "locate_navigate": 12,
    "explain_understand": 12,
    "applicability_eligibility_obligation": 10,
    "procedure_next_step": 10,
    "compare_choose": 8,
    "temporal_current_historical_change": 8,
    "jurisdiction_language_audience": 8,
    "relationship_multi_hop": 8,
    "attachment_data_api": 8,
    "provenance_citation_verification": 6,
    "negative_no_answer": 5,
    "ambiguous_adversarial_noisy": 5,
}

PRIMARY_OPERATION_STRATUM = {
    "locate_known_item": "locate_navigate",
    "explain_define": "explain_understand",
    "determine_applicability": "applicability_eligibility_obligation",
    "follow_process": "procedure_next_step",
    "compare_decide": "compare_choose",
    "traverse_relationships": "relationship_multi_hop",
    "verify_provenance": "provenance_citation_verification",
    "check_lifecycle": "temporal_current_historical_change",
    "obtain_resource": "attachment_data_api",
    "handle_ambiguity": "ambiguous_adversarial_noisy",
}

CHALLENGE_STRATUM = {
    "noisy_wording": "ambiguous_adversarial_noisy",
    "temporal": "temporal_current_historical_change",
    "jurisdiction": "jurisdiction_language_audience",
    "language_access": "jurisdiction_language_audience",
    "multi_hop": "relationship_multi_hop",
    "conflicting_evidence": "provenance_citation_verification",
    "structured_agent": "attachment_data_api",
    "unsupported_premise": "negative_no_answer",
}


def canonical_json(value: Any) -> str:
    """Return the byte-stable JSON representation used for record hashes."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_wording(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def record_with_checksum(record: dict[str, Any]) -> dict[str, Any]:
    output = dict(record)
    output["checksum"] = sha256_text(canonical_json(output))
    return output


def render_wording(story: dict[str, Any], persona: dict[str, Any], operation: dict[str, str], challenge: dict[str, Any]) -> str:
    need = str(story["need"]).rstrip(". ")
    prompt = operation["prompt"].format(need=need)
    given = str(story["given"]).rstrip(". ")
    return f"For the {persona['title']} scenario, given {given}, {prompt}? {challenge['clause']}"


def build_question(
    story: dict[str, Any],
    persona: dict[str, Any],
    operation: dict[str, str],
    challenge: dict[str, Any],
    operation_index: int,
    challenge_index: int,
) -> dict[str, Any]:
    expected_unanswerable = bool(challenge.get("expected_unanswerable", False))
    question_id = f"q-{story['story_id'].removeprefix('story-')}-{operation_index:02d}-{challenge_index:02d}"
    evidence_ids = sorted(set(story["evidence_ids"]) | set(persona["evidence_ids"]))
    gold_status = (
        "contract_classified_unanswerable_pending_independent_review"
        if expected_unanswerable
        else "pending_independent_corpus_verification"
    )
    record = {
        "schema_version": 1,
        "question_id": question_id,
        "story_id": story["story_id"],
        "persona_ids": [persona["persona_id"]],
        "operation": operation["id"],
        "operation_label": operation["label"],
        "challenge": challenge["id"],
        "challenge_label": challenge["label"],
        "wording": render_wording(story, persona, operation, challenge),
        "intent": operation["intent"],
        "target_entities": story["target_entities"],
        "target_relationships": story["target_relationships"],
        "expected_response_type": operation["answer_shape"],
        "discovery_stage": operation["stage"],
        "channel": "agent" if persona["channel_class"] == "agent_or_system" else "human_or_assisted",
        "risk": persona["risk_level"],
        "difficulty": challenge["difficulty"],
        "ambiguity": bool(challenge["ambiguous"]),
        "temporal_sensitivity": challenge["id"] in {"temporal", "conflicting_evidence", "unsupported_premise"},
        "locale": "cy" if challenge["id"] == "language_access" else "en",
        "jurisdiction": persona["jurisdiction"],
        "provenance_requirements": [
            "canonical_govuk_url",
            "content_id_when_present",
            "source_evidence_url",
            "retrieved_at",
            "snapshot_id",
        ],
        "evidence_ids": evidence_ids,
        "expected_unanswerable": expected_unanswerable,
        "unanswerable_reason": (
            "The wording deliberately asks for a guaranteed personal outcome without the case facts needed to support it."
            if expected_unanswerable
            else None
        ),
        "gold_content_ids": [],
        "gold_urls": [],
        "gold_status": gold_status,
        "gold": {
            "classification": "deliberately_unanswerable" if expected_unanswerable else "answerable_candidate",
            "verification_status": "pending_independent_review",
            "content_ids": [],
            "urls": [],
            "relationship_targets": story["target_relationships"],
            "evidence_ids": evidence_ids,
            "snapshot_id": "question-contract-v1" if expected_unanswerable else CORPUS_SNAPSHOT_ID,
            "snapshot_date": QUESTION_CONTRACT_DATE if expected_unanswerable else None,
        },
        "generation_method": GENERATOR_VERSION,
        "independent_assignment": "pending",
        "adjudication": "pending",
        "split": "held_out" if challenge_index >= 9 else "development",
        "matrix_cell": {"row": operation_index, "column": challenge_index},
    }
    return record_with_checksum(record)


def build_story_questions(story: dict[str, Any], persona: dict[str, Any]) -> list[dict[str, Any]]:
    questions = [
        build_question(story, persona, operation, challenge, operation_index, challenge_index)
        for operation_index, operation in enumerate(OPERATIONS, start=1)
        for challenge_index, challenge in enumerate(CHALLENGES, start=1)
    ]
    validate_story_questions(questions)
    return questions


def validate_story_questions(questions: list[dict[str, Any]]) -> None:
    if len(questions) != 100:
        raise ValueError(f"expected 100 questions, found {len(questions)}")
    cells = {(item["operation"], item["challenge"]) for item in questions}
    if len(cells) != 100:
        raise ValueError("operation/challenge matrix does not contain 100 unique cells")
    wording = [normalize_wording(str(item["wording"])) for item in questions]
    if len(set(wording)) != 100:
        raise ValueError("question wording is not unique")
    if Counter(item["operation"] for item in questions) != Counter({item["id"]: 10 for item in OPERATIONS}):
        raise ValueError("each operation must occur exactly ten times")
    if Counter(item["challenge"] for item in questions) != Counter({item["id"]: 10 for item in CHALLENGES}):
        raise ValueError("each challenge must occur exactly ten times")


def _stratum_score(question: dict[str, Any], stratum: str) -> int:
    if PRIMARY_OPERATION_STRATUM[question["operation"]] == stratum:
        return 0
    if CHALLENGE_STRATUM.get(question["challenge"]) == stratum:
        return 0
    related = {
        "locate_navigate": {"relationship_multi_hop", "attachment_data_api"},
        "explain_understand": {"applicability_eligibility_obligation", "procedure_next_step"},
        "applicability_eligibility_obligation": {"jurisdiction_language_audience", "compare_choose"},
        "procedure_next_step": {"locate_navigate", "temporal_current_historical_change"},
        "compare_choose": {"jurisdiction_language_audience", "temporal_current_historical_change"},
        "temporal_current_historical_change": {"negative_no_answer", "provenance_citation_verification"},
        "jurisdiction_language_audience": {"applicability_eligibility_obligation", "ambiguous_adversarial_noisy"},
        "relationship_multi_hop": {"locate_navigate", "provenance_citation_verification"},
        "attachment_data_api": {"provenance_citation_verification", "locate_navigate"},
        "provenance_citation_verification": {"temporal_current_historical_change", "relationship_multi_hop"},
        "negative_no_answer": {"ambiguous_adversarial_noisy", "temporal_current_historical_change"},
        "ambiguous_adversarial_noisy": {"negative_no_answer", "jurisdiction_language_audience"},
    }
    primary = PRIMARY_OPERATION_STRATUM[question["operation"]]
    challenge = CHALLENGE_STRATUM.get(question["challenge"])
    return 1 if primary in related[stratum] or challenge in related[stratum] else 3


def _hungarian(cost: list[list[int]]) -> list[int]:
    """Return the minimum-cost column for every row of a square matrix."""

    n = len(cost)
    if n == 0 or any(len(row) != n for row in cost):
        raise ValueError("Hungarian assignment requires a non-empty square matrix")
    u = [0] * (n + 1)
    v = [0] * (n + 1)
    p = [0] * (n + 1)
    way = [0] * (n + 1)
    for row in range(1, n + 1):
        p[0] = row
        column0 = 0
        minimum = [10**9] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[column0] = True
            row0 = p[column0]
            delta = 10**9
            column1 = 0
            for column in range(1, n + 1):
                if used[column]:
                    continue
                current = cost[row0 - 1][column - 1] - u[row0] - v[column]
                if current < minimum[column]:
                    minimum[column] = current
                    way[column] = column0
                if minimum[column] < delta:
                    delta = minimum[column]
                    column1 = column
            for column in range(n + 1):
                if used[column]:
                    u[p[column]] += delta
                    v[column] -= delta
                else:
                    minimum[column] -= delta
            column0 = column1
            if p[column0] == 0:
                break
        while True:
            column1 = way[column0]
            p[column0] = p[column1]
            column0 = column1
            if column0 == 0:
                break
    assignment = [-1] * n
    for column in range(1, n + 1):
        assignment[p[column] - 1] = column - 1
    return assignment


def curate_persona_suite(persona: dict[str, Any], questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign all 100 story questions to the pre-registered persona quotas."""

    validate_story_questions(questions)
    slots = [stratum for stratum, quota in SUITE_QUOTAS.items() for _ in range(quota)]
    if len(slots) != len(questions):
        raise ValueError("persona-suite quotas must total 100")
    ordered_questions = sorted(questions, key=lambda item: item["question_id"])
    costs = [[_stratum_score(question, stratum) for stratum in slots] for question in ordered_questions]
    assignment = _hungarian(costs)
    suite = [
        record_with_checksum(
            {
                "schema_version": 1,
                "suite_id": f"suite-{persona['persona_id']}-v1",
                "persona_id": persona["persona_id"],
                "question_id": question["question_id"],
                "story_id": question["story_id"],
                "suite_stratum": slots[assignment[index]],
                "question_checksum": question["checksum"],
                "curation_method": "minimum-cost deterministic quota assignment",
            }
        )
        for index, question in enumerate(ordered_questions)
    ]
    counts = Counter(item["suite_stratum"] for item in suite)
    if counts != Counter(SUITE_QUOTAS):
        raise ValueError(f"persona suite does not meet quota: {dict(counts)}")
    if len({item["question_id"] for item in suite}) != 100:
        raise ValueError("persona suite contains duplicate question IDs")
    return suite


def manifest_root(entries: Iterable[tuple[str, str]]) -> str:
    material = "".join(f"{path}\0{digest}\n" for path, digest in sorted(entries))
    return sha256_text(material)
