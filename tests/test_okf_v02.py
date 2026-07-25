from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.okf_v02 import (  # noqa: E402
    OKF_VERSION,
    parse_frontmatter,
    render_frontmatter,
    validate_concept_metadata,
    validate_okf_v02_bundle,
)
from govuk_okf.publication import build_publication, load_jsonl  # noqa: E402
from govuk_okf.util import read_gzip_json  # noqa: E402


class OkfV02Tests(unittest.TestCase):
    fixture = ROOT / "tests" / "fixtures" / "corpus" / "source-records.jsonl"
    generated_at = "2026-07-25T09:30:00Z"
    snapshot = "fixture-okf-v02"

    def build(self, output: Path) -> None:
        build_publication(
            load_jsonl(self.fixture),
            output,
            self.generated_at,
            self.snapshot,
        )

    def test_generated_bundle_is_conformant_and_declares_only_root_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            self.assertEqual([], validate_okf_v02_bundle(output))
            root, _body = parse_frontmatter(
                (output / "index.md").read_text(encoding="utf-8")
            )
            self.assertEqual({"okf_version": OKF_VERSION}, root)
            for reserved in output.rglob("index.md"):
                metadata, _body = parse_frontmatter(
                    reserved.read_text(encoding="utf-8")
                )
                if reserved != output / "index.md":
                    self.assertIsNone(metadata, reserved)
            log_metadata, _body = parse_frontmatter(
                (output / "log.md").read_text(encoding="utf-8")
            )
            self.assertIsNone(log_metadata)

    def test_canonical_concepts_separate_generation_source_change_and_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            manifest = json.loads(
                (output / "data/manifest.json").read_text(encoding="utf-8")
            )
            datasets = read_gzip_json(output / manifest["chunks"]["datasets"][0])
            row = next(
                item for item in datasets if item.get("public_updated_at")
            )
            concept = output / "concepts" / f"{row['open']}.md"
            metadata, body = parse_frontmatter(concept.read_text(encoding="utf-8"))
            assert metadata is not None
            self.assertEqual("GOV.UK Content Item", metadata["type"])
            self.assertEqual(
                {"at": self.generated_at, "by": "govuk-okf/0.1.0"},
                metadata["generated"],
            )
            self.assertEqual(
                row["public_updated_at"][:10],
                metadata["sources"][0]["last_modified"],
            )
            self.assertEqual(row["retrieved_at"], metadata["govuk"]["retrieved_at"])
            self.assertEqual("draft", metadata["status"])
            self.assertNotIn("verified", metadata)
            self.assertNotIn("stale_after", metadata)
            self.assertIn("# Trust and authority", body)

    def test_descriptor_preserves_extensions_and_surfaces_snapshot_live_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            descriptor = json.loads(
                (output / "okf-explorer.json").read_text(encoding="utf-8")
            )
            semantic = json.loads(
                (output / "okf-bundle.jsonld").read_text(encoding="utf-8")
            )
            self.assertEqual(OKF_VERSION, descriptor["okf_version"])
            self.assertEqual(OKF_VERSION, semantic["okfVersion"])
            self.assertEqual(
                "concepts/index.md",
                descriptor["entrypoints"]["canonical_concepts"],
            )
            self.assertEqual(
                "governed-snapshot", descriptor["snapshot_state"]["mode"]
            )
            self.assertEqual(
                "https://www.gov.uk/",
                descriptor["snapshot_state"]["live_authority"],
            )
            self.assertTrue(descriptor["snapshot_state"]["drift_expected"])
            self.assertIn("govuk-okf-profile.v1", descriptor["extensions"])
            self.assertIn("okf-v0.2", descriptor["extensions"])

    def test_canonical_concept_links_and_descriptor_counts_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            descriptor = json.loads(
                (output / "okf-explorer.json").read_text(encoding="utf-8")
            )
            concept_files = [
                path
                for path in output.rglob("*.md")
                if path.name not in {"index.md", "log.md"}
            ]
            extension = descriptor["extensions"]["okf-v0.2"]
            self.assertEqual(len(concept_files), extension["canonical_concepts"])
            domain_concepts = list(
                (output / "concepts").rglob("*.md")
            )
            self.assertEqual(
                len(
                    [
                        path
                        for path in domain_concepts
                        if path.name not in {"index.md", "log.md"}
                    ]
                ),
                extension["domain_concepts"],
            )
            link_pattern = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
            for concept in domain_concepts:
                if concept.name in {"index.md", "log.md"}:
                    continue
                _metadata, body = parse_frontmatter(
                    concept.read_text(encoding="utf-8")
                )
                for target in link_pattern.findall(body):
                    if "://" in target or target.startswith(("#", "/")):
                        continue
                    self.assertTrue(
                        (concept.parent / target).resolve().is_file(),
                        f"{concept.relative_to(output)} -> {target}",
                    )

    def test_unknown_extensions_are_tolerated_and_v02_families_are_validated(self) -> None:
        concept = {
            "type": "Unknown future type",
            "generated": {
                "by": "producer/1.0",
                "at": "2026-07-25T09:30:00Z",
            },
            "verified": {
                "by": "process:nightly-check",
                "at": "2026-07-25T10:00:00Z",
            },
            "sources": [
                {
                    "resource": "https://example.test/source",
                    "last_modified": "2026-07-24",
                }
            ],
            "future_extension": {"preserved": True},
        }
        self.assertEqual(
            [],
            validate_concept_metadata(concept, label="future.md"),
        )

    def test_missing_type_and_invalid_attested_computation_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "index.md").write_text(
                render_frontmatter({"okf_version": OKF_VERSION}) + "# Bundle\n",
                encoding="utf-8",
            )
            (bundle / "broken.md").write_text(
                render_frontmatter(
                    {
                        "title": "Missing type",
                        "generated": {
                            "by": "producer/1.0",
                            "at": "2026-07-25T09:30:00Z",
                        },
                    }
                )
                + "# Broken\n",
                encoding="utf-8",
            )
            errors = validate_okf_v02_bundle(bundle)
            self.assertTrue(any("type must be" in error for error in errors))
        errors = validate_concept_metadata(
            {"type": "Attested Computation"},
            label="computation.md",
        )
        self.assertTrue(any("requires runtime" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
