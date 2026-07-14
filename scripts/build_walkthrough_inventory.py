#!/usr/bin/env python3
"""Build or check the persona, story and recall-provenance walkthrough inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SEED_PATH = ROOT / "personas" / "seed.json"
EVIDENCE_PATH = ROOT / "personas" / "evidence.json"
STORIES_PATH = ROOT / "stories" / "catalogue.jsonl"
QUESTIONS_ROOT = ROOT / "questions" / "bindings"
COMPARISON_PATH = ROOT / "evaluation" / "govuk-chat" / "new-parent-multi-service.json"
PUBLISHED_EXAMPLE_PATH = ROOT / "evaluation" / "govuk-chat" / "official-published-example.json"
JSON_OUTPUT = ROOT / "evaluation" / "walkthroughs" / "persona-story-inventory.json"
MARKDOWN_OUTPUT = ROOT / "reports" / "persona-story-walkthroughs.md"

CLASS_LABELS = {
    "public_life_event": "Public life events",
    "business_organisation": "Businesses and organisations",
    "professional_intermediary": "Professional intermediaries",
    "agent_system": "Agents and systems",
}


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _persona_id(seed: dict[str, Any]) -> str:
    prefixes = {
        "public_life_event": "public",
        "business_organisation": "business",
        "professional_intermediary": "professional",
        "agent_system": "agent",
    }
    return f"persona-{prefixes[seed['class']]}-{seed['slug']}"


def _question_for_story(story_id: str) -> tuple[dict[str, Any], Path]:
    path = QUESTIONS_ROOT / f"{story_id}.jsonl"
    candidates = [
        question
        for question in _jsonl(path)
        if question["operation"] == "traverse_relationships" and question["challenge"] == "direct"
    ]
    if len(candidates) != 1:
        raise ValueError(f"{story_id} must have exactly one direct relationship-traversal question")
    return candidates[0], path


def build_inventory() -> dict[str, Any]:
    seed_document = _json(SEED_PATH)
    evidence_document = _json(EVIDENCE_PATH)
    stories = _jsonl(STORIES_PATH)
    comparison = _json(COMPARISON_PATH)
    published_example = _json(PUBLISHED_EXAMPLE_PATH)

    stories_by_persona: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for story in stories:
        if len(story["persona_ids"]) != 1:
            raise ValueError(f"{story['story_id']} is not a one-primary-persona development story")
        stories_by_persona[story["persona_ids"][0]].append(story)

    evidence_by_id = {item["evidence_id"]: item for item in evidence_document["references"]}
    rows: list[dict[str, Any]] = []
    question_paths: set[Path] = set()
    for seed in seed_document["primary_personas"]:
        persona_id = _persona_id(seed)
        matching_stories = stories_by_persona.get(persona_id, [])
        if len(matching_stories) != 1:
            raise ValueError(f"{persona_id} must map to exactly one development story")
        story = matching_stories[0]
        question, question_path = _question_for_story(story["story_id"])
        question_paths.add(question_path)
        evidence = []
        for evidence_id in story["evidence_ids"]:
            if evidence_id not in evidence_by_id:
                raise ValueError(f"{story['story_id']} refers to unknown evidence {evidence_id}")
            item = evidence_by_id[evidence_id]
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "title": item["title"],
                    "url": item.get("url"),
                    "source_path": item.get("source_path"),
                    "verification_status": item["verification_status"],
                    "limitations": item["limitations"],
                }
            )

        rows.append(
            {
                "persona_class": seed["class"],
                "persona_id": persona_id,
                "persona_title": seed["title"],
                "channel_class": seed["channel_class"],
                "risk_level": seed["risk_level"],
                "jurisdiction": seed["jurisdiction"],
                "overlay_ids": seed["overlays"],
                "story_id": story["story_id"],
                "story_status": story["story_status"],
                "given": story["given"],
                "need": story["need"],
                "so_that": story["so_that"],
                "statement": story["statement"],
                "target_entities": story["target_entities"],
                "target_relationships": story["target_relationships"],
                "evidence": evidence,
                "representative_question": {
                    "question_id": question["question_id"],
                    "wording": question["wording"],
                    "operation": question["operation"],
                    "challenge": question["challenge"],
                    "gold_status": question["gold_status"],
                    "provenance_requirements": question["provenance_requirements"],
                },
                "recall_contract": {
                    "discovery": "Bundle search result and typed route/relationship traversal",
                    "record": "Checksummed record shard with canonical URL and source-native identity",
                    "evidence": "Evidence locator, retrieval time, derivation and confidence on the selected record",
                    "authoritative_answer": "Retrieve current page content only from the canonical GOV.UK hand-off",
                    "comparator": "Keep GOV.UK Chat answer text and ordered GOV.UK source cards separate from bundle evidence",
                },
            }
        )

    if len(rows) != 48 or len({row["persona_id"] for row in rows}) != 48:
        raise ValueError("the walkthrough inventory must contain 48 unique primary personas")
    if len({row["story_id"] for row in rows}) != 48:
        raise ValueError("the walkthrough inventory must contain 48 unique development stories")

    class_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        class_counts[row["persona_class"]] += 1

    source_paths = [
        SEED_PATH,
        EVIDENCE_PATH,
        STORIES_PATH,
        COMPARISON_PATH,
        PUBLISHED_EXAMPLE_PATH,
        *sorted(question_paths),
    ]
    return {
        "schema": "govuk-okf-persona-story-walkthrough-inventory.v1",
        "status": "development_hypotheses_not_human_validated",
        "scope": {
            "checked_in_development": "one deterministic story per primary persona",
            "release_v2": "six corpus-anchored stories per primary persona after final snapshot closure",
            "release_v2_status": "not included in this inventory; final-snapshot regeneration pending",
        },
        "counts": {
            "primary_personas": len(rows),
            "development_stories": len(rows),
            "representative_questions": len(rows),
            "classes": dict(sorted(class_counts.items())),
        },
        "recall_layers": [
            {
                "layer": "persona_and_story_hypothesis",
                "source": "personas/seed.json, personas/evidence.json and stories/catalogue.jsonl",
                "claim_boundary": "Explains why the task is in scope; it is not participant validation.",
            },
            {
                "layer": "question_contract",
                "source": "questions/bindings/story-*.jsonl",
                "claim_boundary": "Defines the retrieval task and provenance fields; gold remains pending corpus verification.",
            },
            {
                "layer": "bundle_discovery",
                "source": "search shard, record shard, route index and relationship adjacency",
                "claim_boundary": "Finds and connects source-native metadata; it does not answer from retained page bodies.",
            },
            {
                "layer": "authoritative_retrieval",
                "source": "canonical GOV.UK URL selected from the bundle",
                "claim_boundary": "Current page content is recalled from GOV.UK at answer time, outside the metadata bundle.",
            },
            {
                "layer": "govuk_chat_comparator",
                "source": "captured answer plus ordered source cards",
                "claim_boundary": "A time-stamped comparator observation, not OKF ground truth or a stable GOV.UK Chat contract.",
            },
        ],
        "selected_comparison_walkthrough": {
            "path": COMPARISON_PATH.relative_to(ROOT).as_posix(),
            "walkthrough_id": comparison["walkthrough_id"],
            "persona_ids": comparison["persona_ids"],
            "story_ids": comparison["story_ids"],
            "status": comparison["status"],
            "rights_and_reuse": comparison["rights_and_reuse"],
            "published_example": {
                "path": PUBLISHED_EXAMPLE_PATH.relative_to(ROOT).as_posix(),
                "question": published_example["question"],
                "status": published_example["status"],
                "rights_and_reuse": published_example["rights_and_reuse"],
            },
        },
        "generated_from": [
            {"path": path.relative_to(ROOT).as_posix(), "sha256": _sha256(path)} for path in source_paths
        ],
        "rows": rows,
    }


def render_markdown(inventory: dict[str, Any]) -> str:
    lines = [
        "# Persona and user-story walkthrough inventory",
        "",
        "This report lists every primary persona and checked-in development user story currently in scope for the What’s on GOV.UK OKF bundle. It is generated deterministically from the persona, story, evidence and question contracts; run `python3 scripts/build_walkthrough_inventory.py --check` to verify it.",
        "",
        "> These 48 personas and 48 stories are research hypotheses, not findings from authorised participant research. The release-v2 design calls for six corpus-anchored stories per persona (288 total), but those are not in this inventory until the hydrated final snapshot is closed and independently verified.",
        "",
        "## Where recalled information comes from",
        "",
        "| Layer | Recalled from | Boundary |",
        "|---|---|---|",
    ]
    for layer in inventory["recall_layers"]:
        lines.append(f"| `{layer['layer']}` | {layer['source']} | {layer['claim_boundary']} |")
    counts = inventory["counts"]
    lines.extend(
        [
            "",
            "The checked-in development inventory contains "
            f"{counts['primary_personas']} personas, {counts['development_stories']} stories and "
            f"{counts['representative_questions']} representative relationship-traversal questions.",
            "",
        ]
    )

    rows_by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in inventory["rows"]:
        rows_by_class[row["persona_class"]].append(row)

    for class_id in CLASS_LABELS:
        rows = rows_by_class[class_id]
        lines.extend(
            [
                f"## {CLASS_LABELS[class_id]} ({len(rows)})",
                "",
                "| Persona | Development user story | Risk and scope | Recall anchors | Representative question |",
                "|---|---|---|---|---|",
            ]
        )
        for row in rows:
            entities = ", ".join(f"`{value}`" for value in row["target_entities"])
            relationships = ", ".join(f"`{value}`" for value in row["target_relationships"])
            evidence = ", ".join(f"`{item['evidence_id']}`" for item in row["evidence"])
            story_text = f"Given {row['given']} Need: {row['need']}."
            risk_scope = f"{row['risk_level']}; entities: {entities}; relationships: {relationships}"
            question = row["representative_question"]
            lines.append(
                f"| **{row['persona_title']}**<br><code>{row['persona_id']}</code> "
                f"| {story_text}<br><code>{row['story_id']}</code> "
                f"| {risk_scope} | {evidence} | {question['wording']}<br><code>{question['question_id']}</code> |"
            )
        lines.append("")

    comparison = inventory["selected_comparison_walkthrough"]
    lines.extend(
        [
            "## Selected GOV.UK Chat comparison",
            "",
            f"`{comparison['walkthrough_id']}` combines the **Person navigating a multi-service life event** and **Parent or carer** stories. It starts with the public GOV.UK Chat example “I’ve just had a baby, what help can I get?” and then asks for applicability boundaries, ordered next steps and the GOV.UK pages used.",
            "",
            f"The prompt and capture contract is `{comparison['path']}`. GOV.UK Chat answer text and source-card order must be captured with a retrieval time and kept separate from bundle-derived evidence. Current capture status: `{comparison['status']}`.",
            "",
            f"One official published question/answer example is already recorded at `{comparison['published_example']['path']}`: “{comparison['published_example']['question']}” Its status is `{comparison['published_example']['status']}`; it is not a live replay of the new-parent journey.",
            "",
            "The machine-readable walkthrough also carries each comparator asset's explicit rights disposition. The repository retains only links, minimal source-card metadata, one bounded attributed excerpt, a structured paraphrase and the official image digest; it does not retain or publish the image bytes. These controls trigger item-level review and are not a legal conclusion that OGL, fair dealing or another permission applies.",
            "",
            "## Machine-readable companion",
            "",
            "The complete field-level inventory, expanded evidence references, provenance requirements and source-file hashes are in `evaluation/walkthroughs/persona-story-inventory.json`.",
            "",
        ]
    )
    return "\n".join(lines)


def _encoded_json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _write_or_check(path: Path, content: bytes, *, check: bool) -> bool:
    if check:
        return path.is_file() and path.read_bytes() == content
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if generated outputs are not current")
    args = parser.parse_args()

    inventory = build_inventory()
    outputs = {
        JSON_OUTPUT: _encoded_json(inventory),
        MARKDOWN_OUTPUT: render_markdown(inventory).encode("utf-8"),
    }
    stale = [path for path, content in outputs.items() if not _write_or_check(path, content, check=args.check)]
    if stale:
        for path in stale:
            print(f"stale walkthrough projection: {path.relative_to(ROOT)}", file=sys.stderr)
        return 1
    print("walkthrough inventory is synchronized" if args.check else "wrote persona/story walkthrough inventory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
