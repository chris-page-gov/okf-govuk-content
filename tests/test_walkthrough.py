from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.util import read_gzip_json  # noqa: E402
from govuk_okf.walkthrough import WalkthroughError, build_walkthrough  # noqa: E402


class WalkthroughTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = ROOT / "bundle"
        self.manifest = json.loads((self.bundle / "data/manifest.json").read_text(encoding="utf-8"))
        self.datasets = [
            row
            for path in self.manifest["chunks"]["datasets"]
            for row in read_gzip_json(self.bundle / path)
        ]
        self.publishers = [
            row
            for path in self.manifest["chunks"]["publishers"]
            for row in read_gzip_json(self.bundle / path)
        ]
        self.relationships = [
            row
            for path in self.manifest["chunks"]["relationships"]
            for row in read_gzip_json(self.bundle / path)
        ]

    def test_case_replays_from_source_rows_and_emits_bound_diagrams(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            case = json.loads((ROOT / "questions/new-child-metadata-walkthrough.json").read_text(encoding="utf-8"))
            routes = [case["primary_route"], case["negative_control_route"], case["related_route"], case["publisher_route"], *case["associated_organisation_routes"]]
            for route in routes:
                source = self.bundle / "concepts" / f"{route}.md"
                target = output / "concepts" / f"{route}.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            generated = build_walkthrough(output, self.datasets, self.publishers, self.relationships, "NEW-CHILD-20260715")
            evidence = json.loads((output / "walkthroughs/new-child-metadata-evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(2, len(evidence["records"]["associated_organisations"]))
            self.assertEqual("reader", parse_qs(urlparse(evidence["reader_link"]).query)["view"][0])
            self.assertEqual(
                "Plan your Shared Parental Leave and Pay",
                parse_qs(urlparse(evidence["reader_link"]).query)["q"][0],
            )
            mobile_svg = ET.parse(output / "walkthroughs/new-child-metadata-mobile.svg").getroot()
            self.assertEqual("mobile", mobile_svg.attrib["class"])
            heading_lines = [
                line.text or ""
                for heading in mobile_svg.findall(".//{http://www.w3.org/2000/svg}text[@class='heading']")
                for line in heading
            ]
            self.assertIn("Department for Business, Energy & Industrial Strategy", [
                " ".join((line.text or "") for line in heading)
                for heading in mobile_svg.findall(".//{http://www.w3.org/2000/svg}text[@class='heading']")
            ])
            self.assertTrue(all(len(line) <= 36 for line in heading_lines))
            for item in generated.values():
                self.assertTrue((output / item["path"]).is_file())

    def test_membership_drift_and_false_negative_control_fail_closed(self) -> None:
        case = json.loads((ROOT / "questions/new-child-metadata-walkthrough.json").read_text(encoding="utf-8"))
        primary_route = case["primary_route"]
        negative_route = case["negative_control_route"]
        changed = [dict(row, source_memberships=[]) if row["open"] == primary_route else row for row in self.datasets]
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(WalkthroughError):
                build_walkthrough(Path(temporary), changed, self.publishers, self.relationships, "NEW-CHILD-20260715")
        changed_negative = [
            dict(row, source_memberships=[*row["source_memberships"], "mainstream-browse:childcare-parenting/pregnancy-birth"])
            if row["open"] == negative_route
            else row
            for row in self.datasets
        ]
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(WalkthroughError):
                build_walkthrough(Path(temporary), changed_negative, self.publishers, self.relationships, "NEW-CHILD-20260715")


if __name__ == "__main__":
    unittest.main()
