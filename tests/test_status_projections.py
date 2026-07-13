from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_status_projections as MODULE  # noqa: E402
import check_lockstep as LOCKSTEP  # noqa: E402


FULL_TERMINAL = "ACT-E3-FULL-PROGRAMME-TERMINAL-001"
RELEASE_ID = "T1-20260713-closed"
REQUEST_COUNT = 6000


def canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def reproduction_contract() -> dict[str, Any]:
    return {
        "source": "corpus/release-source",
        "generated_at": "2026-07-13T12:00:00Z",
        "compiler": "disk",
        "source_binding": {
            "path": "corpus/release-source",
            "kind": "directory",
            "file_count": 1,
            "bytes": 1,
            "tree_sha256": "a" * 64,
        },
    }


def release_controls(
    release_kind: str,
    *,
    finalized: bool | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    fixture = release_kind == "fixture"
    checkpoint = release_kind in {"fixture", "full_corpus_checkpoint"}
    full_programme = release_kind == "full_programme"
    release_id = "fixture-2026-07-11" if fixture else RELEASE_ID
    gates = {flag: not checkpoint for flag in MODULE.RELEASE_FLAGS}
    manifest: dict[str, Any] = {
        "schema": "afhf-govuk-okf-release-manifest.v1",
        "release_id": release_id,
        "release_kind": release_kind,
        "publication_ready": not checkpoint,
        "snapshot": {
            "id": release_id,
            "kind": "fixture" if fixture else "full_corpus",
            "sampled": fixture,
        },
        "gates": gates,
    }
    status: dict[str, Any] = {
        **gates,
        "schema": "afhf-govuk-okf-release-status.v1",
        "release_id": release_id,
        "status": "checkpoint" if checkpoint else release_kind,
        "publication_ready": not checkpoint,
        "completion_statement": (
            MODULE.FULL_MARKER
            if full_programme
            else MODULE.MACHINE_MARKER
            if not checkpoint
            else "AFHF_GOVUK_OKF_CHECKPOINT_V1"
        ),
        "machine_rc_complete": not checkpoint,
        "agent_evaluation_status": "completed" if not checkpoint else "not_started",
        "aims_assessed": not checkpoint,
        "programme_complete": full_programme,
        "full_evaluation_complete": full_programme,
        "human_evaluation_status": "completed" if full_programme else "not_authorised",
        "human_ui_of_choice_status": "completed" if full_programme else "not_yet_testable",
        "unexplained_omissions": 0 if not fixture else None,
    }
    if release_kind == "full_corpus_checkpoint":
        manifest["promotion_contract"] = {
            "schema": "afhf-govuk-okf-two-stage-promotion.v1",
            "stage": "full_corpus_checkpoint",
            "target_release_kind": "machine_release_candidate",
            "reproduction": reproduction_contract(),
        }
        status["completion_statement"] = "AFHF_GOVUK_OKF_FULL_CORPUS_CHECKPOINT_V1"
        status["promotion_finalized"] = False
    elif release_kind in {"machine_release_candidate", "full_programme"}:
        is_finalized = bool(finalized)
        status["promotion_finalized"] = is_finalized
        status["reason"] = (
            "Full programme completed with authorised human evidence."
            if full_programme
            else MODULE.MACHINE_FINAL_REASON
            if is_finalized
            else MODULE.MACHINE_CANDIDATE_REASON
        )
        manifest["promotion"] = {
            "schema": "afhf-govuk-okf-two-stage-promotion.v1",
            "from": "full_corpus_checkpoint",
            "staged_manifest_sha256": "b" * 64,
            "staged_status_sha256": "c" * 64,
            "reproduction": reproduction_contract(),
            "finalized": False,
        }
        if is_finalized:
            candidate_manifest_sha = canonical_sha256(manifest)
            candidate_status = copy.deepcopy(status)
            candidate_status["promotion_finalized"] = False
            candidate_status["reason"] = MODULE.MACHINE_CANDIDATE_REASON
            manifest["promotion"].update(
                {
                    "finalized": True,
                    "candidate_manifest_sha256": candidate_manifest_sha,
                    "candidate_status_sha256": canonical_sha256(candidate_status),
                }
            )
    return manifest, status


def selected_terminal_ids(*, finalized: bool, full_programme: bool = False) -> list[str]:
    declarations = json.loads(
        (ROOT / MODULE.DECLARATIONS_RELATIVE).read_text(encoding="utf-8")
    )
    ids = [
        row["terminal_activity_id"]
        for row in declarations["final_activity_entries_required"]
        if finalized
        or row["terminal_activity_id"] != MODULE.POST_PUBLICATION_TERMINAL_ACTIVITY_ID
    ]
    if full_programme:
        ids.extend(
            row["terminal_activity_id"]
            for row in declarations["full_programme_activity_entries_required"]
        )
    return ids


def set_machine_dispositions(source: dict[str, Any], root: Path) -> None:
    source["milestone"] = "machine_release_candidate"
    contracts = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((root / "orchestration/task-contracts").glob("*.json"))
    }
    blocked_requirements = MODULE.human_blocked_requirements(contracts)
    all_requirements = {
        row["id"]
        for row in json.loads((root / "governance/requirements.yaml").read_text(encoding="utf-8"))[
            "requirements"
        ]
    }
    source["requirement_status_groups"] = [
        {
            "status": "passed",
            "artifact_tier": "machine_release",
            "requirement_ids": sorted(all_requirements - blocked_requirements),
            "evidence": ["release/status.json"],
            "qualification": "All non-human machine release requirements passed.",
        },
        {
            "status": "blocked",
            "artifact_tier": "human_not_authorised",
            "requirement_ids": sorted(blocked_requirements),
            "evidence": ["governance/launch-manifest.yaml"],
            "qualification": "Direct human-evidence requirements remain blocked.",
        },
    ]
    blocked = MODULE.human_blocked_tasks(contracts)
    source["task_status_groups"] = [
        {
            "status": "accepted",
            "artifact_tier": "machine_release",
            "task_ids": sorted(set(contracts) - blocked),
            "terminal_activity_ids": selected_terminal_ids(finalized=False),
            "qualification": "Machine release gates passed with snapshot-bound terminal evidence.",
        },
        {
            "status": "blocked",
            "artifact_tier": "human_not_authorised",
            "task_ids": sorted(blocked),
            "qualification": "Human evaluation and its dependent full-programme tasks remain blocked.",
        },
    ]


