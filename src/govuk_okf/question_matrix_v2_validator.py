"""Independent deterministic validation for the corpus-anchored question matrix.

The implementation intentionally does not import the v2 generator.  It rebuilds
corpus identities and evidence digests from the frozen envelopes and treats the
generated manifest, gold and split decisions as untrusted inputs.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterator

from govuk_okf.sharded_jsonl import input_sha256, iter_jsonl_records

VERIFIER_VERSION = "deterministic-corpus-anchor-validator-v2"
EXPECTED_OPERATIONS = {
    "locate_known_item",
    "explain_define",
    "determine_applicability",
    "follow_process",
    "compare_decide",
    "traverse_relationships",
    "verify_provenance",
    "check_lifecycle",
    "obtain_resource",
    "handle_ambiguity",
}
EXPECTED_CHALLENGES = {
    "direct",
    "novice_wording",
    "noisy_wording",
    "temporal",
    "jurisdiction",
    "language_access",
    "multi_hop",
    "conflicting_evidence",
    "structured_agent",
    "unsupported_premise",
}
EXPECTED_SUITE_QUOTAS = {
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
REQUIRED_QUESTION_FIELDS = {
    "question_id",
    "story_id",
    "persona_ids",
    "operation",
    "challenge",
    "wording",
    "intent",
    "target_entities",
    "target_relationships",
    "expected_response_type",
    "discovery_stage",
    "risk",
    "difficulty",
    "ambiguity",
    "locale",
    "jurisdiction",
    "provenance_requirements",
    "gold",
    "gold_status",
    "expected_unanswerable",
    "split",
    "split_group",
    "matrix_cell",
    "checksum",
}
LEAKAGE_PATTERNS = (
    "for the persona scenario",
    "use a direct, well-formed request",
    "treat one imprecise synonym",
    "require at least two typed relationships",
    "the request asserts that gov.uk guarantees",
    "question-contract-v1",
    "pending-corpus-t0",
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    yield from iter_jsonl_records(path)


def checked_record(record: dict[str, Any]) -> bool:
    material = dict(record)
    checksum = str(material.pop("checksum", ""))
    return bool(checksum) and checksum == digest_text(canonical_json(material))


def source_url(record: dict[str, Any]) -> str:
    value = str(record.get("canonical_url") or record.get("link") or "").strip()
    if value:
        return value
    base_path = str(record.get("base_path") or "")
    return f"https://www.gov.uk{base_path}" if base_path.startswith("/") else ""


def source_identity(record: dict[str, Any]) -> str:
    content_id = str(record.get("content_id") or "").strip()
    locale = str(record.get("locale") or "en")
    return f"content:{content_id}:{locale}" if content_id else f"url:{source_url(record)}:{locale}"


def source_projection(record: dict[str, Any]) -> dict[str, Any]:
    record_sha256 = digest_text(canonical_json(record))
    evidence_url = str(record.get("source_evidence_url") or "").strip()
    if not evidence_url:
        if str(record.get("source_id", "")).endswith("content-api") and str(record.get("base_path") or "").startswith("/"):
            evidence_url = f"https://content-api.publishing.service.gov.uk/content{record['base_path']}"
        else:
            evidence_url = source_url(record)
    return {
        "identity": source_identity(record),
        "content_id": str(record.get("content_id") or "") or None,
        "url": source_url(record),
        "title": re.sub(r"\s+", " ", str(record.get("title") or "")).strip(),
        "locale": str(record.get("locale") or "en"),
        "document_type": str(record.get("document_type") or "unknown"),
        "schema_name": str(record.get("schema_name") or record.get("document_type") or "unknown"),
        "record_sha256": record_sha256,
        "source_evidence_url": evidence_url,
        "source_evidence_sha256": str(record.get("source_evidence_sha256") or record.get("evidence_sha256") or record_sha256),
        "raw": record,
    }


class Validation:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.error_count = 0
        self.checks: Counter[str] = Counter()

    def require(self, condition: bool, code: str, detail: str = "") -> None:
        self.checks[code] += 1
        if not condition:
            self.error_count += 1
            suffix = f":{detail}" if detail else ""
            if len(self.errors) < 250:
                self.errors.append(f"{code}{suffix}")


def verify_manifest(root: Path, manifest: dict[str, Any], validation: Validation) -> None:
    material = ""
    for entry in manifest.get("files", []):
        relative = Path(str(entry.get("path", "")))
        validation.require(not relative.is_absolute() and ".." not in relative.parts, "manifest_path_safe", str(relative))
        path = root / relative
        validation.require(path.is_file(), "manifest_file_exists", str(relative))
        if not path.is_file():
            continue
        digest = digest_file(path)
        validation.require(digest == entry.get("sha256"), "manifest_file_sha256", str(relative))
        validation.require(path.stat().st_size == entry.get("bytes"), "manifest_file_size", str(relative))
        material += f"{relative.as_posix()}\0{digest}\n"
    validation.require(digest_text(material) == manifest.get("root_sha256"), "manifest_root_sha256")
    ledger = root / "checksums.txt"
    validation.require(ledger.is_file(), "checksum_ledger_exists")
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                expected, relative_text = line.split("  ", 1)
            except ValueError:
                validation.require(False, "checksum_ledger_syntax", line)
                continue
            relative = Path(relative_text)
            path = root / relative
            validation.require(not relative.is_absolute() and ".." not in relative.parts, "checksum_ledger_path_safe", relative_text)
            validation.require(path.is_file(), "checksum_ledger_file_exists", relative_text)
            if path.is_file():
                validation.require(digest_file(path) == expected, "checksum_ledger_sha256", relative_text)


def collect_gold(root: Path, validation: Validation) -> tuple[dict[str, dict[str, Any]], set[str]]:
    gold_records: dict[str, dict[str, Any]] = {}
    wanted_identities: set[str] = set()
    for item in iter_jsonl(root / "gold" / "catalogue.jsonl"):
        question_id = str(item.get("question_id") or "")
        validation.require(checked_record(item), "gold_record_checksum", question_id)
        validation.require(question_id not in gold_records, "gold_question_unique", question_id)
        gold_records[question_id] = item
        gold = item.get("gold") or {}
        for field in ("primary_targets", "near_misses", "supporting_source_anchors"):
            for target in gold.get(field) or []:
                identity = str(target.get("identity") or "")
                if identity.startswith(("content:", "url:")):
                    wanted_identities.add(identity)
        for path in gold.get("expected_paths") or []:
            for node in path.get("nodes") or []:
                identity = str(node.get("identity") or "")
                if identity.startswith(("content:", "url:")):
                    wanted_identities.add(identity)
    return gold_records, wanted_identities


def load_source_evidence(corpus: Path, wanted: set[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in iter_jsonl(corpus):
        identity = source_identity(raw)
        if identity in wanted:
            result[identity] = source_projection(raw)
    return result


def validate_target(target: dict[str, Any], sources: dict[str, dict[str, Any]], validation: Validation, question_id: str) -> None:
    identity = str(target.get("identity") or "")
    source = sources.get(identity)
    validation.require(source is not None, "gold_target_in_frozen_corpus", f"{question_id}:{identity}")
    if source is None:
        return
    for field in (
        "content_id",
        "url",
        "title",
        "locale",
        "document_type",
        "record_sha256",
        "source_evidence_url",
        "source_evidence_sha256",
    ):
        validation.require(target.get(field) == source.get(field), f"gold_target_{field}", f"{question_id}:{identity}")


def validate_resources(gold: dict[str, Any], primary: dict[str, Any] | None, validation: Validation, question_id: str) -> None:
    if primary is None:
        return
    known = {
        str(item.get("url"))
        for item in (primary.get("raw", {}).get("details") or {}).get("attachments") or []
        if isinstance(item, dict) and item.get("url")
    }
    for resource in gold.get("resources") or []:
        validation.require(resource.get("url") in known, "gold_resource_declared_by_source", question_id)
        validation.require(resource.get("parent_record_sha256") == primary.get("record_sha256"), "gold_resource_parent_hash", question_id)


def validate_paths(gold: dict[str, Any], sources: dict[str, dict[str, Any]], validation: Validation, question: dict[str, Any]) -> None:
    paths = gold.get("expected_paths") or []
    requires_two_hops = question.get("operation") == "traverse_relationships" or question.get("challenge") == "multi_hop"
    if requires_two_hops and not question.get("expected_unanswerable"):
        validation.require(bool(paths), "gold_path_required", question["question_id"])
        validation.require(all(len(path.get("edges") or []) >= 2 for path in paths), "gold_path_two_hops", question["question_id"])
    for path in paths:
        edges = path.get("edges") or []
        nodes = {str(node.get("identity")) for node in path.get("nodes") or []}
        for edge in edges:
            subject = str(edge.get("subject") or "")
            object_id = str(edge.get("object") or "")
            validation.require(subject in nodes and object_id in nodes, "gold_path_nodes_resolve", question["question_id"])
            if subject in sources:
                validation.require(
                    edge.get("source_evidence_sha256") == sources[subject]["source_evidence_sha256"],
                    "gold_path_evidence_hash",
                    question["question_id"],
                )
                if edge.get("predicate") == "has_content_type":
                    validation.require(
                        object_id == f"content-type:{sources[subject]['document_type']}",
                        "gold_path_content_type",
                        question["question_id"],
                    )
                elif edge.get("predicate") not in {"uses_schema_family"} and object_id in sources:
                    declared = False
                    for value in (sources[subject]["raw"].get("links") or {}).get(str(edge.get("predicate"))) or []:
                        if not isinstance(value, dict):
                            continue
                        target = sources[object_id]
                        declared = bool(
                            (value.get("content_id") and value.get("content_id") == target.get("content_id"))
                            or (
                                value.get("base_path")
                                and f"https://www.gov.uk{value.get('base_path')}" == target.get("url")
                            )
                        )
                        if declared:
                            break
                    validation.require(declared, "gold_path_source_relationship", question["question_id"])


def validate_question(
    question: dict[str, Any],
    gold_record: dict[str, Any] | None,
    sources: dict[str, dict[str, Any]],
    validation: Validation,
) -> None:
    question_id = str(question.get("question_id") or "")
    validation.require(REQUIRED_QUESTION_FIELDS <= set(question), "question_required_fields", question_id)
    validation.require(checked_record(question), "question_checksum", question_id)
    validation.require(question.get("operation") in EXPECTED_OPERATIONS, "question_operation", question_id)
    validation.require(question.get("challenge") in EXPECTED_CHALLENGES, "question_challenge", question_id)
    validation.require(question.get("split") in {"development", "held_out"}, "question_split", question_id)
    wording = re.sub(r"\s+", " ", str(question.get("wording") or "").casefold()).strip()
    validation.require(len(wording.split()) >= 10 and wording.endswith(("?", ".")), "question_natural_wording", question_id)
    for phrase in LEAKAGE_PATTERNS:
        validation.require(phrase not in wording, "question_prompt_leakage", f"{question_id}:{phrase}")
    validation.require(gold_record is not None, "question_has_gold_record", question_id)
    if gold_record is None:
        return
    validation.require(gold_record.get("question_checksum") == question.get("checksum"), "gold_question_checksum_link", question_id)
    validation.require(gold_record.get("gold") == question.get("gold"), "gold_embedded_catalogue_equivalence", question_id)
    gold = question.get("gold") or {}
    validation.require(gold.get("snapshot_id") not in {None, "", "pending-corpus-t0"}, "gold_snapshot_id", question_id)
    validation.require(bool(re.fullmatch(r"[0-9a-f]{64}", str(gold.get("snapshot_manifest_sha256") or ""))), "gold_snapshot_hash", question_id)
    validation.require(gold.get("verification_status") == "independent_verification_required", "gold_generator_does_not_self_verify", question_id)
    anchors = gold.get("supporting_source_anchors") or []
    validation.require(bool(anchors), "gold_supporting_anchor", question_id)
    for anchor in anchors:
        validate_target(anchor, sources, validation, question_id)
    if question.get("expected_unanswerable"):
        validation.require(gold.get("classification") == "deliberately_unanswerable", "gold_unanswerable_classification", question_id)
        validation.require(bool(gold.get("unanswerable_rationale")), "gold_unanswerable_rationale", question_id)
        validation.require(not gold.get("primary_targets") and not gold.get("content_ids") and not gold.get("urls"), "gold_unanswerable_no_target", question_id)
    else:
        targets = gold.get("primary_targets") or []
        validation.require(gold.get("classification") == "answerable", "gold_answerable_classification", question_id)
        validation.require(bool(targets), "gold_answerable_target", question_id)
        validation.require(bool(gold.get("content_ids") or gold.get("urls")), "gold_answerable_id_or_url", question_id)
        for target in targets:
            validate_target(target, sources, validation, question_id)
        target_identities = {target.get("identity") for target in targets}
        misses = gold.get("near_misses") or []
        validation.require(bool(misses), "gold_near_miss_present", question_id)
        for miss in misses:
            validation.require(miss.get("identity") not in target_identities, "gold_near_miss_disjoint", question_id)
            validation.require(bool(miss.get("disallowed_reason")), "gold_near_miss_reason", question_id)
            validate_target(miss, sources, validation, question_id)
        primary = sources.get(str(targets[0].get("identity") or "")) if targets else None
        validate_resources(gold, primary, validation, question_id)
        validate_paths(gold, sources, validation, question)


def verify(root: Path, corpus: Path) -> dict[str, Any]:
    validation = Validation()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    contract = json.loads((root / "contract.json").read_text(encoding="utf-8"))
    verify_manifest(root, manifest, validation)
    validation.require(input_sha256(corpus) == contract.get("snapshot", {}).get("corpus_sha256"), "frozen_corpus_sha256")
    gold_records, wanted = collect_gold(root, validation)
    snapshot = contract.get("snapshot", {})
    for question_id, gold_record in gold_records.items():
        gold = gold_record.get("gold") or {}
        validation.require(gold.get("snapshot_id") == snapshot.get("snapshot_id"), "gold_contract_snapshot_id", question_id)
        validation.require(gold.get("snapshot_date") == snapshot.get("snapshot_date"), "gold_contract_snapshot_date", question_id)
        validation.require(
            gold.get("snapshot_manifest_sha256") == snapshot.get("snapshot_manifest_sha256"),
            "gold_contract_snapshot_manifest_sha256",
            question_id,
        )
    sources = load_source_evidence(corpus, wanted)
    validation.require(wanted <= set(sources), "all_referenced_source_records_found", str(len(wanted - set(sources))))

    stories = list(iter_jsonl(root / "stories" / "catalogue.jsonl"))
    stories_by_persona: defaultdict[str, list[str]] = defaultdict(list)
    for story in stories:
        validation.require(checked_record(story), "story_checksum", str(story.get("story_id")))
        personas = story.get("persona_ids") or []
        validation.require(len(personas) == 1, "story_one_primary_persona", str(story.get("story_id")))
        if personas:
            stories_by_persona[str(personas[0])].append(str(story.get("story_id")))
        anchor = story.get("anchor") or {}
        validate_target(anchor, sources, validation, str(story.get("story_id")))
    for persona_id, story_ids in stories_by_persona.items():
        validation.require(len(story_ids) == 6 and len(set(story_ids)) == 6, "persona_six_stories", persona_id)

    seen_ids: set[str] = set()
    seen_wording: set[str] = set()
    semantic_signatures: set[str] = set()
    split_by_group: defaultdict[str, set[str]] = defaultdict(set)
    question_checksums: dict[str, str] = {}
    question_story: dict[str, str] = {}
    question_count = 0
    question_pass_material: list[str] = []
    verification_ledger: list[dict[str, Any]] = []
    for path in sorted((root / "bindings").glob("*.jsonl")):
        questions = list(iter_jsonl(path))
        validation.require(len(questions) == 100, "story_exactly_100_questions", path.name)
        validation.require(Counter(item.get("operation") for item in questions) == Counter({item: 10 for item in EXPECTED_OPERATIONS}), "story_operation_matrix", path.name)
        validation.require(Counter(item.get("challenge") for item in questions) == Counter({item: 10 for item in EXPECTED_CHALLENGES}), "story_challenge_matrix", path.name)
        validation.require(len({(item.get("operation"), item.get("challenge")) for item in questions}) == 100, "story_unique_matrix_cells", path.name)
        for question in questions:
            error_count_before = validation.error_count
            question_id = str(question.get("question_id") or "")
            validation.require(question_id not in seen_ids, "question_id_unique", question_id)
            seen_ids.add(question_id)
            wording = re.sub(r"\s+", " ", str(question.get("wording") or "").casefold()).strip()
            validation.require(wording not in seen_wording, "question_wording_unique", question_id)
            seen_wording.add(wording)
            gold = question.get("gold") or {}
            target_signature = sorted(
                str(item.get("identity")) for item in (gold.get("primary_targets") or gold.get("supporting_source_anchors") or [])
            )
            signature = canonical_json(
                {
                    "persona": question.get("persona_ids"),
                    "story_role": question.get("story_role"),
                    "intent": question.get("intent"),
                    "operation": question.get("operation"),
                    "challenge": question.get("challenge"),
                    "targets": target_signature,
                    "unanswerable": question.get("expected_unanswerable"),
                }
            )
            validation.require(signature not in semantic_signatures, "question_semantic_signature_unique", question_id)
            semantic_signatures.add(signature)
            split_by_group[str(question.get("split_group"))].add(str(question.get("split")))
            validate_question(question, gold_records.get(question_id), sources, validation)
            question_checksums[question_id] = str(question.get("checksum") or "")
            question_story[question_id] = str(question.get("story_id") or "")
            question_count += 1
            item_passed = validation.error_count == error_count_before
            verification_evidence_sha256 = digest_text(
                canonical_json(
                    {
                        "question_id": question_id,
                        "question_checksum": question.get("checksum"),
                        "gold": question.get("gold"),
                        "corpus_sha256": contract.get("snapshot", {}).get("corpus_sha256"),
                        "verifier": VERIFIER_VERSION,
                    }
                )
            )
            verification_ledger.append(
                {
                    "schema_version": 2,
                    "question_id": question_id,
                    "question_checksum": question.get("checksum"),
                    "gold_verification_status": "verified" if item_passed else "failed",
                    "verification_evidence_sha256": verification_evidence_sha256,
                    "verifier": VERIFIER_VERSION,
                }
            )
            question_pass_material.append(f"{question_id}\0{question.get('checksum')}\0{item_passed}\0{verification_evidence_sha256}\n")
    for group, splits in split_by_group.items():
        validation.require(len(splits) == 1, "split_group_no_leakage", group)
    held_out_groups = sum(splits == {"held_out"} for splits in split_by_group.values())
    validation.require(bool(split_by_group) and held_out_groups / len(split_by_group) >= 0.20, "held_out_group_minimum_20_percent")
    validation.require(question_count == len(gold_records), "question_gold_count_equivalence")

    suites = sorted((root / "persona-suites").glob("*.jsonl"))
    validation.require(len(suites) == len(stories_by_persona), "persona_suite_count")
    for path in suites:
        records = list(iter_jsonl(path))
        validation.require(len(records) == 100, "persona_suite_exactly_100", path.name)
        validation.require(Counter(item.get("suite_stratum") for item in records) == Counter(EXPECTED_SUITE_QUOTAS), "persona_suite_quotas", path.name)
        validation.require(len({item.get("question_id") for item in records}) == 100, "persona_suite_question_unique", path.name)
        persona_ids = {str(item.get("persona_id")) for item in records}
        validation.require(len(persona_ids) == 1, "persona_suite_identity", path.name)
        persona_id = next(iter(persona_ids), "")
        validation.require({str(item.get("story_id")) for item in records} == set(stories_by_persona.get(persona_id, [])), "persona_suite_covers_every_story", persona_id)
        for item in records:
            validation.require(checked_record(item), "persona_suite_record_checksum", str(item.get("question_id")))
            question_id = str(item.get("question_id") or "")
            validation.require(item.get("question_checksum") == question_checksums.get(question_id), "persona_suite_question_checksum", question_id)
            validation.require(item.get("story_id") == question_story.get(question_id), "persona_suite_story_link", question_id)

    counts = manifest.get("counts", {})
    validation.require(counts.get("questions") == question_count, "manifest_question_count")
    validation.require(counts.get("stories") == len(stories), "manifest_story_count")
    validation.require(counts.get("primary_personas") == len(stories_by_persona), "manifest_persona_count")
    machine_passed = validation.error_count == 0
    candidate = bool(contract.get("publication_ready_candidate")) and contract.get("artifact_tier") == "release_candidate"
    question_contract_passed = machine_passed and candidate
    verification_root = digest_text("".join(sorted(question_pass_material)))
    return {
        "schema_version": 2,
        "verifier": {
            "implementation": VERIFIER_VERSION,
            "independent_from_generator": True,
            "model_usage": {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_gbp": 0.0},
        },
        "matrix_version": manifest.get("matrix_version"),
        "manifest_root_sha256": manifest.get("root_sha256"),
        "corpus_sha256": digest_file(corpus),
        "machine_validations_passed": machine_passed,
        "publication_ready_candidate": candidate,
        "question_contract_passed": question_contract_passed,
        "artifact_tier": "release_verified" if question_contract_passed else "development_only",
        "counts": {
            "personas": len(stories_by_persona),
            "stories": len(stories),
            "questions": question_count,
            "gold_records": len(gold_records),
            "referenced_source_records": len(wanted),
            "resolved_source_records": len(sources),
            "split_groups": len(split_by_group),
            "held_out_split_groups": held_out_groups,
            "validation_checks": sum(validation.checks.values()),
            "validation_errors": validation.error_count,
        },
        "check_counts": dict(sorted(validation.checks.items())),
        "question_verifications_sha256": verification_root,
        "errors": validation.errors,
        "errors_truncated": validation.error_count > len(validation.errors),
        "release_blockers": [] if question_contract_passed else sorted(set(contract.get("eligibility_blockers") or []) | ({"independent_validation_failed"} if not machine_passed else set())),
        "claim_constraints": [
            "Gold verification covers frozen metadata identities, URLs, evidence hashes, resources and typed paths; it does not validate body-content answers.",
            "Human persona hypotheses and UI preference remain unvalidated until authorised participant research completes.",
        ],
        "_verification_ledger": verification_ledger,
    }
