from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLOSURE = load_module(
    "finalize_terminal_activity_test",
    ROOT / "scripts" / "finalize_terminal_activity.py",
)
APPEND = load_module("append_activity_terminal_test", ROOT / "scripts" / "append_activity.py")

SNAPSHOT = "T1-20260713-closing"
STARTED = "2026-07-13T01:00:00Z"
ENDED = "2026-07-13T01:01:00Z"
RECORDED = "2026-07-13T01:01:01Z"


def declaration(
    activity_id: str,
    event: str,
    *,
    release: bool = False,
    supersedes: str | None = None,
    outputs: list[str] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "event": event,
        "terminal_activity_id": activity_id,
        "must_bind_release_snapshot": release,
        "required_evidence": ["test evidence"],
    }
    if supersedes:
        value["must_supersede"] = supersedes
    if outputs:
        value["required_output_paths"] = outputs
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def terminal_entry(
    declaration_value: dict[str, object],
    *,
    snapshot: str = SNAPSHOT,
    request: dict[str, object] | None = None,
    outputs: list[dict[str, object]] | None = None,
    recorded_at: str = RECORDED,
) -> dict[str, object]:
    return CLOSURE._base_entry(
        declaration=declaration_value,
        snapshot=snapshot,
        started_at=STARTED,
        ended_at=ENDED,
        recorded_at=recorded_at,
        outputs=outputs or [],
        results=["deterministic evidence passed"],
        source_request_usage=request
        or CLOSURE._not_applicable_requests("no official-source requests"),
        tool="CPython test verifier",
        command="python -m unittest",
    )


def exact_request(label: str, start: int, end: int) -> dict[str, object]:
    interval = CLOSURE._counter_interval(
        label,
        start,
        end,
        ".tmp/request-budget/official-sources.count",
    )
    return CLOSURE._exact_requests(interval, ENDED, f"Exact shared counter interval {start}..{end}")


class TerminalAppendTests(unittest.TestCase):
    def test_append_preserves_t0_chain_is_exactly_idempotent_and_rejects_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "provenance/activity-ledger.jsonl"
            ledger.parent.mkdir(parents=True)
            schema = root / "provenance/activity-ledger.schema.json"
            shutil.copy2(ROOT / "provenance/activity-ledger.schema.json", schema)

            t0_declaration = declaration(
                CLOSURE.T0_TERMINAL,
                "T0 census terminal disposition",
            )
            t0 = terminal_entry(
                t0_declaration,
                snapshot="T0-20260712",
                request=exact_request("t0_census", 3, 7),
            )
            APPEND.append_entries([t0], ledger, schema)
            t0_bytes = ledger.read_bytes()

            evidence = root / "evidence/closed.json"
            write_json(evidence, {"closed": True})
            hydration_declaration = declaration(
                CLOSURE.TERMINAL_IDS["hydration"],
                "T0 hydration terminal disposition",
                outputs=["evidence/closed.json"],
            )
            hydration = terminal_entry(
                hydration_declaration,
                snapshot="T0-20260712",
                request=exact_request("t0_hydration", 7, 11),
                outputs=[CLOSURE._output(root, evidence)],
            )
            appended = CLOSURE._append_idempotent(
                hydration,
                ledger,
                schema,
                hydration_declaration,
                root,
            )
            self.assertEqual("appended", appended["status"])
            self.assertTrue(ledger.read_bytes().startswith(t0_bytes))
            rows, raw = CLOSURE._ledger_rows(ledger)
            self.assertEqual([CLOSURE.T0_TERMINAL, CLOSURE.TERMINAL_IDS["hydration"]], [row["activity_id"] for row in rows])
            self.assertEqual(
                hashlib.sha256(raw[0].encode("utf-8")).hexdigest(),
                rows[1]["previous_entry_sha256"],
            )

            stable = ledger.read_bytes()
            repeated = CLOSURE._append_idempotent(
                hydration,
                ledger,
                schema,
                hydration_declaration,
                root,
            )
            self.assertEqual("already_present", repeated["status"])
            self.assertEqual(stable, ledger.read_bytes())

            conflicting = dict(hydration)
            conflicting["recorded_at"] = "2026-07-13T01:01:02Z"
            with self.assertRaisesRegex(CLOSURE.ClosureError, "conflicting existing"):
                CLOSURE._append_idempotent(
                    conflicting,
                    ledger,
                    schema,
                    hydration_declaration,
                    root,
                )
            self.assertEqual(stable, ledger.read_bytes())

            evidence.write_text('{"closed":false}\n', encoding="utf-8")
            with self.assertRaisesRegex(CLOSURE.ClosureError, "changed before append"):
                CLOSURE._append_idempotent(
                    hydration,
                    ledger,
                    schema,
                    hydration_declaration,
                    root,
                )
            self.assertEqual(stable, ledger.read_bytes())

    def test_interval_arithmetic_and_canonical_external_urls_fail_closed(self) -> None:
        with self.assertRaisesRegex(CLOSURE.ClosureError, "invalid"):
            CLOSURE._counter_interval("bad", 10, 9, None)
        with self.assertRaisesRegex(CLOSURE.ClosureError, "canonical"):
            CLOSURE._https_url("https://github.com:443/example", "test URL")
        with self.assertRaisesRegex(CLOSURE.ClosureError, "unexpected GitHub"):
            CLOSURE._github_url(
                "https://example.com/chris-page-gov/okf-govuk-content/pull/1",
                "PR URL",
                r"/chris-page-gov/okf-govuk-content/pull/[1-9][0-9]*",
            )