def set_full_programme_dispositions(source: dict[str, Any], root: Path) -> None:
    source["milestone"] = "full_programme_complete"
    for group in source["requirement_status_groups"]:
        group["status"] = "passed"
        group["artifact_tier"] = "full_programme"
    task_ids = sorted(path.stem for path in (root / "orchestration/task-contracts").glob("*.json"))
    source["task_status_groups"] = [
        {
            "status": "accepted",
            "artifact_tier": "full_programme",
            "task_ids": task_ids,
            "terminal_activity_ids": selected_terminal_ids(
                finalized=True, full_programme=True
            ),
            "qualification": "Full-programme gates passed with snapshot-bound terminal evidence.",
        }
    ]


def set_finalized_machine_dispositions(source: dict[str, Any], root: Path) -> None:
    set_machine_dispositions(source, root)
    source["milestone"] = "machine_release_finalized"
    source["task_status_groups"][0]["terminal_activity_ids"] = selected_terminal_ids(
        finalized=True
    )


def activity_row(
    activity_id: str,
    output: dict[str, Any],
    *,
    source_snapshots: list[str],
    request_status: str = "not_applicable",
    attempts: int | str = "not_applicable",
    supersedes: list[str] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ledger_schema_version": "2.0",
        "activity_id": activity_id,
        "previous_entry_sha256": None,
        "status": "completed",
        "work_class": "deterministic",
        "started_at": "2026-07-13T12:00:00Z",
        "ended_at": "2026-07-13T12:01:00Z",
        "recorded_at": "2026-07-13T12:01:00Z",
        "agent": {
            "id": "deterministic-test-process",
            "role": "terminal evidence fixture",
            "relationship": "deterministic_process",
        },
        "prompt": {
            "capture_status": "not_applicable",
            "objective": "",
            "reference": None,
            "sha256": None,
        },
        "model": None,
        "tool_calls": {
            "capture_status": "complete",
            "calls": [
                {
                    "tool": "test fixture",
                    "command": None,
                    "purpose": "Materialize exact terminal evidence.",
                    "call_count": 1,
                }
            ],
        },
        "source_snapshots": source_snapshots,
        "outputs": [output],
        "validation": {
            "capture_status": "complete",
            "results": ["Terminal evidence fixture passed deterministic validation."],
        },
        "source_request_usage": {
            "status": request_status,
            "attempts": attempts,
            "budget_ledger": None,
            "observation_at": "2026-07-13T12:01:00Z" if request_status == "exact" else None,
            "included_in_model_cost": False,
            "evidence": "Exact deterministic fixture accounting.",
            "intervals": [],
        },
        "usage": {
            "external_paid_model": {
                "api_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_gbp": 0,
            },
            "product_session": {
                "input_tokens": 0,
                "output_tokens": 0,
                "marginal_cost_gbp": 0,
            },
        },
        "tokens": 0,
        "cost_gbp": 0,
        "external_paid_model_api_calls": 0,
    }
    if supersedes:
        row["supersedes_activity_ids"] = supersedes
    return row


