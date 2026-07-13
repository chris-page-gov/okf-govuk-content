from __future__ import annotations

import importlib.util
import hashlib
import json
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = load_module("check_provenance", ROOT / "scripts" / "check_provenance.py")
APPEND = load_module("append_activity", ROOT / "scripts" / "append_activity.py")

RELEASE_ID = "T1-20260712-closing"


def deterministic_entry(activity_id: str) -> dict[str, object]:
    return {
        "ledger_schema_version": "2.0",
        "activity_id": activity_id,
        "status": "completed",
        "work_class": "deterministic",
        "started_at": "2026-07-12T08:00:00Z",
        "ended_at": "2026-07-12T08:00:01Z",
        "recorded_at": "2026-07-12T08:00:01Z",
        "commit": None,
        "agent": {"id": "test", "role": "validator", "relationship": "deterministic_process"},
        "prompt": {"capture_status": "not_applicable", "objective": "", "reference": None, "sha256": None},
        "model": None,
        "tool_calls": {
            "capture_status": "complete",
            "calls": [{"tool": "CPython", "command": "test", "purpose": "test", "call_count": 1}],
        },
        "source_snapshots": [],
        "outputs": [],
        "validation": {"capture_status": "complete", "results": ["passed"]},
        "source_request_usage": {
            "status": "not_applicable",
            "attempts": "not_applicable",
            "budget_ledger": None,
            "observation_at": None,
            "included_in_model_cost": False,
            "evidence": None,
        },
        "usage": {
            "external_paid_model": {"api_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_gbp": 0},
            "product_session": {"input_tokens": 0, "output_tokens": 0, "marginal_cost_gbp": 0},
        },
        "tokens": 0,
        "cost_gbp": 0,
        "external_paid_model_api_calls": 0,
    }


