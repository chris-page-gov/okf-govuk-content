from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.acquisition import (
    AcquisitionError,
    HostLimiter,
    build_candidate_ledger,
    candidate_key,
    dedupe_pending_paths,
    expand_candidate_records,
    merge_records,
    normalise_url,
    parse_sitemap,
    sanitise_content_api,
    search_partition_value,
    search_result_record,
    search_source_identity,
    write_jsonl_gzip_shards,
)


class AcquisitionTests(unittest.TestCase):
    def test_navigation_queue_is_stably_deduplicated_against_visited_paths(self) -> None:
        queue = dedupe_pending_paths(["/a", "/b", "/a", "", "/b"], {"/b"})
        self.assertEqual(["/a", ""], list(queue))
        with self.assertRaises(AcquisitionError):
            dedupe_pending_paths(["https://example.test/path"], set())

    def test_search_partition_value_accepts_scalar_and_documented_slug_object(self) -> None:
        self.assertEqual("guidance", search_partition_value({"value": "guidance"}))
        self.assertEqual("aaib_report", search_partition_value({"value": {"slug": "aaib_report"}}))

    def test_search_partition_value_rejects_ambiguous_or_unsafe_shapes(self) -> None:
        for option in (
            {},
            {"value": None},
            {"value": {"slug": "guidance", "title": "Guidance"}},
            {"value": {"title": "Guidance"}},
            {"value": "guidance&count=0"},
        ):
            with self.subTest(option=option), self.assertRaises(AcquisitionError):
                search_partition_value(option)

    def test_sitemap_index_and_urlset_parsing(self) -> None:
        index = b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://www.gov.uk/sitemaps/sitemap_1.xml</loc><lastmod>2026-07-11</lastmod></sitemap></sitemapindex>'
        urls = b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://www.gov.uk/example</loc><lastmod>2026-07-10</lastmod></url></urlset>'
        self.assertEqual("https://www.gov.uk/sitemaps/sitemap_1.xml", parse_sitemap(index)[0]["url"])
        self.assertEqual("https://www.gov.uk/example", parse_sitemap(urls)[0]["url"])

    def test_url_and_candidate_canonicalisation(self) -> None:
        self.assertEqual("https://www.gov.uk/example", normalise_url("/example"))
        self.assertEqual("https://www.gov.uk/", normalise_url("https://gov.uk/"))
        self.assertEqual(
            "https://www.gov.uk/example?view=full",
            normalise_url("https://GOV.UK/example?token=secret&utm_source=x&view=full#fragment"),
        )
        self.assertEqual(
            "https://www.gov.uk/example?view=full",
            normalise_url("/example?token=secret&view=full#fragment"),
        )
        with self.assertRaises(AcquisitionError):
            normalise_url("https://user:password@www.gov.uk/example")
        with self.assertRaises(AcquisitionError):
            normalise_url("//evil.example/path")
        self.assertEqual(candidate_key("https://www.gov.uk/example"), candidate_key("https://www.gov.uk/example"))

    def test_search_metadata_wins_over_sitemap_placeholder(self) -> None:
        sitemap = {
            "candidate_key": "x",
            "canonical_url": "https://www.gov.uk/example",
            "document_type": "sitemap_route",
            "title": "Example",
            "source_memberships": ["sitemap"],
        }
        search = search_result_record(
            {"_id": "/example", "link": "/example", "title": "Better title", "description": "Summary", "content_store_document_type": "guidance"},
            "search-ascending",
            "2026-07-11T00:00:00+00:00",
        )
        merged = merge_records(sitemap, search)
        self.assertEqual("guidance", merged["document_type"])
        self.assertEqual(["search-ascending", "sitemap"], merged["source_memberships"])

    def test_search_source_rows_remain_distinct_when_canonical_route_is_shared(self) -> None:
        first = search_result_record(
            {
                "_id": "c1a9b1c4-ad96-4c2c-ad5f-709881c6c1ce",
                "link": "https://www.southnorfolkandbroadland.gov.uk/",
                "title": "Broadland District Council",
                "content_store_document_type": "external_content",
            },
            "search-ascending",
            "2026-07-12T00:00:00Z",
        )
        second = search_result_record(
            {
                "_id": "9611a90a-4fbe-43e4-bf5d-8badc63fda5c",
                "link": "https://www.southnorfolkandbroadland.gov.uk/",
                "title": "South Norfolk District Council",
                "content_store_document_type": "external_content",
            },
            "search-descending",
            "2026-07-12T00:00:01Z",
        )
        self.assertNotEqual(first["search_index_id"], second["search_index_id"])
        self.assertEqual(first["canonical_url"], second["canonical_url"])
        self.assertEqual(2, len(merge_records(first, second)["search_index_ids"]))

    def test_search_source_identity_fails_closed_when_missing(self) -> None:
        with self.assertRaises(AcquisitionError):
            search_source_identity({"link": "/example"})

    def test_content_api_allowlist_drops_body_fields_and_keeps_attachment_metadata(self) -> None:
        record = sanitise_content_api(
            {
                "content_id": "id",
                "base_path": "/example",
                "title": "Example",
                "description": "Summary",
                "document_type": "guidance",
                "schema_name": "publication",
                "locale": "en",
                "details": {"body": "must not persist", "attachments": [{"id": "a", "title": "A", "url": "https://assets.publishing.service.gov.uk/a.pdf", "content_type": "application/pdf"}]},
                "links": {},
            },
            "2026-07-11T00:00:00+00:00",
        )
        self.assertNotIn("body", record["details"])
        self.assertEqual("a", record["details"]["attachments"][0]["id"])

    def test_candidate_accounting_keeps_native_entity_classes_disjoint(self) -> None:
        record = sanitise_content_api(
            {
                "content_id": "content-id",
                "base_path": "/example",
                "title": "Example",
                "document_type": "publication",
                "schema_name": "publication",
                "locale": "en",
                "public_updated_at": "2026-07-12T00:00:00Z",
                "details": {
                    "attachments": [
                        {
                            "id": "attachment-id",
                            "title": "Attachment",
                            "url": "https://assets.publishing.service.gov.uk/a.pdf",
                        }
                    ]
                },
                "links": {},
            },
            "2026-07-12T00:00:00Z",
        )
        candidates = expand_candidate_records(record, "T0")
        self.assertEqual(
            {"content_identity", "document", "edition", "route", "resource"},
            {candidate["entity_class"] for candidate in candidates},
        )
        ledger = build_candidate_ledger([record], "T0")
        self.assertEqual(5, len(ledger))
        self.assertTrue(all(candidate["snapshot_id"] == "T0" for candidate in ledger.values()))

    def test_large_jsonl_outputs_are_content_addressed_bounded_shards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            records = [{"id": index, "title": "record " + str(index)} for index in range(5)]
            first = write_jsonl_gzip_shards(Path(directory), "records", records, max_records=2)
            second = write_jsonl_gzip_shards(Path(directory), "records", records, max_records=2)
            self.assertEqual(first["root"], second["root"])
            self.assertEqual([2, 2, 1], [row["records"] for row in first["shards"]])
            self.assertTrue(all(row["bytes"] < 50 * 1024 * 1024 for row in first["shards"]))

    def test_shard_writer_admits_each_file_before_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls: list[tuple[str, int]] = []

            def before_write(path: Path, pending_bytes: int) -> None:
                self.assertFalse(path.exists())
                self.assertGreater(pending_bytes, 0)
                calls.append((path.name, pending_bytes))

            write_jsonl_gzip_shards(
                Path(directory),
                "records",
                [{"id": "a"}, {"id": "b"}],
                max_records=1,
                before_write=before_write,
            )
            self.assertEqual(
                ["part-00000.jsonl.gz", "part-00001.jsonl.gz", "index.json"],
                [name for name, _pending_bytes in calls],
            )

    def test_shared_request_budget_fails_closed_before_exceeding_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "official.count"
            limiter = HostLimiter(1_000_000, budget_path=ledger, max_requests=2)
            limiter.wait()
            limiter.wait()
            with self.assertRaisesRegex(AcquisitionError, "request ceiling exhausted"):
                limiter.wait()
            self.assertEqual("2", ledger.read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    unittest.main()
