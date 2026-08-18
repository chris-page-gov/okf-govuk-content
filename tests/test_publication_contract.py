from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_publication_contract as MODULE  # noqa: E402


class PublicationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads((ROOT / "okf.publication.json").read_text(encoding="utf-8"))

    def test_repository_contract_has_valid_local_references(self) -> None:
        self.assertEqual([], MODULE.validate_document(self.document))

    def test_unknown_command_fails_closed(self) -> None:
        document = copy.deepcopy(self.document)
        document["planes"][0]["command_ids"].append("not-declared")
        self.assertTrue(any("unknown command" in error for error in MODULE.validate_document(document)))

    def test_plane_cycle_is_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["planes"][0]["depends_on"] = [document["planes"][-1]["id"]]
        self.assertTrue(any("cycle" in error for error in MODULE.validate_document(document)))

    def test_contract_is_documented_in_lockstep_surfaces(self) -> None:
        for path in ("README.md", "AGENTS.md", "CHANGELOG.md"):
            self.assertIn("okf.publication.json", (ROOT / path).read_text(encoding="utf-8"), path)


if __name__ == "__main__":
    unittest.main()
