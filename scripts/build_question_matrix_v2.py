#!/usr/bin/env python3
"""Build the release-quality, corpus-anchored question matrix.

The command assigns gold targets but intentionally does not verify them.  Run
``verify_question_matrix_v2.py`` as a separate process and retain its report.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
PERSONA_SATURATION_PATH = ROOT / "personas" / "saturation.json"
PERSONA_COVERAGE_ROWS_PATH = ROOT / "personas" / "coverage-matrix.jsonl"

from govuk_okf.question_factory import record_with_checksum  # noqa: E402
from govuk_okf.question_matrix_v2 import (  # noqa: E402
    CHALLENGES,
    GENERATOR_VERSION,
    LEAKAGE_DENYLIST,
    MATRIX_VERSION,
    OPERATIONS,
    QUESTIONS_PER_PERSONA_SUITE,
    QUESTIONS_PER_STORY,
    STORIES_PER_PERSONA,
    STORY_ROLES,
    assign_splits,
    build_story,
    build_story_questions,
    choose_anchors,
    curate_suite,
    load_anchor_pool,
    manifest_for,
    release_prerequisites,
    sha256_file,
)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def json_line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def load_personas(limit: int | None) -> list[dict[str, Any]]:
    paths = sorted((ROOT / "personas" / "profiles").glob("*.json"))
    personas = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if limit is not None:
        personas = personas[:limit]
    if not personas:
        raise ValueError("no persona profiles found")
    return personas


def load_optional_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def load_persona_saturation(personas: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    saturation = json.loads(PERSONA_SATURATION_PATH.read_text(encoding="utf-8"))
    if saturation.get("machine_applicable_gate_status") != "passed":
        raise ValueError("persona machine saturation is not passed")
    if saturation.get("human_validation_status") != "not_authorised_not_run":
        raise ValueError("persona human-validation boundary is missing")
    rows = {
        row["persona_id"]: row
        for row in (
            json.loads(line)
            for line in PERSONA_COVERAGE_ROWS_PATH.read_text(encoding="utf-8").splitlines()
            if line
        )
    }
    persona_ids = {persona["persona_id"] for persona in personas}
    if not persona_ids <= set(rows):
        raise ValueError("persona saturation rows do not cover the selected persona set")
    return saturation, {persona_id: rows[persona_id] for persona_id in persona_ids}


def safe_output(path: Path) -> Path:
    resolved = path.resolve()
    forbidden = {Path("/").resolve(), Path.home().resolve(), ROOT.resolve()}
    if resolved in forbidden or ROOT.resolve() not in resolved.parents:
        raise ValueError("output must be a dedicated directory beneath the repository root")
    return resolved


def build(args: argparse.Namespace, output_root: Path) -> dict[str, Any]:
    personas = load_personas(args.persona_limit)
    saturation, saturation_rows = load_persona_saturation(personas)
    saturation_sha256 = sha256_file(PERSONA_SATURATION_PATH)
    coverage_matrix_sha256 = sha256_file(ROOT / saturation["coverage_matrix"]["path"])
    if coverage_matrix_sha256 != saturation["coverage_matrix"]["sha256"]:
        raise ValueError("persona saturation coverage-matrix hash mismatch")
    pool, eligible_record_count = load_anchor_pool(args.corpus)
    assignments, anchor_blockers = choose_anchors(personas, pool)
    split_by_identity = assign_splits(assignments)
    index = {record.identity: record for record in pool}
    snapshot_manifest_path = args.snapshot_manifest or args.corpus
    snapshot_manifest_sha256 = sha256_file(snapshot_manifest_path)
    reconciliation = load_optional_json(args.reconciliation)
    eligible, eligibility_blockers = release_prerequisites(
        mode=args.mode,
        snapshot_id=args.snapshot_id,
        personas=personas,
        reconciliation=reconciliation,
        blockers=anchor_blockers,
    )
    if args.mode == "release" and args.snapshot_manifest is None:
        eligible = False
        eligibility_blockers.append("missing_independent_snapshot_manifest")
    if eligible_record_count < len(personas) * STORIES_PER_PERSONA:
        eligible = False
        eligibility_blockers.append(
            f"insufficient_unique_corpus_anchors:{eligible_record_count}:required:{len(personas) * STORIES_PER_PERSONA}"
        )
    eligibility_blockers = sorted(set(eligibility_blockers))

    matrix_path = output_root / "matrix.json"
    write_text(
        matrix_path,
        json_text(
            {
                "schema_version": 2,
                "matrix_version": MATRIX_VERSION,
                "operations": OPERATIONS,
                "challenge_modes": CHALLENGES,
                "story_roles": STORY_ROLES,
                "questions_per_story": QUESTIONS_PER_STORY,
                "stories_per_persona": STORIES_PER_PERSONA,
                "questions_per_persona_suite": QUESTIONS_PER_PERSONA_SUITE,
                "split_rule": "canonical anchor identity groups; every fifth sorted group held out",
                "leakage_denylist": LEAKAGE_DENYLIST,
                "persona_saturation": {
                    "path": "persona-saturation.json",
                    "source_path": PERSONA_SATURATION_PATH.relative_to(ROOT).as_posix(),
                    "sha256": saturation_sha256,
                    "coverage_matrix_path": "persona-coverage-matrix.json",
                    "coverage_matrix_sha256": coverage_matrix_sha256,
                    "machine_applicable_gate_status": saturation["machine_applicable_gate_status"],
                    "human_validation_status": saturation["human_validation_status"],
                    "human_ui_preference_status": saturation["human_ui_preference_status"],
                },
            }
        ),
    )

    story_path = output_root / "stories" / "catalogue.jsonl"
    story_path.parent.mkdir(parents=True, exist_ok=True)
    gold_path = output_root / "gold" / "catalogue.jsonl"
    gold_path.parent.mkdir(parents=True, exist_ok=True)
    saturation_copy_path = output_root / "persona-saturation.json"
    coverage_matrix_copy_path = output_root / "persona-coverage-matrix.json"
    write_text(saturation_copy_path, PERSONA_SATURATION_PATH.read_text(encoding="utf-8"))
    write_text(
        coverage_matrix_copy_path,
        (ROOT / saturation["coverage_matrix"]["path"]).read_text(encoding="utf-8"),
    )
    generated_files: list[Path] = [
        matrix_path,
        saturation_copy_path,
        coverage_matrix_copy_path,
        story_path,
        gold_path,
    ]
    question_count = 0
    suite_count = 0
    story_count = 0
    answerable_count = 0
    operation_counts: Counter[str] = Counter()
    challenge_counts: Counter[str] = Counter()
    split_counts: Counter[str] = Counter()
    story_split_counts: Counter[str] = Counter()

    with story_path.open("w", encoding="utf-8") as story_handle, gold_path.open("w", encoding="utf-8") as gold_handle:
        for persona in personas:
            persona_story_questions: list[list[dict[str, Any]]] = []
            for ordinal, (role, anchor) in enumerate(zip(STORY_ROLES, assignments[persona["persona_id"]]), start=1):
                story = build_story(
                    persona,
                    role,
                    anchor,
                    ordinal,
                    coverage_dimensions=saturation_rows[persona["persona_id"]]["dimension_values"],
                    persona_saturation_sha256=saturation_sha256,
                )
                story_handle.write(json_line(story))
                story_count += 1
                split = split_by_identity[anchor.identity]
                story_split_counts[split] += 1
                questions = build_story_questions(
                    persona=persona,
                    story=story,
                    record=anchor,
                    split=split,
                    snapshot_id=args.snapshot_id,
                    snapshot_date=args.snapshot_date,
                    snapshot_manifest_sha256=snapshot_manifest_sha256,
                    pool=pool,
                    index=index,
                )
                binding_path = output_root / "bindings" / f"{story['story_id']}.jsonl"
                write_text(binding_path, "".join(json_line(item) for item in questions))
                generated_files.append(binding_path)
                persona_story_questions.append(questions)
                for question in questions:
                    gold_record = record_with_checksum(
                        {
                            "schema_version": 2,
                            "question_id": question["question_id"],
                            "question_checksum": question["checksum"],
                            "story_id": question["story_id"],
                            "persona_ids": question["persona_ids"],
                            "split": question["split"],
                            "gold_status": question["gold_status"],
                            "gold": question["gold"],
                        }
                    )
                    gold_handle.write(json_line(gold_record))
                    question_count += 1
                    answerable_count += not question["expected_unanswerable"]
                    operation_counts[question["operation"]] += 1
                    challenge_counts[question["challenge"]] += 1
                    split_counts[question["split"]] += 1

            suite = curate_suite(persona, persona_story_questions)
            suite_path = output_root / "persona-suites" / f"{persona['persona_id']}.jsonl"
            write_text(suite_path, "".join(json_line(item) for item in suite))
            generated_files.append(suite_path)
            suite_count += len(suite)

    split_groups_path = output_root / "split-groups.json"
    write_text(
        split_groups_path,
        json_text(
            {
                "schema_version": 2,
                "rule": "all questions sharing a canonical gold anchor identity use one split",
                "groups": split_by_identity,
                "counts": dict(sorted(Counter(split_by_identity.values()).items())),
            }
        ),
    )
    generated_files.append(split_groups_path)

    usage_path = output_root / "generation-usage.json"
    write_text(
        usage_path,
        json_text(
            {
                "schema_version": 2,
                "generation_method": GENERATOR_VERSION,
                "model_usage": {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_gbp": 0.0},
                "source_access": {
                    "mode": "frozen_local_metadata_envelope",
                    "network_requests": 0,
                    "restrictions": ["No body content is read or retained.", "Only the supplied frozen source records are used."],
                },
                "licensing_and_fair_use_triggers": [
                    "Attachment metadata is retained, but attachment bodies are not copied.",
                    "Canonical GOV.UK source links and evidence hashes are preserved.",
                    "Third-party material remains subject to item-level review before redistribution.",
                ],
                "fallbacks_used": [
                    "Source-native content type and schema paths provide a deterministic two-hop gold path when no admitted link target is present."
                ],
                "judge_separation": "This run assigns targets only; verification must be a separate validator process.",
                "persona_saturation": {
                    "sha256": saturation_sha256,
                    "model_usage": saturation["model_usage"],
                    "final_snapshot_question_regeneration": "this generator run",
                },
            }
        ),
    )
    generated_files.append(usage_path)

    contract_path = output_root / "contract.json"
    write_text(
        contract_path,
        json_text(
            {
                "schema_version": 2,
                "contract_id": "govuk-question-release-contract-v2",
                "matrix_version": MATRIX_VERSION,
                "mode": args.mode,
                "artifact_tier": "release_candidate" if eligible else "development_only",
                "publication_ready_candidate": eligible,
                "question_contract_passed": False,
                "independent_verification_status": "required_not_run",
                "eligibility_blockers": eligibility_blockers,
                "snapshot": {
                    "snapshot_id": args.snapshot_id,
                    "snapshot_date": args.snapshot_date,
                    "snapshot_manifest_path": str(snapshot_manifest_path),
                    "snapshot_manifest_sha256": snapshot_manifest_sha256,
                    "corpus_path": str(args.corpus),
                    "corpus_sha256": sha256_file(args.corpus),
                    "eligible_source_records": eligible_record_count,
                },
                "corpus_reconciliation": {
                    "path": str(args.reconciliation) if args.reconciliation else None,
                    "sha256": sha256_file(args.reconciliation) if args.reconciliation else None,
                },
                "release_rule": (
                    "A separate deterministic validator must verify every target, path, near miss, checksum, split and leakage control. "
                    "Only its report may set question_contract_passed=true."
                ),
                "persona_saturation": {
                    "path": "persona-saturation.json",
                    "source_path": PERSONA_SATURATION_PATH.relative_to(ROOT).as_posix(),
                    "sha256": saturation_sha256,
                    "coverage_matrix_path": "persona-coverage-matrix.json",
                    "coverage_matrix_sha256": coverage_matrix_sha256,
                    "machine_applicable_gate_status": saturation["machine_applicable_gate_status"],
                    "human_validation_status": saturation["human_validation_status"],
                    "human_ui_preference_status": saturation["human_ui_preference_status"],
                },
            }
        ),
    )
    generated_files.append(contract_path)

    checksum_path = output_root / "checksums.txt"
    write_text(
        checksum_path,
        "".join(
            f"{sha256_file(path)}  {path.relative_to(output_root).as_posix()}\n" for path in sorted(generated_files)
        ),
    )
    generated_files.append(checksum_path)
    manifest = manifest_for(
        output_root,
        generated_files,
        {
            "primary_personas": len(personas),
            "stories": story_count,
            "stories_per_persona": STORIES_PER_PERSONA,
            "questions": question_count,
            "questions_per_story": QUESTIONS_PER_STORY,
            "persona_suite_entries": suite_count,
            "persona_suite_entries_per_persona": QUESTIONS_PER_PERSONA_SUITE,
            "answerable_questions": answerable_count,
            "deliberately_unanswerable_questions": question_count - answerable_count,
            "operations": dict(sorted(operation_counts.items())),
            "challenge_modes": dict(sorted(challenge_counts.items())),
            "question_splits": dict(sorted(split_counts.items())),
            "story_splits": dict(sorted(story_split_counts.items())),
            "eligible_source_records": eligible_record_count,
            "bounded_anchor_pool_records": len(pool),
        },
        {
            "snapshot_id": args.snapshot_id,
            "snapshot_date": args.snapshot_date,
            "snapshot_manifest_sha256": snapshot_manifest_sha256,
            "artifact_tier": "release_candidate" if eligible else "development_only",
            "publication_ready_candidate": eligible,
            "independent_verification_status": "required_not_run",
            "persona_saturation_sha256": saturation_sha256,
            "persona_coverage_matrix_sha256": coverage_matrix_sha256,
            "persona_human_validation_status": saturation["human_validation_status"],
            "human_ui_preference_status": saturation["human_ui_preference_status"],
        },
    )
    manifest_path = output_root / "manifest.json"
    write_text(manifest_path, json_text(manifest))
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True, help="Frozen source-record JSONL or JSONL.GZ")
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--snapshot-date", required=True, help="ISO 8601 date or timestamp")
    parser.add_argument("--snapshot-manifest", type=Path)
    parser.add_argument("--reconciliation", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "questions" / "release-v2")
    parser.add_argument("--mode", choices=("development-fixture", "release"), default="development-fixture")
    parser.add_argument("--persona-limit", type=int, help="Development/test only")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.corpus = args.corpus.resolve()
    if args.snapshot_manifest:
        args.snapshot_manifest = args.snapshot_manifest.resolve()
    if args.reconciliation:
        args.reconciliation = args.reconciliation.resolve()
    if args.mode == "release" and args.persona_limit is not None:
        raise SystemExit("--persona-limit is forbidden in release mode")
    output = safe_output(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}-building-", dir=output.parent) as temporary:
        temporary_root = Path(temporary)
        manifest = build(args, temporary_root)
        backup = output.with_name(f".{output.name}-previous")
        if backup.exists():
            shutil.rmtree(backup)
        if output.exists():
            os.replace(output, backup)
        try:
            os.replace(temporary_root, output)
        except Exception:
            if backup.exists() and not output.exists():
                os.replace(backup, output)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    print(
        json.dumps(
            {
                "output": str(output),
                "root_sha256": manifest["root_sha256"],
                "questions": manifest["counts"]["questions"],
                "artifact_tier": manifest["artifact_tier"],
                "publication_ready_candidate": manifest["publication_ready_candidate"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
