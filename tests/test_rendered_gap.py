from __future__ import annotations

import sys
import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.rendered_gap import parse_robots, rendered_observation  # noqa: E402
from govuk_okf.closure_hydration import CompleteCorpusHydrator  # noqa: E402
from govuk_okf.hydration import HydrationError  # noqa: E402


class RenderedGapTests(unittest.TestCase):
    @staticmethod
    def authorise_storage(root: Path) -> None:
        launch = root / "governance" / "launch-manifest.yaml"
        launch.parent.mkdir(parents=True, exist_ok=True)
        launch.write_text("ceilings:\n  minimum_free_disk_gib: 1\n", encoding="utf-8")

    def policy(self):
        return parse_robots(
            b"User-agent: *\nDisallow: /search*\nDisallow: /blocked\nAllow: /blocked/allowed\n",
            {
                "requested_url": "https://www.gov.uk/robots.txt",
                "retrieved_at": "2026-07-12T00:00:00Z",
                "sha256": "b" * 64,
            },
        )

    def test_robots_longest_rule_and_wildcard_are_enforced(self) -> None:
        policy = self.policy()
        self.assertFalse(policy.allows("https://www.gov.uk/search/all?q=tax"))
        self.assertFalse(policy.allows("https://www.gov.uk/blocked"))
        self.assertTrue(policy.allows("https://www.gov.uk/blocked/allowed"))
        self.assertTrue(policy.allows("https://www.gov.uk/browse"))

    def test_transient_parser_retains_only_typed_targets_and_hash_metadata(self) -> None:
        html = b"""<!doctype html><html lang='cy'><head>
        <link rel='canonical' href='/canonical'>
        <script type='application/ld+json'>{"@type":"GovernmentService","url":"/schema-target","articleBody":"discard"}</script>
        </head><body>
        <a href='/child?utm_source=x'>Child</a>
        <a href='/search/all?q=secret'>Search</a>
        <a href='https://assets.publishing.service.gov.uk/a.pdf'>PDF</a>
        <a href='https://service.example.gov.uk/start?token=secret'>Service</a>
        <script>alert('not executed')</script></body></html>"""
        metadata, records = rendered_observation(
            {"canonical_url": "https://www.gov.uk/parent", "locale": "en"},
            html,
            {
                "requested_url": "https://www.gov.uk/parent",
                "final_url": "https://www.gov.uk/parent",
                "retrieved_at": "2026-07-12T00:00:00Z",
                "sha256": "c" * 64,
            },
            self.policy(),
        )
        by_url = {record["canonical_url"]: record for record in records}
        self.assertIn("https://www.gov.uk/child", by_url)
        self.assertIn("https://www.gov.uk/schema-target", by_url)
        self.assertIn("https://assets.publishing.service.gov.uk/a.pdf", by_url)
        self.assertIn("https://service.example.gov.uk/start", by_url)
        self.assertNotIn("https://www.gov.uk/search/all", by_url)
        self.assertEqual("resource", by_url["https://assets.publishing.service.gov.uk/a.pdf"]["entity_class"])
        self.assertEqual("external_boundary", by_url["https://service.example.gov.uk/start"]["entity_class"])
        self.assertEqual(["GovernmentService"], metadata["schema_org_types"])
        self.assertEqual(0, metadata["retained_body_bytes"])
        self.assertEqual("https://www.gov.uk/canonical", metadata["canonical_url"])

    def test_complete_hydrator_closes_rendered_links_without_body_retention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.authorise_storage(root)
            source = root / "corpus/inventory/T0-source-records.jsonl.gz"
            source.parent.mkdir(parents=True)
            with gzip.open(source, "wt", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "candidate_key": "root",
                            "entity_class": "route",
                            "source_native_id": "https://www.gov.uk/",
                            "source_id": "govuk-sitemap",
                            "source_memberships": ["sitemap"],
                            "coverage_disposition": "represented",
                            "canonical_url": "https://www.gov.uk/",
                            "base_path": "/",
                            "title": "GOV.UK",
                            "document_type": "homepage",
                            "schema_name": "homepage",
                            "locale": "en",
                            "links": {},
                        }
                    )
                    + "\n"
                )

            def observation(url: str, **_: object):
                if url.endswith("/robots.txt"):
                    body = b"User-agent: *\nDisallow: /search\n"
                    content_type = "text/plain"
                elif "/api/content" in url:
                    path = url.split("/api/content", 1)[1] or "/"
                    body = json.dumps(
                        {
                            "content_id": "root-id" if path == "/" else "child-id",
                            "base_path": path,
                            "title": "Root" if path == "/" else "Child",
                            "document_type": "homepage" if path == "/" else "guidance",
                            "schema_name": "homepage" if path == "/" else "publication",
                            "locale": "en",
                            "links": {},
                            "details": {"body": "discard"},
                        }
                    ).encode()
                    content_type = "application/json"
                elif url == "https://www.gov.uk/":
                    body = b"<html><body><a href='/child'>Child</a><a href='https://outside.example/start'>Outside</a></body></html>"
                    content_type = "text/html"
                else:
                    body = b"<html><body><p>Child page</p></body></html>"
                    content_type = "text/html"
                return body, {
                    "ok": True,
                    "status": 200,
                    "partial": False,
                    "requested_url": url,
                    "final_url": url,
                    "retrieved_at": "2026-07-12T00:00:00Z",
                    "sha256": "d" * 64,
                    "headers": {"content-type": content_type},
                }

            hydrator = CompleteCorpusHydrator(
                root,
                "T0",
                source,
                requests_per_second=100000,
                workers=2,
                max_queue_records=10,
                minimum_free_disk_bytes=1024**3,
            )
            with patch("govuk_okf.closure_hydration.request_observation", side_effect=observation), patch(
                "govuk_okf.hydration.request_observation", side_effect=observation
            ):
                progress = hydrator.run()
            self.assertTrue(progress["closed"])
            self.assertEqual(3, progress["queue_records"])
            reconciliation = hydrator.export()
            self.assertTrue(reconciliation["rendered_gap_proof"]["closed"])
            self.assertEqual(0, reconciliation["rendered_gap_proof"]["retained_body_bytes"])
            self.assertEqual(2, reconciliation["source_counts"]["rendered_links"])

    def test_rendered_gap_detector_publishes_bounded_sample_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.authorise_storage(root)
            source = root / "corpus/inventory/T0-source-records.jsonl.gz"
            source.parent.mkdir(parents=True)
            with gzip.open(source, "wt", encoding="utf-8") as stream:
                for path in ("/alpha", "/beta"):
                    stream.write(
                        json.dumps(
                            {
                                "candidate_key": path,
                                "entity_class": "route",
                                "source_native_id": f"https://www.gov.uk{path}",
                                "source_id": "govuk-sitemap",
                                "source_memberships": ["sitemap"],
                                "coverage_disposition": "represented",
                                "canonical_url": f"https://www.gov.uk{path}",
                                "base_path": path,
                                "title": path[1:].title(),
                                "document_type": "guidance",
                                "schema_name": "publication",
                                "locale": "en",
                                "links": {},
                            }
                        )
                        + "\n"
                    )

            def observation(url: str, **_: object):
                if url.endswith("/robots.txt"):
                    body, content_type = b"User-agent: *\n", "text/plain"
                elif "/api/content" in url:
                    path = url.split("/api/content", 1)[1]
                    body = json.dumps(
                        {
                            "content_id": path[1:] + "-id",
                            "base_path": path,
                            "title": path[1:].title(),
                            "document_type": "guidance",
                            "schema_name": "publication",
                            "locale": "en",
                            "links": {},
                        }
                    ).encode()
                    content_type = "application/json"
                else:
                    body, content_type = b"<html><body></body></html>", "text/html"
                return body, {
                    "ok": True,
                    "status": 200,
                    "partial": False,
                    "requested_url": url,
                    "final_url": url,
                    "retrieved_at": "2026-07-12T00:00:00Z",
                    "sha256": "e" * 64,
                    "headers": {"content-type": content_type},
                    "acquisition_attempt": 1,
                }

            hydrator = CompleteCorpusHydrator(
                root,
                "T0",
                source,
                requests_per_second=100000,
                rendered_requests_per_second=100000,
                workers=2,
                max_queue_records=10,
                max_rendered_requests=1,
                minimum_free_disk_bytes=1024**3,
            )
            with patch("govuk_okf.closure_hydration.request_observation", side_effect=observation), patch(
                "govuk_okf.hydration.request_observation", side_effect=observation
            ):
                self.assertTrue(hydrator.run()["closed"])
            proof = hydrator.export()["rendered_gap_proof"]
            self.assertEqual(2, proof["eligible_population"])
            self.assertEqual(1, proof["selected_records"])
            self.assertEqual(1, proof["unsampled_records"])
            self.assertEqual(1, proof["status_counts"]["not_selected_by_bounded_detector"])
            self.assertEqual(1_000_000, proof["request_accounting"]["programme_ceiling"])

    def test_rendered_proof_failure_rolls_back_base_export_and_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.authorise_storage(root)
            source = root / "corpus/inventory/T0-source-records.jsonl.gz"
            source.parent.mkdir(parents=True)
            with gzip.open(source, "wt", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "candidate_key": "root",
                            "entity_class": "route",
                            "source_native_id": "https://www.gov.uk/",
                            "source_id": "govuk-sitemap",
                            "source_memberships": ["sitemap"],
                            "coverage_disposition": "represented",
                            "canonical_url": "https://www.gov.uk/",
                            "base_path": "/",
                            "title": "GOV.UK",
                            "document_type": "homepage",
                            "schema_name": "homepage",
                            "locale": "en",
                            "links": {},
                        }
                    )
                    + "\n"
                )

            def observation(url: str, **_: object):
                if url.endswith("/robots.txt"):
                    body, content_type = b"User-agent: *\n", "text/plain"
                elif "/api/content" in url:
                    body = json.dumps(
                        {
                            "content_id": "root-id",
                            "base_path": "/",
                            "title": "GOV.UK",
                            "document_type": "homepage",
                            "schema_name": "homepage",
                            "locale": "en",
                            "links": {},
                        }
                    ).encode()
                    content_type = "application/json"
                else:
                    body, content_type = b"<html><body></body></html>", "text/html"
                return body, {
                    "ok": True,
                    "status": 200,
                    "partial": False,
                    "requested_url": url,
                    "final_url": url,
                    "retrieved_at": "2026-07-13T00:00:00Z",
                    "sha256": "f" * 64,
                    "headers": {"content-type": content_type},
                    "acquisition_attempt": 1,
                }

            hydrator = CompleteCorpusHydrator(
                root,
                "T0",
                source,
                requests_per_second=100000,
                rendered_requests_per_second=100000,
                minimum_free_disk_bytes=1024**3,
            )
            with patch("govuk_okf.closure_hydration.request_observation", side_effect=observation), patch(
                "govuk_okf.hydration.request_observation", side_effect=observation
            ):
                self.assertTrue(hydrator.run()["closed"])
            with sqlite3.connect(hydrator.database_path) as connection:
                row = connection.execute("SELECT url, locale, record_json FROM queue").fetchone()
                record = json.loads(row[2])
                record["rendered_observation"]["retained_body_bytes"] = 1
                connection.execute(
                    "UPDATE queue SET record_json=? WHERE url=? AND locale=?",
                    (json.dumps(record, sort_keys=True), row[0], row[1]),
                )

            old_manifest = "previous manifest\n"
            old_reconciliation = "previous reconciliation\n"
            hydrator.records_root.mkdir(parents=True, exist_ok=True)
            hydrator.reconciliation_root.mkdir(parents=True, exist_ok=True)
            manifest_path = hydrator.records_root / "manifest.json"
            reconciliation_path = hydrator.reconciliation_root / "T0-hydrated.json"
            manifest_path.write_text(old_manifest, encoding="utf-8")
            reconciliation_path.write_text(old_reconciliation, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "retained body bytes"):
                hydrator.export()
            self.assertEqual(old_manifest, manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                old_reconciliation,
                reconciliation_path.read_text(encoding="utf-8"),
            )
            self.assertFalse(any(hydrator.records_root.glob("source-records-*")))
            self.assertFalse(
                any((hydrator.inventory_root / hydrator.label).glob("hydrated-candidates-*"))
            )
            with sqlite3.connect(hydrator.database_path) as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])

    def test_minimum_free_disk_stops_before_robots_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.authorise_storage(root)
            source = root / "corpus/inventory/T0-source-records.jsonl.gz"
            source.parent.mkdir(parents=True)
            with gzip.open(source, "wt", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "candidate_key": "root",
                            "entity_class": "route",
                            "source_native_id": "https://www.gov.uk/",
                            "source_id": "govuk-sitemap",
                            "source_memberships": ["sitemap"],
                            "coverage_disposition": "represented",
                            "canonical_url": "https://www.gov.uk/",
                            "base_path": "/",
                            "title": "GOV.UK",
                            "document_type": "homepage",
                            "schema_name": "homepage",
                            "locale": "en",
                            "links": {},
                        }
                    )
                    + "\n"
                )
            hydrator = CompleteCorpusHydrator(
                root,
                "T0",
                source,
                requests_per_second=100000,
                minimum_free_disk_bytes=1,
            )
            with patch("govuk_okf.storage.free_disk_bytes", return_value=0), patch(
                "govuk_okf.closure_hydration.request_observation"
            ) as request:
                with self.assertRaisesRegex(HydrationError, "insufficient free disk"):
                    hydrator.run()
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
