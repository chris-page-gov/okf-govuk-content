from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from govuk_okf.preview_publication import (
    PREVIEW_SNAPSHOT,
    PreviewPublicationError,
    check_preview_package,
    package_preview,
    validate_preview_source,
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_checksums(bundle: Path) -> None:
    rows = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.name in {"checksums.json", ".DS_Store"}:
            continue
        payload = path.read_bytes()
        rows.append(
            {
                "path": path.relative_to(bundle).as_posix(),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    write_json(
        bundle / "checksums.json",
        {
            "schema": "okf-checksums.v1",
            "algorithm": "sha256",
            "file_count": len(rows),
            "files": rows,
        },
    )


class PreviewPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="demonstrator-preview-"))
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        (self.bundle / "index.html").write_text("<!doctype html><title>Preview</title>\n", encoding="utf-8")
        (self.bundle / ".nojekyll").write_text("", encoding="utf-8")
        (self.bundle / "404.html").write_text("<!doctype html><title>Not found</title>\n", encoding="utf-8")
        write_json(
            self.bundle / "okf-explorer.json",
            {
                "snapshot": PREVIEW_SNAPSHOT,
                "status": "bounded-demonstrator",
                "description": "Derived preview; not a complete GOV.UK corpus.",
            },
        )
        write_json(
            self.bundle / "data" / "manifest.json",
            {"snapshot": PREVIEW_SNAPSHOT, "counts": {"records": 69}},
        )
        write_json(
            self.bundle / "data" / "demonstrator.json",
            {
                "schema": "govuk-new-child-demonstrator.v1",
                "snapshot": PREVIEW_SNAPSHOT,
                "status": "bounded_demonstrator",
                "coverage": {
                    "seed_expected": 69,
                    "seed_represented": 69,
                    "unexplained_seed_omissions": 0,
                },
            },
        )
        write_checksums(self.bundle)
        write_json(
            self.root / "release" / "manifest.yaml",
            {"snapshot": {"id": PREVIEW_SNAPSHOT, "kind": "fixture", "sampled": True}},
        )
        write_json(
            self.root / "release" / "status.json",
            {
                "release_id": PREVIEW_SNAPSHOT,
                "status": "checkpoint",
                "publication_ready": False,
                "machine_rc_complete": False,
                "programme_complete": False,
                "full_corpus_reconciled": False,
                "completion_statement": "AFHF_GOVUK_OKF_CHECKPOINT_V1",
            },
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def test_exact_fixture_is_packaged_and_transport_verifies(self) -> None:
        output = self.root / "preview"
        commit = "a" * 40
        manifest = package_preview(
            repository_root=self.root,
            bundle=self.bundle,
            output=output,
            commit=commit,
        )
        self.assertEqual(manifest["publication_tier"], "bounded-demonstrator-preview")
        self.assertFalse(manifest["release_promotion"])
        self.assertEqual(check_preview_package(output, expected_commit=commit), [])
        self.assertEqual(
            (self.bundle / "checksums.json").read_bytes(),
            (output / "site" / "checksums.json").read_bytes(),
        )

    def test_release_promotion_or_full_corpus_state_is_rejected(self) -> None:
        status_path = self.root / "release" / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["publication_ready"] = True
        status["machine_rc_complete"] = True
        write_json(status_path, status)
        errors = validate_preview_source(self.root, self.bundle)
        self.assertIn("release status publication_ready must remain false for the preview", errors)
        self.assertIn("release status machine_rc_complete must remain false for the preview", errors)

    def test_misleading_or_incomplete_fixture_is_rejected(self) -> None:
        descriptor_path = self.bundle / "okf-explorer.json"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor["description"] = "Complete GOV.UK corpus."
        write_json(descriptor_path, descriptor)
        demonstrator_path = self.bundle / "data" / "demonstrator.json"
        demonstrator = json.loads(demonstrator_path.read_text(encoding="utf-8"))
        demonstrator["coverage"]["seed_represented"] = 68
        write_json(demonstrator_path, demonstrator)
        write_checksums(self.bundle)
        errors = validate_preview_source(self.root, self.bundle)
        self.assertIn("Explorer descriptor does not state that the preview is incomplete", errors)
        self.assertIn("demonstrator represented count is not 69", errors)

    def test_tampering_after_packaging_is_rejected(self) -> None:
        output = self.root / "preview"
        package_preview(
            repository_root=self.root,
            bundle=self.bundle,
            output=output,
            commit="b" * 40,
        )
        (output / "site" / "index.html").write_text("tampered\n", encoding="utf-8")
        errors = check_preview_package(output, expected_commit="b" * 40)
        self.assertIn("bundle checksum manifest does not exactly cover the preview site", errors)

    def test_invalid_commit_and_nonempty_output_fail_closed(self) -> None:
        with self.assertRaisesRegex(PreviewPublicationError, "40-character"):
            package_preview(
                repository_root=self.root,
                bundle=self.bundle,
                output=self.root / "invalid",
                commit="main",
            )
        output = self.root / "nonempty"
        output.mkdir()
        (output / "existing").write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(PreviewPublicationError, "absent or empty"):
            package_preview(
                repository_root=self.root,
                bundle=self.bundle,
                output=output,
                commit="c" * 40,
            )


if __name__ == "__main__":
    unittest.main()
