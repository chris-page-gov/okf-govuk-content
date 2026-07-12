from __future__ import annotations

import json
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

from govuk_okf.question_matrix_v2 import release_prerequisites


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
        self.assertEqual(len(by_persona), 2)
        self.assertTrue(all(len(items) == 6 for items in by_persona.values()))
        self.assertTrue(all(len({item["story_role"] for item in items}) == 6 for items in by_persona.values()))

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
        passed, blockers = release_prerequisites(
            mode="release",
            snapshot_id="T0-20260712",
            personas=personas,
            reconciliation={"unexplained_omissions": 0, "sampled": False},
            blockers=[],
        )
        self.assertTrue(passed)
        self.assertEqual(blockers, [])
        passed, blockers = release_prerequisites(
            mode="release",
            snapshot_id="sample-T0",
            personas=personas,
            reconciliation={"unexplained_omissions": 1, "sampled": True},
            blockers=[],
        )
        self.assertFalse(passed)
        self.assertIn("corpus_unexplained_omissions_not_zero", blockers)
        self.assertIn("corpus_is_sampled", blockers)
        self.assertIn("snapshot_id_is_not_release_eligible", blockers)


if __name__ == "__main__":
    unittest.main()
