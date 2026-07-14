from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.discovery import DiscoveryError, DiscoveryIndex
from govuk_okf.publication import build_publication, load_jsonl
from govuk_okf.util import read_gzip_json


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.bundle = Path(self.directory.name) / "bundle"
        records = load_jsonl(ROOT / "tests" / "fixtures" / "corpus" / "source-records.jsonl")
        build_publication(records, self.bundle, "2026-07-11T23:30:00Z", "fixture-2026-07-11")
        self.index = DiscoveryIndex(self.bundle)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_search_fetch_traverse_and_citation(self) -> None:
        result = self.index.search("universal credit", limit=5)
        self.assertEqual("metadata_discovery", result["answerability"])
        self.assertTrue(result["results"])
        selected = result["results"][0]
        fetched = self.index.fetch(selected["open"])
        self.assertEqual(selected["ordinal"], fetched["ordinal"])
        traversed = self.index.traverse(selected["open"])
        self.assertTrue(traversed["relationships"])
        citation = self.index.citation(selected["open"])
        self.assertEqual(fetched["url"], citation["canonical_govuk_url"])
        self.assertTrue(citation["derived_non_authoritative"])
        attachment = next(
            row
            for path in self.index.manifest["chunks"]["resources"]
            for row in read_gzip_json(self.bundle / path)
        )
        self.assertEqual(attachment["open"], self.index.fetch(attachment["id"])["open"])

    def test_no_result_and_unknown_identifier_are_explicit(self) -> None:
        self.assertEqual("no_supported_result", self.index.search("zzzzzzzzzzzzzz")["answerability"])
        with self.assertRaises(DiscoveryError):
            self.index.fetch("dataset/not-present")
        with self.assertRaises(DiscoveryError):
            self.index.search("passport", limit=0)
        with self.assertRaises(DiscoveryError):
            self.index.traverse("dataset/not-present", limit=-1)

    def test_shared_native_identifiers_require_an_entity_kind(self) -> None:
        content_id = "00000000-0000-4000-8000-000000000004"
        with self.assertRaisesRegex(DiscoveryError, "ambiguous content identifier"):
            self.index.fetch(content_id)
        dataset = self.index.fetch(content_id, kind="dataset")
        publisher = self.index.fetch(content_id, kind="publisher")
        self.assertTrue(dataset["open"].startswith("dataset/"))
        self.assertTrue(publisher["open"].startswith("publisher/"))

        url = "https://www.gov.uk/government/organisations/department-for-work-pensions"
        with self.assertRaisesRegex(DiscoveryError, "ambiguous content identifier"):
            self.index.fetch(url)
        self.assertEqual(dataset["open"], self.index.fetch(url, kind="datasets")["open"])
        self.assertEqual(publisher["open"], self.index.fetch(url, kind="publishers")["open"])
        with self.assertRaisesRegex(DiscoveryError, "conflicts with requested kind"):
            self.index.fetch(dataset["open"], kind="publisher")

    def test_route_index_paths_cannot_escape_the_bundle(self) -> None:
        descriptor_path = self.bundle / "okf-explorer.json"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor["entrypoints"]["route_index"] = "../outside.json"
        descriptor["entrypoint_integrity"]["route_index"]["path"] = "../outside.json"
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
        with self.assertRaisesRegex(DiscoveryError, "unsafe bundle path"):
            DiscoveryIndex(self.bundle)

    def test_integrity_bearing_search_entrypoint_is_verified(self) -> None:
        descriptor_path = self.bundle / "okf-explorer.json"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor["entrypoint_integrity"]["search_manifest"]["sha256"] = "0" * 64
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
        with self.assertRaisesRegex(DiscoveryError, "SHA-256 differs"):
            DiscoveryIndex(self.bundle)

    def rewrite_search_manifest(self, search: dict[str, object]) -> None:
        search_path = self.bundle / "data/search/manifest.json"
        search_path.write_text(
            json.dumps(search, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        descriptor_path = self.bundle / "okf-explorer.json"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor["entrypoint_integrity"]["search_manifest"]["sha256"] = hashlib.sha256(
            search_path.read_bytes()
        ).hexdigest()
        descriptor_path.write_text(
            json.dumps(descriptor, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_legacy_singleton_search_manifest_remains_discoverable(self) -> None:
        search_path = self.bundle / "data/search/manifest.json"
        search = json.loads(search_path.read_text(encoding="utf-8"))
        search.pop("postings_partitioning")
        search.pop("doc_map_partitioning")
        search["entrypoints"]["doc_map"] = search["entrypoints"]["doc_map"][0]
        self.rewrite_search_manifest(search)
        legacy = DiscoveryIndex(self.bundle)
        self.assertTrue(legacy.search("universal credit")["results"])

    def test_declared_partition_contract_drift_fails_closed(self) -> None:
        search_path = self.bundle / "data/search/manifest.json"
        search = json.loads(search_path.read_text(encoding="utf-8"))
        search["postings_partitioning"]["token_atomic"] = False
        self.rewrite_search_manifest(search)
        with self.assertRaisesRegex(DiscoveryError, "partitioning contract"):
            DiscoveryIndex(self.bundle)

        for field, value in (
            ("postings_partitioning", None),
            ("doc_map_partitioning", None),
        ):
            with self.subTest(field=field, value=value):
                build_publication(
                    load_jsonl(
                        ROOT / "tests" / "fixtures" / "corpus" / "source-records.jsonl"
                    ),
                    self.bundle,
                    "2026-07-11T23:30:00Z",
                    "fixture-2026-07-11",
                )
                search = json.loads(search_path.read_text(encoding="utf-8"))
                search[field] = value
                self.rewrite_search_manifest(search)
                with self.assertRaisesRegex(DiscoveryError, "partitioning contract"):
                    DiscoveryIndex(self.bundle)

        build_publication(
            load_jsonl(ROOT / "tests" / "fixtures" / "corpus" / "source-records.jsonl"),
            self.bundle,
            "2026-07-11T23:30:00Z",
            "fixture-2026-07-11",
        )
        search = json.loads(search_path.read_text(encoding="utf-8"))
        search["postings_partitioning"]["token_atomic"] = 1
        self.rewrite_search_manifest(search)
        with self.assertRaisesRegex(DiscoveryError, "partitioning contract"):
            DiscoveryIndex(self.bundle)

        build_publication(
            load_jsonl(ROOT / "tests" / "fixtures" / "corpus" / "source-records.jsonl"),
            self.bundle,
            "2026-07-11T23:30:00Z",
            "fixture-2026-07-11",
        )
        search = json.loads(search_path.read_text(encoding="utf-8"))
        search["lexicon_shard_length"] = 3
        self.rewrite_search_manifest(search)
        with self.assertRaisesRegex(DiscoveryError, "logical lexicon width"):
            DiscoveryIndex(self.bundle)


if __name__ == "__main__":
    unittest.main()
