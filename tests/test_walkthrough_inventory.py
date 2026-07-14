from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_builder():
    path = ROOT / "scripts" / "build_walkthrough_inventory.py"
    spec = importlib.util.spec_from_file_location("build_walkthrough_inventory", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WalkthroughInventoryTests(unittest.TestCase):
    def test_walkthrough_inventory_covers_every_primary_persona_and_story(self) -> None:
        inventory = _load_builder().build_inventory()
        rows = inventory["rows"]
        self.assertEqual(
            inventory["counts"],
            {
                "classes": {
                    "agent_system": 12,
                    "business_organisation": 8,
                    "professional_intermediary": 10,
                    "public_life_event": 18,
                },
                "development_stories": 48,
                "primary_personas": 48,
                "representative_questions": 48,
            },
        )
        self.assertEqual(len({row["persona_id"] for row in rows}), 48)
        self.assertEqual(len({row["story_id"] for row in rows}), 48)
        self.assertTrue(
            all(row["story_status"] == "research_hypothesis_pending_authorised_human_validation" for row in rows)
        )
        self.assertTrue(all(row["representative_question"]["operation"] == "traverse_relationships" for row in rows))
        self.assertTrue(
            all(
                row["representative_question"]["gold_status"] == "pending_independent_corpus_verification"
                for row in rows
            )
        )

    def test_govuk_chat_walkthrough_is_non_personal_and_mapped_to_in_scope_stories(self) -> None:
        comparison = json.loads(
            (ROOT / "evaluation" / "govuk-chat" / "new-parent-multi-service.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            comparison["persona_ids"],
            ["persona-public-multi-service-life-event", "persona-public-parent-carer"],
        )
        self.assertEqual(comparison["story_ids"], ["story-multi-service-life-event", "story-parent-carer"])
        self.assertEqual(
            [turn["turn_id"] for turn in comparison["turns"]],
            ["new-parent-01", "new-parent-02", "new-parent-03", "new-parent-04", "new-parent-05"],
        )
        self.assertIn("personal information", comparison["capture_contract"]["do_not_store"])
        published = json.loads(
            (ROOT / "evaluation" / "govuk-chat" / "official-published-example.json").read_text(encoding="utf-8")
        )
        self.assertEqual(published["status"], "official_published_example_not_a_live_replay")
        self.assertEqual(len(published["source_cards"]), 2)
        self.assertEqual(len(published["capture"]["asset_sha256"]), 64)
