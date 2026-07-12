"""Corpus-anchored story and evaluation-question matrix construction.

This module is deliberately limited to deterministic assignment.  Independent
verification lives in :mod:`govuk_okf.question_matrix_v2_validator` so that the
generator cannot be the sole judge of its own gold labels.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from govuk_okf.question_factory import SUITE_QUOTAS, canonical_json, record_with_checksum, sha256_text
from govuk_okf.sharded_jsonl import input_sha256, iter_jsonl_records

MATRIX_VERSION = "govuk-question-matrix-v2"
GENERATOR_VERSION = "deterministic-corpus-anchor-generator-v2.1-saturation-bound"
STORIES_PER_PERSONA = 6
QUESTIONS_PER_STORY = 100
QUESTIONS_PER_PERSONA_SUITE = 100
SAMPLE_POOL_LIMIT = 4096
RECONCILIATION_DISPOSITIONS = (
    "represented",
    "alias_of_represented",
    "redirect_only",
    "tombstone_only",
    "exceptioned",
)

OPERATIONS: tuple[dict[str, str], ...] = (
    {"id": "locate_known_item", "intent": "lookup", "answer_shape": "ranked canonical records"},
    {"id": "explain_define", "intent": "explanation", "answer_shape": "record and authoritative source hand-off"},
    {"id": "determine_applicability", "intent": "applicability", "answer_shape": "bounded applicability evidence"},
    {"id": "follow_process", "intent": "procedure", "answer_shape": "ordered source-backed next steps"},
    {"id": "compare_decide", "intent": "comparison", "answer_shape": "comparison with explicit boundaries"},
    {"id": "traverse_relationships", "intent": "relationship_traversal", "answer_shape": "typed evidence-bearing path"},
    {"id": "verify_provenance", "intent": "provenance", "answer_shape": "source and provenance record"},
    {"id": "check_lifecycle", "intent": "lifecycle", "answer_shape": "dated lifecycle disposition"},
    {"id": "obtain_resource", "intent": "resource_discovery", "answer_shape": "resource metadata or explicit absence"},
    {"id": "handle_ambiguity", "intent": "abstention_and_clarification", "answer_shape": "clarification or explicit abstention"},
)

CHALLENGES: tuple[dict[str, Any], ...] = (
    {"id": "direct", "difficulty": "easy", "ambiguity": False},
    {"id": "novice_wording", "difficulty": "medium", "ambiguity": False},
    {"id": "noisy_wording", "difficulty": "medium", "ambiguity": True},
    {"id": "temporal", "difficulty": "hard", "ambiguity": False},
    {"id": "jurisdiction", "difficulty": "hard", "ambiguity": False},
    {"id": "language_access", "difficulty": "hard", "ambiguity": False},
    {"id": "multi_hop", "difficulty": "hard", "ambiguity": False},
    {"id": "conflicting_evidence", "difficulty": "hard", "ambiguity": True},
    {"id": "structured_agent", "difficulty": "hard", "ambiguity": False},
    {"id": "unsupported_premise", "difficulty": "adversarial", "ambiguity": True, "unanswerable": True},
)

STORY_ROLES: tuple[dict[str, str], ...] = (
    {"id": "known-item", "need": "find the current authoritative item"},
    {"id": "publisher-and-provenance", "need": "verify who published the item and where its metadata came from"},
    {"id": "lifecycle-and-freshness", "need": "distinguish the current item from old, redirected or replaced material"},
    {"id": "relationship-path", "need": "follow the item's source-native relationships without losing evidence"},
    {"id": "resource-and-representation", "need": "find an attachment or machine representation, or establish that none is declared"},
    {"id": "language-jurisdiction-boundary", "need": "keep language and jurisdiction boundaries explicit"},
)

STRATUM_OPERATION = {
    "locate_navigate": "locate_known_item",
    "explain_understand": "explain_define",
    "applicability_eligibility_obligation": "determine_applicability",
    "procedure_next_step": "follow_process",
    "compare_choose": "compare_decide",
    "temporal_current_historical_change": "check_lifecycle",
    "jurisdiction_language_audience": "determine_applicability",
    "relationship_multi_hop": "traverse_relationships",
    "attachment_data_api": "obtain_resource",
    "provenance_citation_verification": "verify_provenance",
    "negative_no_answer": "handle_ambiguity",
    "ambiguous_adversarial_noisy": "handle_ambiguity",
}

STRATUM_CHALLENGE = {
    "temporal_current_historical_change": "temporal",
    "jurisdiction_language_audience": "jurisdiction",
    "relationship_multi_hop": "multi_hop",
    "attachment_data_api": "structured_agent",
    "provenance_citation_verification": "conflicting_evidence",
    "negative_no_answer": "unsupported_premise",
    "ambiguous_adversarial_noisy": "noisy_wording",
}

LEAKAGE_DENYLIST = (
    "for the persona scenario",
    "for the {persona}",
    "use a direct, well-formed request",
    "treat one imprecise synonym",
    "require at least two typed relationships",
    "the request asserts that gov.uk guarantees",
    "question-contract-v1",
    "pending-corpus-t0",
)


@dataclass(frozen=True)
class CorpusRecord:
    raw: dict[str, Any]
    identity: str
    title: str
    canonical_url: str
    content_id: str | None
    locale: str
    document_type: str
    schema_name: str
    record_sha256: str
    evidence_sha256: str
    evidence_url: str


def sha256_file(path: Path) -> str:
    return input_sha256(path)


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    yield from iter_jsonl_records(path)


def canonical_url(record: dict[str, Any]) -> str:
    url = str(record.get("canonical_url") or record.get("link") or "").strip()
    if url:
        return url
    base_path = str(record.get("base_path") or "").strip()
    if base_path.startswith("/"):
        return f"https://www.gov.uk{base_path}"
    return ""


def normalise_record(record: dict[str, Any]) -> CorpusRecord | None:
    if record.get("coverage_disposition") not in {None, "represented", "redirect_only", "tombstone_only"}:
        return None
    title = re.sub(r"\s+", " ", str(record.get("title") or "")).strip()
    url = canonical_url(record)
    content_id = str(record.get("content_id") or "").strip() or None
    if not title or not (content_id or url):
        return None
    locale = str(record.get("locale") or "en")
    identity = f"content:{content_id}:{locale}" if content_id else f"url:{url}:{locale}"
    record_digest = sha256_text(canonical_json(record))
    evidence_digest = str(record.get("source_evidence_sha256") or record.get("evidence_sha256") or record_digest)
    evidence_url = str(record.get("source_evidence_url") or "").strip()
    if not evidence_url:
        if str(record.get("source_id", "")).endswith("content-api") and str(record.get("base_path") or "").startswith("/"):
            evidence_url = f"https://content-api.publishing.service.gov.uk/content{record['base_path']}"
        else:
            evidence_url = url
    return CorpusRecord(
        raw=record,
        identity=identity,
        title=title,
        canonical_url=url,
        content_id=content_id,
        locale=locale,
        document_type=str(record.get("document_type") or "unknown"),
        schema_name=str(record.get("schema_name") or record.get("document_type") or "unknown"),
        record_sha256=record_digest,
        evidence_sha256=evidence_digest,
        evidence_url=evidence_url,
    )


def _pool_score(record: CorpusRecord) -> str:
    return sha256_text(record.identity)


def load_anchor_pool(path: Path, limit: int = SAMPLE_POOL_LIMIT) -> tuple[list[CorpusRecord], int]:
    """Return a deterministic bounded min-hash sample plus all scarce classes."""

    heap: list[tuple[int, str, int, CorpusRecord]] = []
    scarce_heaps: dict[str, list[tuple[int, str, int, CorpusRecord]]] = defaultdict(list)
    scarce_limit = max(64, limit // 8)
    seen = 0
    for raw in iter_jsonl(path):
        record = normalise_record(raw)
        if record is None:
            continue
        seen += 1
        score_hex = _pool_score(record)
        score = int(score_hex, 16)
        entry = (-score, record.identity, seen, record)
        if len(heap) < limit:
            heapq.heappush(heap, entry)
        elif score < -heap[0][0]:
            heapq.heapreplace(heap, entry)
        has_links = any(isinstance(value, list) and value for value in (raw.get("links") or {}).values())
        has_resources = bool((raw.get("details") or {}).get("attachments"))
        is_lifecycle = record.document_type in {"redirect", "gone", "vanish", "substitute"} or bool(raw.get("redirects"))
        is_language = record.locale != "en" or bool((raw.get("links") or {}).get("available_translations"))
        for label, condition in (
            ("links", has_links),
            ("resources", has_resources),
            ("lifecycle", is_lifecycle),
            ("language", is_language),
        ):
            if condition:
                scarce_heap = scarce_heaps[label]
                scarce_entry = (-score, record.identity, seen, record)
                if len(scarce_heap) < scarce_limit:
                    heapq.heappush(scarce_heap, scarce_entry)
                elif score < -scarce_heap[0][0]:
                    heapq.heapreplace(scarce_heap, scarce_entry)
    merged = {item[3].identity: item[3] for item in heap}
    for scarce_heap in scarce_heaps.values():
        merged.update({item[3].identity: item[3] for item in scarce_heap})
    return sorted(merged.values(), key=lambda item: item.identity), seen


def _has_links(record: CorpusRecord) -> bool:
    return any(isinstance(value, list) and value for value in (record.raw.get("links") or {}).values())


def _has_resources(record: CorpusRecord) -> bool:
    return bool((record.raw.get("details") or {}).get("attachments"))


def _is_lifecycle(record: CorpusRecord) -> bool:
    return record.document_type in {"redirect", "gone", "vanish", "substitute"} or bool(record.raw.get("redirects"))


def _is_language(record: CorpusRecord) -> bool:
    return record.locale != "en" or bool((record.raw.get("links") or {}).get("available_translations"))


def role_pool(role: str, records: list[CorpusRecord]) -> list[CorpusRecord]:
    filters = {
        "publisher-and-provenance": _has_links,
        "lifecycle-and-freshness": _is_lifecycle,
        "relationship-path": _has_links,
        "resource-and-representation": _has_resources,
        "language-jurisdiction-boundary": _is_language,
    }
    filtered = [record for record in records if filters.get(role, lambda _: True)(record)]
    return filtered or records


def choose_anchors(personas: list[dict[str, Any]], records: list[CorpusRecord]) -> tuple[dict[str, list[CorpusRecord]], list[str]]:
    if not records:
        raise ValueError("corpus contains no eligible source records")
    assignments: dict[str, list[CorpusRecord]] = {}
    used: set[str] = set()
    blockers: list[str] = []
    for persona in personas:
        chosen: list[CorpusRecord] = []
        for role in STORY_ROLES:
            pool = sorted(
                role_pool(role["id"], records),
                key=lambda item: sha256_text(f"{persona['persona_id']}\0{role['id']}\0{item.identity}"),
            )
            available = next((item for item in pool if item.identity not in used), None)
            if available is None:
                available = pool[0]
                blockers.append(f"anchor_reused:{persona['persona_id']}:{role['id']}:{available.identity}")
            chosen.append(available)
            used.add(available.identity)
        assignments[persona["persona_id"]] = chosen
    return assignments, sorted(set(blockers))


def assign_splits(assignments: dict[str, list[CorpusRecord]]) -> dict[str, str]:
    identities = sorted({record.identity for records in assignments.values() for record in records})
    # Entity-group assignment prevents the same gold target appearing in tuning
    # and held-out sets.  Every fifth group is held out, deterministically.
    return {identity: "held_out" if index % 5 == 0 else "development" for index, identity in enumerate(identities)}


def _record_ref(record: CorpusRecord) -> dict[str, Any]:
    return {
        "identity": record.identity,
        "content_id": record.content_id,
        "url": record.canonical_url,
        "title": record.title,
        "locale": record.locale,
        "document_type": record.document_type,
        "record_sha256": record.record_sha256,
        "source_evidence_url": record.evidence_url,
        "source_evidence_sha256": record.evidence_sha256,
    }


def _find_link_target(record: CorpusRecord, index: dict[str, CorpusRecord]) -> tuple[str, CorpusRecord] | None:
    for predicate, values in sorted((record.raw.get("links") or {}).items()):
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            content_id = str(value.get("content_id") or "")
            base_path = str(value.get("base_path") or "")
            candidates = [
                candidate
                for candidate in index.values()
                if (content_id and candidate.content_id == content_id)
                or (base_path and candidate.canonical_url == f"https://www.gov.uk{base_path}")
            ]
            if candidates:
                return predicate, sorted(candidates, key=lambda item: item.identity)[0]
    return None


def build_paths(record: CorpusRecord, index: dict[str, CorpusRecord]) -> list[dict[str, Any]]:
    source = _record_ref(record)
    linked = _find_link_target(record, index)
    if linked:
        predicate, target = linked
        target_ref = _record_ref(target)
        return [
            {
                "path_id": f"path-{sha256_text(record.identity + predicate + target.identity)[:16]}",
                "nodes": [source, target_ref, {"identity": f"content-type:{target.document_type}", "label": target.document_type}],
                "edges": [
                    {
                        "subject": record.identity,
                        "predicate": predicate,
                        "object": target.identity,
                        "source_evidence_sha256": record.evidence_sha256,
                    },
                    {
                        "subject": target.identity,
                        "predicate": "has_content_type",
                        "object": f"content-type:{target.document_type}",
                        "source_evidence_sha256": target.evidence_sha256,
                    },
                ],
            }
        ]
    return [
        {
            "path_id": f"path-{sha256_text(record.identity + record.document_type + record.schema_name)[:16]}",
            "nodes": [
                source,
                {"identity": f"content-type:{record.document_type}", "label": record.document_type},
                {"identity": f"schema-family:{record.schema_name}", "label": record.schema_name},
            ],
            "edges": [
                {
                    "subject": record.identity,
                    "predicate": "has_content_type",
                    "object": f"content-type:{record.document_type}",
                    "source_evidence_sha256": record.evidence_sha256,
                },
                {
                    "subject": f"content-type:{record.document_type}",
                    "predicate": "uses_schema_family",
                    "object": f"schema-family:{record.schema_name}",
                    "source_evidence_sha256": record.evidence_sha256,
                },
            ],
        }
    ]


def resources(record: CorpusRecord) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for attachment in (record.raw.get("details") or {}).get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        url = str(attachment.get("url") or "")
        if url:
            result.append(
                {
                    "id": str(attachment.get("id") or sha256_text(url)[:24]),
                    "url": url,
                    "title": str(attachment.get("title") or attachment.get("filename") or "Attachment"),
                    "content_type": str(attachment.get("content_type") or "application/octet-stream"),
                    "parent_record_sha256": record.record_sha256,
                }
            )
    return result


def near_miss(record: CorpusRecord, records: list[CorpusRecord]) -> dict[str, Any]:
    candidates = [item for item in records if item.identity != record.identity]
    if not candidates:
        return {}
    same_type = [item for item in candidates if item.document_type == record.document_type]
    pool = same_type or candidates
    selected = min(pool, key=lambda item: sha256_text(f"{record.identity}\0near\0{item.identity}"))
    result = _record_ref(selected)
    result["disallowed_reason"] = "Different canonical identity; title or content type similarity is not sufficient evidence."
    return result


def _noisy_title(title: str) -> str:
    words = title.split()
    if not words:
        return title
    index = max(range(len(words)), key=lambda position: len(words[position]))
    word = words[index]
    if len(word) >= 5:
        middle = len(word) // 2
        word = word[:middle] + word[middle + 1] + word[middle] + word[middle + 2 :]
    else:
        word = word + "s"
    words[index] = word
    return " ".join(words)


def render_wording(
    *, persona: dict[str, Any], story: dict[str, Any], operation: dict[str, str], challenge: dict[str, Any], record: CorpusRecord
) -> str:
    title = record.title
    actor = str(persona["title"])
    task = str(story["need"])
    bases = {
        "locate_known_item": f"Where is the official GOV.UK item “{title}”?",
        "explain_define": f"What does “{title}” cover, and where is its authoritative GOV.UK version?",
        "determine_applicability": f"What does GOV.UK say about the scope of “{title}”, and what details decide whether it applies?",
        "follow_process": f"Which official step comes from “{title}”, and where should someone go next?",
        "compare_decide": f"How does “{title}” differ from similar GOV.UK material, and which canonical item should be used?",
        "traverse_relationships": f"How is “{title}” connected to its content type, schema, publisher, topic or collection?",
        "verify_provenance": f"Who is responsible for “{title}”, and what source evidence proves this record is authoritative?",
        "check_lifecycle": f"Is “{title}” current, when was it updated, and is there a redirect or replacement?",
        "obtain_resource": f"Does “{title}” declare an attachment or machine-readable representation, and where is it?",
        "handle_ambiguity": f"If “{title}” is not enough to answer the request safely, what should be clarified or left unanswered?",
    }
    suffixes = {
        "direct": f" I need the canonical link for the {actor.lower()} task.",
        "novice_wording": " I do not know the government terminology, so explain the result in everyday language.",
        "noisy_wording": f" I searched for “{_noisy_title(title)}”; confirm the correction instead of assuming it is the same item.",
        "temporal": f" Use the frozen snapshot date and distinguish current from historical material; the recorded update is {record.raw.get('public_updated_at') or 'not supplied'}.",
        "jurisdiction": f" Keep the boundary to {', '.join(persona.get('jurisdiction') or ['UK'])} explicit and do not infer personal eligibility.",
        "language_access": f" The source locale is {record.locale}; identify a Welsh or accessible alternative only when the metadata supports one.",
        "multi_hop": " Show at least two typed, evidence-bearing relationship steps and name every intermediate node.",
        "conflicting_evidence": " Preserve any missing or conflicting publisher, date or relationship evidence instead of silently resolving it.",
        "structured_agent": " Give the canonical URL, content ID when present, source hash, snapshot ID and typed relationships as structured fields.",
        "unsupported_premise": " Does GOV.UK guarantee that following this item will produce the outcome I want, even though no case facts have been supplied?",
    }
    wording = f"As {actor.lower()}, I need to {task}. {bases[operation['id']]}{suffixes[challenge['id']]}"
    return re.sub(r"\s+", " ", wording).strip()


def build_story(
    persona: dict[str, Any],
    role: dict[str, str],
    record: CorpusRecord,
    ordinal: int,
    *,
    coverage_dimensions: dict[str, list[str]],
    persona_saturation_sha256: str,
) -> dict[str, Any]:
    story_id = f"story-v2-{persona['persona_id'].removeprefix('persona-')}-{ordinal:02d}-{role['id']}"
    return record_with_checksum(
        {
            "schema_version": 2,
            "story_id": story_id,
            "persona_ids": [persona["persona_id"]],
            "story_role": role["id"],
            "given": persona["context"],
            "as_a": persona["title"],
            "need": role["need"],
            "so_that": persona["goal"],
            "statement": (
                f"Given {persona['context']}, as {persona['title']}, I need to {role['need']} "
                f"so that I can {persona['goal']}."
            ),
            "risk_level": persona["risk_level"],
            "jurisdiction": persona["jurisdiction"],
            "overlay_ids": persona["overlay_ids"],
            "evidence_ids": persona["evidence_ids"],
            "coverage_dimensions": coverage_dimensions,
            "persona_saturation_sha256": persona_saturation_sha256,
            "anchor": _record_ref(record),
            "acceptance_criteria": [
                "The canonical GOV.UK identity and URL match the frozen source record.",
                "The answer states the corpus snapshot and source evidence hash.",
                "Source-native content type, locale, lifecycle and relationships remain distinct.",
                "Unsupported personal applicability or guaranteed outcomes are rejected.",
                "Discovery is distinguished from retrieval of authoritative body content.",
            ],
            "failure_harms": [
                "wrong or stale official destination",
                "lost jurisdiction or language boundary",
                "unsupported certainty about a personal outcome",
                "untraceable answer or citation",
            ],
            "generation_method": GENERATOR_VERSION,
        }
    )


def build_question(
    *,
    persona: dict[str, Any],
    story: dict[str, Any],
    record: CorpusRecord,
    operation: dict[str, str],
    challenge: dict[str, Any],
    row: int,
    column: int,
    split: str,
    snapshot_id: str,
    snapshot_date: str,
    snapshot_manifest_sha256: str,
    expected_paths: list[dict[str, Any]],
    resource_records: list[dict[str, Any]],
    misses: list[dict[str, Any]],
) -> dict[str, Any]:
    expected_unanswerable = bool(challenge.get("unanswerable"))
    question_id = f"q-v2-{story['story_id'].removeprefix('story-v2-')}-{row:02d}-{column:02d}"
    primary = _record_ref(record)
    gold = {
        "classification": "deliberately_unanswerable" if expected_unanswerable else "answerable",
        "verification_status": "independent_verification_required",
        "content_ids": [] if expected_unanswerable else ([record.content_id] if record.content_id else []),
        "urls": [] if expected_unanswerable else ([record.canonical_url] if record.canonical_url else []),
        "primary_targets": [] if expected_unanswerable else [primary],
        "near_misses": misses,
        "expected_paths": [] if expected_unanswerable else expected_paths,
        "resources": [] if expected_unanswerable else resource_records,
        "unanswerable_rationale": (
            "The question asks for a guaranteed personal outcome without the facts or authoritative body content needed to support one."
            if expected_unanswerable
            else None
        ),
        "supporting_source_anchors": [primary],
        "snapshot_id": snapshot_id,
        "snapshot_date": snapshot_date,
        "snapshot_manifest_sha256": snapshot_manifest_sha256,
        "assignment_method": GENERATOR_VERSION,
        "independent_verifier_required": "deterministic-corpus-anchor-validator-v2",
    }
    record_value = {
        "schema_version": 2,
        "question_id": question_id,
        "story_id": story["story_id"],
        "story_role": story["story_role"],
        "persona_ids": [persona["persona_id"]],
        "operation": operation["id"],
        "challenge": challenge["id"],
        "wording": render_wording(persona=persona, story=story, operation=operation, challenge=challenge, record=record),
        "intent": operation["intent"],
        "target_entities": [record.document_type, record.schema_name],
        "target_relationships": sorted({edge["predicate"] for path in expected_paths for edge in path["edges"]}),
        "expected_response_type": operation["answer_shape"],
        "discovery_stage": (
            "discovery_plus_authoritative_retrieval"
            if operation["id"] in {"explain_define", "determine_applicability", "follow_process", "compare_decide"}
            else "metadata_only_discovery"
        ),
        "channel": "agent" if persona["channel_class"] == "agent_or_system" else "human_or_assisted",
        "risk": persona["risk_level"],
        "difficulty": challenge["difficulty"],
        "ambiguity": challenge["ambiguity"],
        "locale": record.locale,
        "jurisdiction": persona["jurisdiction"],
        "coverage_dimensions": story["coverage_dimensions"],
        "persona_saturation_sha256": story["persona_saturation_sha256"],
        "temporal_sensitivity": challenge["id"] in {"temporal", "conflicting_evidence", "unsupported_premise"},
        "expected_unanswerable": expected_unanswerable,
        "provenance_requirements": [
            "canonical_govuk_url",
            "content_id_when_present",
            "source_evidence_url",
            "source_evidence_sha256",
            "record_sha256",
            "snapshot_id",
            "snapshot_manifest_sha256",
        ],
        "gold": gold,
        "gold_status": "assigned_pending_independent_verification",
        "generation_method": GENERATOR_VERSION,
        "generation_run_role": "assignment_only_not_judgement",
        "split": split,
        "split_group": record.identity,
        "matrix_cell": {"row": row, "column": column},
    }
    return record_with_checksum(record_value)


def build_story_questions(
    *,
    persona: dict[str, Any],
    story: dict[str, Any],
    record: CorpusRecord,
    split: str,
    snapshot_id: str,
    snapshot_date: str,
    snapshot_manifest_sha256: str,
    pool: list[CorpusRecord],
    index: dict[str, CorpusRecord],
) -> list[dict[str, Any]]:
    expected_paths = build_paths(record, index)
    resource_records = resources(record)
    misses = [item for item in [near_miss(record, pool)] if item]
    questions = [
        build_question(
            persona=persona,
            story=story,
            record=record,
            operation=operation,
            challenge=challenge,
            row=row,
            column=column,
            split=split,
            snapshot_id=snapshot_id,
            snapshot_date=snapshot_date,
            snapshot_manifest_sha256=snapshot_manifest_sha256,
            expected_paths=expected_paths,
            resource_records=resource_records,
            misses=misses,
        )
        for row, operation in enumerate(OPERATIONS, start=1)
        for column, challenge in enumerate(CHALLENGES, start=1)
    ]
    if len(questions) != QUESTIONS_PER_STORY:
        raise AssertionError("matrix did not produce exactly 100 questions")
    if len({item["wording"].casefold() for item in questions}) != QUESTIONS_PER_STORY:
        raise ValueError(f"duplicate wording in {story['story_id']}")
    return questions


def curate_suite(persona: dict[str, Any], story_questions: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    if len(story_questions) != STORIES_PER_PERSONA:
        raise ValueError("persona suite requires exactly six approved stories")
    slots = [stratum for stratum, quota in SUITE_QUOTAS.items() for _ in range(quota)]
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, stratum in enumerate(slots):
        story_index = index % len(story_questions)
        preferred_operation = STRATUM_OPERATION[stratum]
        preferred_challenge = STRATUM_CHALLENGE.get(stratum)
        ordered = story_questions[story_index:] + story_questions[:story_index]
        candidates = [
            item
            for item in story_questions[story_index]
            if item["question_id"] not in used
            and item["operation"] == preferred_operation
            and (preferred_challenge is None or item["challenge"] == preferred_challenge)
        ]
        if not candidates:
            candidates = [item for questions in ordered for item in questions if item["question_id"] not in used]
        chosen = min(candidates, key=lambda item: sha256_text(f"{persona['persona_id']}\0{stratum}\0{item['question_id']}"))
        used.add(chosen["question_id"])
        selected.append(
            record_with_checksum(
                {
                    "schema_version": 2,
                    "suite_id": f"suite-v2-{persona['persona_id']}",
                    "persona_id": persona["persona_id"],
                    "question_id": chosen["question_id"],
                    "question_checksum": chosen["checksum"],
                    "story_id": chosen["story_id"],
                    "suite_stratum": stratum,
                    "split": chosen["split"],
                    "curation_method": "deterministic-six-story-quota-assignment-v2",
                }
            )
        )
    if len(selected) != QUESTIONS_PER_PERSONA_SUITE or Counter(item["suite_stratum"] for item in selected) != Counter(SUITE_QUOTAS):
        raise AssertionError("persona suite quota construction failed")
    if {item["story_id"] for item in selected} != {questions[0]["story_id"] for questions in story_questions}:
        raise ValueError(f"suite does not cover all stories for {persona['persona_id']}")
    return selected


def recursive_values(value: Any, key: str) -> list[Any]:
    result: list[Any] = []
    if isinstance(value, dict):
        for candidate_key, candidate_value in value.items():
            if candidate_key == key:
                result.append(candidate_value)
            result.extend(recursive_values(candidate_value, key))
    elif isinstance(value, list):
        for item in value:
            result.extend(recursive_values(item, key))
    return result


def reconciliation_release_errors(
    reconciliation: dict[str, Any] | None,
    *,
    snapshot_id: str,
    snapshot_manifest: dict[str, Any] | None,
) -> list[str]:
    """Validate the exact independent closing-reconciliation contract."""

    if not isinstance(reconciliation, dict):
        return ["missing_corpus_reconciliation"]
    errors: list[str] = []
    if reconciliation.get("schema_version") != 1:
        errors.append("corpus_reconciliation_schema_invalid")
    if reconciliation.get("snapshot") != snapshot_id:
        errors.append("corpus_reconciliation_snapshot_mismatch")
    if reconciliation.get("sampled") is not False:
        errors.append("corpus_is_sampled")
    expected = reconciliation.get("expected_candidate_keys")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        errors.append("corpus_expected_candidate_keys_invalid")
    counts = [reconciliation.get(name) for name in RECONCILIATION_DISPOSITIONS]
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts):
        errors.append("corpus_disposition_counts_invalid")
    elif isinstance(expected, int) and sum(counts) != expected:
        errors.append("corpus_accounting_identity_invalid")
    if reconciliation.get("unexplained_omissions") != 0:
        errors.append("corpus_unexplained_omissions_not_zero")
    entity_counts = reconciliation.get("entity_class_counts")
    if (
        not isinstance(entity_counts, dict)
        or not entity_counts
        or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in entity_counts.values())
        or (isinstance(expected, int) and sum(entity_counts.values()) != expected)
    ):
        errors.append("corpus_entity_class_accounting_invalid")
    publication_records = reconciliation.get("publication_records")
    if not isinstance(publication_records, int) or isinstance(publication_records, bool) or publication_records < 1:
        errors.append("corpus_publication_record_count_invalid")
    for field in ("inventory_canonical_sha256", "candidate_ledger_canonical_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(reconciliation.get(field) or "")):
            errors.append(f"corpus_{field}_invalid")
    proof_requirements = {
        "search_partitions_closed": reconciliation.get("search_partitions_closed") is True,
        "search_partition_proofs": isinstance(reconciliation.get("search_partition_proofs"), list)
        and bool(reconciliation.get("search_partition_proofs")),
        "sitemap_byte_stable": reconciliation.get("sitemap_byte_stable") is True,
        "sitemap_proof": isinstance(reconciliation.get("sitemap_proof"), dict)
        and reconciliation["sitemap_proof"].get("closed") is True,
        "organisations_proof": isinstance(reconciliation.get("organisations_proof"), dict)
        and reconciliation["organisations_proof"].get("closed") is True,
        "navigation_proof": isinstance(reconciliation.get("navigation_proof"), dict)
        and reconciliation["navigation_proof"].get("closed") is True,
    }
    errors.extend(f"corpus_{name}_invalid" for name, passed in proof_requirements.items() if not passed)
    if not isinstance(snapshot_manifest, dict):
        errors.append("missing_independent_snapshot_manifest")
    else:
        if snapshot_manifest.get("snapshot") != snapshot_id:
            errors.append("snapshot_manifest_snapshot_mismatch")
        if snapshot_manifest.get("reconciliation") != reconciliation:
            errors.append("snapshot_manifest_reconciliation_mismatch")
    return sorted(set(errors))


def release_prerequisites(
    *,
    mode: str,
    snapshot_id: str,
    personas: list[dict[str, Any]],
    reconciliation: dict[str, Any] | None,
    snapshot_manifest: dict[str, Any] | None = None,
    blockers: list[str],
) -> tuple[bool, list[str]]:
    reasons = list(blockers)
    if mode != "release":
        reasons.append("development_fixture_mode")
    if len(personas) != 48:
        reasons.append(f"primary_persona_count:{len(personas)}:expected:48")
    if any(marker in snapshot_id.casefold() for marker in ("pending", "fixture", "sample", "capacity")):
        reasons.append("snapshot_id_is_not_release_eligible")
    if mode == "release":
        reasons.extend(
            reconciliation_release_errors(
                reconciliation,
                snapshot_id=snapshot_id,
                snapshot_manifest=snapshot_manifest,
            )
        )
    return not reasons, sorted(set(reasons))


def manifest_for(output_root: Path, files: Iterable[Path], counts: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    entries = []
    for path in sorted(files):
        digest = sha256_file(path)
        entries.append({"path": path.relative_to(output_root).as_posix(), "bytes": path.stat().st_size, "sha256": digest})
    material = "".join(f"{entry['path']}\0{entry['sha256']}\n" for entry in entries)
    return {
        "schema_version": 2,
        "matrix_version": MATRIX_VERSION,
        "generation_method": GENERATOR_VERSION,
        "files": entries,
        "root_sha256": sha256_text(material),
        "counts": counts,
        **metadata,
    }
