#!/usr/bin/env python3
"""Check generated walkthrough links, source digests and journey identity."""

from __future__ import annotations

import hashlib
import html
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "bundle"
WALKTHROUGH = BUNDLE / "walkthroughs"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    case = json.loads((ROOT / "questions" / "new-child-metadata-walkthrough.json").read_text(encoding="utf-8"))
    evidence_path = WALKTHROUGH / "new-child-metadata-evidence.json"
    journeys_path = WALKTHROUGH / "new-child-metadata-explorer-journeys.json"
    html_path = WALKTHROUGH / "new-child-metadata.html"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    journeys = json.loads(journeys_path.read_text(encoding="utf-8"))
    if evidence.get("case_id") != case.get("id") or evidence.get("snapshot") != "NEW-CHILD-20260715":
        raise SystemExit("walkthrough evidence identity differs from its case or source snapshot")
    if evidence.get("case_sha256") != hashlib.sha256((ROOT / "questions" / "new-child-metadata-walkthrough.json").read_bytes()).hexdigest():
        raise SystemExit("walkthrough case digest differs")
    rows = [(role, row) for role, row in evidence["records"].items() if isinstance(row, dict)]
    rows.extend((f"associated_organisation_{index}", row) for index, row in enumerate(evidence["records"]["associated_organisations"]))
    for role, row in rows:
        source = BUNDLE / row["path"]
        if not source.is_file() or digest(source) != row["sha256"]:
            raise SystemExit(f"walkthrough source digest differs: {role}")
    html_text = html_path.read_text(encoding="utf-8")
    if evidence["reader_link"] not in html.unescape(html_text) or "Open this record in OKF Explorer" not in html_text:
        raise SystemExit("walkthrough has no exact descriptor-bound Reader link")
    if case["primary_route"] not in evidence["reader_link"]:
        raise SystemExit("Reader link does not select the primary source record")
    if parse_qs(urlparse(evidence["reader_link"]).query).get("view") != ["reader"]:
        raise SystemExit("walkthrough Reader link must open the selected record in Reader view")
    if parse_qs(urlparse(evidence["reader_link"]).query).get("q") != [evidence["records"]["primary"]["title"]]:
        raise SystemExit("walkthrough Reader link must bind its search to the exact primary source title")
    if not all((WALKTHROUGH / name).is_file() for name in ("new-child-metadata-desktop.svg", "new-child-metadata-mobile.svg")):
        raise SystemExit("responsive walkthrough diagrams are incomplete")
    if len(journeys.get("journeys", [])) != 2:
        raise SystemExit("walkthrough Reader manifest must include graph and search journeys")
    identity = journeys.get("expected_identity") or {}
    descriptor = json.loads((BUNDLE / "okf-explorer.json").read_text(encoding="utf-8"))
    if descriptor["extensions"]["okf-v0.3"]["domain_concepts"] != 83 or descriptor["extensions"]["okf-v0.3"]["documentation_concepts"] != 3:
        raise SystemExit("bounded demonstrator concept counts differ from the reviewed source scope")
    for key, actual in (("schema", descriptor.get("schema")), ("id", descriptor.get("@id")), ("version", descriptor.get("version")), ("snapshot", descriptor.get("snapshot"))):
        if identity.get(key) != actual:
            raise SystemExit(f"Reader journey {key} does not match the generated descriptor")
    print(json.dumps({"passed": True, "case": case["id"], "snapshot": evidence["snapshot"], "source_records": len(evidence["records"]), "reader_journeys": len(journeys["journeys"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
