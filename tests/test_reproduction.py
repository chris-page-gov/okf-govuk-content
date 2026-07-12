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
