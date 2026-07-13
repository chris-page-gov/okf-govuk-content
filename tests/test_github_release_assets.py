from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.verify_github_release_assets import (
    build_expectation,
    verify_release_assets,
    verify_release_expectation,
)


class GitHubReleaseAssetTests(unittest.TestCase):
    def test_draft_and_published_assets_are_bound_by_size_and_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "bundle.pack"
            asset.write_bytes(b"verified bytes")
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            document = {
                "tag_name": "v1.0.0",
                "draft": True,
                "immutable": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": asset.name,
                        "size": asset.stat().st_size,
                        "digest": f"sha256:{digest}",
                        "state": "uploaded",
                    }
                ],
            }
            self.assertEqual(verify_release_assets(document, [asset], tag="v1.0.0", published=False), [])
            expectation = build_expectation([asset], tag="v1.0.0")
            self.assertEqual(
                verify_release_expectation(document, expectation, tag="v1.0.0", published=False),
                [],
            )
            document.update({"draft": False, "immutable": True})
            self.assertEqual(verify_release_assets(document, [asset], tag="v1.0.0", published=True), [])

    def test_digest_asset_set_and_immutable_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "bundle.pack"
            asset.write_bytes(b"verified bytes")
            document = {
                "tag_name": "v1.0.0",
                "draft": False,
                "immutable": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": asset.name,
                        "size": asset.stat().st_size,
                        "digest": "sha256:" + "0" * 64,
                        "state": "uploaded",
                    },
                    {"name": "unexpected", "size": 1, "digest": "sha256:" + "1" * 64, "state": "uploaded"},
                ],
            }
            errors = verify_release_assets(document, [asset], tag="v1.0.0", published=True)
            self.assertTrue(any("not immutable" in error for error in errors))
            self.assertTrue(any("names differ" in error for error in errors))
            self.assertTrue(any("SHA-256 differs" in error for error in errors))
            expectation = build_expectation([asset], tag="v1.0.0")
            expectation["root_sha256"] = "0" * 64
            errors = verify_release_expectation(document, expectation, tag="v1.0.0", published=True)
            self.assertTrue(any("expectation count or root differs" in error for error in errors))

            duplicate = build_expectation([asset], tag="v1.0.0")
            duplicate["assets"] = [duplicate["assets"][0], duplicate["assets"][0]]
            duplicate["asset_count"] = 2
            material = "".join(
                f"{row['name']}\0{row['bytes']}\0{row['digest']}\n" for row in duplicate["assets"]
            )
            duplicate["root_sha256"] = hashlib.sha256(material.encode()).hexdigest()
            errors = verify_release_expectation(document, duplicate, tag="v1.0.0", published=True)
            self.assertTrue(any("not sorted and unique" in error for error in errors))

    def test_attested_channel_and_prerelease_state_reject_wrong_published_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "bundle.pack"
            asset.write_bytes(b"verified bytes")
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            candidate_expectation = build_expectation([asset], tag="v1.0.0-rc.1")
            self.assertEqual(candidate_expectation["channel"], "release-candidate")
            self.assertIs(candidate_expectation["prerelease"], True)
            with self.assertRaisesRegex(ValueError, "differs from the semantic-version tag"):
                build_expectation([asset], tag="v1.0.0-rc.1", channel="final")
            wrong_candidate = {
                "tag_name": "v1.0.0-rc.1",
                "draft": False,
                "immutable": True,
                "prerelease": False,
                "assets": [
                    {
                        "name": asset.name,
                        "size": asset.stat().st_size,
                        "digest": f"sha256:{digest}",
                        "state": "uploaded",
                    }
                ],
            }
            errors = verify_release_expectation(
                wrong_candidate,
                candidate_expectation,
                tag="v1.0.0-rc.1",
                published=True,
            )
            self.assertTrue(any("prerelease state differs" in error for error in errors))

            final_expectation = build_expectation([asset], tag="v1.0.0")
            self.assertEqual(final_expectation["channel"], "final")
            self.assertIs(final_expectation["prerelease"], False)
            wrong_final = {**wrong_candidate, "tag_name": "v1.0.0", "prerelease": True}
            errors = verify_release_expectation(
                wrong_final,
                final_expectation,
                tag="v1.0.0",
                published=True,
            )
            self.assertTrue(any("prerelease state differs" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
