from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from tests.test_release_gate import MODULE as RELEASE_MODULE
from tests.test_release_gate import make_release, mutate, write_json


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_ci_snapshot", ROOT / "scripts/check_ci_snapshot.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CiSnapshotTests(unittest.TestCase):
    def test_fixture_dispatch_runs_exact_rebuild_and_release_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "release/manifest.yaml",
                {
                    "release_kind": "fixture",
                    "publication_ready": False,
                    "snapshot": {"id": "fixture-v1", "kind": "fixture", "sampled": True},
                },
            )
            commands: list[list[str]] = []
            mode = MODULE.check_snapshot(
                root, runner=lambda command, cwd: commands.append(command)
            )
            self.assertEqual("fixture", mode)
            self.assertEqual(
                [
                    [MODULE.sys.executable, "scripts/build_bundle.py", "--check"],
                    [MODULE.sys.executable, "scripts/reproduce_release.py", "--check"],
                    [MODULE.sys.executable, "scripts/check_release.py"],
                ],
                commands,
            )

    def test_checkpoint_and_ambiguous_states_fail_closed(self) -> None:
        for manifest in (
            {
                "release_kind": "full_corpus_checkpoint",
                "publication_ready": False,
                "snapshot": {"id": "T1", "kind": "full_corpus", "sampled": False},
            },
            {
                "release_kind": "machine_release_candidate",
                "publication_ready": True,
                "snapshot": {"id": "T1", "kind": "full_corpus", "sampled": True},
            },
        ):
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_json(root / "release/manifest.yaml", manifest)
                with self.assertRaises(MODULE.SnapshotCheckError):
                    MODULE.check_snapshot(root, runner=lambda *_: None)

    def test_full_programme_dispatch_requires_finalized_promotion(self) -> None:
        manifest = {
            "release_kind": "full_programme",
            "publication_ready": True,
            "snapshot": {"id": "T1", "kind": "full_corpus", "sampled": False},
            "promotion": {"finalized": True},
        }
        self.assertEqual("finalized", MODULE.snapshot_mode(manifest))
        manifest["promotion"]["finalized"] = False
        with self.assertRaisesRegex(MODULE.SnapshotCheckError, "not promotion-finalized"):
            MODULE.snapshot_mode(manifest)

    def test_candidate_static_dispatch_accepts_archived_source_but_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            reproduction = json.loads(
                (root / "release/manifest.yaml").read_text(encoding="utf-8")
            )["promotion"]["reproduction"]
            (root / reproduction["source"]).unlink()
            rights = json.loads(
                (root / "release/rights-privacy-audit.json").read_text(encoding="utf-8")
            )
            (root / rights["audit_input_contract"]["corpus_manifests"][0]["path"]).unlink()
            self.assertEqual("candidate", MODULE.check_snapshot(root))
            manifest_path = root / "release/manifest.yaml"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["promotion"]["reproduction"]["source_binding"]["file_count"] = True
            write_json(manifest_path, manifest)
            with self.assertRaisesRegex(MODULE.SnapshotCheckError, "frozen reproduction source"):
                MODULE.check_snapshot(root)
            manifest["promotion"]["reproduction"]["source_binding"]["file_count"] = 1
            write_json(manifest_path, manifest)
            rights["snapshot_binding"]["release_manifest"]["sha256"] = RELEASE_MODULE._file_sha256(
                manifest_path
            )
            write_json(root / "release/rights-privacy-audit.json", rights)
            clean_path = root / "release/clean-room-reproduction.json"
            clean = json.loads(clean_path.read_text(encoding="utf-8"))
            clean["source_binding"]["tree_sha256"] = "f" * 64
            write_json(clean_path, clean)
            with self.assertRaisesRegex(MODULE.SnapshotCheckError, "immutable reproduction contract"):
                MODULE.check_snapshot(root)

    def test_finalized_static_dispatch_enforces_strict_release_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            manifest_path = root / "release/manifest.yaml"
            status_path = root / "release/status.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["promotion"].update(
                {
                    "finalized": True,
                    "candidate_manifest_sha256": RELEASE_MODULE._file_sha256(manifest_path),
                    "candidate_status_sha256": RELEASE_MODULE._file_sha256(status_path),
                }
            )
            write_json(manifest_path, manifest)
            rights_path = root / "release/rights-privacy-audit.json"
            rights = json.loads(rights_path.read_text(encoding="utf-8"))
            rights["snapshot_binding"]["release_manifest"]["sha256"] = RELEASE_MODULE._file_sha256(
                manifest_path
            )
            write_json(rights_path, rights)
            mutate(status_path, "promotion_finalized", True)
            mutate(status_path, "reason", RELEASE_MODULE.MACHINE_FINAL_REASON)
            provenance_path = root / "release/provenance-validation.json"
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            provenance.update(
                {
                    "validation_mode": "release",
                    "validation_tier": "release",
                    "candidate_mode": False,
                    "release_mode": True,
                    "release_requirements_satisfied": True,
                    "publication_workflow_status": "completed",
                    "release_blockers": [],
                }
            )
            provenance["required_terminal_events"].update(
                {"required": 11, "satisfied": 11, "all_satisfied": True}
            )
            write_json(provenance_path, provenance)
            self.assertEqual("finalized", MODULE.check_snapshot(root))
            provenance["required_terminal_events"]["satisfied"] = 10
            write_json(provenance_path, provenance)
            with self.assertRaisesRegex(MODULE.SnapshotCheckError, "strict 11-of-11"):
                MODULE.check_snapshot(root)


if __name__ == "__main__":
    unittest.main()
