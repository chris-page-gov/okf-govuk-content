from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "smoke_published_bundle", ROOT / "scripts" / "smoke_published_bundle.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PublishedSmokeTests(unittest.TestCase):
    def build_verified(self, root: Path) -> tuple[Path, dict[str, bytes]]:
        site = root / "verified" / "site"
        import gzip

        source_payload = b'{"shard":true}\n'
        pack_payload = gzip.compress(source_payload, mtime=0)
        pack_hash = hashlib.sha256(pack_payload).hexdigest()
        source_hash = hashlib.sha256(source_payload).hexdigest()
        pack_name = "okf-govuk-data-v1.0.0-00000.pack.gz"
        pack_index = {
            "schema": "govuk-okf-github-release-pack-index.v1",
            "packs": [
                {
                    "id": "pack-00000",
                    "asset_name": pack_name,
                    "path": f"data-packs/{pack_name}",
                    "bytes": len(pack_payload),
                    "sha256": pack_hash,
                }
            ],
            "entries": [
                {
                    "path": "data/records-0.json",
                    "pack": "pack-00000",
                    "offset": 0,
                    "bytes": len(source_payload),
                    "sha256": source_hash,
                    "compression": "identity",
                    "packed_bytes": len(pack_payload),
                    "packed_sha256": pack_hash,
                    "transport_compression": "gzip",
                }
            ],
        }
        payloads = {
            "index.html": b"<!doctype html><title>OKF</title>\n",
            "okf-explorer.json": b'{"schema":"okf-explorer.v1"}\n',
            "data/manifest.json": b'{"snapshot":"T1-closed"}\n',
            "release-data-plane.json": (json.dumps(pack_index, sort_keys=True) + "\n").encode(),
            f"data-packs/{pack_name}": pack_payload,
        }
        rows = []
        for relative, payload in payloads.items():
            path = site / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            rows.append(
                {
                    "path": relative,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        (site / "checksums.json").write_text(
            json.dumps(
                {
                    "schema": "okf-checksums.v1",
                    "algorithm": "sha256",
                    "file_count": len(rows),
                    "files": rows,
                }
            ),
            encoding="utf-8",
        )
        return root / "verified", payloads

    def test_exact_live_bytes_and_snapshot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            verified, payloads = self.build_verified(Path(directory))

            def fetch(url: str) -> tuple[bytes, str, int, str]:
                relative = url.removeprefix("https://example.test/wiki/")
                return payloads[relative], url, 200, "application/octet-stream"

            def fetch_range(url: str, start: int, end: int) -> tuple[bytes, str, int, str, str, str]:
                relative = url.removeprefix("https://example.test/wiki/")
                payload = payloads[relative][start : end + 1]
                return payload, url, 206, f"bytes {start}-{end}/{len(payloads[relative])}", "", "application/gzip"

            with mock.patch.object(MODULE, "_fetch", side_effect=fetch), mock.patch.object(
                MODULE, "_fetch_range", side_effect=fetch_range
            ):
                report = MODULE.run("https://example.test/wiki", verified)
            self.assertTrue(report["passed"], report["errors"])
            self.assertEqual(report["snapshot"], "T1-closed")
            self.assertEqual(len(report["results"]), 4)
            self.assertEqual(len(report["range_results"]), 1)

    def test_cross_host_redirect_or_changed_bytes_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            verified, payloads = self.build_verified(Path(directory))

            def fetch(url: str) -> tuple[bytes, str, int, str]:
                relative = url.removeprefix("https://example.test/wiki/")
                payload = b"changed" if relative == "index.html" else payloads[relative]
                return payload, url.replace("example.test", "elsewhere.test"), 200, "text/plain"

            def fetch_range(url: str, start: int, end: int) -> tuple[bytes, str, int, str, str, str]:
                relative = url.removeprefix("https://example.test/wiki/")
                payload = payloads[relative][start : end + 1]
                return payload, url.replace("example.test", "elsewhere.test"), 206, f"bytes {start}-{end}/{len(payloads[relative])}", "", "application/gzip"

            with mock.patch.object(MODULE, "_fetch", side_effect=fetch), mock.patch.object(
                MODULE, "_fetch_range", side_effect=fetch_range
            ):
                report = MODULE.run("https://example.test/wiki/", verified)
            self.assertFalse(report["passed"])
            self.assertTrue(any("live byte verification failed" in error for error in report["errors"]))

    def test_non_https_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            verified, _ = self.build_verified(Path(directory))
            with self.assertRaisesRegex(ValueError, "absolute HTTPS"):
                MODULE.run("http://example.test/wiki/", verified)

    def test_wrong_live_pack_content_range_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            verified, payloads = self.build_verified(Path(directory))

            def fetch(url: str) -> tuple[bytes, str, int, str]:
                relative = url.removeprefix("https://example.test/wiki/")
                return payloads[relative], url, 200, "application/octet-stream"

            def fetch_range(url: str, start: int, end: int) -> tuple[bytes, str, int, str, str, str]:
                relative = url.removeprefix("https://example.test/wiki/")
                payload = payloads[relative][start : end + 1]
                return payload, url, 206, f"bytes {start}-{end}/{len(payloads[relative]) + 1}", "", "application/gzip"

            with mock.patch.object(MODULE, "_fetch", side_effect=fetch), mock.patch.object(
                MODULE, "_fetch_range", side_effect=fetch_range
            ):
                report = MODULE.run("https://example.test/wiki/", verified)
            self.assertFalse(report["passed"])
            self.assertTrue(any("range verification failed" in error for error in report["errors"]))

    def test_range_transport_gzip_expansion_is_bounded_by_declared_size(self) -> None:
        import gzip

        payload = gzip.compress(b"x" * 4096, mtime=0)
        with self.assertRaisesRegex(ValueError, "decoded length differs"):
            MODULE._bounded_gunzip(payload, 1)
        with self.assertRaisesRegex(ValueError, "invalid logical member size"):
            MODULE._bounded_gunzip(payload, MODULE.MAX_RANGE_BYTES + 1)


if __name__ == "__main__":
    unittest.main()
