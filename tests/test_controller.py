from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.controller import Controller, ControllerError, load_dag, materialize_contracts
from govuk_okf.util import safe_child_path, safe_identifier


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

    def test_task_identifiers_and_derived_paths_cannot_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dag = root / "dag.json"
            dag.write_text(
                '{"tasks":[{"id":"../../outside","name":"unsafe","depends":[],"output":"x"}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ControllerError, "unsafe DAG task ID"):
                load_dag(dag)
            with self.assertRaisesRegex(ValueError, "unsafe persona ID"):
                safe_identifier("persona-../../outside", label="persona ID")
            with self.assertRaisesRegex(ValueError, "unsafe task-contract path"):
                safe_child_path(root / "contracts", "../outside.json", label="task-contract path")

    def test_concurrent_leases_have_one_compare_and_swap_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite"
            events = root / "events.jsonl"
            initial = Controller(database, events)
            initial.bootstrap()
            initial.close()

            def lease() -> str:
                controller = Controller(database, events)
                try:
                    return controller.transition("G0-01", "leased").state
                except ControllerError:
                    return "rejected"
                finally:
                    controller.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _index: lease(), range(2)))
            self.assertEqual(["leased", "rejected"], sorted(outcomes))
            self.assertEqual(1, len(events.read_text(encoding="utf-8").splitlines()))

    def test_event_projection_failure_rolls_back_state_and_database_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = root / "events.jsonl"
            controller = Controller(root / "state.sqlite", events)
            try:
                controller.bootstrap()
                events.unlink()
                events.mkdir()
                with self.assertRaisesRegex(ControllerError, "rolled back before durable evidence"):
                    controller.transition("G0-01", "leased")
                self.assertEqual("queued", controller.state("G0-01").state)
                count = controller.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                self.assertEqual(0, count)
            finally:
                controller.close()


if __name__ == "__main__":
    unittest.main()
