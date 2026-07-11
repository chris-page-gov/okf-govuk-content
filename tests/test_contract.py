from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.contract import expand_requirement_references, parse_requirements, render, validate_inputs


class ContractTests(unittest.TestCase):
    def test_controlling_hashes(self) -> None:
        validate_inputs()

    def test_requirement_and_gate_counts(self) -> None:
        requirements, gates = parse_requirements()
        self.assertEqual(95, len(requirements))
        self.assertEqual(11, len(gates))
        self.assertEqual("REQ-001", requirements[0]["id"])
        self.assertEqual("REQ-095", requirements[-1]["id"])

    def test_range_expansion(self) -> None:
        self.assertEqual(
            ["REQ-009", "REQ-010", "REQ-011", "REQ-012", "REQ-013", "REQ-033"],
            expand_requirement_references("REQ-009–REQ-013, REQ-033"),
        )

    def test_projections_are_valid_json_compatible_yaml(self) -> None:
        rendered = render()
        for content in rendered.values():
            self.assertIsInstance(json.loads(content), dict)


if __name__ == "__main__":
    unittest.main()
