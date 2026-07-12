from __future__ import annotations

import gzip
import json
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
        path = root / "corpus" / "inventory" / "T0-source-records.jsonl.gz"
        path.parent.mkdir(parents=True)
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
                progress = hydrator.run()
            self.assertTrue(progress["closed"])
            self.assertEqual(2, progress["queue_records"])
            reconciliation = hydrator.export()
            self.assertEqual(0, reconciliation["unexplained_omissions"])
            self.assertEqual(2, reconciliation["publication_records"])
            self.assertEqual(1, reconciliation["entity_class_counts"]["resource"])
            rows = list(read_source_records(root / reconciliation["hydrated_records_path"]))
            self.assertEqual(["https://www.gov.uk/", "https://www.gov.uk/child"], [row["canonical_url"] for row in rows])
            self.assertTrue(all("body" not in row.get("details", {}) for row in rows))

    def test_bounded_run_cannot_export_an_open_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hydrator = CorpusHydrator(root, "T0", self.source(root), requests_per_second=100000, workers=1)
            with patch("govuk_okf.hydration.request_observation", side_effect=self.observation):
                progress = hydrator.run(request_limit=1)
            self.assertFalse(progress["closed"])
            with self.assertRaises(HydrationError):
                hydrator.export()


if __name__ == "__main__":
    unittest.main()
