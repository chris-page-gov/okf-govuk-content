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
    DOC_MAP_PARTITIONING_CONTRACT,
    MAX_DATA_PLANE_SHARD_COMPRESSED_BYTES,
    POSTINGS_PARTITIONING_CONTRACT,
    PublicationError,
    build_publication,
    data_plane_manifest_root,
    load_jsonl,
    shard_manifest_sha256,
    write_doc_map_partitions,
    write_postings_partitions,
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

    def rewrite_search_manifest(
        self, output: Path, search: dict[str, object]
    ) -> None:
        search_path = output / "data/search/manifest.json"
        search_path.write_text(
            json.dumps(search, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        descriptor_path = output / "okf-explorer.json"
        descriptor = self.load(descriptor_path)
        descriptor["entrypoint_integrity"]["search_manifest"]["sha256"] = hashlib.sha256(  # type: ignore[index]
            search_path.read_bytes()
        ).hexdigest()
        descriptor_path.write_text(
            json.dumps(descriptor, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
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
                search["entrypoints"]["postings"],  # type: ignore[index]
                [
                    row["path"]
                    for row in search_shard_document["shards"]["postings"]  # type: ignore[index]
                ],
            )
            self.assertEqual(
                search["entrypoints"]["doc_map"],  # type: ignore[index]
                [
                    row["path"]
                    for row in search_shard_document["shards"]["doc_map"]  # type: ignore[index]
                ],
            )
            self.assertEqual(
                POSTINGS_PARTITIONING_CONTRACT,
                search["postings_partitioning"],
            )
            self.assertEqual(
                DOC_MAP_PARTITIONING_CONTRACT,
                search["doc_map_partitioning"],
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

    def test_postings_partitions_are_exact_byte_bounded_atomic_and_deterministic(self) -> None:
        posting_rows = [[ordinal, 16, 1] for ordinal in range(2_000)]
        entries = [(f"ca{ordinal:06d}", posting_rows) for ordinal in range(64)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first_paths, first_metadata = write_postings_partitions(
                first,
                "ca",
                entries,
                snapshot_id=self.snapshot,
            )
            second_paths, second_metadata = write_postings_partitions(
                second,
                "ca",
                entries,
                snapshot_id=self.snapshot,
            )
            self.assertEqual(
                [
                    "data/search/postings/ca-00000.json",
                    "data/search/postings/ca-00001.json",
                ],
                first_paths,
            )
            self.assertEqual(first_paths, second_paths)
            self.assertEqual(first_metadata, second_metadata)
            observed: list[str] = []
            for relative, row in zip(first_paths, first_metadata, strict=True):
                path = first / relative
                self.assertLessEqual(
                    path.stat().st_size, MAX_DATA_PLANE_SHARD_COMPRESSED_BYTES
                )
                self.assertEqual(
                    path.read_bytes(), (second / relative).read_bytes()
                )
                payload = self.load(path)
                tokens = list(payload["tokens"])  # type: ignore[arg-type]
                observed.extend(tokens)
                self.assertEqual(tokens[0], row["first_key"])
                self.assertEqual(tokens[-1], row["last_key"])
                self.assertEqual(len(tokens), row["count"])
                self.assertEqual(len(first_paths), row["partition_count"])
                self.assertEqual(
                    POSTINGS_PARTITIONING_CONTRACT["schema"],
                    row["partitioning_schema"],
                )
            self.assertEqual([token for token, _rows in entries], observed)

            exact_root = root / "exact"
            exact_paths, _metadata = write_postings_partitions(
                exact_root,
                "zz",
                [("zz-token", [[0, 1, 1]])],
                snapshot_id=self.snapshot,
            )
            exact_size = (exact_root / exact_paths[0]).stat().st_size
            exact_fit = root / "exact-fit"
            write_postings_partitions(
                exact_fit,
                "zz",
                [("zz-token", [[0, 1, 1]])],
                snapshot_id=self.snapshot,
                max_bytes=exact_size,
            )
            with self.assertRaisesRegex(PublicationError, "one token cannot fit"):
                write_postings_partitions(
                    root / "too-small",
                    "zz",
                    [("zz-token", [[0, 1, 1]])],
                    snapshot_id=self.snapshot,
                    max_bytes=exact_size - 1,
                )

            boundary_entries = [
                ("aa0", [[0, 1, 1]]),
                ("aa1", [[1, 1, 1]]),
                ("aa2", [[2, 1, 1]]),
            ]
            two_token_root = root / "two-token-size"
            two_token_paths, _ = write_postings_partitions(
                two_token_root,
                "aa",
                boundary_entries[:2],
                snapshot_id=self.snapshot,
            )
            exact_two_token_size = (
                two_token_root / two_token_paths[0]
            ).stat().st_size
            boundary_root = root / "greedy-boundary"
            boundary_paths, _ = write_postings_partitions(
                boundary_root,
                "aa",
                boundary_entries,
                snapshot_id=self.snapshot,
                max_bytes=exact_two_token_size,
            )
            self.assertEqual(
                [
                    "data/search/postings/aa-00000.json",
                    "data/search/postings/aa-00001.json",
                ],
                boundary_paths,
            )
            self.assertEqual(
                ["aa0", "aa1"],
                list(self.load(boundary_root / boundary_paths[0])["tokens"]),  # type: ignore[arg-type]
            )
            self.assertEqual(
                ["aa2"],
                list(self.load(boundary_root / boundary_paths[1])["tokens"]),  # type: ignore[arg-type]
            )

    def test_document_map_partitions_cover_every_ordinal_once(self) -> None:
        datasets = [
            {"ordinal": ordinal, "open": f"dataset/{ordinal:05d}"}
            for ordinal in range(2_501)
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            paths, metadata = write_doc_map_partitions(
                output, datasets, snapshot_id=self.snapshot
            )
            self.assertEqual(
                [
                    "data/search/doc-map-00000.json",
                    "data/search/doc-map-00001.json",
                    "data/search/doc-map-00002.json",
                ],
                paths,
            )
            observed: dict[int, str] = {}
            for partition, (relative, row) in enumerate(
                zip(paths, metadata, strict=True)
            ):
                path = output / relative
                self.assertLessEqual(
                    path.stat().st_size, MAX_DATA_PLANE_SHARD_COMPRESSED_BYTES
                )
                payload = self.load(path)
                for ordinal, route in payload.items():
                    self.assertNotIn(int(ordinal), observed)
                    observed[int(ordinal)] = str(route)
                self.assertEqual(partition, row["partition"])
                self.assertEqual(len(paths), row["partition_count"])
            self.assertEqual(
                {ordinal: f"dataset/{ordinal:05d}" for ordinal in range(2_501)},
                observed,
            )

    def test_validator_accepts_legacy_singleton_search_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            search_path = output / "data/search/manifest.json"
            search = self.load(search_path)
            search.pop("postings_partitioning")
            search.pop("doc_map_partitioning")
            search["entrypoints"]["doc_map"] = search["entrypoints"]["doc_map"][0]  # type: ignore[index]
            search_path.write_text(
                json.dumps(search, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            descriptor_path = output / "okf-explorer.json"
            descriptor = self.load(descriptor_path)
            descriptor["entrypoint_integrity"]["search_manifest"]["sha256"] = hashlib.sha256(  # type: ignore[index]
                search_path.read_bytes()
            ).hexdigest()
            descriptor_path.write_text(
                json.dumps(descriptor, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            result = validate_bundle(output)
            self.assertTrue(result.passed, result.errors)

    def test_validator_rejects_present_null_and_type_coerced_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            cases = (
                ("postings_partitioning", None),
                ("doc_map_partitioning", None),
                (
                    "postings_partitioning",
                    {**POSTINGS_PARTITIONING_CONTRACT, "token_atomic": 1},
                ),
            )
            for field, value in cases:
                with self.subTest(field=field, value=value):
                    self.build(output)
                    search = self.load(output / "data/search/manifest.json")
                    search[field] = value
                    self.rewrite_search_manifest(output, search)
                    result = validate_bundle(output)
                    self.assertFalse(result.passed)
                    self.assertTrue(
                        any(
                            "partitioning contract is unsupported or has drifted"
                            in error
                            for error in result.errors
                        ),
                        result.errors,
                    )

    def test_validator_rejects_equal_length_noncanonical_postings_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            search = self.load(output / "data/search/manifest.json")
            relative = search["entrypoints"]["postings"][0]  # type: ignore[index]
            path = output / str(relative)
            original = path.read_bytes()
            tampered = original.replace(b'  "tokens"', b'\t "tokens"', 1)
            self.assertEqual(len(original), len(tampered))
            self.assertNotEqual(original, tampered)
            path.write_bytes(tampered)
            result = validate_bundle(output)
            self.assertFalse(result.passed)
            self.assertTrue(
                any(
                    "postings partition is not canonical pretty JSON" in error
                    for error in result.errors
                ),
                result.errors,
            )

    def test_validator_rejects_partition_metadata_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            search = self.load(output / "data/search/manifest.json")
            metadata_path = output / str(search["shard_metadata"])
            metadata = self.load(metadata_path)
            metadata["shards"]["postings"][0]["partition"] = 99  # type: ignore[index]
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            result = validate_bundle(output)
            self.assertFalse(result.passed)
            self.assertTrue(
                any(
                    "postings partition metadata differs" in error
                    for error in result.errors
                ),
                result.errors,
            )

    def test_validator_recomputes_site_topology_from_record_shards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            topology_path = output / "data/site-topology.json"
            topology = self.load(topology_path)
            topology["hosts"][0]["record_count"] += 1  # type: ignore[index]
            topology_path.write_text(
                json.dumps(topology, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            descriptor_path = output / "okf-explorer.json"
            descriptor = self.load(descriptor_path)
            descriptor["entrypoint_integrity"]["site_topology"]["sha256"] = hashlib.sha256(  # type: ignore[index]
                topology_path.read_bytes()
            ).hexdigest()
            descriptor_path.write_text(
                json.dumps(descriptor, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            result = validate_bundle(output)
            self.assertFalse(result.passed)
            self.assertTrue(
                any(
                    "host inventory differs from record shards" in error
                    for error in result.errors
                )
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
