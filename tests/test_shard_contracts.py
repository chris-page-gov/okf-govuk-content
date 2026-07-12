from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.publication import (  # noqa: E402
    DATA_PLANE_SCHEMA_VERSION,
    MAX_DATA_PLANE_SHARD_COMPRESSED_BYTES,
    PublicationError,
    build_publication,
    data_plane_manifest_root,
    load_jsonl,
    shard_manifest_sha256,
)
from govuk_okf.publication_validation import validate_bundle  # noqa: E402


class DataPlaneShardContractTests(unittest.TestCase):
    fixture = ROOT / "tests" / "fixtures" / "corpus" / "source-records.jsonl"
    generated_at = "2026-07-11T23:30:00Z"
    snapshot = "fixture-2026-07-11"

    def build(self, output: Path) -> None:
        build_publication(
            load_jsonl(self.fixture), output, self.generated_at, self.snapshot
        )

    @staticmethod
    def load(path: Path) -> dict[str, object]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise AssertionError(path)
        return value

    def test_all_path_contracts_have_metadata_and_one_release_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            data = self.load(output / "data/manifest.json")
            search = self.load(output / "data/search/manifest.json")
            search_shard_document = self.load(output / search["shard_metadata"])
            adjacency = self.load(output / "data/adjacency/manifest.json")
            routes = self.load(output / "data/routes/manifest.json")

            required = {
                "path",
                "schema",
                "schema_version",
                "snapshot",
                "count",
                "first_key",
                "last_key",
                "compression",
                "compressed_bytes",
                "uncompressed_bytes",
                "sha256",
            }
            record_rows = [
                row
                for rows in data["shards"].values()  # type: ignore[union-attr]
                for row in rows
            ]
            search_rows = [
                row
                for rows in search_shard_document["shards"].values()  # type: ignore[union-attr]
                for row in rows
            ]
            all_rows = [
                *record_rows,
                *search_rows,
                *adjacency["shards"],  # type: ignore[misc]
                *routes["shards"],  # type: ignore[misc]
            ]
            self.assertTrue(all(required <= set(row) for row in all_rows))
            self.assertTrue(
                all(row["schema_version"] == DATA_PLANE_SCHEMA_VERSION for row in all_rows)
            )
            self.assertTrue(all(row["snapshot"] == self.snapshot for row in all_rows))
            self.assertEqual(
                data["chunks"],
                {
                    kind: [row["path"] for row in data["shards"][kind]]  # type: ignore[index]
                    for kind in ("datasets", "resources", "publishers", "relationships")
                },
            )
            self.assertEqual(
                list(adjacency["buckets"].values()),  # type: ignore[union-attr]
                [row["path"] for row in adjacency["shards"]],  # type: ignore[index]
            )
            self.assertEqual(
                list(routes["buckets"].values()),  # type: ignore[union-attr]
                [row["path"] for row in routes["shards"]],  # type: ignore[index]
            )
            self.assertEqual(
                search["entrypoints"]["result_docs"],  # type: ignore[index]
                [
                    row["path"]
                    for row in search_shard_document["shards"]["result_docs"]  # type: ignore[index]
                ],
            )
            self.assertEqual(
                shard_manifest_sha256(data["shards"]),
                data["integrity"]["record_shard_manifest_sha256"],  # type: ignore[index]
            )
            self.assertEqual(
                shard_manifest_sha256(search_shard_document["shards"]),
                search["shard_manifest_sha256"],
            )
            expected_root = data_plane_manifest_root(all_rows)
            self.assertEqual(
                expected_root,
                data["integrity"]["manifest_root_sha256"],  # type: ignore[index]
            )
            descriptor = self.load(output / "okf-explorer.json")
            self.assertEqual(
                expected_root, descriptor["data_plane_manifest_root_sha256"]
            )
            for row in all_rows:
                path = output / row["path"]
                self.assertEqual(path.stat().st_size, row["compressed_bytes"])
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row["sha256"])

    def test_validator_rejects_tampered_shard_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            manifest = self.load(output / "data/manifest.json")
            relative = manifest["chunks"]["datasets"][0]  # type: ignore[index]
            path = output / relative
            path.write_bytes(path.read_bytes() + b"tamper")
            result = validate_bundle(output)
            self.assertFalse(result.passed)
            self.assertTrue(
                any(
                    "compressed size differs" in error or "SHA-256 differs" in error
                    for error in result.errors
                )
            )

    def test_validator_rejects_silent_budget_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            manifest_path = output / "data/manifest.json"
            manifest = self.load(manifest_path)
            manifest["budgets"]["search_warm_p95_ms"] = 501  # type: ignore[index]
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            result = validate_bundle(output)
            self.assertFalse(result.passed)
            self.assertTrue(
                any("frozen data-plane budgets" in error for error in result.errors)
            )

    def test_build_refuses_an_oversized_ordinary_search_shard(self) -> None:
        record = {
            "content_id": "00000000-0000-4000-8000-000000000001",
            "base_path": "/oversized",
            "title": "Oversized metadata test",
            "description": "x" * (MAX_DATA_PLANE_SHARD_COMPRESSED_BYTES + 1024),
            "document_type": "guidance",
            "schema_name": "publication",
            "locale": "en",
            "coverage_disposition": "represented",
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PublicationError, "compressed budget"):
                build_publication(
                    [record],
                    Path(directory) / "bundle",
                    self.generated_at,
                    self.snapshot,
                )


if __name__ == "__main__":
    unittest.main()
