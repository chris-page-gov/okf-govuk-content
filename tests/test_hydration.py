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

from govuk_okf.hydration import CorpusHydrator, HydrationError, read_source_records  # noqa: E402


class HydrationTests(unittest.TestCase):
    def source(self, root: Path) -> Path:
        launch = root / "governance" / "launch-manifest.yaml"
        launch.parent.mkdir(parents=True, exist_ok=True)
        launch.write_text("ceilings:\n  retained_metadata_storage_gib: 1\n", encoding="utf-8")
        path = root / "corpus" / "inventory" / "T0-source-records.jsonl.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "candidate_key": "route-root",
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
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        return path

    @staticmethod
    def observation(url: str, **_: object):
        path = url.removeprefix("https://www.gov.uk/api/content") or "/"
        if path == "/":
            payload = {
                "content_id": "root-id",
                "base_path": "/",
                "title": "GOV.UK",
                "document_type": "homepage",
                "schema_name": "homepage",
                "locale": "en",
                "links": {
                    "child_taxons": [
                        {
                            "content_id": "child-id",
                            "base_path": "/child",
                            "title": "Child",
                            "document_type": "taxon",
                            "schema_name": "taxon",
                            "locale": "en",
                        }
                    ]
                },
                "details": {"body": "must not survive"},
            }
        else:
            payload = {
                "content_id": "child-id",
                "base_path": "/child",
                "title": "Child",
                "document_type": "taxon",
                "schema_name": "taxon",
                "locale": "en",
                "links": {},
                "details": {
                    "body": "must not survive",
                    "attachments": [
                        {
                            "id": "attachment-id",
                            "title": "Attachment",
                            "url": "https://assets.publishing.service.gov.uk/file.pdf",
                            "content_type": "application/pdf",
                        }
                    ],
                },
            }
        body = json.dumps(payload).encode()
        return body, {
            "ok": True,
            "status": 200,
            "partial": False,
            "requested_url": url,
            "final_url": url,
            "retrieved_at": "2026-07-12T00:00:00Z",
            "sha256": "a" * 64,
        }

    def test_recursive_hydration_is_resumable_metadata_only_and_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hydrator = CorpusHydrator(root, "T0", self.source(root), requests_per_second=100000, workers=2)
            with patch("govuk_okf.hydration.request_observation", side_effect=self.observation):
                progress = hydrator.run(request_limit=1)
            self.assertFalse(progress["closed"])
            checkpoint = root / "corpus/cache/T0/hydration/checkpoint.sqlite"
            with sqlite3.connect(checkpoint) as connection:
                rows = connection.execute(
                    "SELECT state, input_json IS NULL, record_json IS NULL FROM queue ORDER BY url"
                ).fetchall()
            self.assertEqual(
                [("complete", 1, 0), ("pending", 0, 1)],
                rows,
            )

            resumed = CorpusHydrator(root, "T0", self.source(root), requests_per_second=100000, workers=2)
            with patch("govuk_okf.hydration.request_observation", side_effect=self.observation):
                progress = resumed.run()
            self.assertTrue(progress["closed"])
            self.assertEqual(2, progress["queue_records"])
            self.assertEqual(1, progress["processed_this_run"])
            reconciliation = resumed.export()
            self.assertEqual(0, reconciliation["unexplained_omissions"])
            self.assertEqual(2, reconciliation["publication_records"])
            self.assertEqual(1, reconciliation["entity_class_counts"]["resource"])
            rows = list(read_source_records(root / reconciliation["hydrated_records_path"]))
            self.assertEqual(
                ["https://www.gov.uk/", "https://www.gov.uk/child"],
                [row["canonical_url"] for row in rows],
            )
            self.assertTrue(all("body" not in row.get("details", {}) for row in rows))
            self.assertTrue(reconciliation["storage_accounting"]["within_authorised_ceiling"])
            with sqlite3.connect(checkpoint) as connection:
                self.assertEqual(
                    [("complete", 1), ("complete", 1)],
                    connection.execute(
                        "SELECT state, input_json IS NULL FROM queue ORDER BY url"
                    ).fetchall(),
                )
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])

    def test_bounded_run_cannot_export_an_open_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hydrator = CorpusHydrator(root, "T0", self.source(root), requests_per_second=100000, workers=1)
            with patch("govuk_okf.hydration.request_observation", side_effect=self.observation):
                progress = hydrator.run(request_limit=1)
            self.assertFalse(progress["closed"])
            with self.assertRaises(HydrationError):
                hydrator.export()

    def test_legacy_queue_migrates_and_clears_only_completed_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.source(root)
            database = root / "corpus/cache/T0/hydration/checkpoint.sqlite"
            database.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE queue (
                        url TEXT NOT NULL,
                        locale TEXT NOT NULL,
                        input_json TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('pending', 'complete')),
                        hydration_status TEXT,
                        record_json TEXT,
                        PRIMARY KEY (url, locale)
                    );
                    CREATE INDEX queue_state_url ON queue(state, url, locale);
                    """
                )
                connection.execute(
                    "INSERT INTO queue VALUES (?, 'en', '{}', 'complete', 'represented', '{}')",
                    ("https://www.gov.uk/complete",),
                )
                connection.execute(
                    "INSERT INTO queue VALUES (?, 'en', '{}', 'pending', NULL, NULL)",
                    ("https://www.gov.uk/pending",),
                )

            hydrator = CorpusHydrator(root, "T0", source, requests_per_second=100000)
            connection = hydrator._connect()
            try:
                input_column = next(
                    row for row in connection.execute("PRAGMA table_info(queue)") if row[1] == "input_json"
                )
                self.assertEqual(0, input_column[3])
                self.assertEqual(
                    [("complete", 1, 0), ("pending", 0, 1)],
                    connection.execute(
                        "SELECT state, input_json IS NULL, record_json IS NULL FROM queue ORDER BY url"
                    ).fetchall(),
                )
            finally:
                connection.close()

    def test_storage_ceiling_stops_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hydrator = CorpusHydrator(
                root,
                "T0",
                self.source(root),
                requests_per_second=100000,
                retained_storage_bytes=1024,
            )
            with patch("govuk_okf.hydration.request_observation") as request:
                with self.assertRaisesRegex(HydrationError, "retained metadata storage"):
                    hydrator.run()
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
