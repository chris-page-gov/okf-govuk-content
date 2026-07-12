from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.acquisition import write_jsonl_gzip_shards
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


if __name__ == "__main__":
    unittest.main()