def budget_fixture(root: Path, *, discontinuous: bool = False) -> tuple[SimpleNamespace, dict[str, object], bytes, bytes]:
    provenance = root / "provenance"
    provenance.mkdir(parents=True)
    schema = provenance / "activity-ledger.schema.json"
    shutil.copy2(ROOT / "provenance/activity-ledger.schema.json", schema)
    ledger = provenance / "activity-ledger.jsonl"

    live = root / ".tmp/request-budget/official-sources.count"
    live.parent.mkdir(parents=True)
    live.write_text("15\n", encoding="utf-8")
    (root / "governance").mkdir()
    (root / "governance/launch-manifest.yaml").write_text(
        "authority:\n  official_source_requests: 100\n",
        encoding="utf-8",
    )
    write_json(
        root / "research/source-preflight.json",
        {
            "sources": [
                {"requested_url": "https://www.gov.uk/", "status": 200, "attempts": 1}
            ]
        },
    )
    citation = root / "provenance/citation-request-aggregate.json"
    write_json(citation, {"totals": {"attempts": 7}})
    budget = root / "provenance/source-request-budget.json"
    write_json(
        budget,
        {
            "schema": "afhf-govuk-okf-source-request-budget.v1",
            "status": "open_checkpoint",
            "consumed_attempts_at_observation": 7,
        },
    )

    t0_declaration = declaration(CLOSURE.T0_TERMINAL, "T0 census terminal disposition")
    t0_request = exact_request("t0_census", 3, 7)
    # Exercise compatibility with the immutable terminal written before the
    # structured interval schema existed.
    t0_request.pop("intervals")
    t0 = terminal_entry(t0_declaration, snapshot="T0-20260712", request=t0_request)

    hydration_declaration = declaration(
        CLOSURE.TERMINAL_IDS["hydration"],
        "T0 hydration terminal disposition",
    )
    hydration = terminal_entry(
        hydration_declaration,
        snapshot="T0-20260712",
        request=exact_request("t0_hydration", 9 if discontinuous else 8, 11),
    )
    hardening_request = exact_request("pre_hardening_hydration", 7, 8)
    hardening_request.pop("intervals")
    hardening_request["evidence"] = (
        "The shared official-source counter advanced from 7 to 8 for one "
        "robots.txt request during a pre-hardening hydration start."
    )
    hardening = terminal_entry(
        declaration(CLOSURE.HYDRATION_HARDENING_ACTIVITY, "hydration hardening"),
        snapshot="T0-20260712",
        request=hardening_request,
    )
    reconciliation_declaration = declaration(
        CLOSURE.TERMINAL_IDS["reconciliation"],
        "T1 census and closing reconciliation",
        release=True,
    )
    reconciliation = terminal_entry(
        reconciliation_declaration,
        request=exact_request("t1_and_closing", 11, 15),
    )
    citation_declaration = declaration(
        CLOSURE.TERMINAL_IDS["citations"],
        "release citation verification",
        release=True,
    )
    citation_terminal = terminal_entry(
        citation_declaration,
        request=CLOSURE._exact_requests(
            CLOSURE._counter_interval("citations", 0, 7, None),
            ENDED,
            "separate citation interval 0..7",
        ),
    )
    capacity = terminal_entry(
        declaration("ACT-B1-CAPACITY-20260712-001", "capacity checkpoint"),
        snapshot="T0-20260712",
    )
    APPEND.append_entries(
        [t0, hardening, hydration, reconciliation, citation_terminal, capacity],
        ledger,
        schema,
    )
    old_ledger = ledger.read_bytes()
    old_budget = budget.read_bytes()
    final_declaration = declaration(
        CLOSURE.TERMINAL_IDS["source-budget"],
        "final source-request budget snapshot",
        release=True,
        supersedes="ACT-B1-CAPACITY-20260712-001",
    )
    args = SimpleNamespace(
        root=root,
        snapshot=SNAPSHOT,
        started_at=STARTED,
        ended_at=ENDED,
        recorded_at=RECORDED,
        request_start=0,
        request_end=15,
        ledger=ledger,
        schema=schema,
        live_counter=live.relative_to(root).as_posix(),
        budget_snapshot=budget.relative_to(root).as_posix(),
        citation_aggregate=citation.relative_to(root).as_posix(),
    )
    return args, final_declaration, old_ledger, old_budget


