from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_checksums", ROOT / "scripts" / "build_checksums.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ChecksumTests(unittest.TestCase):
    def test_checksum_manifest_is_sorted_and_excludes_itself(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "b.txt").write_text("b", encoding="utf-8")
            (root / "a.txt").write_text("a", encoding="utf-8")
            (root / "checksums.json").write_text("old", encoding="utf-8")
            manifest = json.loads(MODULE.render(root))
            self.assertEqual(["a.txt", "b.txt"], [item["path"] for item in manifest["files"]])
            self.assertEqual(2, manifest["file_count"])
            self.assertTrue(all(len(item["sha256"]) == 64 for item in manifest["files"]))

    def test_file_hashing_is_chunked_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.bin"
            payload = bytes(range(256)) * (MODULE.HASH_CHUNK_SIZE // 256 + 5)
            path.write_bytes(payload)
            size, digest = MODULE.hash_file(path)
            import hashlib

            self.assertEqual(len(payload), size)
            self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
