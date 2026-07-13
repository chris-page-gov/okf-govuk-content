from __future__ import annotations

import importlib.util
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.test_release_gate import make_release, write_json


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("promote_release", ROOT / "scripts/promote_release.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def prepare_staged(root: Path) -> str:
    make_release(root)
    snapshot = "T1-20260712-closing"
    source = root / "corpus/records/T1-20260712-closing/source-records.jsonl.gz"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"frozen full-corpus source\n")
    write_json(
        root / "corpus/records/T1/rights-audit-manifest.json",
        {"snapshot": snapshot, "metadata_only": True, "complete_page_bodies_retained": False},
    )
    MODULE.stage_release(
        root,
        snapshot=snapshot,
        reconciliation_relative="corpus/reconciliation/closing.json",
        source_relative="corpus/records/T1-20260712-closing/source-records.jsonl.gz",
        generated_at="2026-07-12T23:59:59Z",
    )
    return snapshot


def existing_provenance(root: Path, snapshot: str) -> dict[str, object]:
    value = json.loads((root / "release/provenance-validation.json").read_text(encoding="utf-8"))
    value["snapshot"] = snapshot
    ledger_path = root / "provenance/activity-ledger.jsonl"
    lines = [line for line in ledger_path.read_text(encoding="utf-8").splitlines() if line]
    value["hash_chain"].update(
        {
            "ledger_sha256": hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
            "last_entry_sha256": hashlib.sha256(lines[-1].encode("utf-8")).hexdigest(),
            "hash_chained_v2_rows": len(lines),
        }
    )
    value["inputs"]["activity_ledger_sha256"] = hashlib.sha256(
        ledger_path.read_bytes()
    ).hexdigest()
    return value


