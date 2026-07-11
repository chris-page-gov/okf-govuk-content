from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.controller import Controller, ControllerError, load_dag, materialize_contracts


class ControllerTests(unittest.TestCase):
    def test_dag_is_acyclic_and_contracts_synchronize(self) -> None:
        dag = load_dag()
        self.assertGreaterEqual(len(dag["tasks"]), 30)
        self.assertEqual([], materialize_contracts(check=True))

    def test_state_machine_and_dependency_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = Controller(root / "state.sqlite", root / "events.jsonl")
            try:
                controller.bootstrap()
                self.assertIn("G0-01", controller.ready())
                self.assertNotIn("G0-02", controller.ready())
                controller.transition("G0-01", "leased")
                controller.transition("G0-01", "running")
                controller.transition("G0-01", "validating")
                controller.transition("G0-01", "accepted")
                self.assertIn("G0-02", controller.ready())
                self.assertIn("G0-03", controller.ready())
            finally:
                controller.close()

    def test_invalid_transition_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = Controller(root / "state.sqlite", root / "events.jsonl")
            try:
                controller.bootstrap()
                with self.assertRaises(ControllerError):
                    controller.transition("G0-01", "accepted")
            finally:
                controller.close()


if __name__ == "__main__":
    unittest.main()
