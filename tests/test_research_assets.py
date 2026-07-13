from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.question_factory import (
    CHALLENGES,
    OPERATIONS,
    SUITE_QUOTAS,
    canonical_json,
    manifest_root,
    normalize_wording,
    sha256_text,
)


def read_json(path: str) -> dict[str, Any]:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def assert_record_checksum(record: dict[str, Any]) -> None:
    material = dict(record)
    checksum = material.pop("checksum")
    assert checksum == sha256_text(canonical_json(material))


def assert_manifest(path: str) -> None:
    manifest = read_json(path)
    entries: list[tuple[str, str]] = []
    for item in manifest["files"]:
        content = (ROOT / item["path"]).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        assert digest == item["sha256"]
        assert len(content) == item["bytes"]
        entries.append((item["path"], digest))
    assert manifest["root_sha256"] == manifest_root(entries)


def test_persona_seed_has_exact_approved_archetype_counts() -> None:
    seed = read_json("personas/seed.json")
    counts = Counter(item["class"] for item in seed["primary_personas"])
    assert counts == {
        "public_life_event": 18,
        "business_organisation": 8,
        "professional_intermediary": 10,
        "agent_system": 12,
    }
    assert len(seed["primary_personas"]) == 48
    assert len({item["slug"] for item in seed["primary_personas"]}) == 48


def test_persona_profiles_are_evidenced_hypotheses_with_overlay_coverage() -> None:
    evidence = read_json("personas/evidence.json")
    evidence_ids = {item["evidence_id"] for item in evidence["references"]}
    overlays = read_json("personas/overlays/catalogue.json")
    overlay_ids = {item["overlay_id"] for item in overlays["overlays"]}
    profiles = [read_json(path.relative_to(ROOT).as_posix()) for path in sorted((ROOT / "personas/profiles").glob("*.json"))]

    assert len(profiles) == 48
    assert len({item["persona_id"] for item in profiles}) == 48
    assert {"high-stakes", "welsh-language", "screen-reader-keyboard", "agent_system"} - (
        overlay_ids | {item["archetype_class"] for item in profiles}
    ) == set()
    for profile in profiles:
        assert profile["evidence_status"] == "research_hypothesis_not_human_validated"
        assert set(profile["evidence_ids"]) <= evidence_ids
        assert set(profile["overlay_ids"]) <= overlay_ids
        assert profile["known_limitations"]
        assert_record_checksum(profile)


def test_every_primary_persona_has_a_story_and_all_pinned_formats_are_mapped() -> None:
    stories = read_jsonl(ROOT / "stories/catalogue.jsonl")
    coverage = read_json("stories/coverage.json")
    assert len(stories) == 48
    assert len({item["story_id"] for item in stories}) == 48
    assert len({persona_id for story in stories for persona_id in story["persona_ids"]}) == 48
    assert coverage["content_schema_family_count"] == 83
    assert len(coverage["mapped_content_schema_families"]) == 83
    assert coverage["unmapped_content_schema_families"] == []
    for story in stories:
        assert story["story_status"] == "research_hypothesis_pending_authorised_human_validation"
        assert story["target_entities"]
        assert story["target_relationships"]
        assert len(story["acceptance_criteria"]) >= 5
        assert_record_checksum(story)


def test_each_story_has_an_exact_unique_ten_by_ten_question_matrix() -> None:
    operation_ids = {item["id"] for item in OPERATIONS}
    challenge_ids = {item["id"] for item in CHALLENGES}
    all_ids: set[str] = set()
    all_wording: set[str] = set()
    total = 0
    for path in sorted((ROOT / "questions/bindings").glob("*.jsonl")):
        questions = read_jsonl(path)
        assert len(questions) == 100
        assert Counter(item["operation"] for item in questions) == Counter({item: 10 for item in operation_ids})
        assert Counter(item["challenge"] for item in questions) == Counter({item: 10 for item in challenge_ids})
        assert len({(item["operation"], item["challenge"]) for item in questions}) == 100
        assert sum(item["split"] == "held_out" for item in questions) == 20
        assert sum(item["expected_unanswerable"] for item in questions) == 10
        assert all(item["gold_status"] for item in questions)
        for question in questions:
            assert question["question_id"] not in all_ids
            all_ids.add(question["question_id"])
            wording = normalize_wording(question["wording"])
            assert wording not in all_wording
            all_wording.add(wording)
            assert_record_checksum(question)
        total += len(questions)
    assert total == 4_800


