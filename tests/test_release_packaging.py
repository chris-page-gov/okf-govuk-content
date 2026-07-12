from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from govuk_okf.release_packaging import PackagingError, check_verified_release, package_verified_release
from govuk_okf.release_ref import release_channel, validate_release_ref, validate_tag_name


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_bundle_checksums(bundle: Path) -> None:
    rows = []
    for path in sorted(bundle.rglob("*")):
        if path.is_file() and path.name != "checksums.json":
            payload = path.read_bytes()
            rows.append(
                {
                    "path": path.relative_to(bundle).as_posix(),
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
    write_json(bundle / "checksums.json", {"schema": "okf-checksums.v1", "algorithm": "sha256", "file_count": len(rows), "files": rows})


class ReleasePackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="release-package-"))
        self.bundle = self.root / "bundle"
        write_json(self.bundle / "data" / "manifest.json", {"snapshot": "T1-20260712", "counts": {"records": 1}})
        write_json(self.bundle / "okf-explorer.json", {"entrypoints": {"data_manifest": "data/manifest.json"}})
        (self.bundle / "index.html").write_text("<!doctype html><title>Verified</title>\n", encoding="utf-8")
        build_bundle_checksums(self.bundle)
        write_json(self.root / "release" / "status.json", {"publication_ready": True})
        write_json(self.root / "release" / "sbom.cdx.json", {"bomFormat": "CycloneDX", "specVersion": "1.6"})
        write_json(
            self.root / "release" / "manifest.yaml",
            {
                "artifacts": {
                    "bundle": "bundle",
                    "checksums": "bundle/checksums.json",
                    "descriptor": "bundle/okf-explorer.json",
                    "status": "release/status.json",
                    "sbom": "release/sbom.cdx.json",
                }
            },
        )
        write_json(self.root / "browser.json", {"overall_status": "automated_full_release_evidence_pass"})

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def test_packaging_is_byte_deterministic_and_self_verifying(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        one = package_verified_release(
            repository_root=self.root,
            bundle=self.bundle,
            output=first,
            tag="v0.1.0",
            browser_evidence=self.root / "browser.json",
        )
        two = package_verified_release(
            repository_root=self.root,
            bundle=self.bundle,
            output=second,
            tag="v0.1.0",
            browser_evidence=self.root / "browser.json",
        )
        self.assertEqual(one["root_sha256"], two["root_sha256"])
        first_files = {path.relative_to(first): hashlib.sha256(path.read_bytes()).hexdigest() for path in first.rglob("*") if path.is_file()}
        second_files = {path.relative_to(second): hashlib.sha256(path.read_bytes()).hexdigest() for path in second.rglob("*") if path.is_file()}
        self.assertEqual(first_files, second_files)
        self.assertEqual(check_verified_release(first), [])
        self.assertTrue((first / "assets" / "bundle-checksums.json").is_file())
        self.assertTrue((first / "assets" / "evidence-sbom.cdx.json").is_file())
        self.assertTrue((first / "assets" / "evidence-browser-workflow.json").is_file())

    def test_tampering_and_nonempty_output_fail_closed(self) -> None:
        output = self.root / "verified"
        package_verified_release(
            repository_root=self.root,
            bundle=self.bundle,
            output=output,
            tag="v0.1.0",
            browser_evidence=self.root / "browser.json",
        )
        (output / "site" / "index.html").write_text("tampered", encoding="utf-8")
        self.assertTrue(check_verified_release(output))
        with self.assertRaises(PackagingError):
            package_verified_release(
                repository_root=self.root,
                bundle=self.bundle,
                output=output,
                tag="v0.1.0",
                browser_evidence=self.root / "browser.json",
            )

    def test_tag_name_matches_project_version(self) -> None:
        self.assertEqual(validate_tag_name("v0.1.0", "0.1.0"), [])
        self.assertEqual(validate_tag_name("v0.1.0-rc.1", "0.1.0"), [])
        self.assertEqual(release_channel("v0.1.0-rc.1"), "release-candidate")
        self.assertEqual(release_channel("v0.1.0"), "final")
        self.assertTrue(validate_tag_name("v0.1.0-rc.0", "0.1.0"))
        self.assertTrue(validate_tag_name("v00.1.0", "0.1.0"))
        self.assertTrue(validate_tag_name("v0.01.0-rc.1", "0.1.0"))
        self.assertTrue(validate_tag_name("release-0.1.0", "0.1.0"))
        self.assertTrue(validate_tag_name("v0.2.0", "0.1.0"))

    def test_annotated_tag_and_main_ancestry_can_be_checked_without_signature_in_fixture(self) -> None:
        repository = self.root / "git"
        repository.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
        (repository / "pyproject.toml").write_text('[project]\nname="fixture"\nversion="0.1.0"\n', encoding="utf-8")
        subprocess.run(["git", "add", "pyproject.toml"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "tag", "-a", "v0.1.0", "-m", "fixture release"], cwd=repository, check=True)
        report = validate_release_ref(
            repository,
            tag="v0.1.0",
            expected_commit=commit,
            main_ref="main",
        )
        self.assertTrue(report["passed"], report["errors"])
        self.assertFalse(report["signature_present"])
        self.assertEqual(report["signature_status"], "absent_optional")
        self.assertEqual(report["channel"], "final")

    def test_release_candidate_is_packaged_deterministically(self) -> None:
        output = self.root / "candidate"
        result = package_verified_release(
            repository_root=self.root,
            bundle=self.bundle,
            output=output,
            tag="v0.1.0-rc.1",
            browser_evidence=self.root / "browser.json",
        )
        self.assertEqual(result["tag"], "v0.1.0-rc.1")
        self.assertEqual(result["channel"], "release-candidate")
        self.assertEqual(check_verified_release(output), [])


if __name__ == "__main__":
    unittest.main()
