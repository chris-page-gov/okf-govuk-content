from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_release", ROOT / "scripts" / "check_release.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def refresh_checksums(root: Path) -> None:
    bundle = root / "bundle"
    rows = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.name in {"checksums.json", ".DS_Store"}:
            continue
        payload = path.read_bytes()
        rows.append(
            {
                "bytes": len(payload),
                "path": path.relative_to(bundle).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    write_json(
        bundle / "checksums.json",
        {"algorithm": "sha256", "file_count": len(rows), "files": rows, "schema": "okf-checksums.v1"},
    )


def clean_room_input_manifest(
    root: Path,
    reproduction: dict[str, object],
    *,
    staged_manifest_sha256: str,
) -> dict[str, object]:
    components = [
        {
            "path": relative,
            **MODULE._content_summary(root / relative, "test clean-room input"),
        }
        for relative in MODULE.CLEAN_ROOM_INPUT_PATHS
    ]
    source_binding = reproduction["source_binding"]
    assert isinstance(source_binding, dict)
    manifest_index = MODULE.CLEAN_ROOM_INPUT_PATHS.index("release/manifest.yaml")
    manifest_bytes = int(components[manifest_index]["bytes"])
    components[manifest_index] = {
        "path": "release/manifest.yaml",
        **MODULE._single_file_summary(
            "manifest.yaml",
            manifest_bytes,
            staged_manifest_sha256,
        ),
    }
    components.append(
        {
            "path": "frozen_source",
            "source": reproduction["source"],
            **{
                key: source_binding[key]
                for key in ("file_count", "bytes", "tree_sha256")
            },
        }
    )
    canonical = json.dumps(
        components, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return {
        "schema": "afhf-govuk-okf-reproduction-input-manifest.v1",
        "components": components,
        "component_count": len(components),
        "tree_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def make_release(root: Path) -> None:
    release_id = "T1-20260712-closing"
    counts = {"datasets": 3, "records": 3, "relationships": 0, "resources": 0, "publishers": 1}
    (root / "semantic").mkdir(parents=True, exist_ok=True)
    (root / "uv.lock").write_text("locked Python dependencies\n", encoding="utf-8")
    (root / "semantic/package-lock.json").write_text("locked Node dependencies\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("project metadata\n", encoding="utf-8")
    for relative in (
        "provenance/activity-ledger.jsonl",
        "provenance/activity-ledger.schema.json",
        "provenance/reproduction-declarations.json",
        "provenance/source-request-budget.json",
        "scripts/check_provenance.py",
        "governance/launch-manifest.yaml",
        "orchestration/models.lock.yaml",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == "provenance/activity-ledger.jsonl":
            path.write_text("", encoding="utf-8")
        elif relative == "provenance/activity-ledger.schema.json":
            path.write_text(
                (ROOT / relative).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        else:
            path.write_text(f"test input {relative}\n", encoding="utf-8")
    for relative in MODULE.CLEAN_ROOM_INPUT_PATHS:
        path = root / relative
        if path.exists():
            continue
        template = ROOT / relative
        if template.is_dir():
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"test input {relative}\n", encoding="utf-8")
    write_json(
        root / "bundle/okf-explorer.json",
        {
            "counts": counts,
            "entrypoints": {"data_manifest": "data/manifest.json"},
            "kind": "okf-large-corpus",
            "schema": "okf-explorer-large-corpus.v1",
        },
    )
    write_json(root / "bundle/data/manifest.json", {"counts": counts, "snapshot": release_id})
    (root / "bundle/data/records.txt").write_text("three records\n", encoding="utf-8")
    refresh_checksums(root)
    frozen_source = root / "corpus/records/T1/source-records.jsonl.gz"
    frozen_source.parent.mkdir(parents=True, exist_ok=True)
    frozen_source.write_bytes(b"frozen full-corpus source\n")
    reproduction = {
        "source": "corpus/records/T1/source-records.jsonl.gz",
        "generated_at": "2026-07-12T23:59:59Z",
        "compiler": "disk",
        "source_binding": MODULE._source_binding(frozen_source, root),
    }

    write_json(
        root / "corpus/reconciliation/closing.json",
        {
            "alias_of_represented": 0,
            "entity_class_counts": {"content_identity": 3},
            "exceptioned": 0,
            "expected_candidate_keys": 3,
            "organisations_proof": {
                "closed": True,
                "reported_total": 2,
                "returned_rows": 2,
                "unique_urls": 2,
            },
            "publication_records": 3,
            "redirect_only": 0,
            "represented": 3,
            "sampled": False,
            "search_partition_proofs": [
                {
                    "canonical_overlap_with_prior_partitions": 0,
                    "expected": 3,
                    "partition": "guidance",
                    "sibling_disjoint": True,
                    "passes": [
                        {
                            "canonical_alias_rows": 0,
                            "canonical_url_sha256": "a" * 64,
                            "closed": True,
                            "identity_sha256": "b" * 64,
                            "order": "public_timestamp",
                            "returned_rows": 3,
                            "unique_source_rows": 3,
                            "unique_urls": 3,
                        },
                        {
                            "canonical_alias_rows": 0,
                            "canonical_url_sha256": "a" * 64,
                            "closed": True,
                            "identity_sha256": "b" * 64,
                            "order": "-public_timestamp",
                            "returned_rows": 3,
                            "unique_source_rows": 3,
                            "unique_urls": 3,
                        },
                    ],
                }
            ],
            "search_partitions_closed": True,
            "sitemap_byte_stable": True,
            "snapshot": release_id,
            "tombstone_only": 0,
            "unexplained_omissions": 0,
        },
    )
    write_json(
        root / "release/citation-verification.json",
        {"citation_verification_passed": True, "snapshot": release_id},
    )
    write_json(
        root / "release/semantic-validation.json",
        {"semantic_validation_passed": True, "snapshot": release_id},
    )
    write_json(
        root / "release/question-contract-validation.json",
        {"question_contract_passed": True, "snapshot": release_id},
    )
    sbom = {
        "$schema": "https://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "bom-ref": "urn:test:application",
                "type": "application",
                "name": "test-application",
                "version": "1",
            },
            "properties": [
                {
                    "name": "govuk-okf:lock:uv.sha256",
                    "value": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
                },
                {
                    "name": "govuk-okf:lock:semantic-package-lock.sha256",
                    "value": hashlib.sha256((root / "semantic/package-lock.json").read_bytes()).hexdigest(),
                },
                {
                    "name": "govuk-okf:input:pyproject.sha256",
                    "value": hashlib.sha256((root / "pyproject.toml").read_bytes()).hexdigest(),
                },
            ],
        },
        "components": [
            {
                "bom-ref": "pkg:generic/test-dependency@1",
                "type": "library",
                "name": "test-dependency",
                "version": "1",
            }
        ],
        "dependencies": [
            {"ref": "urn:test:application", "dependsOn": ["pkg:generic/test-dependency@1"]},
            {"ref": "pkg:generic/test-dependency@1", "dependsOn": []},
        ],
    }
    write_json(root / "release/sbom.cdx.json", sbom)
    write_json(
        root / "release/clean-room-reproduction.json",
        {
            "checkout": {"unchanged": True},
            "clean_room_reproduction_passed": True,
            "fixture_reproduction_passed": True,
            "inputs": clean_room_input_manifest(
                root,
                reproduction,
                staged_manifest_sha256="1" * 64,
            ),
            "network": {
                "external_model_requests": 0,
                "official_source_requests": 0,
                "required": False,
            },
            "outputs": {
                "bundle": {
                    "exact_match": True,
                    "expected": MODULE._content_summary(root / "bundle", "test bundle"),
                },
                "sbom": {
                    "exact_match": True,
                    "expected_sha256": hashlib.sha256(
                        (root / "release/sbom.cdx.json").read_bytes()
                    ).hexdigest(),
                },
            },
            "release_inputs_passed": True,
            "source": reproduction["source"],
            "source_binding": reproduction["source_binding"],
            "generated_at": reproduction["generated_at"],
            "compiler": reproduction["compiler"],
            "release_control": {
                "manifest_kind": "full_corpus_checkpoint",
                "requested_release_kind": "machine_release_candidate",
                "prospective": True,
                "manifest_sha256": "1" * 64,
                "status_sha256": "2" * 64,
                "source_binding": reproduction["source_binding"],
            },
            "sampled": False,
            "schema": "afhf-govuk-okf-clean-room-reproduction.v1",
            "snapshot": release_id,
            "snapshot_kind": "full_corpus",
            "test_evidence": {"scope": "full_repository", "tests_passed": True},
            "validators": {"passed": True},
        },
    )
    flags = {
        "aim_assessment_passed": True,
        "browser_validation_passed": True,
        "checksum_validation_passed": True,
        "citation_verification_passed": True,
        "clean_room_reproduction_passed": True,
        "evaluation_passed": True,
        "full_corpus_reconciled": True,
        "full_repository_tests_passed": True,
        "provenance_validation_passed": True,
        "question_contract_passed": True,
        "rights_privacy_audit_passed": True,
        "sbom_validation_passed": True,
        "security_scan_passed": True,
        "semantic_validation_passed": True,
    }
    write_json(
        root / "release/manifest.yaml",
        {
            "artifacts": {
                "aim_assessment": "release/aim-assessment.json",
                "browser_validation": "release/accessibility-browser.json",
                "bundle": "bundle",
                "checksums": "bundle/checksums.json",
                "citation_verification": "release/citation-verification.json",
                "clean_room_reproduction": "release/clean-room-reproduction.json",
                "descriptor": "bundle/okf-explorer.json",
                "evaluation": "evaluation/results/status.json",
                "full_repository_tests": "release/full-repository-tests.json",
                "provenance_validation": "release/provenance-validation.json",
                "question_contract": "questions/release-v2/verification-report.json",
                "reconciliation": "corpus/reconciliation/closing.json",
                "rights_privacy_audit": "release/rights-privacy-audit.json",
                "sbom": "release/sbom.cdx.json",
                "security_scan": "release/security-scan.json",
                "semantic_validation": "release/semantic-validation.json",
                "status": "release/status.json",
            },
            "counts": {"publication_records": 3},
            "gates": flags,
            "publication_ready": True,
            "release_id": release_id,
            "release_kind": "machine_release_candidate",
            "schema": "afhf-govuk-okf-release-manifest.v1",
            "snapshot": {"id": release_id, "kind": "full_corpus", "sampled": False},
            "promotion": {
                "schema": "afhf-govuk-okf-two-stage-promotion.v1",
                "from": "full_corpus_checkpoint",
                "staged_manifest_sha256": "1" * 64,
                "staged_status_sha256": "2" * 64,
                "reproduction": reproduction,
                "finalized": False,
            },
        },
    )
    write_json(
        root / "release/status.json",
        {
            **flags,
            "agent_evaluation_status": "completed",
            "aims_assessed": True,
            "completion_statement": MODULE.MACHINE_MARKER,
            "full_evaluation_complete": False,
            "human_evaluation_status": "not_authorised",
            "human_ui_of_choice_status": "not_yet_testable",
            "machine_rc_complete": True,
            "programme_complete": False,
            "promotion_finalized": False,
            "publication_ready": True,
            "reason": MODULE.MACHINE_CANDIDATE_REASON,
            "release_id": release_id,
            "schema": "afhf-govuk-okf-release-status.v1",
            "status": "machine_release_candidate",
            "unexplained_omissions": 0,
        },
    )
    write_json(
        root / "release/rights-privacy-audit.json",
        {
            "mechanical_controls_passed": True,
            "rights_privacy_audit_passed": True,
            "schema": "afhf-govuk-okf-rights-privacy-audit.v1",
            "snapshot": release_id,
        },
    )
    question_root = root / "questions/release-v2"
    write_json(
        question_root / "verification-report.json",
        {
            "schema_version": 2,
            "snapshot_id": release_id,
            "question_contract_passed": True,
            "machine_validations_passed": True,
            "publication_ready_candidate": True,
            "artifact_tier": "release_verified",
            "counts": {"questions": 28_800, "validation_errors": 0},
            "verification_ledger": {"count": 28_800, "verified": 28_800, "failed": 0},
        },
    )
    write_json(
        root / "evaluation/results/status.json",
        {
            "mode": "release",
            "snapshot_id": release_id,
            "questions": 28_800,
            "systems": 10,
            "outcomes": 288_000,
            "all_questions_all_systems_complete": True,
            "release_question_contract_passed": True,
            "serialization_invariance": {"passed": True},
            "agent_evaluation_status": "completed",
            "machine_evaluation_complete": True,
            "release_eligible": True,
            "human_evaluation_status": "not_authorised",
            "human_ui_of_choice_status": "not_yet_testable",
        },
    )
    write_json(
        root / "release/accessibility-browser.json",
        {
            "schema": "govuk-okf-explorer-browser-evidence.v1",
            "snapshot": release_id,
            "artifact_tier": "full_release_snapshot",
            "publication_ready": True,
            "overall_status": "automated_full_release_evidence_pass",
            "accessibility": {"pass": True},
            "routing_and_data": {"pass": True},
            "performance": {"pass": True},
            "full_release_gates": {"full_corpus_browser_measurement": "passed"},
            "console_exceptions": [],
        },
    )
    security_report = root / "reports/security.md"
    security_report.parent.mkdir(parents=True, exist_ok=True)
    security_report.write_text("# Passing security scan\n", encoding="utf-8")
    write_json(
        root / "release/security-scan.json",
        {
            "schema": "afhf-govuk-okf-security-scan.v1",
            "snapshot": release_id,
            "scope": "full_release_repository",
            "status": "completed",
            "security_scan_passed": True,
            "scan_id": "scan-test-1",
            "scanned_commit": "a" * 40,
            "code_tree": {
                "paths": list(MODULE.SECURITY_SCAN_INPUT_PATHS),
                "sha256": MODULE._tree_sha256(root, MODULE.SECURITY_SCAN_INPUT_PATHS),
            },
            "findings": {"critical_open": 0, "high_open": 0},
            "report": {
                "path": "reports/security.md",
                "sha256": hashlib.sha256(security_report.read_bytes()).hexdigest(),
            },
        },
    )
    write_json(
        root / "release/provenance-validation.json",
        {
            "schema": "afhf-govuk-okf-provenance-validation.v1",
            "snapshot": release_id,
            "validation_mode": "candidate",
            "validation_tier": "candidate",
            "candidate_mode": True,
            "release_mode": False,
            "provenance_validation_passed": True,
            "candidate_requirements_satisfied": True,
            "release_requirements_satisfied": False,
            "publication_workflow_status": "pending_post_publication",
            "validation_errors": [],
            "candidate_blockers": [],
            "release_blockers": ["publication terminal pending"],
            "hash_chain": {
                "passed": True,
                "ledger_sha256": hashlib.sha256(
                    (root / "provenance/activity-ledger.jsonl").read_bytes()
                ).hexdigest(),
                "last_entry_sha256": "a" * 64,
                "hash_chained_v2_rows": 11,
            },
            "source_request_budget": {
                "status": "final",
                "final_shared_request_ceiling": 1000,
                "final_shared_request_count": 500,
                "included_in_model_cost": False,
            },
            "external_paid_model_usage": {
                "api_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_gbp": 0.0,
                "totals_complete": True,
            },
            "fallbacks": {"count": 4},
            "source_access_restrictions": {"count": 4},
            "required_terminal_events": {
                "required": 11,
                "satisfied": 10,
                "all_satisfied": False,
                "candidate_required": 10,
                "candidate_satisfied": 10,
                "candidate_all_satisfied": True,
                "pending_post_publication_terminal_activity_id": "ACT-F2-PUBLICATION-REGISTRY-TERMINAL-001",
            },
            "candidate_terminal_events": {
                "required": 10,
                "satisfied": 10,
                "all_satisfied": True,
                "excluded_post_publication_terminal_activity_id": "ACT-F2-PUBLICATION-REGISTRY-TERMINAL-001",
            },
            "unresolved_activity_status": {
                "unresolved_pending_final_activity_ids": [],
                "unresolved_in_progress_activity_ids": [],
            },
            "inputs": {
                "activity_ledger_sha256": hashlib.sha256(
                    (root / "provenance/activity-ledger.jsonl").read_bytes()
                ).hexdigest(),
                "activity_schema_sha256": hashlib.sha256(
                    (root / "provenance/activity-ledger.schema.json").read_bytes()
                ).hexdigest(),
                "declarations_sha256": hashlib.sha256(
                    (root / "provenance/reproduction-declarations.json").read_bytes()
                ).hexdigest(),
                "source_request_budget_sha256": hashlib.sha256(
                    (root / "provenance/source-request-budget.json").read_bytes()
                ).hexdigest(),
                "validator_sha256": hashlib.sha256(
                    (root / "scripts/check_provenance.py").read_bytes()
                ).hexdigest(),
                "launch_manifest_sha256": hashlib.sha256(
                    (root / "governance/launch-manifest.yaml").read_bytes()
                ).hexdigest(),
                "model_lock_sha256": hashlib.sha256(
                    (root / "orchestration/models.lock.yaml").read_bytes()
                ).hexdigest(),
            },
        },
    )
    write_json(
        root / "release/full-repository-tests.json",
        {
            "schema": "afhf-govuk-okf-full-repository-tests.v1",
            "snapshot": release_id,
            "scope": "full_repository",
            "passed": True,
            "python_tests_run": 1,
            "commands": [{"returncode": 0}, {"returncode": 0}, {"returncode": 0}],
            "code_tree": {
                "paths": list(MODULE.TEST_INPUT_PATHS),
                "sha256": MODULE._tree_sha256(root),
            },
        },
    )
    clean_path = root / "release/clean-room-reproduction.json"
    clean = json.loads(clean_path.read_text(encoding="utf-8"))
    clean["test_evidence"]["sha256"] = hashlib.sha256(
        (root / "release/full-repository-tests.json").read_bytes()
    ).hexdigest()
    write_json(clean_path, clean)
    write_json(
        root / "release/aim-assessment.json",
        {
            "schema": "afhf-govuk-okf-aim-assessment.v1",
            "assessment_tier": "machine_release_candidate",
            "snapshot": {"release_id": release_id, "kind": "full_corpus", "sampled": False},
            "coverage": {"aims": 9, "requirements": 95, "controlling_clauses": 21},
            "gate_11": {"passed": True, "status": "passed", "unmet_check_ids": []},
            "aims": [
                {
                    "aim_id": f"AIM-{index:03d}",
                    "status": "partly_fulfilled",
                    "confidence": {"level": "high"},
                    "negative_findings": [{"text": "bounded limitation retained"}],
                    "evidence": [{"path": "release/status.json", "sha256": "0" * 64}],
                }
                for index in range(1, 10)
            ],
        },
    )


def mutate(path: Path, key: str, value: object) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    document[key] = value
    write_json(path, document)


class ReleaseGateTests(unittest.TestCase):
    def test_repository_release_documents_are_internally_consistent(self) -> None:
        self.assertEqual([], MODULE.validate_release(ROOT))
        status = json.loads((ROOT / "release/status.json").read_text(encoding="utf-8"))
        errors = MODULE.validate_release(ROOT, require_publication_ready=True)
        if status["publication_ready"]:
            self.assertEqual([], errors)
        else:
            self.assertTrue(any("publication_ready is false" in error for error in errors))

    def test_complete_machine_candidate_passes_with_human_claim_untested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            self.assertEqual([], MODULE.validate_release(root, require_publication_ready=True))

    def test_disallowed_snapshot_labels_fail_closed(self) -> None:
        for marker in ("fixture", "sample", "capacity"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                make_release(root)
                manifest_path = root / "release/manifest.yaml"
                status_path = root / "release/status.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["release_id"] = f"{marker}-20260712"
                manifest["snapshot"]["id"] = manifest["release_id"]
                write_json(manifest_path, manifest)
                mutate(status_path, "release_id", manifest["release_id"])
                errors = MODULE.validate_release(root, require_publication_ready=True)
                self.assertTrue(any("cannot be published" in error for error in errors), errors)

    def test_every_corpus_closure_proof_is_required(self) -> None:
        cases = (
            ("sampled", True, "sampled"),
            ("search_partitions_closed", False, "Search API partitions"),
            ("sitemap_byte_stable", False, "sitemap byte-stability"),
            ("unexplained_omissions", 1, "unexplained_omissions"),
        )
        for field, value, expected_error in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                make_release(root)
                mutate(root / "corpus/reconciliation/closing.json", field, value)
                errors = MODULE.validate_release(root, require_publication_ready=True)
                self.assertTrue(any(expected_error in error for error in errors), errors)

    def test_organisation_proof_and_publication_counts_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            reconciliation_path = root / "corpus/reconciliation/closing.json"
            reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))
            reconciliation["organisations_proof"]["closed"] = False
            reconciliation["publication_records"] = 2
            write_json(reconciliation_path, reconciliation)
            errors = MODULE.validate_release(root, require_publication_ready=True)
            self.assertTrue(any("organisations enumeration proof" in error for error in errors), errors)
            self.assertTrue(any("publication-record counts differ" in error for error in errors), errors)

    def test_search_opposing_identity_and_sibling_overlap_proofs_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            reconciliation_path = root / "corpus/reconciliation/closing.json"
            reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))
            proof = reconciliation["search_partition_proofs"][0]
            proof["passes"][1]["identity_sha256"] = "c" * 64
            proof["sibling_disjoint"] = False
            proof.pop("canonical_overlap_with_prior_partitions")
            write_json(reconciliation_path, reconciliation)
            errors = MODULE.validate_release(root, require_publication_ready=True)
            self.assertTrue(any("opposing source-identity hashes differ" in error for error in errors), errors)
            self.assertTrue(any("sibling source-identity disjointness" in error for error in errors), errors)
            self.assertTrue(any("canonical-route overlap accounting" in error for error in errors), errors)

    def test_release_flags_and_checksum_evidence_are_not_self_asserting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            mutate(root / "questions/release-v2/verification-report.json", "question_contract_passed", False)
            (root / "bundle/data/records.txt").write_text("tampered\n", encoding="utf-8")
            errors = MODULE.validate_release(root, require_publication_ready=True)
            self.assertTrue(any("question-v2 evidence" in error for error in errors), errors)
            self.assertTrue(any("checksum differs" in error for error in errors), errors)

    def test_rights_privacy_evidence_is_required_for_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            mutate(root / "release/rights-privacy-audit.json", "rights_privacy_audit_passed", False)
            errors = MODULE.validate_release(root, require_publication_ready=True)
            self.assertTrue(any("rights_privacy_audit evidence" in error for error in errors), errors)

    def test_new_promotion_gates_require_structural_evidence_not_status_booleans(self) -> None:
        cases = (
            ("evaluation/results/status.json", "outcomes", 1, "28,800 by 10"),
            ("release/accessibility-browser.json", "artifact_tier", "representative_fixture", "full-release browser"),
            ("release/provenance-validation.json", "provenance_validation_passed", False, "provenance validation"),
            ("release/full-repository-tests.json", "python_tests_run", 0, "full-repository test evidence"),
        )
        for relative, key, value, expected in cases:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                make_release(root)
                mutate(root / relative, key, value)
                errors = MODULE.validate_release(root, require_publication_ready=True)
                self.assertTrue(any(expected in error for error in errors), errors)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            security_path = root / "release/security-scan.json"
            security = json.loads(security_path.read_text(encoding="utf-8"))
            security["findings"]["high_open"] = 1
            write_json(security_path, security)
            errors = MODULE.validate_release(root, require_publication_ready=True)
            self.assertTrue(any("critical or high" in error for error in errors), errors)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            clean_path = root / "release/clean-room-reproduction.json"
            clean = json.loads(clean_path.read_text(encoding="utf-8"))
            clean["release_control"]["manifest_sha256"] = "f" * 64
            write_json(clean_path, clean)
            errors = MODULE.validate_release(root, require_publication_ready=True)
            self.assertTrue(any("exact staged release manifest" in error for error in errors), errors)

    def test_security_scan_is_commit_and_current_code_tree_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            security_path = root / "release/security-scan.json"
            security = json.loads(security_path.read_text(encoding="utf-8"))
            security["scanned_commit"] = "not-a-full-git-commit"
            write_json(security_path, security)

            errors = MODULE.validate_release(root, require_publication_ready=True)
            self.assertTrue(
                any("security scan has no valid scanned commit" in error for error in errors),
                errors,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            stale_security = json.loads(
                (root / "release/security-scan.json").read_text(encoding="utf-8")
            )
            post_scan_change = root / "tests/post-scan-change.py"
            post_scan_change.parent.mkdir(parents=True, exist_ok=True)
            post_scan_change.write_text(
                "# changed after the retained security scan\n", encoding="utf-8"
            )

            # Model legitimate tests being rerun after the code change while the
            # older security-scan evidence is incorrectly retained.
            tests_path = root / "release/full-repository-tests.json"
            tests = json.loads(tests_path.read_text(encoding="utf-8"))
            tests["code_tree"]["sha256"] = MODULE._tree_sha256(root)
            write_json(tests_path, tests)
            clean_path = root / "release/clean-room-reproduction.json"
            clean = json.loads(clean_path.read_text(encoding="utf-8"))
            clean["test_evidence"]["sha256"] = hashlib.sha256(
                tests_path.read_bytes()
            ).hexdigest()
            write_json(clean_path, clean)

            self.assertNotEqual(
                stale_security["code_tree"]["sha256"],
                MODULE._tree_sha256(root, MODULE.SECURITY_SCAN_INPUT_PATHS),
            )
            errors = MODULE.validate_release(root, require_publication_ready=True)
            self.assertTrue(
                any(
                    "security scan is not bound to current code, automation and tests" in error
                    for error in errors
                ),
                errors,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            changed_workflow = root / ".github/workflows/release.yml"
            changed_workflow.parent.mkdir(parents=True, exist_ok=True)
            changed_workflow.write_text(
                "# changed publication authority after the retained scan\n",
                encoding="utf-8",
            )

            errors = MODULE.validate_release(root, require_publication_ready=True)
            self.assertTrue(
                any(
                    "security scan is not bound to current code, automation and tests" in error
                    for error in errors
                ),
                errors,
            )

    def test_sbom_and_clean_room_evidence_are_bound_to_release_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            clean_path = root / "release/clean-room-reproduction.json"
            clean = json.loads(clean_path.read_text(encoding="utf-8"))
            clean["outputs"]["bundle"]["exact_match"] = False
            write_json(clean_path, clean)
            (root / "uv.lock").write_text("changed dependency lock\n", encoding="utf-8")
            errors = MODULE.validate_release(root, require_publication_ready=True)
            self.assertTrue(any("SBOM lock digest differs" in error for error in errors), errors)
            self.assertTrue(any("reproduced bundle did not exactly match" in error for error in errors), errors)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            (root / "bundle/data/records.txt").write_text(
                "three records, altered after clean-room verification\n", encoding="utf-8"
            )
            refresh_checksums(root)
            (root / "semantic/context/post-scan.jsonld").write_text(
                '{"changed":true}\n', encoding="utf-8"
            )
            errors = MODULE.validate_release(root, require_publication_ready=True)
            self.assertTrue(
                any("current released bundle tree" in error for error in errors), errors
            )
            self.assertTrue(
                any(
                    "clean-room immutable input tree differs: semantic/context" in error
                    for error in errors
                ),
                errors,
            )

    def test_human_ui_claim_stays_untested_without_human_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            mutate(root / "release/status.json", "human_ui_of_choice_status", "fulfilled")
            errors = MODULE.validate_release(root, require_publication_ready=True)
            self.assertTrue(any("must remain not_yet_testable" in error for error in errors), errors)

    def test_finalized_release_requires_strict_post_publication_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_release(root)
            errors = MODULE.validate_release(root, require_finalized=True)
            self.assertTrue(any("not finalized" in error for error in errors), errors)
            self.assertTrue(any("strict 11-of-11" in error for error in errors), errors)

            manifest_path = root / "release/manifest.yaml"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            candidate_manifest_sha = MODULE._file_sha256(manifest_path)
            candidate_status_sha = MODULE._file_sha256(root / "release/status.json")
            manifest["promotion"]["finalized"] = True
            manifest["promotion"]["candidate_manifest_sha256"] = candidate_manifest_sha
            manifest["promotion"]["candidate_status_sha256"] = candidate_status_sha
            write_json(manifest_path, manifest)
            mutate(root / "release/status.json", "promotion_finalized", True)
            mutate(root / "release/status.json", "reason", MODULE.MACHINE_FINAL_REASON)
            provenance_path = root / "release/provenance-validation.json"
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            provenance.update(
                {
                    "validation_mode": "release",
                    "validation_tier": "release",
                    "candidate_mode": False,
                    "release_mode": True,
                    "release_requirements_satisfied": True,
                    "publication_workflow_status": "completed",
                    "release_blockers": [],
                }
            )
            provenance["required_terminal_events"].update(
                {"required": 11, "satisfied": 11, "all_satisfied": True}
            )
            write_json(provenance_path, provenance)
            self.assertEqual([], MODULE.validate_release(root, require_finalized=True))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["counts"]["publication_records"] = 4
            write_json(manifest_path, manifest)
            errors = MODULE.validate_release(root, require_finalized=True)
            self.assertTrue(
                any("candidate-manifest hash differs" in error for error in errors), errors
            )


if __name__ == "__main__":
    unittest.main()
