from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "reproduce_release", ROOT / "scripts" / "reproduce_release.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReproductionTests(unittest.TestCase):
    def test_detached_standard_shard_index_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source-records-deadbeef"
            root.mkdir()
            index = root / "index.json"
            index.write_text(
                json.dumps({"schema": "govuk-okf-jsonl-shards.v1", "shards": []}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(MODULE.ReproductionError, "containing directory"):
                MODULE.source_binding(index, root.parent)

    def test_copy_inputs_preserves_complete_sharded_source_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            source = temporary / "source-records-deadbeef"
            source.mkdir()
            shard = source / "part-00000.jsonl.gz"
            shard.write_bytes(b"frozen shard bytes")
            (source / "index.json").write_text(
                json.dumps(
                    {
                        "schema": "govuk-okf-jsonl-shards.v1",
                        "shards": [{"path": shard.name}],
                    }
                ),
                encoding="utf-8",
            )

            copied, manifest = MODULE._copy_inputs(temporary / "workspace", source)

            self.assertTrue(copied.is_dir())
            self.assertEqual((source / "index.json").read_bytes(), (copied / "index.json").read_bytes())
            self.assertEqual(shard.read_bytes(), (copied / shard.name).read_bytes())
            self.assertEqual("frozen_source", manifest["components"][-1]["path"])

    def test_staged_checkpoint_is_a_valid_prospective_candidate_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = "T1-20260712-closing"
            source = root / "corpus/records/T1/source-records.jsonl.gz"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"frozen source\n")
            binding = MODULE.source_binding(source, root)
            manifest = root / "release/manifest.yaml"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {
                        "release_id": snapshot,
                        "release_kind": "full_corpus_checkpoint",
                        "snapshot": {"id": snapshot, "kind": "full_corpus", "sampled": False},
                        "promotion_contract": {
                            "schema": "afhf-govuk-okf-two-stage-promotion.v1",
                            "stage": "full_corpus_checkpoint",
                            "target_release_kind": "machine_release_candidate",
                            "reproduction": {
                                "source": "corpus/records/T1/source-records.jsonl.gz",
                                "generated_at": "2026-07-12T23:59:59Z",
                                "compiler": "disk",
                                "source_binding": binding,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            status = manifest.parent / "status.json"
            status.write_text(
                json.dumps(
                    {
                        "release_id": snapshot,
                        "status": "checkpoint",
                        "publication_ready": False,
                    }
                ),
                encoding="utf-8",
            )
            tests = root / "release/full-repository-tests.json"
            tests.write_text(
                json.dumps(
                    {
                        "snapshot": snapshot,
                        "scope": "full_repository",
                        "tests_passed": True,
                    }
                ),
                encoding="utf-8",
            )

            passed, failures, _, control = MODULE._release_inputs_pass(
                release_kind="machine_release_candidate",
                snapshot=snapshot,
                snapshot_kind="full_corpus",
                sampled=False,
                source=source,
                generated_at="2026-07-12T23:59:59Z",
                compiler="disk",
                release_manifest=manifest,
                test_evidence=tests,
            )

            self.assertTrue(passed, failures)
            self.assertEqual([], failures)
            self.assertTrue(control["prospective"])
            self.assertEqual(MODULE._file_sha256(manifest), control["manifest_sha256"])
            self.assertEqual(MODULE._file_sha256(status), control["status_sha256"])

            for changed_generated_at, changed_compiler, expected in (
                ("2026-07-13T00:00:00Z", "disk", "generated_at differs"),
                ("2026-07-12T23:59:59Z", "memory", "compiler differs"),
            ):
                passed, failures, _, _ = MODULE._release_inputs_pass(
                    release_kind="machine_release_candidate",
                    snapshot=snapshot,
                    snapshot_kind="full_corpus",
                    sampled=False,
                    source=source,
                    generated_at=changed_generated_at,
                    compiler=changed_compiler,
                    release_manifest=manifest,
                    test_evidence=tests,
                )
                self.assertFalse(passed)
                self.assertTrue(any(expected in failure for failure in failures), failures)

            source.write_bytes(b"changed frozen source\n")
            passed, failures, _, _ = MODULE._release_inputs_pass(
                release_kind="machine_release_candidate",
                snapshot=snapshot,
                snapshot_kind="full_corpus",
                sampled=False,
                source=source,
                generated_at="2026-07-12T23:59:59Z",
                compiler="disk",
                release_manifest=manifest,
                test_evidence=tests,
            )
            self.assertFalse(passed)
            self.assertTrue(any("content/tree binding differs" in failure for failure in failures))
            source.write_bytes(b"frozen source\n")

            status.write_text(
                json.dumps(
                    {
                        "release_id": snapshot,
                        "status": "machine_release_candidate",
                        "publication_ready": True,
                    }
                ),
                encoding="utf-8",
            )
            passed, failures, _, _ = MODULE._release_inputs_pass(
                release_kind="machine_release_candidate",
                snapshot=snapshot,
                snapshot_kind="full_corpus",
                sampled=False,
                source=source,
                generated_at="2026-07-12T23:59:59Z",
                compiler="disk",
                release_manifest=manifest,
                test_evidence=tests,
            )
            self.assertFalse(passed)
            self.assertIn("staged release status is not a non-publishable checkpoint", failures)

    def test_directory_source_binding_matches_release_validator_and_rejects_escape(self) -> None:
        from tests.test_release_gate import MODULE as CHECK_RELEASE

        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            self.assertEqual(tuple(MODULE.COPY_INPUTS), CHECK_RELEASE.CLEAN_ROOM_INPUT_PATHS)
            source = root / "corpus/records/T1/shards"
            source.mkdir(parents=True)
            (source / "a.jsonl").write_text("a\n", encoding="utf-8")
            (source / "b.jsonl").write_text("b\n", encoding="utf-8")
            (source / ".DS_Store").write_bytes(b"ignored")
            self.assertEqual(
                MODULE.source_binding(source, root),
                CHECK_RELEASE._source_binding(source, root),
            )
            before = MODULE.source_binding(source, root)
            (source / ".DS_Store").write_bytes(b"still ignored")
            self.assertEqual(before, MODULE.source_binding(source, root))
            (source / "b.jsonl").write_text("changed\n", encoding="utf-8")
            self.assertNotEqual(before, MODULE.source_binding(source, root))

            outside_source = Path(outside) / "source.jsonl"
            outside_source.write_text("outside\n", encoding="utf-8")
            nested_link = source / "outside-link.jsonl"
            nested_link.symlink_to(outside_source)
            with self.assertRaisesRegex(MODULE.ReproductionError, "cannot contain symlinks"):
                MODULE.source_binding(source, root)
            with self.assertRaisesRegex(
                CHECK_RELEASE.ReleaseDocumentError, "cannot contain symlinks"
            ):
                CHECK_RELEASE._source_binding(source, root)
            nested_link.unlink()
            link = root / "corpus/records/T1/outside-link"
            link.symlink_to(outside_source)
            with self.assertRaises(CHECK_RELEASE.ReleaseDocumentError):
                CHECK_RELEASE._resolve_relative(root, "corpus/records/T1/outside-link", "source")

    def test_tree_manifest_is_stable_and_excludes_runtime_caches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "b").mkdir()
            (root / "b/z.txt").write_text("z", encoding="utf-8")
            (root / "a.txt").write_text("a", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules/host.js").write_text("host", encoding="utf-8")
            (root / "__pycache__").mkdir()
            (root / "__pycache__/host.pyc").write_bytes(b"host")
            first = MODULE.manifest_summary(root)
            second = MODULE.manifest_summary(root)
            self.assertEqual(first, second)
            self.assertEqual(["a.txt", "b/z.txt"], [row["path"] for row in first["rows"]])

    def test_unknown_product_usage_is_not_relabelled_as_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activities.jsonl"
            rows = [
                {
                    "activity_id": "model-assisted",
                    "exact_model_version": "unavailable_to_session",
                    "tokens": "unavailable",
                    "cost_gbp": "unavailable",
                    "external_paid_model_api_calls": 0,
                },
                {
                    "activity_id": "deterministic",
                    "exact_model_version": None,
                    "tokens": 0,
                    "cost_gbp": 0,
                    "external_paid_model_api_calls": 0,
                },
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            summary = MODULE.activity_usage(path)
            self.assertEqual(1, summary["activities_with_unavailable_tokens"])
            self.assertEqual(1, summary["activities_with_unavailable_cost"])
            self.assertEqual(0, summary["known_tokens"])
            self.assertEqual(0.0, summary["known_cost_gbp"])

    def test_v2_activity_usage_uses_structured_model_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activities.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "activity_id": "deterministic-v2",
                        "ledger_schema_version": "2.0",
                        "model": None,
                        "tokens": 0,
                        "cost_gbp": 0,
                        "external_paid_model_api_calls": 0,
                        "usage": {
                            "external_paid_model": {
                                "api_calls": 0,
                                "input_tokens": 0,
                                "output_tokens": 0,
                                "cost_gbp": 0,
                            }
                        },
                        "source_request_usage": {
                            "status": "exact",
                            "attempts": 7,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            summary = MODULE.activity_usage(path)
            self.assertEqual(1, summary["activities"])
            self.assertEqual(7, summary["source_request_exact_attempts"])
            self.assertEqual(0, summary["activities_with_unavailable_tokens"])

    def test_checked_fixture_evidence_is_honest_and_structurally_valid(self) -> None:
        document = json.loads(
            (ROOT / "release/clean-room-reproduction.json").read_text(encoding="utf-8")
        )
        self.assertEqual([], MODULE.validate_evidence(document))
        self.assertTrue(document["fixture_reproduction_passed"])
        self.assertFalse(document["clean_room_reproduction_passed"])
        release_errors = MODULE.validate_evidence(document, require_release=True)
        self.assertTrue(any("clean_room_reproduction_passed is false" in error for error in release_errors))
        self.assertTrue(any("full-repository test evidence" in error for error in release_errors))


if __name__ == "__main__":
    unittest.main()