def bind_output(root: Path, relative: str, content: str) -> dict[str, Any]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {
        "path": relative,
        "state": "produced",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def rechain(rows: list[dict[str, Any]]) -> None:
    previous: str | None = None
    for row in rows:
        row["previous_entry_sha256"] = previous
        line = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        previous = hashlib.sha256(line.encode("utf-8")).hexdigest()


def materialize_terminal_evidence(
    root: Path,
    *,
    finalized: bool,
    full_programme: bool,
    mutate_activities: Callable[[list[dict[str, Any]]], None] | None = None,
) -> list[dict[str, Any]]:
    provenance = root / "provenance"
    provenance.mkdir(parents=True, exist_ok=True)
    for relative in (MODULE.ACTIVITY_SCHEMA_RELATIVE, MODULE.DECLARATIONS_RELATIVE):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    budget = json.loads((ROOT / MODULE.REQUEST_BUDGET_RELATIVE).read_text(encoding="utf-8"))
    budget.update(
        {
            "recorded_at": "2026-07-13T12:01:00Z",
            "snapshot_id": RELEASE_ID,
            "status": "final",
            "consumed_attempts_at_observation": REQUEST_COUNT,
            "remaining_attempts_at_observation": budget["authorised_ceiling"] - REQUEST_COUNT,
        }
    )
    (root / MODULE.REQUEST_BUDGET_RELATIVE).write_text(
        json.dumps(budget, indent=2) + "\n", encoding="utf-8"
    )
    declarations = json.loads((root / MODULE.DECLARATIONS_RELATIVE).read_text(encoding="utf-8"))
    selected = [
        row
        for row in declarations["final_activity_entries_required"]
        if finalized
        or row["terminal_activity_id"] != MODULE.POST_PUBLICATION_TERMINAL_ACTIVITY_ID
    ]
    if full_programme:
        selected.extend(declarations["full_programme_activity_entries_required"])
    predecessor_ids = sorted(
        {
            item
            for declaration in selected
            for item in MODULE.check_provenance._required_supersedes(
                declaration.get("must_supersede")
            )
        }
    )
    rows = [
        activity_row(
            activity_id,
            bind_output(
                root,
                f"terminal-evidence/predecessor-{index:02d}.json",
                f"{{\"activity_id\":\"{activity_id}\"}}\n",
            ),
            source_snapshots=["T0-20260712"],
        )
        for index, activity_id in enumerate(predecessor_ids)
    ]
    request_events = {
        "T0 census terminal disposition",
        "T0 hydration terminal disposition",
        "T1 census and closing reconciliation",
        "final release-snapshot citation independent semantic and joint-support reviews",
        "final source-request budget snapshot",
    }
    for index, declaration in enumerate(selected):
        activity_id = declaration["terminal_activity_id"]
        required_paths = declaration.get("required_output_paths", [])
        outputs = [
            bind_output(root, relative, f"terminal evidence for {activity_id}\n")
            for relative in required_paths
        ]
        if not outputs:
            outputs = [
                bind_output(
                    root,
                    f"terminal-evidence/terminal-{index:02d}.json",
                    f"{{\"activity_id\":\"{activity_id}\"}}\n",
                )
            ]
        request_status = "exact" if declaration["event"] in request_events else "not_applicable"
        attempts: int | str = (
            REQUEST_COUNT
            if declaration["event"] == "final source-request budget snapshot"
            else 1
            if request_status == "exact"
            else "not_applicable"
        )
        row = activity_row(
            activity_id,
            outputs[0],
            source_snapshots=(
                [RELEASE_ID]
                if declaration.get("must_bind_release_snapshot") is True
                else ["T0-20260712"]
            ),
            request_status=request_status,
            attempts=attempts,
            supersedes=MODULE.check_provenance._required_supersedes(
                declaration.get("must_supersede")
            ),
        )
        row["outputs"] = outputs
        rows.append(row)
    if mutate_activities is not None:
        mutate_activities(rows)
    rechain(rows)
    ledger = root / MODULE.ACTIVITY_LEDGER_RELATIVE
    ledger.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    return rows


def materialize_root(
    root: Path,
    manifest: dict[str, Any],
    status: dict[str, Any],
    *,
    mutate_source: Callable[[dict[str, Any], Path], None] | None = None,
    mutate_activities: Callable[[list[dict[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    (root / "governance").mkdir(parents=True)
    (root / "release").mkdir(parents=True)
    (root / "orchestration").mkdir(parents=True)
    for relative in (
        "governance/requirements.yaml",
        "governance/traceability.json",
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    shutil.copytree(
        ROOT / "orchestration/task-contracts",
        root / "orchestration/task-contracts",
    )
    source = json.loads(
        (ROOT / "governance/implementation-status-source.json").read_text(encoding="utf-8")
    )
    if mutate_source is not None:
        mutate_source(source, root)
    for group in source["requirement_status_groups"]:
        for relative in group.get("evidence", []):
            destination = root / relative
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if (ROOT / relative).is_dir():
                destination.mkdir()
            else:
                destination.write_text("fixture evidence\n", encoding="utf-8")
    (root / MODULE.SOURCE_RELATIVE).write_text(
        json.dumps(source, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / MODULE.RELEASE_MANIFEST_RELATIVE).write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / MODULE.RELEASE_STATUS_RELATIVE).write_text(
        json.dumps(status, indent=2) + "\n",
        encoding="utf-8",
    )
    if manifest["release_kind"] in {"machine_release_candidate", "full_programme"}:
        materialize_terminal_evidence(
            root,
            finalized=manifest["promotion"]["finalized"],
            full_programme=manifest["release_kind"] == "full_programme",
            mutate_activities=mutate_activities,
        )
    return source


class ReleaseStateClassificationTests(unittest.TestCase):
    def test_valid_transition_matrix(self) -> None:
        cases = (
            (release_controls("fixture"), "checkpoint", False),
            (release_controls("full_corpus_checkpoint"), "checkpoint", False),
            (release_controls("machine_release_candidate", finalized=False), "candidate", False),
            (release_controls("machine_release_candidate", finalized=True), "release", True),
            (release_controls("full_programme", finalized=True), "release", True),
        )
        for (manifest, status), expected_state, expected_finalized in cases:
            with self.subTest(kind=manifest["release_kind"], finalized=expected_finalized):
                result = MODULE.classify_release_state(manifest, status)
                self.assertEqual(expected_state, result["release_state"])
                self.assertIs(expected_finalized, result["promotion_finalized"])

    def test_invalid_control_combinations_fail_closed(self) -> None:
        candidate_manifest, candidate_status = release_controls(
            "machine_release_candidate", finalized=False
        )
        final_manifest, final_status = release_controls(
            "machine_release_candidate", finalized=True
        )
        full_manifest, full_status = release_controls("full_programme", finalized=True)
        cases: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

        def changed(
            manifest: dict[str, Any],
            status: dict[str, Any],
            *,
            manifest_change: Callable[[dict[str, Any]], None] | None = None,
            status_change: Callable[[dict[str, Any]], None] | None = None,
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            manifest_copy = copy.deepcopy(manifest)
            status_copy = copy.deepcopy(status)
            if manifest_change:
                manifest_change(manifest_copy)
            if status_change:
                status_change(status_copy)
            return manifest_copy, status_copy

        cases.append(
            (
                "manifest schema",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    manifest_change=lambda row: row.update(schema="wrong"),
                ),
            )
        )
        cases.append(
            (
                "status schema",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    status_change=lambda row: row.update(schema="wrong"),
                ),
            )
        )
        cases.append(
            (
                "release ID",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    status_change=lambda row: row.update(release_id="other"),
                ),
            )
        )
        cases.append(
            (
                "snapshot ID",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    manifest_change=lambda row: row["snapshot"].update(id="other"),
                ),
            )
        )
        cases.append(
            (
                "readiness",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    status_change=lambda row: row.update(publication_ready=False),
                ),
            )
        )
        cases.append(
            (
                "snapshot kind",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    manifest_change=lambda row: row["snapshot"].update(kind="fixture"),
                ),
            )
        )
        cases.append(
            (
                "sampled",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    manifest_change=lambda row: row["snapshot"].update(sampled=True),
                ),
            )
        )
        cases.append(
            (
                "status kind",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    status_change=lambda row: row.update(status="checkpoint"),
                ),
            )
        )
        cases.append(
            (
                "missing promotion",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    manifest_change=lambda row: row.pop("promotion"),
                ),
            )
        )
        cases.append(
            (
                "promotion schema",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    manifest_change=lambda row: row["promotion"].update(schema="wrong"),
                ),
            )
        )
        cases.append(
            (
                "staged hash",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    manifest_change=lambda row: row["promotion"].update(
                        staged_manifest_sha256="not-a-hash"
                    ),
                ),
            )
        )
        cases.append(
            (
                "reproduction binding",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    manifest_change=lambda row: row["promotion"]["reproduction"].pop(
                        "source_binding"
                    ),
                ),
            )
        )
        cases.append(
            (
                "release gate",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    status_change=lambda row: row.update(security_scan_passed=False),
                ),
            )
        )
        cases.append(
            (
                "completion marker",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    status_change=lambda row: row.update(machine_rc_complete=False),
                ),
            )
        )
        cases.append(
            (
                "finalization mismatch",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    status_change=lambda row: row.update(promotion_finalized=True),
                ),
            )
        )
        cases.append(
            (
                "candidate programme complete",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    status_change=lambda row: row.update(programme_complete=True),
                ),
            )
        )
        cases.append(
            (
                "full promotion missing",
                *changed(
                    full_manifest,
                    full_status,
                    manifest_change=lambda row: row.pop("promotion"),
                ),
            )
        )
        cases.append(
            (
                "final candidate hash",
                *changed(
                    final_manifest,
                    final_status,
                    manifest_change=lambda row: row["promotion"].update(
                        candidate_manifest_sha256="d" * 64
                    ),
                ),
            )
        )
        cases.append(
            (
                "full promotion false",
                *changed(
                    full_manifest,
                    full_status,
                    manifest_change=lambda row: row["promotion"].update(finalized=False),
                ),
            )
        )
        cases.append(
            (
                "full status not finalized",
                *changed(
                    full_manifest,
                    full_status,
                    status_change=lambda row: row.update(promotion_finalized=False),
                ),
            )
        )
        cases.append(
            (
                "full human incomplete",
                *changed(
                    full_manifest,
                    full_status,
                    status_change=lambda row: row.update(
                        human_evaluation_status="not_authorised"
                    ),
                ),
            )
        )
        cases.append(
            (
                "full evaluation incomplete",
                *changed(
                    full_manifest,
                    full_status,
                    status_change=lambda row: row.update(full_evaluation_complete=False),
                ),
            )
        )
        cases.append(
            (
                "full programme incomplete",
                *changed(
                    full_manifest,
                    full_status,
                    status_change=lambda row: row.update(programme_complete=False),
                ),
            )
        )
        cases.append(
            (
                "unknown kind",
                *changed(
                    candidate_manifest,
                    candidate_status,
                    manifest_change=lambda row: row.update(release_kind="unknown"),
                ),
            )
        )
        for label, manifest, status in cases:
            with self.subTest(label=label), self.assertRaises(ValueError):
                MODULE.classify_release_state(manifest, status)

    def test_provenance_modes_follow_release_state(self) -> None:
        self.assertEqual(
            {"require_candidate": False, "require_release": False},
            LOCKSTEP.provenance_validation_flags("checkpoint"),
        )
        self.assertEqual(
            {"require_candidate": True, "require_release": False},
            LOCKSTEP.provenance_validation_flags("candidate"),
        )
        self.assertEqual(
            {"require_candidate": False, "require_release": True},
            LOCKSTEP.provenance_validation_flags("release"),
        )
        with self.assertRaises(ValueError):
            LOCKSTEP.provenance_validation_flags("ambiguous")

    def test_lockstep_json_loader_reports_missing_or_malformed_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            errors: list[str] = []
            self.assertEqual({}, LOCKSTEP.load_json_document(root / "missing.json", "missing", errors))
            malformed = root / "malformed.json"
            malformed.write_text("{", encoding="utf-8")
            self.assertEqual({}, LOCKSTEP.load_json_document(malformed, "malformed", errors))
            self.assertEqual(2, len(errors))