class SourceBudgetTransactionTests(unittest.TestCase):
    def test_final_budget_and_terminal_commit_together_and_repeat_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, final_declaration, old_ledger, _ = budget_fixture(root)
            result = CLOSURE.close_source_budget(args, final_declaration)
            self.assertEqual("appended", result["status"])
            self.assertTrue(args.ledger.read_bytes().startswith(old_ledger))
            budget = json.loads((root / args.budget_snapshot).read_text(encoding="utf-8"))
            self.assertEqual("final", budget["status"])
            self.assertEqual(15, budget["consumed_attempts_at_observation"])
            self.assertEqual(85, budget["remaining_attempts_at_observation"])
            self.assertEqual(1, budget["preflight_attempts"])
            self.assertIs(False, budget["included_in_model_cost"])
            self.assertEqual(
                [(3, 7), (7, 8), (8, 11), (11, 15)],
                [
                    (row["start_counter"], row["end_counter"])
                    for row in budget["shared_request_intervals"]
                ],
            )
            self.assertEqual(
                {
                    "counter_at_t0_start": 3,
                    "source_preflight_attempts": 1,
                    "additional_historical_shared_attempts": 2,
                    "per_run_breakdown_status": "unavailable_not_reconstructed",
                    "limitation_evidence_activity_id": "ACT-B1-CAPACITY-20260712-001",
                },
                budget["pre_t0_accounting"],
            )
            rows, _ = CLOSURE._ledger_rows(args.ledger)
            terminal = rows[-1]
            self.assertEqual(CLOSURE.TERMINAL_IDS["source-budget"], terminal["activity_id"])
            self.assertEqual(15, terminal["source_request_usage"]["attempts"])
            output = next(
                row
                for row in terminal["outputs"]
                if row["path"] == args.budget_snapshot
            )
            self.assertEqual(
                hashlib.sha256((root / args.budget_snapshot).read_bytes()).hexdigest(),
                output["sha256"],
            )

            stable_ledger = args.ledger.read_bytes()
            stable_budget = (root / args.budget_snapshot).read_bytes()
            repeated = CLOSURE.close_source_budget(args, final_declaration)
            self.assertEqual("already_present", repeated["status"])
            self.assertEqual(stable_ledger, args.ledger.read_bytes())
            self.assertEqual(stable_budget, (root / args.budget_snapshot).read_bytes())

            args.request_end = 16
            with self.assertRaisesRegex(CLOSURE.ClosureError, "live shared counter"):
                CLOSURE.close_source_budget(args, final_declaration)
            self.assertEqual(stable_ledger, args.ledger.read_bytes())
            self.assertEqual(stable_budget, (root / args.budget_snapshot).read_bytes())

    def test_append_failure_rolls_back_budget_ledger_and_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, final_declaration, old_ledger, old_budget = budget_fixture(root)
            with mock.patch.object(CLOSURE, "append_entries", side_effect=RuntimeError("injected append failure")):
                with self.assertRaisesRegex(RuntimeError, "injected append failure"):
                    CLOSURE.close_source_budget(args, final_declaration)
            self.assertEqual(old_ledger, args.ledger.read_bytes())
            self.assertEqual(old_budget, (root / args.budget_snapshot).read_bytes())
            self.assertEqual([], list((root / ".tmp/locks").glob("*.txn.json")))

            result = CLOSURE.close_source_budget(args, final_declaration)
            self.assertEqual("appended", result["status"])

    def test_discontinuous_stage_intervals_do_not_modify_either_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, final_declaration, old_ledger, old_budget = budget_fixture(
                root,
                discontinuous=True,
            )
            with self.assertRaisesRegex(CLOSURE.ClosureError, "not contiguous"):
                CLOSURE.close_source_budget(args, final_declaration)
            self.assertEqual(old_ledger, args.ledger.read_bytes())
            self.assertEqual(old_budget, (root / args.budget_snapshot).read_bytes())


if __name__ == "__main__":
    unittest.main()
