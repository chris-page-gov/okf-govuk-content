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
        payloads = {
            "index.html": b"<!doctype html><title>OKF</title>\n",
            "okf-explorer.json": b'{"schema":"okf-explorer.v1"}\n',
            "data/manifest.json": b'{"snapshot":"T1-closed"}\n',
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

            with mock.patch.object(MODULE, "_fetch", side_effect=fetch):
                report = MODULE.run("https://example.test/wiki", verified)
            self.assertTrue(report["passed"], report["errors"])
            self.assertEqual(report["snapshot"], "T1-closed")
            self.assertEqual(len(report["results"]), 3)

    def test_cross_host_redirect_or_changed_bytes_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            verified, payloads = self.build_verified(Path(directory))

            def fetch(url: str) -> tuple[bytes, str, int, str]:
                relative = url.removeprefix("https://example.test/wiki/")
                payload = b"changed" if relative == "index.html" else payloads[relative]
                return payload, url.replace("example.test", "elsewhere.test"), 200, "text/plain"

            with mock.patch.object(MODULE, "_fetch", side_effect=fetch):
                report = MODULE.run("https://example.test/wiki/", verified)
            self.assertFalse(report["passed"])
            self.assertTrue(any("live byte verification failed" in error for error in report["errors"]))

    def test_non_https_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            verified, _ = self.build_verified(Path(directory))
            with self.assertRaisesRegex(ValueError, "absolute HTTPS"):
                MODULE.run("http://example.test/wiki/", verified)


if __name__ == "__main__":
    unittest.main()