def write_chain(path: Path, rows: list[dict[str, object]]) -> None:
    lines: list[str] = []
    previous: str | None = None
    for supplied in rows:
        row = dict(supplied)
        row["previous_entry_sha256"] = previous
        line = APPEND.canonical_line(row)
        lines.append(line)
        previous = hashlib.sha256(line.encode("utf-8")).hexdigest()
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def release_fixture(
    root: Path,
    *,
    omit_terminal: str | None = None,
    extra_pending: bool = False,
    request_status: str = "final",
    consumed: int = 10,
    ceiling: int = 10,
) -> dict[str, Path]:
    schema = root / "activity-ledger.schema.json"
    declarations = root / "reproduction-declarations.json"
    launch = root / "launch-manifest.yaml"
    model_lock = root / "models.lock.yaml"
    shutil.copy2(ROOT / "provenance/activity-ledger.schema.json", schema)
    shutil.copy2(ROOT / "provenance/reproduction-declarations.json", declarations)
    shutil.copy2(ROOT / "governance/launch-manifest.yaml", launch)
    shutil.copy2(ROOT / "orchestration/models.lock.yaml", model_lock)
    declaration = json.loads(declarations.read_text(encoding="utf-8"))

    rows: list[dict[str, object]] = []
    checkpoints = {
        "ACT-B1-T0-20260712-CHECKPOINT-001": ("in_progress", "checkpoint"),
        "ACT-B1-CAPACITY-20260712-001": ("completed", "pending_final"),
        "ACT-F2-CITATION-COLLECTOR-001": ("completed", "pending_final"),
        "ACT-D1-SHARD-CONTRACT-AUDIT-001": ("in_progress", "not_applicable"),
        "ACT-D2-SECURITY-SCAN-001": ("in_progress", "not_applicable"),
    }
    for activity_id, (status, request_state) in checkpoints.items():
        row = deterministic_entry(activity_id)
        row["status"] = status
        if status == "in_progress":
            row["ended_at"] = None
            row["validation"] = {"capture_status": "pending", "results": ["pending"]}
        if request_state == "checkpoint":
            row["source_request_usage"] = {
                "status": "checkpoint",
                "attempts": 1,
                "budget_ledger": "test-ledger",
                "observation_at": "2026-07-12T08:00:00Z",
                "included_in_model_cost": False,
                "evidence": "test",
            }
        elif request_state == "pending_final":
            row["source_request_usage"] = {
                "status": "pending_final",
                "attempts": "pending_final",
                "budget_ledger": "test-ledger",
                "observation_at": None,
                "included_in_model_cost": False,
                "evidence": "test",
            }
        rows.append(row)

    request_events = {
        "T0 census terminal disposition",
        "T0 hydration terminal disposition",
        "T1 census and closing reconciliation",
        "citation independent semantic and joint-support reviews",
        "final source-request budget snapshot",
    }
    for item in declaration["final_activity_entries_required"]:
        activity_id = item["terminal_activity_id"]
        if activity_id == omit_terminal:
            continue
        row = deterministic_entry(activity_id)
        row["source_snapshots"] = [RELEASE_ID]
        if item["event"] in request_events:
            row["source_request_usage"] = {
                "status": "exact",
                "attempts": consumed if item["event"] == "final source-request budget snapshot" else 1,
                "budget_ledger": "test-ledger",
                "observation_at": "2026-07-12T08:00:01Z",
                "included_in_model_cost": False,
                "evidence": "test",
            }
        if item.get("must_supersede"):
            row["supersedes_activity_ids"] = [item["must_supersede"]]
        rows.append(row)

    if extra_pending:
        row = deterministic_entry("ACT-EXTRA-PENDING-001")
        row["source_request_usage"] = {
            "status": "pending_final",
            "attempts": "pending_final",
            "budget_ledger": "test-ledger",
            "observation_at": None,
            "included_in_model_cost": False,
            "evidence": "test",
        }
        rows.append(row)

    ledger = root / "activity-ledger.jsonl"
    write_chain(ledger, rows)
    request = root / "source-request-budget.json"
    request.write_text(
        json.dumps(
            {
                "schema": "afhf-govuk-okf-source-request-budget.v1",
                "recorded_at": "2026-07-12T08:00:01Z",
                "snapshot_id": RELEASE_ID,
                "status": request_status,
                "authorised_ceiling": ceiling,
                "consumed_attempts_at_observation": consumed,
                "remaining_attempts_at_observation": ceiling - consumed,
                "preflight_attempts": 1,
                "included_in_model_cost": False,
                "final_entries_required": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    live = root / "official-sources.count"
    live.write_text(f"{consumed}\n", encoding="utf-8")
    return {
        "ledger_path": ledger,
        "schema_path": schema,
        "declarations_path": declarations,
        "request_snapshot_path": request,
        "live_request_ledger": live,
        "launch_path": launch,
        "model_lock_path": model_lock,
    }


class ProvenanceTests(unittest.TestCase):
    def test_append_side_lock_serializes_writers_and_rejects_symlink_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "provenance/activity-ledger.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text("", encoding="utf-8")
            schema = ROOT / "provenance/activity-ledger.schema.json"
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        lambda activity_id: APPEND.append_entries(
                            [deterministic_entry(activity_id)], ledger, schema
                        ),
                        ("ACT-CONCURRENT-001", "ACT-CONCURRENT-002"),
                    )
                )
            self.assertEqual(2, len(results))
            summary = CHECK.validate_ledger(ledger, schema)
            self.assertEqual(2, summary["hash_chained_v2_rows"])

        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            ledger = root / "provenance/activity-ledger.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text("", encoding="utf-8")
            (root / ".tmp").symlink_to(Path(outside), target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "cannot be a symlink"):
                APPEND.append_entries([deterministic_entry("ACT-SYMLINK-001")], ledger, schema)

        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            ledger = root / "provenance/activity-ledger.jsonl"
            ledger.parent.mkdir(parents=True)
            outside_ledger = Path(outside) / "outside-ledger.jsonl"
            outside_ledger.write_text("", encoding="utf-8")
            ledger.symlink_to(outside_ledger)
            with self.assertRaisesRegex(ValueError, "cannot be symlinks"):
                APPEND.append_entries(
                    [deterministic_entry("ACT-LEDGER-SYMLINK-001")], ledger, schema
                )
            with self.assertRaisesRegex(ValueError, "cannot be symlinks"):
                APPEND.append_entries(
                    [deterministic_entry("ACT-LEDGER-SYMLINK-UNLOCKED-001")],
                    ledger,
                    schema,
                    acquire_lock=False,
                )
            self.assertEqual("", outside_ledger.read_text(encoding="utf-8"))

            ledger.unlink()
            ledger.write_text("", encoding="utf-8")
            schema_link = root / "schema-link.json"
            schema_link.symlink_to(schema)
            with self.assertRaisesRegex(ValueError, "schema cannot be a symlink"):
                APPEND.append_entries(
                    [deterministic_entry("ACT-SCHEMA-SYMLINK-001")],
                    ledger,
                    schema_link,
                )

    def test_checked_in_provenance_is_valid(self) -> None:
        summary = CHECK.validate_all()
        self.assertEqual(0, summary["ledger"]["external_paid_model_api_calls"])
        self.assertGreaterEqual(summary["declarations"]["fallbacks"], 4)

    def test_append_hash_chains_and_rejects_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "activity.jsonl"
            legacy = {
                "activity_id": "ACT-LEGACY-001",
                "exact_model_version": None,
                "tokens": 0,
                "cost_gbp": 0,
                "external_paid_model_api_calls": 0,
            }
            ledger.write_text(json.dumps(legacy, separators=(",", ":")) + "\n", encoding="utf-8")
            entry_path = root / "entry.json"
            entry_path.write_text(json.dumps(deterministic_entry("ACT-TEST-001")), encoding="utf-8")
            APPEND.append_entry(entry_path, ledger, ROOT / "provenance/activity-ledger.schema.json")
            summary = CHECK.validate_ledger(ledger, ROOT / "provenance/activity-ledger.schema.json")
            self.assertEqual(1, summary["hash_chained_v2_rows"])
            with self.assertRaisesRegex(ValueError, "duplicate activity_id"):
                APPEND.append_entry(entry_path, ledger, ROOT / "provenance/activity-ledger.schema.json")

    def test_hash_chain_tampering_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "activity.jsonl"
            first = deterministic_entry("ACT-TEST-001")
            first["previous_entry_sha256"] = None
            second = deterministic_entry("ACT-TEST-002")
            first_line = APPEND.canonical_line(first)
            second["previous_entry_sha256"] = "0" * 64
            ledger.write_text(first_line + "\n" + APPEND.canonical_line(second) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(CHECK.ProvenanceError, "does not hash-chain"):
                CHECK.validate_ledger(ledger, ROOT / "provenance/activity-ledger.schema.json")

    def test_release_document_passes_only_with_closed_terminal_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = release_fixture(Path(directory))
            document = CHECK.build_validation_document(
                snapshot=RELEASE_ID,
                require_release=True,
                **paths,
            )
            self.assertTrue(document["provenance_validation_passed"], document["validation_errors"])
            self.assertTrue(document["release_requirements_satisfied"])
            self.assertEqual(11, document["required_terminal_events"]["satisfied"])
            self.assertEqual(10, document["source_request_budget"]["final_shared_request_count"])
            self.assertEqual(0, document["external_paid_model_usage"]["api_calls"])
            self.assertTrue(document["hash_chain"]["passed"])

            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"
            CHECK.write_validation_document(first, document)
            CHECK.write_validation_document(second, document)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_candidate_allows_only_the_post_publication_terminal_to_remain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = release_fixture(
                Path(directory),
                omit_terminal=CHECK.PUBLICATION_TERMINAL_ACTIVITY_ID,
            )
            candidate = CHECK.build_validation_document(
                snapshot=RELEASE_ID,
                require_candidate=True,
                **paths,
            )
            self.assertTrue(candidate["provenance_validation_passed"], candidate["validation_errors"])
            self.assertTrue(candidate["candidate_requirements_satisfied"])
            self.assertFalse(candidate["release_requirements_satisfied"])
            self.assertEqual("candidate", candidate["validation_tier"])
            self.assertEqual("pending_post_publication", candidate["publication_workflow_status"])
            self.assertEqual(10, candidate["required_terminal_events"]["candidate_satisfied"])
            self.assertEqual(
                CHECK.PUBLICATION_TERMINAL_ACTIVITY_ID,
                candidate["required_terminal_events"]["pending_post_publication_terminal_activity_id"],
            )

            strict = CHECK.build_validation_document(
                snapshot=RELEASE_ID,
                require_release=True,
                **paths,
            )
            self.assertFalse(strict["provenance_validation_passed"])
            self.assertIn(CHECK.PUBLICATION_TERMINAL_ACTIVITY_ID, "\n".join(strict["validation_errors"]))

    def test_release_rejects_open_budget_missing_terminal_and_pending_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = release_fixture(
                root,
                omit_terminal="ACT-E2-AUTOMATED-EVALUATION-TERMINAL-001",
                extra_pending=True,
                request_status="open_checkpoint",
            )
            document = CHECK.build_validation_document(snapshot=RELEASE_ID, require_release=True, **paths)
            self.assertFalse(document["provenance_validation_passed"])
            errors = "\n".join(document["validation_errors"])
            self.assertIn("open checkpoint", errors)
            self.assertIn("ACT-E2-AUTOMATED-EVALUATION-TERMINAL-001", errors)
            self.assertIn("ACT-EXTRA-PENDING-001", errors)

    def test_release_rejects_over_ceiling_and_non_release_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = release_fixture(Path(directory), consumed=11, ceiling=10)
            document = CHECK.build_validation_document(
                snapshot="fixture-2026-07-11",
                require_release=True,
                **paths,
            )
            self.assertFalse(document["provenance_validation_passed"])
            errors = "\n".join(document["validation_errors"])
            self.assertIn("source request budget arithmetic does not reconcile", errors)
            self.assertIn("non-release snapshot label", errors)


if __name__ == "__main__":
    unittest.main()