def post_clean_provenance(root: Path, snapshot: str) -> dict[str, object]:
    rows = [
        json.loads(line)
        for line in (root / "provenance/activity-ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    if [row["activity_id"] for row in rows] != [MODULE.CLEAN_ROOM_TERMINAL_ID]:
        raise AssertionError("candidate provenance was built before the clean-room terminal")
    return existing_provenance(root, snapshot)


def existing_tests(root: Path, snapshot: str) -> dict[str, object]:
    value = json.loads((root / "release/full-repository-tests.json").read_text(encoding="utf-8"))
    value["snapshot"] = snapshot
    value["tests_passed"] = True
    value["code_tree"]["sha256"] = MODULE.check_release._tree_sha256(root)
    return value


def finalized_provenance(root: Path, snapshot: str) -> dict[str, object]:
    value = existing_provenance(root, snapshot)
    value.update(
        {
            "validation_mode": "release",
            "validation_tier": "release",
            "candidate_mode": False,
            "release_mode": True,
            "candidate_requirements_satisfied": True,
            "release_requirements_satisfied": True,
            "publication_workflow_status": "completed",
            "candidate_blockers": [],
            "release_blockers": [],
        }
    )
    value["required_terminal_events"].update(
        {"required": 11, "satisfied": 11, "all_satisfied": True}
    )
    return value


def fake_aim_renderer(root: Path) -> dict[Path, str]:
    assessment = json.loads((root / "release/aim-assessment.json").read_text(encoding="utf-8"))
    assessment["test_transition_bindings"] = {
        "manifest_sha256": hashlib.sha256(
            (root / "release/manifest.yaml").read_bytes()
        ).hexdigest(),
        "rights_sha256": hashlib.sha256(
            (root / "release/rights-privacy-audit.json").read_bytes()
        ).hexdigest(),
    }
    return {
        root / "release/aim-assessment.json": json.dumps(assessment, indent=2, sort_keys=True) + "\n",
        root / "reports/aim-scorecard.md": "# Test aim scorecard\n",
    }


def assert_transition_bindings_current(test: unittest.TestCase, root: Path) -> None:
    manifest_path = root / "release/manifest.yaml"
    rights_path = root / "release/rights-privacy-audit.json"
    rights = json.loads(rights_path.read_text(encoding="utf-8"))
    assessment = json.loads((root / "release/aim-assessment.json").read_text(encoding="utf-8"))
    test.assertEqual(
        hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        rights["snapshot_binding"]["release_manifest"]["sha256"],
    )
    test.assertEqual(
        {
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "rights_sha256": hashlib.sha256(rights_path.read_bytes()).hexdigest(),
        },
        assessment["test_transition_bindings"],
    )


def refreshed_rights(root: Path, snapshot: str) -> dict[str, object]:
    manifest_path = root / "release/manifest.yaml"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    publication_path = root / "bundle/data/manifest.json"
    corpus_path = root / "corpus/records/T1/rights-audit-manifest.json"
    transition = manifest.get("promotion") or manifest.get("promotion_contract")
    reproduction = transition["reproduction"]

    def bound(path: Path) -> dict[str, object]:
        return {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }

    return {
        "schema": "afhf-govuk-okf-rights-privacy-audit.v1",
        "snapshot": snapshot,
        "snapshot_kind": "full_corpus",
        "sampled": False,
        "generated_at": "2026-07-12T23:59:59Z",
        "status": "passed",
        "rights_privacy_audit_passed": True,
        "release_eligible": True,
        "mechanical_controls_passed": True,
        "audit_input_contract": {
            "schema": "afhf-govuk-okf-rights-audit-inputs.v1",
            "generated_at": "2026-07-12T23:59:59Z",
            "publication_manifest": bound(publication_path),
            "corpus_manifests": [bound(corpus_path)],
            "review_ledger": None,
        },
        "snapshot_binding": {
            "release_manifest": {
                "path": "release/manifest.yaml",
                "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            },
            "publication_manifest": {
                "path": "bundle/data/manifest.json",
                "sha256": hashlib.sha256(publication_path.read_bytes()).hexdigest(),
            },
            "publication_asset_set_sha256": "a" * 64,
            "corpus_manifest_count": 1,
            "resolved_corpus_record_manifest_count": 1,
            "corpus_asset_set_sha256": "b" * 64,
            "frozen_source": reproduction["source_binding"],
            "full_unsampled_snapshot": True,
            "corpus_snapshot_bound": True,
        },
        "review": {"provided": False, "review_count": 0},
        "errors": [],
        "remaining_release_blockers": [],
    }


def prospective_clean_room(
    root: Path,
    snapshot: str,
    staged_manifest: dict[str, object],
    tests: dict[str, object],
) -> dict[str, object]:
    manifest_path = root / "release/manifest.yaml"
    status_path = root / "release/status.json"
    tests_path = root / "release/full-repository-tests.json"
    on_disk_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    on_disk_tests = json.loads(tests_path.read_text(encoding="utf-8"))
    if on_disk_manifest["release_kind"] != "full_corpus_checkpoint":
        raise AssertionError("clean room did not run against the staged checkpoint")
    if on_disk_manifest != staged_manifest or on_disk_tests != tests:
        raise AssertionError("prospective clean-room inputs differ from staged controls")
    reproduction = staged_manifest["promotion_contract"]["reproduction"]
    evidence = json.loads((root / "release/clean-room-reproduction.json").read_text(encoding="utf-8"))
    input_components = evidence["inputs"]["components"]
    source_binding = reproduction["source_binding"]
    input_components[-1] = {
        "path": "frozen_source",
        "source": reproduction["source"],
        **{
            key: source_binding[key]
            for key in ("file_count", "bytes", "tree_sha256")
        },
    }
    manifest_index = MODULE.check_release.CLEAN_ROOM_INPUT_PATHS.index(
        "release/manifest.yaml"
    )
    input_components[manifest_index] = {
        "path": "release/manifest.yaml",
        **MODULE.check_release._content_summary(
            manifest_path,
            "staged release manifest",
        ),
    }
    evidence["inputs"]["tree_sha256"] = hashlib.sha256(
        json.dumps(
            input_components,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    evidence.update(
        {
            "snapshot": snapshot,
            "snapshot_kind": "full_corpus",
            "sampled": False,
            "release_kind": "machine_release_candidate",
            "source": reproduction["source"],
            "source_binding": reproduction["source_binding"],
            "generated_at": reproduction["generated_at"],
            "compiler": reproduction["compiler"],
            "release_inputs_passed": True,
            "clean_room_reproduction_passed": True,
            "release_control": {
                "manifest_kind": "full_corpus_checkpoint",
                "requested_release_kind": "machine_release_candidate",
                "prospective": True,
                "manifest_sha256": MODULE.sha256(manifest_path),
                "status_sha256": MODULE.sha256(status_path),
                "source_binding": reproduction["source_binding"],
            },
            "test_evidence": {
                "path": "release/full-repository-tests.json",
                "sha256": hashlib.sha256(tests_path.read_bytes()).hexdigest(),
                "scope": "full_repository",
                "tests_passed": True,
            },
        }
    )
    return evidence


def prepare_clean_room_crash_checkpoint(root: Path) -> tuple[str, dict[str, object]]:
    snapshot = prepare_staged(root)
    staged_manifest = json.loads((root / "release/manifest.yaml").read_text(encoding="utf-8"))
    tests = existing_tests(root, snapshot)
    write_json(root / "release/full-repository-tests.json", tests)
    clean = prospective_clean_room(root, snapshot, staged_manifest, tests)
    write_json(root / "release/clean-room-reproduction.json", clean)
    terminal = MODULE.build_clean_room_terminal(
        root,
        snapshot,
        clean,
        tests,
        MODULE.sha256(root / "release/manifest.yaml"),
        MODULE.sha256(root / "release/status.json"),
        root / "release/clean-room-reproduction.json",
        root / "release/full-repository-tests.json",
        root / "release/sbom.cdx.json",
    )
    MODULE.append_clean_room_terminal(root, terminal)
    return snapshot, staged_manifest


def write_partial_candidate(
    root: Path, staged_manifest: dict[str, object], *, include_status: bool
) -> None:
    promotion = {
        "schema": "afhf-govuk-okf-two-stage-promotion.v1",
        "from": "full_corpus_checkpoint",
        "staged_manifest_sha256": MODULE.sha256(root / "release/manifest.yaml"),
        "staged_status_sha256": MODULE.sha256(root / "release/status.json"),
        "reproduction": staged_manifest["promotion_contract"]["reproduction"],
        "finalized": False,
    }
    candidate = dict(staged_manifest)
    candidate.update(
        {
            "release_kind": "machine_release_candidate",
            "publication_ready": True,
            "gates": {gate: True for gate in MODULE.ALL_GATES},
            "promotion": promotion,
        }
    )
    candidate.pop("promotion_contract", None)
    write_json(root / "release/manifest.yaml", candidate)
    if include_status:
        write_json(root / "release/status.json", MODULE.machine_candidate_status(candidate["release_id"]))


def append_publication_terminal(root: Path, snapshot: str) -> None:
    manifest = json.loads((root / "release/manifest.yaml").read_text(encoding="utf-8"))
    clean = json.loads(
        (root / "release/clean-room-reproduction.json").read_text(encoding="utf-8")
    )
    tests = json.loads(
        (root / "release/full-repository-tests.json").read_text(encoding="utf-8")
    )
    terminal = MODULE.build_clean_room_terminal(
        root,
        snapshot,
        clean,
        tests,
        manifest["promotion"]["staged_manifest_sha256"],
        manifest["promotion"]["staged_status_sha256"],
        root / "release/clean-room-reproduction.json",
        root / "release/full-repository-tests.json",
        root / "release/sbom.cdx.json",
    )
    terminal["activity_id"] = "ACT-F2-PUBLICATION-REGISTRY-TERMINAL-001"
    terminal["agent"]["role"] = "publication and registry terminal recorder"
    MODULE.append_activity.append_entries(
        [terminal],
        root / "provenance/activity-ledger.jsonl",
        root / "provenance/activity-ledger.schema.json",
    )


def write_partial_final(root: Path, *, include_status: bool) -> None:
    manifest_path = root / "release/manifest.yaml"
    status_path = root / "release/status.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    promotion = dict(manifest["promotion"])
    promotion.update(
        {
            "finalized": True,
            "candidate_manifest_sha256": MODULE.sha256(manifest_path),
            "candidate_status_sha256": MODULE.sha256(status_path),
        }
    )
    manifest["promotion"] = promotion
    write_json(manifest_path, manifest)
    if include_status:
        status = MODULE.machine_candidate_status(manifest["release_id"])
        status["promotion_finalized"] = True
        status["reason"] = MODULE.check_release.MACHINE_FINAL_REASON
        write_json(status_path, status)


class ReleasePromotionTests(unittest.TestCase):
    def test_finalization_accepts_only_hash_bound_archived_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = prepare_staged(root)
            MODULE.promote_release(
                root,
                provenance_builder=post_clean_provenance,
                rights_builder=refreshed_rights,
                test_builder=existing_tests,
                clean_room_builder=prospective_clean_room,
                aim_renderer=fake_aim_renderer,
            )
            manifest = json.loads((root / "release/manifest.yaml").read_text(encoding="utf-8"))
            (root / manifest["promotion"]["reproduction"]["source"]).unlink()
            rights = json.loads(
                (root / "release/rights-privacy-audit.json").read_text(encoding="utf-8")
            )
            (root / rights["audit_input_contract"]["corpus_manifests"][0]["path"]).unlink()

            result = MODULE.finalize_release(
                root,
                provenance_builder=finalized_provenance,
                aim_renderer=fake_aim_renderer,
            )
            self.assertTrue(result["manifest"]["promotion"]["finalized"])
            final_rights = json.loads(
                (root / "release/rights-privacy-audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                "static_archived_input_validation",
                final_rights["release_binding_refresh"]["mode"],
            )

    def test_rights_builder_rebinds_archived_inputs_for_candidate_and_final(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            rights_path = root / "release/rights-privacy-audit.json"
            rights = json.loads(rights_path.read_text(encoding="utf-8"))
            corpus_path = root / rights["audit_input_contract"]["corpus_manifests"][0]["path"]
            corpus_path.unlink()

            candidate = MODULE.build_rights_evidence(root, "T1-20260712-closing")
            self.assertEqual(
                "static_archived_input_validation",
                candidate["release_binding_refresh"]["mode"],
            )
            write_json(rights_path, candidate)

            manifest_path = root / "release/manifest.yaml"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["promotion"]["finalized"] = True
            manifest["promotion"]["candidate_manifest_sha256"] = "a" * 64
            manifest["promotion"]["candidate_status_sha256"] = "b" * 64
            write_json(manifest_path, manifest)
            final = MODULE.build_rights_evidence(root, "T1-20260712-closing")
            self.assertEqual(
                hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                final["snapshot_binding"]["release_manifest"]["sha256"],
            )

            publication_path = root / "bundle/data/manifest.json"
            publication_path.write_bytes(publication_path.read_bytes() + b" ")
            write_json(rights_path, final)
            with self.assertRaisesRegex(MODULE.PromotionError, "publication manifest"):
                MODULE.build_rights_evidence(root, "T1-20260712-closing")

    def test_full_test_evidence_accepts_valid_full_candidate_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            observed: list[list[str]] = []

            def passing_runner(command: list[str], candidate_root: Path) -> dict[str, object]:
                observed.append(command)
                self.assertEqual(
                    [],
                    MODULE.check_release.validate_release(
                        candidate_root,
                        require_publication_ready=True,
                    ),
                )
                output = "Ran 999 tests in 1.0s\n\nOK\n" if "unittest" in command else "ok\n"
                return {
                    "command": command,
                    "returncode": 0,
                    "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
                    "output_tail": output,
                }

            evidence = MODULE.build_test_evidence(
                root,
                "T1-20260712-closing",
                command_runner=passing_runner,
            )
            self.assertTrue(evidence["tests_passed"])
            self.assertEqual(999, evidence["python_tests_run"])
            self.assertEqual(3, len(observed))

    def test_partial_finalization_crash_states_resume_and_completed_final_is_idempotent(self) -> None:
        for include_status in (False, True):
            with self.subTest(include_status=include_status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                snapshot = prepare_staged(root)
                MODULE.promote_release(
                    root,
                    provenance_builder=post_clean_provenance,
                    rights_builder=refreshed_rights,
                    test_builder=existing_tests,
                    clean_room_builder=prospective_clean_room,
                    aim_renderer=fake_aim_renderer,
                )
                write_partial_final(root, include_status=include_status)
                result = MODULE.finalize_release(
                    root,
                    provenance_builder=finalized_provenance,
                    rights_builder=refreshed_rights,
                    aim_renderer=fake_aim_renderer,
                )
                self.assertTrue(result["manifest"]["promotion"]["finalized"])
                self.assertTrue(result["status"]["promotion_finalized"])
                assert_transition_bindings_current(self, root)
                replay = MODULE.finalize_release(
                    root,
                    provenance_builder=lambda *_: (_ for _ in ()).throw(
                        AssertionError("completed finalization should be idempotent")
                    ),
                    aim_renderer=fake_aim_renderer,
                )
                self.assertEqual(snapshot, replay["manifest"]["release_id"])

    def test_partial_candidate_crash_states_resume_without_duplicate_terminal(self) -> None:
        for include_status in (False, True):
            with self.subTest(include_status=include_status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                snapshot, staged_manifest = prepare_clean_room_crash_checkpoint(root)
                write_partial_candidate(root, staged_manifest, include_status=include_status)
                result = MODULE.promote_release(
                    root,
                    provenance_builder=post_clean_provenance,
                    rights_builder=refreshed_rights,
                    test_builder=lambda *_: (_ for _ in ()).throw(
                        AssertionError("prepared tests should be reused")
                    ),
                    clean_room_builder=lambda *_: (_ for _ in ()).throw(
                        AssertionError("prepared clean room should be reused")
                    ),
                    aim_renderer=fake_aim_renderer,
                )
                rows = [
                    json.loads(line)
                    for line in (root / "provenance/activity-ledger.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if line
                ]
                self.assertEqual([MODULE.CLEAN_ROOM_TERMINAL_ID], [row["activity_id"] for row in rows])
                self.assertEqual(snapshot, result["status"]["release_id"])
                self.assertEqual(
                    [], MODULE.check_release.validate_release(root, require_publication_ready=True)
                )

    def test_conflicting_prepared_terminal_fails_closed_with_recovery_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_clean_room_crash_checkpoint(root)
            clean_path = root / "release/clean-room-reproduction.json"
            clean = json.loads(clean_path.read_text(encoding="utf-8"))
            clean["generated_at"] = "2026-07-13T00:00:00Z"
            write_json(clean_path, clean)
            with self.assertRaisesRegex(MODULE.PromotionError, "prepared clean-room recovery"):
                MODULE.promote_release(
                    root,
                    provenance_builder=post_clean_provenance,
                    rights_builder=refreshed_rights,
                    test_builder=existing_tests,
                    clean_room_builder=prospective_clean_room,
                    aim_renderer=fake_aim_renderer,
                )

    def test_stage_is_full_corpus_but_explicitly_non_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = prepare_staged(root)
            manifest = json.loads((root / "release/manifest.yaml").read_text(encoding="utf-8"))
            status = json.loads((root / "release/status.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot, manifest["release_id"])
            self.assertEqual("full_corpus_checkpoint", manifest["release_kind"])
            self.assertFalse(manifest["publication_ready"])
            self.assertTrue(manifest["gates"]["full_corpus_reconciled"])
            self.assertFalse(manifest["gates"]["clean_room_reproduction_passed"])
            self.assertEqual(
                "corpus/records/T1-20260712-closing/source-records.jsonl.gz",
                manifest["promotion_contract"]["reproduction"]["source"],
            )
            self.assertEqual(
                "machine_release_candidate",
                manifest["promotion_contract"]["target_release_kind"],
            )
            self.assertEqual("checkpoint", status["status"])
            self.assertEqual("not_authorised", status["human_evaluation_status"])
            self.assertEqual("not_yet_testable", status["human_ui_of_choice_status"])

    def test_promote_derives_machine_rc_and_preserves_human_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = prepare_staged(root)
            self.assertEqual("", (root / "provenance/activity-ledger.jsonl").read_text(encoding="utf-8"))
            result = MODULE.promote_release(
                root,
                provenance_builder=post_clean_provenance,
                rights_builder=refreshed_rights,
                test_builder=existing_tests,
                clean_room_builder=prospective_clean_room,
                aim_renderer=fake_aim_renderer,
            )
            self.assertEqual("machine_release_candidate", result["manifest"]["release_kind"])
            self.assertTrue(result["manifest"]["publication_ready"])
            self.assertTrue(all(result["manifest"]["gates"].values()))
            self.assertEqual(MODULE.check_release.MACHINE_MARKER, result["status"]["completion_statement"])
            self.assertEqual("not_authorised", result["status"]["human_evaluation_status"])
            self.assertEqual("not_yet_testable", result["status"]["human_ui_of_choice_status"])
            self.assertFalse(result["status"]["programme_complete"])
            assert_transition_bindings_current(self, root)
            self.assertEqual([], MODULE.check_release.validate_release(root, require_publication_ready=True))
            self.assertEqual(snapshot, result["status"]["release_id"])
            terminal = json.loads(
                (root / "provenance/activity-ledger.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(MODULE.CLEAN_ROOM_TERMINAL_ID, terminal["activity_id"])
            self.assertEqual("completed", terminal["status"])
            output_hashes = {row["path"]: row["sha256"] for row in terminal["outputs"]}
            self.assertEqual(
                hashlib.sha256((root / "release/clean-room-reproduction.json").read_bytes()).hexdigest(),
                output_hashes["release/clean-room-reproduction.json"],
            )
            reproduction = result["manifest"]["promotion"]["reproduction"]
            passed, failures, _, replay_control = MODULE.reproduce_release._release_inputs_pass(
                release_kind="machine_release_candidate",
                snapshot=snapshot,
                snapshot_kind="full_corpus",
                sampled=False,
                source=root / reproduction["source"],
                generated_at=reproduction["generated_at"],
                compiler=reproduction["compiler"],
                release_manifest=root / "release/manifest.yaml",
                test_evidence=root / "release/full-repository-tests.json",
            )
            self.assertTrue(passed, failures)
            self.assertTrue(replay_control["prospective"])
            self.assertEqual(
                result["manifest"]["promotion"]["staged_manifest_sha256"],
                replay_control["manifest_sha256"],
            )
            clean_path = root / "release/clean-room-reproduction.json"
            original_clean = json.loads(clean_path.read_text(encoding="utf-8"))
            changed_clean = dict(original_clean)
            changed_clean["reason"] = "post-terminal metadata tamper"
            write_json(clean_path, changed_clean)
            errors = MODULE.check_release.validate_release(
                root, require_publication_ready=True
            )
            self.assertTrue(
                any("terminal output hashes differ" in error for error in errors), errors
            )
            write_json(clean_path, original_clean)
            (root / reproduction["source"]).write_bytes(b"tampered frozen source\n")
            errors = MODULE.check_release.validate_release(root, require_publication_ready=True)
            self.assertTrue(
                any("frozen reproduction source content/tree binding differs" in error for error in errors),
                errors,
            )

    def test_final_check_failure_rolls_back_every_transaction_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_staged(root)
            paths = [
                root / "release/manifest.yaml",
                root / "release/status.json",
                root / "release/provenance-validation.json",
                root / "release/full-repository-tests.json",
                root / "release/clean-room-reproduction.json",
                root / "release/rights-privacy-audit.json",
                root / "provenance/activity-ledger.jsonl",
                root / "release/aim-assessment.json",
                root / "reports/aim-scorecard.md",
            ]
            before = {path: path.read_bytes() if path.is_file() else None for path in paths}
            with self.assertRaisesRegex(MODULE.PromotionError, "forced final failure"):
                MODULE.promote_release(
                    root,
                    provenance_builder=post_clean_provenance,
                    rights_builder=refreshed_rights,
                    test_builder=existing_tests,
                    clean_room_builder=prospective_clean_room,
                    aim_renderer=fake_aim_renderer,
                    validator=lambda candidate_root, publication: ["forced final failure"] if publication else [],
                )
            after = {path: path.read_bytes() if path.is_file() else None for path in paths}
            self.assertEqual(before, after)

    def test_finalize_requires_external_terminal_and_then_enforces_strict_eleven_of_eleven(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_staged(root)
            MODULE.promote_release(
                root,
                provenance_builder=post_clean_provenance,
                rights_builder=refreshed_rights,
                test_builder=existing_tests,
                clean_room_builder=prospective_clean_room,
                aim_renderer=fake_aim_renderer,
            )
            candidate_manifest = (root / "release/manifest.yaml").read_bytes()
            with self.assertRaisesRegex(MODULE.PromotionError, "strict post-publication provenance"):
                MODULE.finalize_release(
                    root,
                    provenance_builder=post_clean_provenance,
                    rights_builder=refreshed_rights,
                    aim_renderer=fake_aim_renderer,
                )
            self.assertEqual(candidate_manifest, (root / "release/manifest.yaml").read_bytes())
            append_publication_terminal(root, "T1-20260712-closing")
            stale_errors = MODULE.check_release.validate_release(
                root, require_publication_ready=True
            )
            self.assertTrue(
                any("provenance input hash differs" in error for error in stale_errors),
                stale_errors,
            )
            result = MODULE.finalize_release(
                root,
                provenance_builder=finalized_provenance,
                rights_builder=refreshed_rights,
                aim_renderer=fake_aim_renderer,
            )
            self.assertTrue(result["manifest"]["promotion"]["finalized"])
            self.assertTrue(result["status"]["promotion_finalized"])
            assert_transition_bindings_current(self, root)
            self.assertEqual(
                [],
                MODULE.check_release.validate_release(
                    root, require_publication_ready=True, require_finalized=True
                ),
            )

    def test_missing_security_evidence_fails_before_manifest_or_status_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_staged(root)
            (root / "release/security-scan.json").unlink()
            before = (root / "release/manifest.yaml").read_bytes(), (root / "release/status.json").read_bytes()
            with self.assertRaises(MODULE.PromotionError):
                MODULE.promote_release(
                    root,
                    provenance_builder=existing_provenance,
                    rights_builder=refreshed_rights,
                    test_builder=existing_tests,
                    clean_room_builder=prospective_clean_room,
                    aim_renderer=fake_aim_renderer,
                )
            self.assertEqual(before, ((root / "release/manifest.yaml").read_bytes(), (root / "release/status.json").read_bytes()))

    def test_stage_rejects_sample_labels_and_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            source = root / "corpus/records/T1/source-records.jsonl.gz"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"frozen full-corpus source\n")
            with self.assertRaisesRegex(MODULE.PromotionError, "rejects fixture"):
                MODULE.stage_release(
                    root,
                    snapshot="sample-T1",
                    reconciliation_relative="corpus/reconciliation/closing.json",
                    source_relative="corpus/records/T1/source-records.jsonl.gz",
                    generated_at="2026-07-12T23:59:59Z",
                )
            reconciliation_path = root / "corpus/reconciliation/closing.json"
            reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))
            reconciliation["publication_records"] = 2
            write_json(reconciliation_path, reconciliation)
            with self.assertRaisesRegex(MODULE.PromotionError, "publication record counts differ"):
                MODULE.stage_release(
                    root,
                    snapshot="T1-20260712-closing",
                    reconciliation_relative="corpus/reconciliation/closing.json",
                    source_relative="corpus/records/T1/source-records.jsonl.gz",
                    generated_at="2026-07-12T23:59:59Z",
                )

    def test_stage_rejects_detached_standard_shard_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            source_root = root / "corpus/records/T1/source-records-deadbeef"
            source_root.mkdir(parents=True)
            (source_root / "index.json").write_text(
                json.dumps({"schema": "govuk-okf-jsonl-shards.v1", "shards": []}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(MODULE.PromotionError, "containing directory"):
                MODULE.stage_release(
                    root,
                    snapshot="T1-20260712-closing",
                    reconciliation_relative="corpus/reconciliation/closing.json",
                    source_relative="corpus/records/T1/source-records-deadbeef/index.json",
                    generated_at="2026-07-12T23:59:59Z",
                )


if __name__ == "__main__":
    unittest.main()
