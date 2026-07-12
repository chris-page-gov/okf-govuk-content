from __future__ import annotations

import hashlib
import json
import sys
import unittest
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.persona_saturation_validation import EXPECTED_DIMENSIONS, validate


def read_json(path: str) -> dict[str, object]:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class PersonaSaturationTests(unittest.TestCase):
    def test_independent_validator_passes_machine_scope_only(self) -> None:
        report = validate(ROOT)
        self.assertTrue(report["machine_validations_passed"], report["errors"])
        self.assertEqual(report["counts"]["primary_personas"], 48)
        self.assertEqual(report["counts"]["initial_overlays"], 16)
        self.assertEqual(report["counts"]["current_overlays"], 17)
        self.assertEqual(report["counts"]["coverage_dimensions"], 11)
        self.assertEqual(report["human_validation_status"], "not_authorised_not_run")
        self.assertEqual(report["human_ui_preference_status"], "not_yet_testable")
        self.assertEqual(report["final_snapshot_question_regeneration"], "required")

    def test_coverage_matrix_has_all_dimensions_and_no_machine_gap(self) -> None:
        matrix = read_json("personas/coverage-matrix.json")
        self.assertEqual(tuple(matrix["required_dimensions"]), EXPECTED_DIMENSIONS)
        self.assertEqual(matrix["unexplained_machine_dimension_gaps"], [])
        self.assertEqual(matrix["counts"]["primary_personas"], 48)
        self.assertEqual(matrix["counts"]["content_schema_families"], 83)
        self.assertEqual(matrix["counts"]["release_story_contracts"], 288)

    def test_overlay_array_covers_every_pair_and_high_risk_tway(self) -> None:
        catalogue = read_json("personas/overlays/catalogue.json")
        overlay_ids = sorted(item["overlay_id"] for item in catalogue["overlays"])
        array = read_json("personas/overlay-covering-array.json")
        pair_rows = [row for row in array["rows"] if row["strength"] == 2]
        triple_rows = [row for row in array["rows"] if row["strength"] == 3]
        self.assertEqual({tuple(row["overlay_ids"]) for row in pair_rows}, set(combinations(overlay_ids, 2)))
        self.assertEqual(len(pair_rows), 136)
        self.assertEqual(len(triple_rows), 5)
        self.assertTrue(array["all_pairs_covered"])
        self.assertTrue(all(row["evidence_status"] == "research_hypothesis_not_human_validated" for row in array["rows"]))

    def test_hash_bound_challenges_reach_two_successive_zero_novel_passes(self) -> None:
        saturation = read_json("personas/saturation.json")
        passes = []
        for reference in saturation["challenge_passes"]:
            path = ROOT / reference["path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), reference["sha256"])
            passes.append(json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(len(passes), 3)
        self.assertEqual(passes[0]["novel_valid_use_classes"], 1)
        self.assertIn("privacy-sensitive-context", saturation["new_overlay_hypotheses"])
        self.assertTrue(all(item["novel_valid_use_classes"] == 0 for item in passes[-2:]))
        self.assertTrue(saturation["stopping_rule"]["passed"])
        self.assertEqual(saturation["machine_applicable_gate_status"], "passed")
        self.assertEqual(saturation["human_validation_status"], "not_authorised_not_run")
        self.assertEqual(saturation["human_ui_preference_status"], "not_yet_testable")


if __name__ == "__main__":
    unittest.main()
