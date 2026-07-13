from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_aim_scorecard", ROOT / "scripts" / "build_aim_scorecard.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AimAssessmentTests(unittest.TestCase):
    def test_repository_scorecard_matches_checked_release_state(self) -> None:
        result = MODULE.build_assessment(ROOT)
        manifest = json.loads((ROOT / "release/manifest.yaml").read_text(encoding="utf-8"))
        status = json.loads((ROOT / "release/status.json").read_text(encoding="utf-8"))
        publication_ready = manifest["publication_ready"] is True
        self.assertEqual(publication_ready, result["gate_11"]["passed"])
        self.assertEqual("passed" if publication_ready else "pending", result["gate_11"]["status"])
        statuses = {row["aim_id"]: row["status"] for row in result["aims"]}
        if status["human_evaluation_status"] != "completed":
            self.assertEqual("not_yet_testable", statuses["AIM-004"])
        if manifest["snapshot"]["kind"] == "fixture":
            self.assertEqual("fixture_checkpoint", result["assessment_tier"])
            self.assertEqual(
                {
                    "fulfilled": 0,
                    "not_fulfilled": 0,
                    "not_yet_testable": 1,
                    "partly_fulfilled": 8,
                },
                result["counts"]["by_status"],
            )
            self.assertIn("E-SNAPSHOT-FULL", result["gate_11"]["unmet_check_ids"])
            self.assertIn("E-RIGHTS-RELEASE", result["gate_11"]["unmet_check_ids"])
        else:
            self.assertEqual("full_corpus", manifest["snapshot"]["kind"])
            self.assertFalse(manifest["snapshot"]["sampled"])
            self.assertIn(result["assessment_tier"], {"machine_release_candidate", "full_programme"})
            if publication_ready:
                self.assertEqual([], result["gate_11"]["unmet_check_ids"])
            else:
                self.assertTrue(result["gate_11"]["unmet_check_ids"])

    def test_every_aim_has_hashed_evidence_negative_findings_and_actions(self) -> None:
        result = MODULE.build_assessment(ROOT)
        for aim in result["aims"]:
            self.assertTrue(aim["evidence"])
            self.assertTrue(aim["negative_findings"])
            for evidence in aim["evidence"]:
                if evidence["sha256"] is not None:
                    self.assertEqual(64, len(evidence["sha256"]))
                    self.assertEqual(
                        MODULE.sha256_file(ROOT / evidence["path"]),
                        evidence["sha256"],
                    )
        human = next(row for row in result["aims"] if row["aim_id"] == "AIM-004")
        self.assertEqual("EXC-HUMAN-001", human["exceptions"][0]["exception_id"])
        self.assertTrue(human["next_actions"])

    def test_source_maps_all_contract_requirements_and_controlling_clauses(self) -> None:
        result = MODULE.build_assessment(ROOT)
        self.assertEqual(
            {"aims": 9, "requirements": 95, "controlling_clauses": 21},
            result["coverage"],
        )
        requirements = {row["id"] for row in json.loads((ROOT / "governance/requirements.yaml").read_text())["requirements"]}
        mapped = {item for aim in result["aims"] for item in aim["requirement_ids"]}
        self.assertEqual(requirements, mapped)

    def test_generated_document_validates_and_rendering_is_deterministic(self) -> None:
        first = MODULE.render(ROOT)
        second = MODULE.render(ROOT)
        self.assertEqual(first, second)
        document = json.loads(first[ROOT / "release/aim-assessment.json"])
        schema = json.loads((ROOT / "governance/aim-assessment.schema.json").read_text())
        Draft202012Validator(schema).validate(document)
        markdown = first[ROOT / "reports/aim-scorecard.md"]
        gate = document["gate_11"]["status"]
        self.assertIn(f"Acceptance Gate 11: `{gate}`", markdown)
        self.assertIn("`not_yet_testable`", markdown)

    def test_decision_order_keeps_human_unavailable_unmerited_and_accepts_negative_results(self) -> None:
        source = json.loads((ROOT / "governance/aim-assessment-source.json").read_text())
        aim = next(row for row in source["aims"] if row["aim_id"] == "AIM-004")
        matches = {check_id: False for check_id in source["evidence_checks"]}
        matches["E-HUMAN-NOT-AVAILABLE"] = True
        self.assertEqual("not_yet_testable", MODULE.choose_status(aim, matches))
        matches["E-HUMAN-NOT-AVAILABLE"] = False
        matches["E-HUMAN-AIM-FAILED"] = True
        self.assertEqual("not_fulfilled", MODULE.choose_status(aim, matches))
        matches["E-HUMAN-AIM-FAILED"] = False
        matches["E-HUMAN-COMPLETE"] = True
        matches["E-HUMAN-AIM-FULFILLED"] = True
        self.assertEqual("fulfilled", MODULE.choose_status(aim, matches))

    def test_referenced_evidence_cannot_escape_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "release/manifest.yaml"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"artifacts": {"reconciliation": "../outside.json"}}))
            row = MODULE.evaluate_check(
                root,
                "E-TEST",
                {
                    "description": "test",
                    "path": "release/manifest.yaml",
                    "pointer": "/artifacts/reconciliation",
                    "operator": "referenced_path_exists",
                    "negative_finding": "test",
                },
            )
            self.assertFalse(row["matched"])
            self.assertIn("unsafe", row["error"])

    def test_source_rejects_an_unknown_evidence_check(self) -> None:
        source = json.loads((ROOT / "governance/aim-assessment-source.json").read_text())
        requirements = json.loads((ROOT / "governance/requirements.yaml").read_text())
        traceability = json.loads((ROOT / "governance/traceability.json").read_text())
        broken = copy.deepcopy(source)
        broken["aims"][0]["evidence_check_ids"].append("E-NOT-DEFINED")
        with self.assertRaisesRegex(MODULE.AimAssessmentError, "unknown evidence checks"):
            MODULE.validate_source(broken, requirements, traceability)


if __name__ == "__main__":
    unittest.main()
