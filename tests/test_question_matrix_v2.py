from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.question_matrix_v2 import reconciliation_release_errors, release_prerequisites
from govuk_okf.question_matrix_v2_validator import (
    Validation,
    load_control_json,
    resolve_matrix_artifact,
    trusted_release_errors,
    verify,
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class QuestionMatrixV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = Path(tempfile.mkdtemp(prefix=".test-question-v2-", dir=ROOT))
        cls.matrix = cls.temporary / "matrix"
        cls.corpus = ROOT / "tests" / "fixtures" / "corpus" / "source-records.jsonl"
        result = subprocess.run(
            [
                sys.executable,
                "scripts/build_question_matrix_v2.py",
                "--corpus",
                str(cls.corpus),
                "--snapshot-id",
                "fixture-v2",
                "--snapshot-date",
                "2026-07-12",
                "--output",
                str(cls.matrix),
                "--persona-limit",
                "2",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        verify = subprocess.run(
            [
                sys.executable,
                "scripts/verify_question_matrix_v2.py",
                "--matrix",
                str(cls.matrix),
                "--corpus",
                str(cls.corpus),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if verify.returncode:
            raise AssertionError(verify.stdout + verify.stderr)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.temporary)

    def test_six_corpus_anchored_stories_per_persona(self) -> None:
        stories = read_jsonl(self.matrix / "stories" / "catalogue.jsonl")
        by_persona: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for story in stories:
            by_persona[story["persona_ids"][0]].append(story)
            anchor = story["anchor"]
            self.assertTrue(anchor["content_id"] or anchor["url"])
            self.assertRegex(anchor["record_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(anchor["source_evidence_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(len(anchor["record_sha256"]), 64)
            self.assertEqual(len(story["persona_saturation_sha256"]), 64)
            self.assertEqual(len(story["coverage_dimensions"]), 11)
        self.assertEqual(len(by_persona), 2)
        self.assertTrue(all(len(items) == 6 for items in by_persona.values()))
        self.assertTrue(all(len({item["story_role"] for item in items}) == 6 for items in by_persona.values()))

    def test_matrix_is_hash_bound_to_machine_saturation_without_human_claims(self) -> None:
        contract = json.loads((self.matrix / "contract.json").read_text(encoding="utf-8"))
        manifest = json.loads((self.matrix / "manifest.json").read_text(encoding="utf-8"))
        saturation = json.loads((self.matrix / "persona-saturation.json").read_text(encoding="utf-8"))
        saturation_sha256 = hashlib.sha256((self.matrix / "persona-saturation.json").read_bytes()).hexdigest()
        self.assertEqual(contract["persona_saturation"]["sha256"], saturation_sha256)
        self.assertEqual(manifest["persona_saturation_sha256"], saturation_sha256)
        self.assertEqual(saturation["machine_applicable_gate_status"], "passed")
        self.assertEqual(saturation["human_validation_status"], "not_authorised_not_run")
        self.assertEqual(saturation["human_ui_preference_status"], "not_yet_testable")
        first_binding = sorted((self.matrix / "bindings").glob("*.jsonl"))[0]
        question = read_jsonl(first_binding)[0]
        self.assertEqual(question["persona_saturation_sha256"], saturation_sha256)
        self.assertEqual(len(question["coverage_dimensions"]), 11)

    def test_matrix_artifact_paths_fail_closed_before_out_of_root_io(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            matrix = parent / "matrix"
            matrix.mkdir()
            outside = parent / "outside.json"
            outside.write_text('{"sentinel":true}\n', encoding="utf-8")
            (matrix / "linked.json").symlink_to(outside)
            for value in ("../outside.json", str(outside), "linked.json"):
                validation = Validation()
                self.assertIsNone(
                    resolve_matrix_artifact(matrix, value, validation, "persona_saturation_path_safe")
                )
                self.assertEqual(validation.error_count, 1)
                self.assertIn("persona_saturation_path_safe", validation.errors[0])

    def test_full_verifier_does_not_read_unsafe_matrix_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            matrix = parent / "matrix"
            shutil.copytree(self.matrix, matrix)
            outside = parent / "outside.json"
            outside.write_text('{"sentinel":true}\n', encoding="utf-8")
            contract_path = matrix / "contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["persona_saturation"]["path"] = "../outside.json"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            loaded: list[Path] = []

            def recording_loader(path: Path) -> dict[str, Any]:
                loaded.append(path.resolve())
                return load_control_json(path)

            with unittest.mock.patch(
                "govuk_okf.question_matrix_v2_validator.load_control_json",
                side_effect=recording_loader,
            ):
                report = verify(matrix, self.corpus)
            self.assertTrue(
                any(error.startswith("persona_saturation_path_safe:") for error in report["errors"])
            )
            self.assertNotIn(outside.resolve(), loaded)

    def test_every_story_has_a_concrete_hundred_question_matrix(self) -> None:
        titles = {json.loads(line)["title"] for line in self.corpus.read_text(encoding="utf-8").splitlines() if line}
        for path in sorted((self.matrix / "bindings").glob("*.jsonl")):
            questions = read_jsonl(path)
            self.assertEqual(len(questions), 100)
            self.assertEqual(len({(item["operation"], item["challenge"]) for item in questions}), 100)
            self.assertEqual(Counter(item["operation"] for item in questions).most_common()[0][1], 10)
            self.assertTrue(all(any(title in item["wording"] for title in titles) for item in questions))
            self.assertTrue(all("For the " not in item["wording"] or " scenario" not in item["wording"] for item in questions))

    def test_answerable_gold_is_nonempty_and_unanswerable_gold_is_explicit(self) -> None:
        records = read_jsonl(self.matrix / "gold" / "catalogue.jsonl")
        self.assertEqual(len(records), 1_200)
        for record in records:
            gold = record["gold"]
            self.assertRegex(gold["snapshot_manifest_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(gold["supporting_source_anchors"])
            if gold["classification"] == "answerable":
                self.assertTrue(gold["primary_targets"])
                self.assertTrue(gold["content_ids"] or gold["urls"])
                self.assertTrue(gold["near_misses"])
                self.assertTrue(gold["expected_paths"])
            else:
                self.assertEqual(gold["classification"], "deliberately_unanswerable")
                self.assertTrue(gold["unanswerable_rationale"])

    def test_independent_validator_passes_machine_checks_but_fixture_fails_release_closed(self) -> None:
        report = json.loads((self.matrix / "verification-report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["verifier"]["independent_from_generator"])
        self.assertEqual(report["verifier"]["implementation"], "deterministic-corpus-anchor-validator-v2")
        self.assertTrue(report["machine_validations_passed"])
        self.assertFalse(report["question_contract_passed"])
        self.assertEqual(report["counts"]["validation_errors"], 0)
        ledger = read_jsonl(self.matrix / "verification-ledger.jsonl")
        self.assertEqual(len(ledger), 1_200)
        self.assertEqual({item["gold_verification_status"] for item in ledger}, {"verified"})
        self.assertEqual(report["verification_ledger"]["verified"], 1_200)
        self.assertEqual(report["verification_ledger"]["failed"], 0)
        required = subprocess.run(
            [
                sys.executable,
                "scripts/verify_question_matrix_v2.py",
                "--matrix",
                str(self.matrix),
                "--corpus",
                str(self.corpus),
                "--require-release",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(required.returncode, 1)

    def test_release_prerequisites_require_reconciled_unsampled_corpus(self) -> None:
        personas = [{"persona_id": f"persona-{index}"} for index in range(48)]
        reconciliation = {
            "schema_version": 1,
            "snapshot": "T0-20260712",
            "sampled": False,
            "expected_candidate_keys": 5,
            "represented": 5,
            "alias_of_represented": 0,
            "redirect_only": 0,
            "tombstone_only": 0,
            "exceptioned": 0,
            "unexplained_omissions": 0,
            "entity_class_counts": {"route": 5},
            "publication_records": 5,
            "inventory_canonical_sha256": "a" * 64,
            "candidate_ledger_canonical_sha256": "b" * 64,
            "search_partitions_closed": True,
            "search_partition_proofs": [{"partition": "fixture"}],
            "sitemap_byte_stable": True,
            "sitemap_proof": {"closed": True},
            "organisations_proof": {"closed": True},
            "navigation_proof": {"closed": True},
        }
        snapshot_manifest = {"snapshot": "T0-20260712", "reconciliation": reconciliation}
        passed, blockers = release_prerequisites(
            mode="release",
            snapshot_id="T0-20260712",
            personas=personas,
            reconciliation=reconciliation,
            snapshot_manifest=snapshot_manifest,
            blockers=[],
        )
        self.assertTrue(passed)
        self.assertEqual(blockers, [])
        passed, blockers = release_prerequisites(
            mode="release",
            snapshot_id="sample-T0",
            personas=personas,
            reconciliation={**reconciliation, "unexplained_omissions": 1, "sampled": True},
            snapshot_manifest=snapshot_manifest,
            blockers=[],
        )
        self.assertFalse(passed)
        self.assertIn("corpus_unexplained_omissions_not_zero", blockers)
        self.assertIn("corpus_is_sampled", blockers)
        self.assertIn("snapshot_id_is_not_release_eligible", blockers)

    def test_release_reconciliation_rejects_recursive_field_name_decoys(self) -> None:
        decoy = {
            "schema_version": 1,
            "snapshot": "T1-final",
            "payload": {"unexplained_omissions": 0, "sampled": False},
        }
        errors = reconciliation_release_errors(
            decoy,
            snapshot_id="T1-final",
            snapshot_manifest={"snapshot": "T1-final", "reconciliation": decoy},
        )
        self.assertIn("corpus_unexplained_omissions_not_zero", errors)
        self.assertIn("corpus_expected_candidate_keys_invalid", errors)
        self.assertIn("corpus_search_partitions_closed_invalid", errors)

    def test_independent_trust_contract_binds_corpus_record_count(self) -> None:
        reconciliation = {
            "schema_version": 1,
            "snapshot": "T1-final",
            "sampled": False,
            "expected_candidate_keys": 2,
            "represented": 2,
            "alias_of_represented": 0,
            "redirect_only": 0,
            "tombstone_only": 0,
            "exceptioned": 0,
            "unexplained_omissions": 0,
            "entity_class_counts": {"route": 2},
            "publication_records": 1,
            "inventory_canonical_sha256": "a" * 64,
            "candidate_ledger_canonical_sha256": "b" * 64,
            "search_partitions_closed": True,
            "search_partition_proofs": [{"partition": "fixture"}],
            "sitemap_byte_stable": True,
            "sitemap_proof": {"closed": True},
            "organisations_proof": {"closed": True},
            "navigation_proof": {"closed": True},
        }
        manifest = {"snapshot": "T1-final", "reconciliation": reconciliation}
        self.assertIn(
            "publication_records",
            trusted_release_errors(
                manifest,
                reconciliation,
                snapshot_id="T1-final",
                corpus_records=2,
            ),
        )


if __name__ == "__main__":
    unittest.main()
