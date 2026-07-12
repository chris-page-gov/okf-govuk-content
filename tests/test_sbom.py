from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_sbom", ROOT / "scripts" / "build_sbom.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SbomTests(unittest.TestCase):
    def test_sbom_is_deterministic_and_covers_both_lockfiles(self) -> None:
        first = MODULE.render(ROOT / "uv.lock", ROOT / "semantic/package-lock.json")
        second = MODULE.render(ROOT / "uv.lock", ROOT / "semantic/package-lock.json")
        self.assertEqual(first, second)
        document = json.loads(first)
        MODULE.validate(document)
        ecosystems = {
            property_row["value"]
            for component in document["components"]
            for property_row in component.get("properties", [])
            if property_row.get("name") == "govuk-okf:ecosystem"
        }
        self.assertEqual({"npm", "python"}, ecosystems)
        self.assertIn(
            "pkg:pypi/jsonschema@4.26.0",
            {component["bom-ref"] for component in document["components"]},
        )
        self.assertIn(
            "pkg:npm/jsonld@9.0.0",
            {component["bom-ref"] for component in document["components"]},
        )

    def test_check_fails_when_a_lockfile_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python_lock = root / "uv.lock"
            node_lock = root / "package-lock.json"
            output = root / "sbom.json"
            shutil.copyfile(ROOT / "uv.lock", python_lock)
            shutil.copyfile(ROOT / "semantic/package-lock.json", node_lock)
            output.write_text(MODULE.render(python_lock, node_lock), encoding="utf-8")
            self.assertEqual(
                0,
                MODULE.main(
                    [
                        "--python-lock",
                        str(python_lock),
                        "--node-lock",
                        str(node_lock),
                        "--output",
                        str(output),
                        "--check",
                    ]
                ),
            )
            node_document = json.loads(node_lock.read_text(encoding="utf-8"))
            node_document["packages"]["node_modules/jsonld"]["version"] = "9.0.1"
            node_lock.write_text(json.dumps(node_document), encoding="utf-8")
            self.assertEqual(
                1,
                MODULE.main(
                    [
                        "--python-lock",
                        str(python_lock),
                        "--node-lock",
                        str(node_lock),
                        "--output",
                        str(output),
                        "--check",
                    ]
                ),
            )


if __name__ == "__main__":
    unittest.main()
