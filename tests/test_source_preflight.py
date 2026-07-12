from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SourcePreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads((ROOT / "research" / "source-preflight.json").read_text(encoding="utf-8"))
        cls.plan_preflight = json.loads(
            (ROOT / "planning" / "PLAN_SOURCE_PREFLIGHT.json").read_text(encoding="utf-8")
        )

    def test_all_official_probes_pass_and_plan_urls_are_accounted(self) -> None:
        summary = self.document["summary"]
        self.assertEqual(32, summary["official_total"])
        self.assertEqual(0, summary["official_failed"])
        self.assertEqual(93, summary["plan_total"])
        self.assertEqual(93, summary["plan_ok"])
        self.assertEqual(0, summary["plan_failed"])

    def test_plan_hash_and_url_accounting_match_the_controlling_document(self) -> None:
        plan = (ROOT / "planning" / "AFHF_GOVUK_OKF_SOTA_RESEARCH_IMPLEMENTATION_PLAN.md").read_bytes()
        urls = sorted(set(re.findall(rb"\]\((https?://[^)]+)\)", plan)))
        expected_urls = [url.decode("utf-8") for url in urls]
        self.assertEqual(hashlib.sha256(plan).hexdigest(), self.plan_preflight["plan_sha256"])
        self.assertEqual(expected_urls, [source["url"] for source in self.plan_preflight["sources"]])
        self.assertEqual(expected_urls, [source["requested_url"] for source in self.document["plan_sources"]])

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

    def test_pirolli_replacement_preserves_transport_and_access_history(self) -> None:
        crossref = "https://api.crossref.org/works/10.1037%2F0033-295X.106.4.643"
        cmu = "https://act-r.psy.cmu.edu/wordpress/wp-content/uploads/2012/12/280uir-1999-05-pirolli.pdf"
        active_urls = {item["requested_url"] for item in self.document["plan_sources"]}
        self.assertIn(crossref, active_urls)
        self.assertNotIn(cmu, active_urls)

        history = self.document["plan_source_history"]
        self.assertEqual(93, history["preserved_original_result_count"])
        self.assertEqual(
            {"plan_total": 93, "plan_ok": 92, "plan_failed": 1},
            history["original_summary"],
        )
        failures = history["superseded_results"]
        self.assertEqual(1, len(failures))
        self.assertEqual(cmu, failures[0]["requested_url"])
        self.assertIn("DH_KEY_TOO_SMALL", failures[0]["error"])
        superseded_by_id = {item["id"]: item for item in failures}
        reconstructed = [
            superseded_by_id.get(item["id"], item) for item in self.document["plan_sources"]
        ]
        original_digest = hashlib.sha256(
            json.dumps(
                reconstructed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(history["original_results_sha256"], original_digest)
        researchgate = [
            item for item in history["access_restrictions"] if "researchgate.net" in item["url"]
        ]
        self.assertEqual(1, len(researchgate))
        self.assertEqual("HTTP 403 Forbidden", researchgate[0]["publication_record_result"])
        self.assertEqual("HTTP 403 Forbidden", researchgate[0]["exact_author_pdf_result"])


if __name__ == "__main__":
    unittest.main()
