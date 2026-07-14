from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from govuk_okf.release_packaging import (
    PackagingError,
    attach_pages_browser_evidence,
    check_verified_release,
    package_verified_release,
)
from govuk_okf.release_data_plane import (
    GITHUB_RELEASE_ASSET_MAX_BYTES,
    DataPlanePackError,
    build_release_packs,
    collect_data_plane_rows,
    verify_release_packs,
)
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


def full_browser_evidence(output: Path, snapshot: str = "T1-20260712") -> dict[str, object]:
    return {
        "schema": "govuk-okf-explorer-browser-evidence.v1",
        "snapshot": snapshot,
        "artifact_tier": "full_release_snapshot",
        "data_plane_index_sha256": hashlib.sha256((output / "site/release-data-plane.json").read_bytes()).hexdigest(),
        "site_checksums_sha256": hashlib.sha256((output / "site/checksums.json").read_bytes()).hexdigest(),
        "publication_ready": True,
        "overall_status": "automated_full_release_evidence_pass",
        "accessibility": {"pass": True},
        "routing_and_data": {"pass": True},
        "performance": {"pass": True},
        "full_release_gates": {"full_corpus_browser_measurement": "passed"},
        "console_exceptions": [],
    }


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

    def test_passing_packed_site_browser_evidence_is_bound_before_release(self) -> None:
        output = self.root / "browser-bound"
        package_verified_release(
            repository_root=self.root,
            bundle=self.bundle,
            output=output,
            tag="v0.1.0",
            browser_evidence=self.root / "browser.json",
        )
        evidence = self.root / "pages-pack-browser.json"
        write_json(evidence, full_browser_evidence(output))
        attach_pages_browser_evidence(output, evidence)
        self.assertEqual(check_verified_release(output), [])
        self.assertTrue((output / "assets/evidence-pages-pack-browser.json").is_file())
        with self.assertRaisesRegex(PackagingError, "already attached"):
            attach_pages_browser_evidence(output, evidence)

    def test_packed_site_browser_evidence_requires_the_real_release_schema(self) -> None:
        output = self.root / "browser-schema"
        package_verified_release(
            repository_root=self.root,
            bundle=self.bundle,
            output=output,
            tag="v0.1.0",
            browser_evidence=self.root / "browser.json",
        )
        evidence = self.root / "incorrect-pages-pack-browser.json"
        write_json(
            evidence,
            {
                **full_browser_evidence(output),
                "schema": "fixture-browser.v1",
            },
        )
        with self.assertRaisesRegex(PackagingError, "full-release schema"):
            attach_pages_browser_evidence(output, evidence)

    def test_packed_site_browser_evidence_is_snapshot_bound_and_fail_closed(self) -> None:
        for suffix, mutation in (
            ("snapshot", {"snapshot": "T1-other"}),
            ("publication", {"publication_ready": False}),
            ("routing", {"routing_and_data": {"pass": False}}),
            ("console", {"console_exceptions": ["boom"]}),
        ):
            with self.subTest(suffix=suffix):
                output = self.root / f"browser-{suffix}"
                package_verified_release(
                    repository_root=self.root,
                    bundle=self.bundle,
                    output=output,
                    tag="v0.1.0",
                    browser_evidence=self.root / "browser.json",
                )
                evidence = self.root / f"incorrect-pages-pack-browser-{suffix}.json"
                write_json(evidence, {**full_browser_evidence(output), **mutation})
                with self.assertRaisesRegex(PackagingError, "full-release schema"):
                    attach_pages_browser_evidence(output, evidence)

    def test_same_snapshot_browser_evidence_cannot_replay_across_different_packed_bytes(self) -> None:
        first = self.root / "browser-first"
        package_verified_release(
            repository_root=self.root,
            bundle=self.bundle,
            output=first,
            tag="v0.1.0",
            browser_evidence=self.root / "browser.json",
        )

        other_root = self.root / "other-repository"
        shutil.copytree(self.root / "release", other_root / "release")
        other_bundle = other_root / "bundle"
        shutil.copytree(self.bundle, other_bundle)
        payload = b'{"same_snapshot":"different_bytes"}\n'
        shard_path = other_bundle / "data" / "different.json"
        shard_path.write_bytes(payload)
        manifest_path = other_bundle / "data" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["shards"] = {
            "datasets": [
                {
                    "path": "data/different.json",
                    "compressed_bytes": len(payload),
                    "compression": "identity",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ]
        }
        write_json(manifest_path, manifest)
        build_bundle_checksums(other_bundle)
        second = self.root / "browser-second"
        package_verified_release(
            repository_root=other_root,
            bundle=other_bundle,
            output=second,
            tag="v0.1.0",
            browser_evidence=self.root / "browser.json",
        )
        self.assertEqual(
            json.loads((first / "site/release-data-plane.json").read_text(encoding="utf-8"))["snapshot"],
            json.loads((second / "site/release-data-plane.json").read_text(encoding="utf-8"))["snapshot"],
        )
        self.assertNotEqual(
            full_browser_evidence(first)["data_plane_index_sha256"],
            full_browser_evidence(second)["data_plane_index_sha256"],
        )
        evidence = self.root / "same-snapshot-different-site.json"
        write_json(evidence, full_browser_evidence(second))
        with self.assertRaisesRegex(PackagingError, "full-release schema"):
            attach_pages_browser_evidence(first, evidence)

    def test_data_shards_are_range_packed_and_absent_from_pages(self) -> None:
        import gzip

        shard = gzip.compress(b'{"fixture":true}\n', mtime=0)
        shard_path = self.bundle / "data" / "records-0.json.gz"
        shard_path.write_bytes(shard)
        manifest_path = self.bundle / "data" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["shards"] = {
            "datasets": [
                {
                    "path": "data/records-0.json.gz",
                    "compressed_bytes": len(shard),
                    "sha256": hashlib.sha256(shard).hexdigest(),
                    "compression": "gzip",
                }
            ]
        }
        write_json(manifest_path, manifest)
        build_bundle_checksums(self.bundle)
        output = self.root / "range-packed"
        package_verified_release(
            repository_root=self.root,
            bundle=self.bundle,
            output=output,
            tag="v0.1.0",
            browser_evidence=self.root / "browser.json",
        )
        self.assertFalse((output / "site/data/records-0.json.gz").exists())
        index = json.loads((output / "site/release-data-plane.json").read_text(encoding="utf-8"))
        self.assertEqual(index["counts"]["virtual_shards"], 1)
        self.assertEqual(index["entries"][0]["path"], "data/records-0.json.gz")
        self.assertEqual(index["entries"][0]["offset"], 0)
        self.assertEqual(index["entries"][0]["bytes"], len(shard))
        self.assertEqual(verify_release_packs(index, output / "assets"), [])
        self.assertEqual(check_verified_release(output), [])
        pack = output / "assets" / index["packs"][0]["asset_name"]
        pack.write_bytes(b"tampered")
        self.assertTrue(check_verified_release(output))

    def test_pack_partitioning_is_deterministic_and_bounded_to_64_mib(self) -> None:
        shard_rows = []
        for ordinal, payload in enumerate((b"aaaa", b"bbbb", b"cccc")):
            path = f"data/records-{ordinal}.json"
            (self.bundle / path).write_bytes(payload)
            shard_rows.append(
                {
                    "path": path,
                    "compressed_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "compression": "identity",
                }
            )
        manifest_path = self.bundle / "data/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["shards"] = {"datasets": shard_rows}
        write_json(manifest_path, manifest)
        assets = self.root / "partitioned-assets"
        index = build_release_packs(
            bundle=self.bundle,
            assets=assets,
            repository="chris-page-gov/okf-govuk-content",
            tag="v0.1.0",
            max_pack_bytes=50,
        )
        self.assertEqual(index["counts"]["packs"], 2)
        self.assertEqual(index["counts"]["virtual_shards"], 3)
        self.assertEqual(index["counts"]["source_bytes"], 12)
        first_length = index["entries"][0]["packed_bytes"]
        self.assertEqual([row["offset"] for row in index["entries"]], [0, first_length, 0])
        self.assertTrue(all(row["transport_compression"] == "gzip" for row in index["entries"]))
        self.assertEqual(verify_release_packs(index, assets), [])
        with self.assertRaisesRegex(DataPlanePackError, "no greater than 64 MiB"):
            build_release_packs(
                bundle=self.bundle,
                assets=self.root / "too-large",
                repository="chris-page-gov/okf-govuk-content",
                tag="v0.1.0",
                max_pack_bytes=GITHUB_RELEASE_ASSET_MAX_BYTES,
            )

    def test_every_physical_postings_partition_is_collected_for_release(self) -> None:
        rows = []
        for partition in range(2):
            relative = f"data/search/postings/ca-{partition:05d}.json"
            payload = (
                json.dumps(
                    {"tokens": {f"ca{partition:06d}": [[partition, 16, 1]]}},
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            (self.bundle / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.bundle / relative).write_bytes(payload)
            rows.append(
                {
                    "path": relative,
                    "compressed_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "compression": "identity",
                }
            )
        write_json(
            self.bundle / "data/search/manifest.json",
            {"shard_metadata": "data/search/shards.json"},
        )
        write_json(
            self.bundle / "data/search/shards.json",
            {"shards": {"postings": rows}},
        )
        self.assertEqual(
            [row["path"] for row in rows],
            [row["path"] for row in collect_data_plane_rows(self.bundle)],
        )
        assets = self.root / "split-search-assets"
        index = build_release_packs(
            bundle=self.bundle,
            assets=assets,
            repository="chris-page-gov/okf-govuk-content",
            tag="v0.1.0",
        )
        self.assertEqual(
            [row["path"] for row in rows],
            [row["path"] for row in index["entries"]],
        )
        self.assertEqual(verify_release_packs(index, assets), [])

    def test_gzip_member_expansion_and_oversized_declaration_fail_bounded(self) -> None:
        payload = b"x" * 4096
        path = self.bundle / "data/records-bomb.json"
        path.write_bytes(payload)
        manifest_path = self.bundle / "data/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["shards"] = {
            "datasets": [
                {
                    "path": "data/records-bomb.json",
                    "compressed_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "compression": "identity",
                }
            ]
        }
        write_json(manifest_path, manifest)
        assets = self.root / "bounded-gzip-assets"
        index = build_release_packs(
            bundle=self.bundle,
            assets=assets,
            repository="chris-page-gov/okf-govuk-content",
            tag="v0.1.0",
        )
        index["entries"][0]["bytes"] = 1
        self.assertTrue(any("source range hash differs" in error for error in verify_release_packs(index, assets)))
        index["entries"][0]["bytes"] = 64 * 1024 * 1024 + 1
        self.assertTrue(any("ranges are not contiguous" in error for error in verify_release_packs(index, assets)))

    def test_unsafe_or_symlinked_pack_assets_are_never_opened(self) -> None:
        payload = b"safe logical bytes"
        path = self.bundle / "data/records-unsafe.json"
        path.write_bytes(payload)
        manifest_path = self.bundle / "data/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["shards"] = {
            "datasets": [
                {
                    "path": "data/records-unsafe.json",
                    "compressed_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "compression": "identity",
                }
            ]
        }
        write_json(manifest_path, manifest)
        assets = self.root / "unsafe-pack-assets"
        index = build_release_packs(
            bundle=self.bundle,
            assets=assets,
            repository="chris-page-gov/okf-govuk-content",
            tag="v0.1.0",
        )
        outside = self.root / "outside.pack.gz"
        outside.write_bytes(b"must never be read")
        original_open = Path.open

        def reject_outside_open(candidate: Path, *args: object, **kwargs: object):
            if candidate == outside or candidate.resolve() == outside.resolve():
                raise AssertionError(f"unsafe pack path was opened: {candidate}")
            return original_open(candidate, *args, **kwargs)

        absolute_index = json.loads(json.dumps(index))
        absolute_index["packs"][0]["asset_name"] = str(outside)
        with patch.object(Path, "open", new=reject_outside_open):
            errors = verify_release_packs(absolute_index, assets)
        self.assertTrue(any("pack name is unsafe" in error for error in errors))

        symlink = assets / "outside-link.pack.gz"
        symlink.symlink_to(outside)
        symlink_index = json.loads(json.dumps(index))
        symlink_index["packs"][0]["asset_name"] = symlink.name
        symlink_index["packs"][0]["path"] = f"data-packs/{symlink.name}"
        symlink_index["packs"][0]["release_url"] = (
            "https://github.com/chris-page-gov/okf-govuk-content/releases/download/"
            f"v0.1.0/{symlink.name}"
        )
        with patch.object(Path, "open", new=reject_outside_open):
            errors = verify_release_packs(symlink_index, assets)
        self.assertTrue(any("pack file is missing or unsafe" in error for error in errors))

    def test_oversized_pack_is_rejected_before_hashing(self) -> None:
        payload = b"bounded bytes"
        path = self.bundle / "data/records-oversized.json"
        path.write_bytes(payload)
        manifest_path = self.bundle / "data/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["shards"] = {
            "datasets": [
                {
                    "path": "data/records-oversized.json",
                    "compressed_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "compression": "identity",
                }
            ]
        }
        write_json(manifest_path, manifest)
        assets = self.root / "oversized-pack-assets"
        index = build_release_packs(
            bundle=self.bundle,
            assets=assets,
            repository="chris-page-gov/okf-govuk-content",
            tag="v0.1.0",
        )
        index["max_pack_bytes"] = 1
        index["packs"][0]["bytes"] = 1
        with patch(
            "govuk_okf.release_data_plane._sha256_file",
            side_effect=AssertionError("oversized pack was hashed"),
        ):
            errors = verify_release_packs(index, assets)
        self.assertTrue(any("pack size differs or exceeds" in error for error in errors))

    def test_symlinked_asset_root_or_parent_is_rejected_before_any_read(self) -> None:
        payload = b"safe root bytes"
        path = self.bundle / "data/records-root.json"
        path.write_bytes(payload)
        manifest_path = self.bundle / "data/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["shards"] = {
            "datasets": [
                {
                    "path": "data/records-root.json",
                    "compressed_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "compression": "identity",
                }
            ]
        }
        write_json(manifest_path, manifest)
        real_parent = self.root / "real-pack-parent"
        assets = real_parent / "assets"
        index = build_release_packs(
            bundle=self.bundle,
            assets=assets,
            repository="chris-page-gov/okf-govuk-content",
            tag="v0.1.0",
        )
        original_open = Path.open

        def reject_pack_open(candidate: Path, *args: object, **kwargs: object):
            if candidate.resolve().is_relative_to(assets.resolve()):
                raise AssertionError(f"pack under symlinked asset root was opened: {candidate}")
            return original_open(candidate, *args, **kwargs)

        direct_root = self.root / "direct-assets-link"
        direct_root.symlink_to(assets, target_is_directory=True)
        parent_link = self.root / "pack-parent-link"
        parent_link.symlink_to(real_parent, target_is_directory=True)
        for unsafe_assets in (direct_root, parent_link / "assets"):
            with self.subTest(unsafe_assets=unsafe_assets):
                with patch.object(Path, "open", new=reject_pack_open):
                    errors = verify_release_packs(index, unsafe_assets)
                self.assertTrue(any("asset directory is missing or unsafe" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