class StatusProjectionTests(unittest.TestCase):
    def test_repository_checkpoint_is_derived_and_has_no_release_passes(self) -> None:
        documents = MODULE.render(ROOT)
        requirements = json.loads(documents[ROOT / MODULE.OUTPUT_RELATIVES["requirements"]])
        tasks = json.loads(documents[ROOT / MODULE.OUTPUT_RELATIVES["tasks"]])
        self.assertEqual("checkpoint", requirements["release_state"])
        self.assertEqual("fixture", requirements["release_kind"])
        self.assertFalse(requirements["publication_ready"])
        self.assertEqual(0, requirements["counts"]["passed"])
        self.assertEqual(0, tasks["counts"]["accepted"])

    def test_checkpoint_rejects_passed_or_accepted_declarations(self) -> None:
        manifest, status = release_controls("fixture")

        def passed(source: dict[str, Any], _root: Path) -> None:
            source["requirement_status_groups"][0]["status"] = "passed"

        def accepted(source: dict[str, Any], _root: Path) -> None:
            source["task_status_groups"][0]["status"] = "accepted"
            source["task_status_groups"][0]["terminal_activity_ids"] = [
                "ACT-F2-CLEAN-ROOM-RC-TERMINAL-001"
            ]

        for mutation in (passed, accepted):
            with self.subTest(mutation=mutation.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                materialize_root(root, manifest, status, mutate_source=mutation)
                with self.assertRaisesRegex(ValueError, "checkpoint status"):
                    MODULE.render(root)

    def test_machine_candidate_requires_terminal_dispositions_and_evidence(self) -> None:
        manifest, status = release_controls("machine_release_candidate", finalized=False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            materialize_root(
                root,
                manifest,
                status,
                mutate_source=set_machine_dispositions,
            )
            documents = MODULE.render(root)
            requirements = json.loads(documents[root / MODULE.OUTPUT_RELATIVES["requirements"]])
            trace = json.loads(documents[root / MODULE.OUTPUT_RELATIVES["traceability"]])
            tasks = json.loads(documents[root / MODULE.OUTPUT_RELATIVES["tasks"]])
            self.assertEqual("candidate", requirements["release_state"])
            self.assertEqual(90, requirements["counts"]["passed"])
            self.assertEqual(5, requirements["counts"]["by_implementation_status"]["blocked"])
            self.assertEqual(
                {"REQ-069", "REQ-070", "REQ-073", "REQ-074", "REQ-077"},
                {
                    row["requirement_id"]
                    for row in requirements["requirements"]
                    if row["implementation_status"] == "blocked"
                },
            )
            self.assertEqual(32, tasks["counts"]["accepted"])
            self.assertEqual(4, tasks["counts"]["by_implementation_status"]["blocked"])
            human_clause = next(
                row for row in trace["clauses"] if "REQ-077" in row["requirement_ids"]
            )
            self.assertEqual("blocked", human_clause["implementation_status"])
            accepted = next(row for row in tasks["tasks"] if row["implementation_status"] == "accepted")
            self.assertEqual(
                selected_terminal_ids(finalized=False),
                accepted["terminal_activity_ids"],
            )

    def test_machine_candidate_rejects_stale_or_fabricated_dispositions(self) -> None:
        manifest, status = release_controls("machine_release_candidate", finalized=False)

        def candidate_milestone_only(source: dict[str, Any], _root: Path) -> None:
            source["milestone"] = "machine_release_candidate"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            materialize_root(
                root,
                manifest,
                status,
                mutate_source=candidate_milestone_only,
            )
            with self.assertRaisesRegex(ValueError, "must remain blocked"):
                MODULE.render(root)

        def wrong_human_requirement(source: dict[str, Any], root: Path) -> None:
            set_machine_dispositions(source, root)
            passed, blocked = source["requirement_status_groups"]
            blocked["requirement_ids"].remove("REQ-077")
            passed["requirement_ids"].append("REQ-077")

        def wrong_human_task(source: dict[str, Any], root: Path) -> None:
            set_machine_dispositions(source, root)
            accepted, blocked = source["task_status_groups"]
            blocked["task_ids"].remove("E3-01")
            accepted["task_ids"].append("E3-01")

        for mutation, message in (
            (wrong_human_requirement, "REQ-077"),
            (wrong_human_task, "E3-01"),
        ):
            with self.subTest(mutation=mutation.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                materialize_root(
                    root,
                    manifest,
                    status,
                    mutate_source=mutation,
                )
                with self.assertRaisesRegex(ValueError, message):
                    MODULE.render(root)

    def test_accepted_terminal_must_be_unique_completed_and_snapshot_bound(self) -> None:
        manifest, status = release_controls("machine_release_candidate", finalized=False)

        def missing_terminal(source: dict[str, Any], root: Path) -> None:
            set_machine_dispositions(source, root)
            source["task_status_groups"][0].pop("terminal_activity_ids")

        def duplicate_terminal(source: dict[str, Any], root: Path) -> None:
            set_machine_dispositions(source, root)
            terminal = source["task_status_groups"][0]["terminal_activity_ids"][0]
            source["task_status_groups"][0]["terminal_activity_ids"] = [
                terminal,
                terminal,
            ]

        for mutation in (missing_terminal, duplicate_terminal):
            with self.subTest(mutation=mutation.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                materialize_root(root, manifest, status, mutate_source=mutation)
                with self.assertRaisesRegex(ValueError, "no unique terminal"):
                    MODULE.render(root)

        def mutate_terminal(field: str, value: Any) -> Callable[[list[dict[str, Any]]], None]:
            def mutation(rows: list[dict[str, Any]]) -> None:
                terminal = next(
                    row
                    for row in rows
                    if row["activity_id"] == "ACT-F2-CLEAN-ROOM-RC-TERMINAL-001"
                )
                if field == "source_snapshots":
                    terminal[field] = value
                elif field == "validation":
                    terminal[field]["capture_status"] = value
                elif field == "request_status":
                    terminal["source_request_usage"]["status"] = value
                    terminal["source_request_usage"]["attempts"] = 1
                elif field == "output_state":
                    terminal["outputs"][0]["state"] = value
                else:
                    terminal[field] = value

            return mutation

        cases = (
            (mutate_terminal("status", "in_progress"), "unsatisfied"),
            (mutate_terminal("source_snapshots", ["other"]), "not bound"),
            (mutate_terminal("validation", "pending"), "unsatisfied"),
            (mutate_terminal("request_status", "checkpoint"), "unresolved request"),
            (mutate_terminal("output_state", "pending"), "unsatisfied"),
        )
        for activity_mutation, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                materialize_root(
                    root,
                    manifest,
                    status,
                    mutate_source=set_machine_dispositions,
                    mutate_activities=activity_mutation,
                )
                with self.assertRaisesRegex(ValueError, message):
                    MODULE.render(root)

    def test_full_programme_requires_every_requirement_and_task(self) -> None:
        manifest, status = release_controls("full_programme", finalized=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            materialize_root(
                root,
                manifest,
                status,
                mutate_source=set_full_programme_dispositions,
            )
            documents = MODULE.render(root)
            requirements = json.loads(documents[root / MODULE.OUTPUT_RELATIVES["requirements"]])
            tasks = json.loads(documents[root / MODULE.OUTPUT_RELATIVES["tasks"]])
            self.assertEqual("release", requirements["release_state"])
            self.assertEqual(95, requirements["counts"]["passed"])
            self.assertEqual(36, tasks["counts"]["accepted"])

    def test_finalized_machine_requires_publication_terminal_mapping(self) -> None:
        manifest, status = release_controls("machine_release_candidate", finalized=True)

        def stale_candidate_mapping(source: dict[str, Any], root: Path) -> None:
            set_machine_dispositions(source, root)
            source["milestone"] = "machine_release_finalized"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            materialize_root(
                root,
                manifest,
                status,
                mutate_source=stale_candidate_mapping,
            )
            with self.assertRaisesRegex(ValueError, "terminal coverage differs"):
                MODULE.render(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            materialize_root(
                root,
                manifest,
                status,
                mutate_source=set_finalized_machine_dispositions,
            )
            documents = MODULE.render(root)
            requirements = json.loads(
                documents[root / MODULE.OUTPUT_RELATIVES["requirements"]]
            )
            tasks = json.loads(documents[root / MODULE.OUTPUT_RELATIVES["tasks"]])
            self.assertEqual("release", requirements["release_state"])
            self.assertEqual(90, requirements["counts"]["passed"])
            self.assertEqual(32, tasks["counts"]["accepted"])
            accepted = next(
                row for row in tasks["tasks"] if row["implementation_status"] == "accepted"
            )
            self.assertEqual(
                selected_terminal_ids(finalized=True),
                accepted["terminal_activity_ids"],
            )

    def test_terminal_ledger_requires_schema_and_exact_hash_chain(self) -> None:
        manifest, status = release_controls("machine_release_candidate", finalized=False)
        for mode in ("schema", "chain"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                materialize_root(
                    root,
                    manifest,
                    status,
                    mutate_source=set_machine_dispositions,
                )
                ledger = root / MODULE.ACTIVITY_LEDGER_RELATIVE
                rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
                if mode == "schema":
                    rows[-1]["unexpected"] = True
                else:
                    rows[-1]["previous_entry_sha256"] = "f" * 64
                ledger.write_text(
                    "".join(
                        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                        + "\n"
                        for row in rows
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "invalid activity ledger"):
                    MODULE.render(root)

    def test_publication_readiness_source_must_name_exact_controls(self) -> None:
        manifest, status = release_controls("fixture")

        def mutate(source: dict[str, Any], _root: Path) -> None:
            source["publication_readiness_source"] = ["governance/implementation-status-source.json"]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            materialize_root(root, manifest, status, mutate_source=mutate)
            with self.assertRaisesRegex(ValueError, "exact release controls"):
                MODULE.render(root)


if __name__ == "__main__":
    unittest.main()
