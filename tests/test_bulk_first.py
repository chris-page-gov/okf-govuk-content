from __future__ import annotations

import gzip
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.acquisition import AcquisitionError, SnapshotBuilder, search_result_record  # noqa: E402
from govuk_okf.hydration import CorpusHydrator  # noqa: E402
from govuk_okf.hydration_policy import (  # noqa: E402
    POLICY_VERSION,
    apply_bulk_metadata_disposition,
    hydration_decision,
    selection_manifest,
)
from govuk_okf.search_extracts import SearchExtractStore, query_extract_database  # noqa: E402
from govuk_okf.storage import StoragePolicyError, load_storage_policy  # noqa: E402


class BulkFirstTests(unittest.TestCase):
    @staticmethod
    def launch(root: Path, *, external: bool = False, required: bool = False) -> None:
        path = root / "governance" / "launch-manifest.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                (
                    "schema_version: 1",
                    f"external_storage_permission: {'true' if external else 'false'}",
                    f"external_cache_required_for_body_extracts: {'true' if required else 'false'}",
                    "external_cache_relative_path: okf-govuk-content",
                    "ceilings:",
                    "  minimum_free_disk_gib: 1",
                    "",
                )
            ),
            encoding="utf-8",
        )

    @staticmethod
    def bulk_record(content_id: str = "content-id", document_type: str = "news_story") -> dict[str, object]:
        return {
            "candidate_key": f"route-{content_id}",
            "entity_class": "route",
            "source_native_id": f"https://www.gov.uk/{content_id}",
            "source_id": "search-api-v1",
            "source_memberships": ["search-news_story-ascending"],
            "coverage_disposition": "represented",
            "content_id": content_id,
            "canonical_url": f"https://www.gov.uk/{content_id}",
            "base_path": f"/{content_id}",
            "title": "Bulk record",
            "description": "Search metadata",
            "document_type": document_type,
            "schema_name": document_type,
            "locale": "en",
            "links": {},
        }

    def non_audit_record(self) -> dict[str, object]:
        for index in range(1000):
            record = self.bulk_record(f"content-{index}")
            if not hydration_decision(record).selected:
                return record
        self.fail("could not construct a stable non-audit record")

    def test_external_volume_detection_uses_actual_extssd_data_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            self.launch(root, external=True, required=True)
            volumes = Path(directory) / "Volumes"
            volume = volumes / "ExtSSD-Data"
            volume.mkdir(parents=True)
            with patch("govuk_okf.storage.os.path.ismount", return_value=True):
                policy = load_storage_policy(root, volumes_root=volumes, environ={})
            self.assertEqual(volume, policy.external_volume)
            self.assertEqual(
                volume / "okf-govuk-content/extracts/T1/search-parts.sqlite",
                policy.extract_database("T1"),
            )
            public_preflight = policy.preflight(disclose_paths=False)
            self.assertEqual("external-cache", public_preflight["external_cache_root"])
            self.assertEqual(
                ["repository", "external-cache"],
                [check["target"] for check in public_preflight["checks"]],
            )
            self.assertNotIn(directory, json.dumps(public_preflight))

    def test_required_extract_cache_fails_when_extssd_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            self.launch(root, external=True, required=True)
            volumes = Path(directory) / "Volumes"
            volumes.mkdir()
            with self.assertRaisesRegex(StoragePolicyError, "requires an external"):
                load_storage_policy(root, volumes_root=volumes, environ={})

    def test_completed_snapshot_label_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.launch(root)
            manifest = root / "corpus/source-manifests/T1/manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(AcquisitionError, "already complete and immutable"):
                SnapshotBuilder(root, "T1")

    def test_search_extract_database_keeps_snippet_out_of_public_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "extracts/search-parts.sqlite"
            result = {
                "_id": "/universal-credit",
                "content_id": "uc-id",
                "link": "/universal-credit",
                "title": "Universal Credit",
                "content_store_document_type": "guide",
                "parts": [
                    {
                        "link": "/universal-credit/eligibility",
                        "title": "Eligibility",
                        "slug": "eligibility",
                        "body": "<p>You may be able to get <strong>Universal Credit</strong>.</p>",
                    }
                ],
            }
            evidence = {
                "requested_url": "https://www.gov.uk/api/search.json?count=1",
                "retrieved_at": "2026-07-15T00:00:00Z",
                "sha256": "a" * 64,
            }
            store = SearchExtractStore(database, "T1")
            try:
                safe = store.ingest_results([result], evidence)[0]
                self.assertNotIn("body", safe["parts"][0])
                summary = store.summary()
                self.assertEqual(1, summary["extract_rows"])
                self.assertFalse(summary["contains_complete_page_bodies"])
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                text = connection.execute("SELECT extract_text FROM search_parts").fetchone()[0]
            self.assertEqual("You may be able to get Universal Credit.", text)
            matches = query_extract_database(database, "eligibility")
            self.assertEqual("Eligibility", matches[0]["part_title"])
            self.assertEqual([], matches[0]["relations"]["organisation_content_ids"])

    def test_expanded_search_record_preserves_ids_relationships_and_part_metadata(self) -> None:
        record = search_result_record(
            {
                "_id": "/example",
                "content_id": "content-id",
                "link": "/example",
                "title": "Example",
                "content_store_document_type": "guide",
                "is_historic": False,
                "content_purpose_supergroup": "services",
                "organisations": [
                    {
                        "content_id": "organisation-id",
                        "link": "/government/organisations/example",
                        "title": "Example organisation",
                        "logo_formatted_title": "unsafe presentation field",
                    }
                ],
                "parts": [{"link": "/example/part", "title": "Part", "body": "not public"}],
            },
            "search-guide-ascending",
            "2026-07-15T00:00:00Z",
        )
        self.assertEqual("content-id", record["content_id"])
        self.assertEqual("organisation-id", record["links"]["organisations"][0]["content_id"])
        self.assertEqual([{"link": "/example/part", "title": "Part"}], record["parts"])
        self.assertNotIn("logo_formatted_title", record["links"]["organisations"][0])

    def test_selective_hydration_closes_bulk_record_without_content_api_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.launch(root)
            source = root / "corpus/inventory/source.jsonl.gz"
            source.parent.mkdir(parents=True)
            record = self.non_audit_record()
            with gzip.open(source, "wt", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")
            hydrator = CorpusHydrator(root, "T1", source, requests_per_second=100000)
            with patch("govuk_okf.hydration.request_observation") as request:
                progress = hydrator.run()
            request.assert_not_called()
            self.assertTrue(progress["closed"])
            self.assertEqual(0, progress["hydration_selection"]["selected_records"])
            with sqlite3.connect(hydrator.database_path) as connection:
                stored = json.loads(connection.execute("SELECT record_json FROM queue").fetchone()[0])
            self.assertEqual(POLICY_VERSION, stored["enrichment_policy"])
            self.assertEqual("bulk_metadata_only", stored["enrichment_status"])

    def test_selection_manifest_exposes_reasons_without_narrowing_accounting(self) -> None:
        sitemap_only = self.bulk_record("sitemap", "sitemap_route")
        sitemap_only["source_id"] = "govuk-sitemap"
        sitemap_only["source_memberships"] = ["sitemap"]
        sitemap_only.pop("content_id")
        bulk = self.non_audit_record()
        manifest = selection_manifest([sitemap_only, bulk], "T1")
        self.assertEqual(2, manifest["source_records"])
        self.assertEqual(1, manifest["selected_records"])
        self.assertEqual(1, manifest["selection_reasons"]["sitemap_only"])

    def test_attachment_and_historic_enrichment_is_explicitly_deferred(self) -> None:
        record = {
            "canonical_url": "https://www.gov.uk/example",
            "candidate_key": "example",
            "content_id": "content-id",
            "document_type": "publication",
            "is_historic": True,
            "source_memberships": ["search-publication-ascending"],
        }
        decision = hydration_decision(record)
        self.assertFalse(decision.selected)
        self.assertEqual(
            "deferred_bulk_source_or_targeted_enrichment", decision.disposition
        )
        self.assertEqual(
            ("deferred_attachments_or_lifecycle", "deferred_historic_content"),
            decision.reasons,
        )
        represented = apply_bulk_metadata_disposition(record, decision)
        self.assertEqual("deferred_enrichment", represented["hydration_status"])
        self.assertEqual(
            "bulk_metadata_only_deferred", represented["enrichment_status"]
        )


if __name__ == "__main__":
    unittest.main()