def test_each_primary_persona_has_exact_preregistered_suite_quotas() -> None:
    suites = sorted((ROOT / "questions/persona-suites").glob("*.jsonl"))
    assert len(suites) == 48
    for path in suites:
        records = read_jsonl(path)
        assert len(records) == 100
        assert len({item["question_id"] for item in records}) == 100
        assert Counter(item["suite_stratum"] for item in records) == Counter(SUITE_QUOTAS)
        assert len({item["story_id"] for item in records}) >= 1
        for record in records:
            assert_record_checksum(record)


def test_gold_catalogue_is_complete_but_does_not_claim_unverified_targets() -> None:
    records = read_jsonl(ROOT / "questions/gold/catalogue.jsonl")
    assert len(records) == 4_800
    assert len({item["question_id"] for item in records}) == 4_800
    assert sum(item["expected_unanswerable"] for item in records) == 480
    for record in records:
        gold = record["gold"]
        assert gold["verification_status"] == "pending_independent_review"
        assert gold["content_ids"] == []
        assert gold["urls"] == []
        if record["expected_unanswerable"]:
            assert record["gold_status"] == "contract_classified_unanswerable_pending_independent_review"
            assert gold["classification"] == "deliberately_unanswerable"
        else:
            assert record["gold_status"] == "pending_independent_corpus_verification"
            assert gold["classification"] == "answerable_candidate"
        assert_record_checksum(record)


def test_manifests_and_question_checksum_ledger_verify() -> None:
    for manifest in (
        "personas/manifest.json",
        "stories/manifest.json",
        "questions/manifest.json",
        "evaluation/manifest.json",
    ):
        assert_manifest(manifest)
    for line in (ROOT / "questions/checksums.txt").read_text(encoding="utf-8").splitlines():
        digest, path = line.split("  ", 1)
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest


def test_usage_and_evaluation_status_are_honest() -> None:
    usage = read_json("evaluation/development/build-usage.json")
    status = read_json("evaluation/development/status.json")
    preregistration = read_json("evaluation/protocol/preregistration.json")
    assert usage["model_usage"] == {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_gbp": 0.0}
    assert usage["human_research"]["status"] == "not_authorised_not_run"
    assert status["status"] == "not_run"
    assert status["result_claims"] == []
    assert preregistration["independent_gold_gate"]["status"].startswith("blocked")
    assert preregistration["human_evaluation"]["status"] == "not_authorised_not_run"


def test_generated_research_assets_are_current() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/build_research_assets.py", "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


class ResearchAssetTests(unittest.TestCase):
    def test_persona_seed_counts(self) -> None:
        test_persona_seed_has_exact_approved_archetype_counts()

    def test_evidenced_personas_and_overlays(self) -> None:
        test_persona_profiles_are_evidenced_hypotheses_with_overlay_coverage()

    def test_story_and_format_coverage(self) -> None:
        test_every_primary_persona_has_a_story_and_all_pinned_formats_are_mapped()

    def test_question_matrices(self) -> None:
        test_each_story_has_an_exact_unique_ten_by_ten_question_matrix()

    def test_persona_suite_quotas(self) -> None:
        test_each_primary_persona_has_exact_preregistered_suite_quotas()

    def test_gold_state(self) -> None:
        test_gold_catalogue_is_complete_but_does_not_claim_unverified_targets()

    def test_checksum_manifests(self) -> None:
        test_manifests_and_question_checksum_ledger_verify()

    def test_usage_and_result_status(self) -> None:
        test_usage_and_evaluation_status_are_honest()

    def test_generation_is_current(self) -> None:
        test_generated_research_assets_are_current()


if __name__ == "__main__":
    unittest.main()
