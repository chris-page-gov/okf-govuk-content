from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SourcePreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads((ROOT / "research" / "source-preflight.json").read_text(encoding="utf-8"))

    def test_all_official_probes_pass_and_plan_urls_are_accounted(self) -> None:
        summary = self.document["summary"]
        self.assertEqual(32, summary["official_total"])
        self.assertEqual(0, summary["official_failed"])
        self.assertEqual(93, summary["plan_total"])
        self.assertEqual(1, summary["plan_failed"])

    def test_live_denominators_are_retained(self) -> None:
        by_id = {item["id"]: item for item in self.document["official_sources"]}
        self.assertEqual(715465, by_id["search-api-root-count"]["facts"]["reported_total"])
        self.assertEqual(35, by_id["sitemap-index"]["facts"]["declared_locations"])
        self.assertEqual(1256, by_id["organisations-api"]["facts"]["total"])
        self.assertEqual(20, by_id["content-api-root"]["facts"]["link_counts"]["level_one_taxons"])
        self.assertEqual(233, by_id["world-taxonomy-root"]["facts"]["link_counts"]["child_taxons"])
        self.assertEqual(16, by_id["mainstream-browse-root"]["facts"]["link_counts"]["top_level_browse_pages"])

    def test_every_probe_has_hash_and_timestamp(self) -> None:
        for item in [*self.document["official_sources"], *self.document["plan_sources"]]:
            self.assertEqual(64, len(item["sha256"]))
            self.assertIn("+00:00", item["retrieved_at"])

    def test_legacy_tls_failure_is_explicit(self) -> None:
        failures = [item for item in self.document["plan_sources"] if not item["ok"]]
        self.assertEqual(1, len(failures))
        self.assertIn("DH_KEY_TOO_SMALL", failures[0]["error"])


if __name__ == "__main__":
    unittest.main()
