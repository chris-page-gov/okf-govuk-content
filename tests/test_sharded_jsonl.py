from __future__ import annotations

import gzip
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.acquisition import write_jsonl_gzip_shards
from govuk_okf import sharded_jsonl
from govuk_okf.sharded_jsonl import ShardedJsonlError, input_sha256, iter_jsonl_records


class ShardedJsonlTests(unittest.TestCase):
    def test_reads_and_binds_standard_shard_directory(self) -> None:
        records = [{"id": "a", "title": "One"}, {"id": "b", "title": "Two"}]
        with TemporaryDirectory() as temporary:
            output = write_jsonl_gzip_shards(Path(temporary), "records", records, max_records=1)
            root = Path(output["root"])
            self.assertEqual(list(iter_jsonl_records(root)), records)
            self.assertEqual(input_sha256(root), input_sha256(root / "index.json"))

    def test_rejects_tampered_shard_index(self) -> None:
        with TemporaryDirectory() as temporary:
            output = write_jsonl_gzip_shards(Path(temporary), "records", [{"id": "a"}])
            root = Path(output["root"])
            index = json.loads((root / "index.json").read_text(encoding="utf-8"))
            index["shards"][0]["file_sha256"] = "0" * 64
            (root / "index.json").write_text(json.dumps(index), encoding="utf-8")
            with self.assertRaisesRegex(ShardedJsonlError, "file hash mismatch"):
                list(iter_jsonl_records(root))

    def test_rejects_duplicate_shard_paths_before_hashing_or_decompression(self) -> None:
        with TemporaryDirectory() as temporary:
            output = write_jsonl_gzip_shards(Path(temporary), "records", [{"id": "a"}])
            root = Path(output["root"])
            index_path = root / "index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["shards"].append(dict(index["shards"][0]))
            index["records"] *= 2
            index_path.write_text(json.dumps(index), encoding="utf-8")
            with patch.object(sharded_jsonl, "file_sha256") as digest:
                with self.assertRaisesRegex(ShardedJsonlError, "duplicate shard path"):
                    list(iter_jsonl_records(root))
                digest.assert_not_called()

    def test_rejects_declared_aggregate_records_before_processing_shards(self) -> None:
        with TemporaryDirectory() as temporary:
            output = write_jsonl_gzip_shards(Path(temporary), "records", [{"id": "a"}, {"id": "b"}])
            root = Path(output["root"])
            with patch.object(sharded_jsonl, "MAX_AGGREGATE_RECORDS", 1):
                with patch.object(sharded_jsonl, "file_sha256") as digest:
                    with self.assertRaisesRegex(ShardedJsonlError, "aggregate record count exceeds"):
                        list(iter_jsonl_records(root))
                    digest.assert_not_called()

    def test_rejects_excess_shard_count_before_processing_shards(self) -> None:
        with TemporaryDirectory() as temporary:
            output = write_jsonl_gzip_shards(Path(temporary), "records", [{"id": "a"}])
            root = Path(output["root"])
            with patch.object(sharded_jsonl, "MAX_SHARDS", 0):
                with patch.object(sharded_jsonl, "file_sha256") as digest:
                    with self.assertRaisesRegex(ShardedJsonlError, "shard count exceeds"):
                        list(iter_jsonl_records(root))
                    digest.assert_not_called()

    def test_rejects_aggregate_compressed_bytes_before_hashing(self) -> None:
        with TemporaryDirectory() as temporary:
            output = write_jsonl_gzip_shards(Path(temporary), "records", [{"id": "a"}])
            root = Path(output["root"])
            with patch.object(sharded_jsonl, "MAX_AGGREGATE_COMPRESSED_BYTES", 0):
                with patch.object(sharded_jsonl, "file_sha256") as digest:
                    with self.assertRaisesRegex(ShardedJsonlError, "aggregate compressed corpus exceeds"):
                        list(iter_jsonl_records(root))
                    digest.assert_not_called()

    def test_bounds_shard_index_and_aggregate_decoded_bytes(self) -> None:
        with TemporaryDirectory() as temporary:
            output = write_jsonl_gzip_shards(Path(temporary), "records", [{"id": "a", "title": "long value"}])
            root = Path(output["root"])
            with patch.object(sharded_jsonl, "MAX_INDEX_BYTES", 1):
                with self.assertRaisesRegex(ShardedJsonlError, "shard index exceeds"):
                    list(iter_jsonl_records(root))
            with patch.object(sharded_jsonl, "MAX_AGGREGATE_UNCOMPRESSED_BYTES", 8):
                with self.assertRaisesRegex(ShardedJsonlError, "aggregate uncompressed corpus exceeds"):
                    list(iter_jsonl_records(root))

    def test_rejects_oversize_compressed_shard_before_hashing(self) -> None:
        with TemporaryDirectory() as temporary:
            output = write_jsonl_gzip_shards(Path(temporary), "records", [{"id": "a"}])
            root = Path(output["root"])
            with patch.object(sharded_jsonl, "MAX_COMPRESSED_SHARD_BYTES", 1):
                with patch.object(sharded_jsonl, "file_sha256") as digest:
                    with self.assertRaisesRegex(ShardedJsonlError, "compressed shard exceeds"):
                        list(iter_jsonl_records(root))
                    digest.assert_not_called()

    def test_shared_generator_and_verifier_reader_bounds_gzip_expansion(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "records.jsonl.gz"
            with gzip.open(path, "wb") as stream:
                stream.write(json.dumps({"value": "a" * 512}).encode("utf-8") + b"\n")
            with patch.object(sharded_jsonl, "MAX_UNCOMPRESSED_SHARD_BYTES", 128):
                with self.assertRaisesRegex(ShardedJsonlError, "uncompressed shard exceeds"):
                    list(iter_jsonl_records(path))

    def test_shared_generator_and_verifier_reader_bounds_single_lines(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "records.jsonl.gz"
            with gzip.open(path, "wb") as stream:
                stream.write(json.dumps({"value": "a" * 512}).encode("utf-8") + b"\n")
            with patch.object(sharded_jsonl, "MAX_RECORD_BYTES", 128):
                with self.assertRaisesRegex(ShardedJsonlError, "record exceeds"):
                    list(iter_jsonl_records(path))


if __name__ == "__main__":
    unittest.main()
