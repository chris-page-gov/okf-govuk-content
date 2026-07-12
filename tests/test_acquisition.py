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
    expand_candidate_records,
    merge_records,
    normalise_url,
    parse_sitemap,
    sanitise_content_api,
    search_partition_value,
    search_result_record,
    write_jsonl_gzip_shards,
)


class AcquisitionTests(unittest.TestCase):
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
            {"link": "/example", "title": "Better title", "description": "Summary", "content_store_document_type": "guidance"},
            "search-ascending",
            "2026-07-11T00:00:00+00:00",
        )
        merged = merge_records(sitemap, search)
        self.assertEqual("guidance", merged["document_type"])
        self.assertEqual(["search-ascending", "sitemap"], merged["source_memberships"])

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
