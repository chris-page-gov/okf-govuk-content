from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from tests.test_release_gate import make_release, write_json


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("promote_release", ROOT / "scripts/promote_release.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def prepare_staged(root: Path) -> str:
    make_release(root)
    snapshot = "T1-20260712-closing"
    MODULE.stage_release(
        root,
        snapshot=snapshot,
        reconciliation_relative="corpus/reconciliation/closing.json",
    )
    clean_path = root / "release/clean-room-reproduction.json"
    clean = json.loads(clean_path.read_text(encoding="utf-8"))
    clean["inputs"]["components"][0]["tree_sha256"] = MODULE.sha256(root / "release/manifest.yaml")
    write_json(clean_path, clean)
    return snapshot


def existing_provenance(root: Path, snapshot: str) -> dict[str, object]:
    value = json.loads((root / "release/provenance-validation.json").read_text(encoding="utf-8"))
    value["snapshot"] = snapshot
    return value


def existing_tests(root: Path, snapshot: str) -> dict[str, object]:
    value = json.loads((root / "release/full-repository-tests.json").read_text(encoding="utf-8"))
    value["snapshot"] = snapshot
    value["code_tree"]["sha256"] = MODULE.check_release._tree_sha256(root)
    return value


def finalized_provenance(root: Path, snapshot: str) -> dict[str, object]:
    value = existing_provenance(root, snapshot)
    value.update(
        {
            "validation_mode": "release",
            "validation_tier": "release",
            "candidate_mode": False,
            "release_mode": True,
            "candidate_requirements_satisfied": True,
            "release_requirements_satisfied": True,
            "publication_workflow_status": "completed",
            "candidate_blockers": [],
            "release_blockers": [],
        }
    )
    value["required_terminal_events"].update(
        {"required": 11, "satisfied": 11, "all_satisfied": True}
    )
    return value


def fake_aim_renderer(root: Path) -> dict[Path, str]:
    assessment = json.loads((root / "release/aim-assessment.json").read_text(encoding="utf-8"))
    return {
        root / "release/aim-assessment.json": json.dumps(assessment, indent=2, sort_keys=True) + "\n",
        root / "reports/aim-scorecard.md": "# Test aim scorecard\n",
    }


class ReleasePromotionTests(unittest.TestCase):
    def test_stage_is_full_corpus_but_explicitly_non_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = prepare_staged(root)
            manifest = json.loads((root / "release/manifest.yaml").read_text(encoding="utf-8"))
            status = json.loads((root / "release/status.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot, manifest["release_id"])
            self.assertEqual("full_corpus_checkpoint", manifest["release_kind"])
            self.assertFalse(manifest["publication_ready"])
            self.assertTrue(manifest["gates"]["full_corpus_reconciled"])
            self.assertFalse(manifest["gates"]["clean_room_reproduction_passed"])
            self.assertEqual("checkpoint", status["status"])
            self.assertEqual("not_authorised", status["human_evaluation_status"])
            self.assertEqual("not_yet_testable", status["human_ui_of_choice_status"])

    def test_promote_derives_machine_rc_and_preserves_human_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = prepare_staged(root)
            result = MODULE.promote_release(
                root,
                provenance_builder=existing_provenance,
                test_builder=existing_tests,
                aim_renderer=fake_aim_renderer,
            )
            self.assertEqual("machine_release_candidate", result["manifest"]["release_kind"])
            self.assertTrue(result["manifest"]["publication_ready"])
            self.assertTrue(all(result["manifest"]["gates"].values()))
            self.assertEqual(MODULE.check_release.MACHINE_MARKER, result["status"]["completion_statement"])
            self.assertEqual("not_authorised", result["status"]["human_evaluation_status"])
            self.assertEqual("not_yet_testable", result["status"]["human_ui_of_choice_status"])
            self.assertFalse(result["status"]["programme_complete"])
            self.assertEqual([], MODULE.check_release.validate_release(root, require_publication_ready=True))
            self.assertEqual(snapshot, result["status"]["release_id"])

    def test_final_check_failure_rolls_back_every_transaction_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_staged(root)
            paths = [
                root / "release/manifest.yaml",
                root / "release/status.json",
                root / "release/provenance-validation.json",
                root / "release/full-repository-tests.json",
                root / "release/aim-assessment.json",
                root / "reports/aim-scorecard.md",
            ]
            before = {path: path.read_bytes() if path.is_file() else None for path in paths}
            with self.assertRaisesRegex(MODULE.PromotionError, "forced final failure"):
                MODULE.promote_release(
                    root,
                    provenance_builder=existing_provenance,
                    test_builder=existing_tests,
                    aim_renderer=fake_aim_renderer,
                    validator=lambda candidate_root, publication: ["forced final failure"] if publication else [],
                )
            after = {path: path.read_bytes() if path.is_file() else None for path in paths}
            self.assertEqual(before, after)

    def test_finalize_requires_external_terminal_and_then_enforces_strict_eleven_of_eleven(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_staged(root)
            MODULE.promote_release(
                root,
                provenance_builder=existing_provenance,
                test_builder=existing_tests,
                aim_renderer=fake_aim_renderer,
            )
            candidate_manifest = (root / "release/manifest.yaml").read_bytes()
            with self.assertRaisesRegex(MODULE.PromotionError, "strict post-publication provenance"):
                MODULE.finalize_release(
                    root,
                    provenance_builder=existing_provenance,
                    aim_renderer=fake_aim_renderer,
                )
            self.assertEqual(candidate_manifest, (root / "release/manifest.yaml").read_bytes())
            result = MODULE.finalize_release(
                root,
                provenance_builder=finalized_provenance,
                aim_renderer=fake_aim_renderer,
            )
            self.assertTrue(result["manifest"]["promotion"]["finalized"])
            self.assertTrue(result["status"]["promotion_finalized"])
            self.assertEqual(
                [],
                MODULE.check_release.validate_release(
                    root, require_publication_ready=True, require_finalized=True
                ),
            )

    def test_missing_security_evidence_fails_before_manifest_or_status_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_staged(root)
            (root / "release/security-scan.json").unlink()
            before = (root / "release/manifest.yaml").read_bytes(), (root / "release/status.json").read_bytes()
            with self.assertRaises(MODULE.PromotionError):
                MODULE.promote_release(
                    root,
                    provenance_builder=existing_provenance,
                    test_builder=existing_tests,
                    aim_renderer=fake_aim_renderer,
                )
            self.assertEqual(before, ((root / "release/manifest.yaml").read_bytes(), (root / "release/status.json").read_bytes()))

    def test_stage_rejects_sample_labels_and_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            with self.assertRaisesRegex(MODULE.PromotionError, "rejects fixture"):
                MODULE.stage_release(
                    root,
                    snapshot="sample-T1",
                    reconciliation_relative="corpus/reconciliation/closing.json",
                )
            reconciliation_path = root / "corpus/reconciliation/closing.json"
            reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))
            reconciliation["publication_records"] = 2
            write_json(reconciliation_path, reconciliation)
            with self.assertRaisesRegex(MODULE.PromotionError, "publication record counts differ"):
                MODULE.stage_release(
                    root,
                    snapshot="T1-20260712-closing",
                    reconciliation_relative="corpus/reconciliation/closing.json",
                )


if __name__ == "__main__":
    unittest.main()
