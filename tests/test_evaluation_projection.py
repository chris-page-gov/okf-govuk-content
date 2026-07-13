from __future__ import annotations

import gzip
import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.evaluation import (  # noqa: E402
    BundleIndex,
    HARNESS_VERSION,
    InputContract,
    OutcomeStore,
    SYSTEMS,
    aggregate_metrics,
    canonical_json,
    failure_analysis,
    grade_result,
    make_trace,
    materialise_traces,
    output_manifest,
    paired_comparisons,
    serialization_invariance,
    slice_analysis,
    write_report,
)
from govuk_okf import evaluation_projection as projection_module  # noqa: E402
from govuk_okf.evaluation_projection import (  # noqa: E402
    PROJECTED_FILES,
    EvaluationProjectionError,
    project_release_results,
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EvaluationProjectionTests(unittest.TestCase):
    question_count = 2

    def setUp(self) -> None:
        outcomes = self.question_count * len(SYSTEMS)
        patches = (
            mock.patch.object(projection_module, "RELEASE_QUESTION_COUNT", self.question_count),
            mock.patch.object(projection_module, "RELEASE_OUTCOME_COUNT", outcomes),
            mock.patch.object(
                projection_module,
                "verify_question_inputs",
                side_effect=self.fake_verify_question_inputs,
            ),
            mock.patch.object(
                projection_module,
                "verify_bundle_inputs",
                side_effect=self.fake_verify_bundle_inputs,
            ),
            mock.patch.object(
                projection_module,
                "iter_questions",
                side_effect=self.fake_iter_questions,
            ),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def question_rows(self) -> list[dict[str, object]]:
        content_id = "00000000-0000-4000-8000-000000000001"
        return [
            {
                "question_id": f"q-{number:05d}",
                "checksum": hashlib.sha256(f"q-{number:05d}".encode("utf-8")).hexdigest(),
                "wording": f'Where is the official GOV.UK item “Alpha service” for release test {number}?',
                "persona_ids": ["persona-release-test"],
                "story_id": f"story-{number:05d}",
                "story_role": "known-item",
                "operation": "locate_known_item",
                "challenge": "direct",
                "risk": "low",
                "difficulty": "easy",
                "locale": "en",
                "jurisdiction": ["United Kingdom"],
                "split": "held_out",
                "split_group": f"group-{number:05d}",
                "discovery_stage": "metadata_only_discovery",
                "expected_unanswerable": False,
                "target_relationships": [],
                "gold": {
                    "classification": "answerable",
                    "content_ids": [content_id],
                    "expected_paths": [],
                    "primary_targets": [
                        {
                            "content_id": content_id,
                            "identity": f"content:{content_id}:en",
                            "source_evidence_sha256": "a" * 64,
                            "source_evidence_url": "https://content-api.publishing.service.gov.uk/content/alpha",
                            "title": "Alpha service",
                            "url": "https://www.gov.uk/alpha",
                        }
                    ],
                    "snapshot_id": "T1-closed",
                    "unanswerable_rationale": "",
                    "urls": ["https://www.gov.uk/alpha"],
                },
            }
            for number in range(self.question_count)
        ]

    def fake_verify_question_inputs(
        self, questions: Path, mode: str
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        self.assertEqual("release", mode)
        manifest = json.loads((questions / "manifest.json").read_text(encoding="utf-8"))
        return manifest, {"snapshot": {"snapshot_id": "T1-closed"}}, {
            "question_contract_passed": True
        }

    def fake_verify_bundle_inputs(
        self, bundle: Path
    ) -> tuple[dict[str, object], dict[str, object]]:
        manifest = json.loads((bundle / "data/manifest.json").read_text(encoding="utf-8"))
        descriptor = json.loads((bundle / "okf-explorer.json").read_text(encoding="utf-8"))
        return descriptor, manifest

    def fake_iter_questions(self, _questions: Path):
        yield from self.question_rows()

    def project(
        self,
        *,
        root: Path,
        run: Path,
        output: Path,
        source_reference: str = "evaluation/agent-runs/release-v0.1.0",
    ) -> dict[str, object]:
        return project_release_results(
            run=run,
            questions=root / "questions/release-v2",
            bundle=root / "bundle",
            output=output,
            source_reference=source_reference,
            repository_root=root,
        )

    def reseal_run(self, run: Path) -> None:
        entries = [
            {"path": path.relative_to(run).as_posix(), "bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in sorted(run.rglob("*"))
            if path.is_file() and path.name not in {"manifest.json", "checksums.txt"}
        ]
        manifest_path = run / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"] = entries
        manifest["root_sha256"] = hashlib.sha256(
            "".join(f"{item['path']}\0{item['sha256']}\n" for item in entries).encode("utf-8")
        ).hexdigest()
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        checksum_paths = sorted(
            path for path in run.rglob("*") if path.is_file() and path.name != "checksums.txt"
        )
        (run / "checksums.txt").write_text(
            "".join(f"{sha256(path)}  {path.relative_to(run).as_posix()}\n" for path in checksum_paths),
            encoding="utf-8",
        )

    def rewrite_traces(self, run: Path, mutate) -> None:
        trace_manifest_path = run / "trace-manifest.json"
        trace_manifest = json.loads(trace_manifest_path.read_text(encoding="utf-8"))
        records: list[dict[str, object]] = []
        for shard in trace_manifest["shards"]:
            path = run / shard["path"]
            with gzip.open(path, "rb") as stream:
                records.extend(json.loads(line) for line in stream if line)
        mutate(records)
        payload = b"".join((canonical_json(trace) + "\n").encode("utf-8") for trace in records)
        shard = trace_manifest["shards"][0]
        trace_path = run / shard["path"]
        for extra in trace_manifest["shards"][1:]:
            (run / extra["path"]).unlink()
        with trace_path.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as stream:
                stream.write(payload)
        canonical_sha = hashlib.sha256(payload).hexdigest()
        shard.update(
            {
                "records": len(records),
                "bytes": trace_path.stat().st_size,
                "file_sha256": sha256(trace_path),
                "canonical_sha256": canonical_sha,
                "first_key": records[0]["question"]["question_id"],
                "last_key": records[-1]["question"]["question_id"],
            }
        )
        trace_manifest["shards"] = [shard]
        trace_manifest["records"] = len(records)
        trace_manifest["max_records_per_shard"] = len(records)
        trace_manifest["root_sha256"] = hashlib.sha256(
            f"{shard['path']}\0{canonical_sha}\n".encode("utf-8")
        ).hexdigest()
        trace_manifest_path.write_text(
            json.dumps(trace_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def rebuild_aggregate_artifacts_from_traces(self, run: Path) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outcomes = OutcomeStore(Path(directory) / "outcomes.sqlite", "fabricated-reseal")
            try:
                manifest = json.loads((run / "trace-manifest.json").read_text(encoding="utf-8"))
                for shard in manifest["shards"]:
                    with gzip.open(run / shard["path"], "rb") as stream:
                        for line in stream:
                            if line:
                                outcomes.add(json.loads(line))
                outcomes.commit()
                metrics = aggregate_metrics(outcomes.connection)
                documents = {
                    "metrics.json": metrics,
                    "paired-comparisons.json": paired_comparisons(outcomes.connection),
                    "slices.json": slice_analysis(outcomes.connection),
                    "failure-analysis.json": failure_analysis(outcomes.connection),
                }
            finally:
                outcomes.close()
        for relative, document in documents.items():
            (run / relative).write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        status = json.loads((run / "status.json").read_text(encoding="utf-8"))
        write_report(
            run / "report.md",
            run_id=status["run_id"],
            mode="release",
            metrics=metrics,
            status=status,
        )

    def build_release_run(self, root: Path) -> Path:
        questions = root / "questions/release-v2"
        questions.mkdir(parents=True)
        (questions / "manifest.json").write_text(
            json.dumps(
                {"snapshot_id": "T1-closed", "counts": {"questions": self.question_count}},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        bundle = root / "bundle"
        (bundle / "data").mkdir(parents=True)
        content_id = "00000000-0000-4000-8000-000000000001"
        records = [
            {
                "@id": "https://www.gov.uk/alpha",
                "canonical_content_id": content_id,
                "confidence": "source-declared",
                "description": "Alpha service guidance.",
                "document_type": "guidance",
                "evidence_sha256": "a" * 64,
                "evidence_url": "https://content-api.publishing.service.gov.uk/content/alpha",
                "jurisdiction": ["United Kingdom"],
                "language": "en",
                "lifecycle": "published",
                "open": "dataset/alpha-en",
                "publisher_title": "Department Alpha",
                "schema_name": "guidance",
                "source_memberships": ["search-v1"],
                "tags": ["alpha", "service"],
                "title": "Alpha service",
                "url": "https://www.gov.uk/alpha",
            }
        ]
        with (bundle / "data/records-0.json.gz").open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as stream:
                stream.write((canonical_json(records) + "\n").encode("utf-8"))
        bundle_manifest = {
            "chunks": {"datasets": ["data/records-0.json.gz"], "relationships": []},
            "snapshot": "T1-closed",
            "counts": {"datasets": 1, "relationships": 0},
        }
        (bundle / "data/manifest.json").write_text(
            json.dumps(bundle_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        bundle_descriptor = {
            "schema": "okf-explorer-large-corpus.v1",
            "counts": bundle_manifest["counts"],
            "entrypoints": {"data_manifest": "data/manifest.json"},
        }
        (bundle / "okf-explorer.json").write_text(
            json.dumps(bundle_descriptor, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        question_manifest_sha256 = sha256(questions / "manifest.json")
        bundle_manifest_sha256 = sha256(bundle / "data/manifest.json")
        run = root / "evaluation/agent-runs/release-v0.1.0"
        run.mkdir(parents=True)
        outcome_count = self.question_count * len(SYSTEMS)
        contract = InputContract(
            mode="release",
            question_manifest={"snapshot_id": "T1-closed"},
            question_contract={"snapshot": {"snapshot_id": "T1-closed"}},
            verification_report={"question_contract_passed": True},
            bundle_manifest=bundle_manifest,
            bundle_descriptor=bundle_descriptor,
            question_manifest_sha256=question_manifest_sha256,
            bundle_manifest_sha256=bundle_manifest_sha256,
            snapshot_id="T1-closed",
            expected_questions=self.question_count,
            release_question_contract_passed=True,
            git_sha="deadbeef",
            git_dirty=False,
            python_version="3.12.0",
            sqlite_version="3.49.0",
        )
        with tempfile.TemporaryDirectory(dir=root) as working:
            working_root = Path(working)
            index = BundleIndex(bundle, working_root / "index.sqlite", contract)
            outcomes = OutcomeStore(working_root / "outcomes.sqlite", "release-test")
            try:
                for question in self.question_rows():
                    outcomes.register_question(question)
                    for system in SYSTEMS:
                        search = index.search(system, str(question["wording"]))
                        metrics, failures, gold = grade_result(question, search, index)
                        outcomes.add(
                            make_trace(
                                run_id="release-v0.1.0",
                                system=system,
                                question=question,
                                search=search,
                                metrics=metrics,
                                failures=failures,
                                gold=gold,
                                contract=contract,
                            )
                        )
                outcomes.commit()
                metrics = aggregate_metrics(outcomes.connection)
                paired = paired_comparisons(outcomes.connection)
                slices = slice_analysis(outcomes.connection)
                failures = failure_analysis(outcomes.connection)
                invariance = serialization_invariance(outcomes.connection)
                traces = materialise_traces(outcomes.connection, run, outcome_count)
            finally:
                outcomes.close()
                index.close()
        status = {
            "schema_version": 1,
            "run_id": "release-v0.1.0",
            "mode": "release",
            "snapshot_id": "T1-closed",
            "questions": self.question_count,
            "systems": len(SYSTEMS),
            "outcomes": outcome_count,
            "all_questions_all_systems_complete": True,
            "release_question_contract_passed": True,
            "serialization_invariance": invariance,
            "model_usage": {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_gbp": 0.0},
            "network_requests": 0,
            "agent_evaluation_status": "completed",
            "human_evaluation_status": "not_authorised",
            "human_ui_of_choice_status": "not_yet_testable",
            "machine_evaluation_complete": True,
            "full_evaluation_complete": False,
            "programme_complete": False,
            "release_eligible": True,
            "claim_boundary": (
                "Machine results cover metadata discovery, retrieval ranking, typed relationships, "
                "citation/provenance and abstention. No human preference or body-content answering claim is made."
            ),
        }
        json_documents = {
            "failure-analysis.json": failures,
            "metrics.json": metrics,
            "paired-comparisons.json": paired,
            "slices.json": slices,
            "status.json": status,
            "usage.json": {
                "schema_version": 1,
                "harness_version": HARNESS_VERSION,
                "execution": "deterministic local Python and SQLite/FTS5",
                "runtime": {
                    "git_sha": "deadbeef",
                    "git_dirty": False,
                    "python_version": "3.12.0",
                    "sqlite_version": "3.49.0",
                },
                "model_usage": status["model_usage"],
                "source_access": {
                    "mode": "frozen local bundle and independently verified question assets",
                    "network_requests": 0,
                    "restrictions": [
                        "No GOV.UK page body is fetched or retained.",
                        "No external search, authenticated source, model provider or paid API is contacted.",
                    ],
                },
                "licensing_and_fair_use_triggers": [
                    "Evaluation traces retain public metadata identifiers, titles, URLs and short evidence fields only.",
                    "Attachment and page bodies are not copied into traces.",
                ],
                "fallbacks_used": [
                    "SQLite FTS5 supplies the reproducible lexical baseline; unavailable dense, live Search API, GOV.UK Chat and internal GovGraph systems remain non-run comparators.",
                    "Normal paired cluster intervals are used without a third-party statistics dependency.",
                ],
                "wall_seconds": 1.0,
                "new_outcomes_this_invocation": outcome_count,
            },
        }
        for name, value in json_documents.items():
            (run / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        write_report(
            run / "report.md",
            run_id="release-v0.1.0",
            mode="release",
            metrics=metrics,
            status=status,
        )
        output_manifest(
            run,
            {
                "run_id": "release-v0.1.0",
                "mode": "release",
                "snapshot_id": "T1-closed",
                "questions": self.question_count,
                "systems": len(SYSTEMS),
                "outcomes": outcome_count,
                "trace_records": traces["records"],
                "question_manifest_sha256": question_manifest_sha256,
                "bundle_manifest_sha256": bundle_manifest_sha256,
                "system_contract_sha256": hashlib.sha256(
                    canonical_json([asdict(system) for system in SYSTEMS]).encode("utf-8")
                ).hexdigest(),
                "git_sha": "deadbeef",
                "git_dirty": False,
                "python_version": "3.12.0",
                "sqlite_version": "3.49.0",
                "release_eligible": True,
            },
        )
        return run

    def test_fabricated_empty_trace_manifest_fails_even_when_outer_run_is_resealed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.build_release_run(root)
            for path in (run / "traces").iterdir():
                path.unlink()
            (run / "traces").rmdir()
            trace_manifest_path = run / "trace-manifest.json"
            trace_manifest = json.loads(trace_manifest_path.read_text(encoding="utf-8"))
            trace_manifest["shards"] = []
            trace_manifest_path.write_text(
                json.dumps(trace_manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.reseal_run(run)

            with self.assertRaisesRegex(EvaluationProjectionError, "does not enumerate"):
                self.project(
                    root=root,
                    run=run,
                    output=root / "evaluation/results",
                )

    def test_resealed_skeletal_trace_fails_complete_make_trace_contract(self) -> None:
        for field in ("gold", "dimensions", "metrics", "efficiency"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run = self.build_release_run(root)

                def remove_required_structure(records):
                    records[0].pop(field)

                self.rewrite_traces(run, remove_required_structure)
                self.reseal_run(run)
                with self.assertRaisesRegex(EvaluationProjectionError, "complete make_trace structure"):
                    self.project(
                        root=root,
                        run=run,
                        output=root / "evaluation/results",
                    )

    def test_resealed_fabricated_trace_and_aggregates_fail_independent_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.build_release_run(root)

            def fabricate_metric(records):
                observed = float(records[0]["metrics"]["end_task_success"])
                records[0]["metrics"]["end_task_success"] = 0.0 if observed else 1.0

            self.rewrite_traces(run, fabricate_metric)
            self.rebuild_aggregate_artifacts_from_traces(run)
            self.reseal_run(run)
            with self.assertRaisesRegex(EvaluationProjectionError, "independent deterministic replay"):
                self.project(
                    root=root,
                    run=run,
                    output=root / "evaluation/results",
                )

    def test_resealed_fabricated_metrics_fail_trace_reconstruction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.build_release_run(root)
            metrics_path = run / "metrics.json"
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            metrics["systems"]["proposal-okf-graph"]["metrics"]["end_task_success"] = 0.123456789
            metrics_path.write_text(
                json.dumps(metrics, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.reseal_run(run)
            with self.assertRaisesRegex(EvaluationProjectionError, "metrics.json differs"):
                self.project(
                    root=root,
                    run=run,
                    output=root / "evaluation/results",
                )

    def test_current_question_manifest_drift_fails_external_input_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.build_release_run(root)
            manifest_path = root / "questions/release-v2/manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["unexpected_drift"] = True
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(EvaluationProjectionError, "exact trace input bindings"):
                self.project(
                    root=root,
                    run=run,
                    output=root / "evaluation/results",
                )

    def test_resealed_arbitrary_trace_question_fails_current_question_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.build_release_run(root)
            trace_path = next((run / "traces").iterdir())
            with gzip.open(trace_path, "rb") as stream:
                traces = [json.loads(line) for line in stream if line]
            traces[0]["question"] = {
                "question_id": "arbitrary-question",
                "checksum": "a" * 64,
                "wording": "Fabricated question",
            }
            payload = b"".join(
                (canonical_json(trace) + "\n").encode("utf-8") for trace in traces
            )
            with trace_path.open("wb") as raw:
                with gzip.GzipFile(
                    filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9
                ) as stream:
                    stream.write(payload)
            trace_manifest_path = run / "trace-manifest.json"
            trace_manifest = json.loads(trace_manifest_path.read_text(encoding="utf-8"))
            shard = trace_manifest["shards"][0]
            shard.update(
                {
                    "bytes": trace_path.stat().st_size,
                    "file_sha256": sha256(trace_path),
                    "canonical_sha256": hashlib.sha256(payload).hexdigest(),
                    "first_key": "arbitrary-question",
                }
            )
            trace_manifest["root_sha256"] = hashlib.sha256(
                f"{shard['path']}\0{shard['canonical_sha256']}\n".encode("utf-8")
            ).hexdigest()
            trace_manifest_path.write_text(
                json.dumps(trace_manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.reseal_run(run)

            with self.assertRaisesRegex(EvaluationProjectionError, "does not match the release run"):
                self.project(
                    root=root,
                    run=run,
                    output=root / "evaluation/results",
                )

    def test_verified_release_run_projects_to_canonical_results_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.build_release_run(root)
            output = root / "evaluation/results"
            first = self.project(
                root=root,
                run=run,
                output=output,
            )
            second = self.project(
                root=root,
                run=run,
                output=output,
            )
            self.assertEqual(first, second)
            self.assertEqual(set(PROJECTED_FILES), {item["path"] for item in first["files"]})
            self.assertEqual(
                "completed",
                json.loads((output / "status.json").read_text(encoding="utf-8"))["agent_evaluation_status"],
            )
            (output / "projection.json").unlink()
            (output / "projection.json").symlink_to("status.json")
            with self.assertRaisesRegex(EvaluationProjectionError, "symbolic link"):
                self.project(
                    root=root,
                    run=run,
                    output=output,
                )

    def test_tampered_run_fails_before_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.build_release_run(root)
            (run / "status.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(EvaluationProjectionError, "does not match its manifest"):
                self.project(
                    root=root,
                    run=run,
                    output=root / "evaluation/results",
                )
            self.assertFalse((root / "evaluation/results").exists())

    def test_unmanifested_run_file_fails_before_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.build_release_run(root)
            (run / "unmanifested.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(EvaluationProjectionError, "unmanifested"):
                self.project(
                    root=root,
                    run=run,
                    output=root / "evaluation/results",
                )

    def test_symlinked_run_and_control_files_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.build_release_run(root)
            run_alias = root / "evaluation/agent-runs/alias"
            run_alias.symlink_to(run, target_is_directory=True)
            with self.assertRaisesRegex(EvaluationProjectionError, "symbolic link"):
                self.project(
                    root=root,
                    run=run_alias,
                    output=root / "evaluation/results",
                    source_reference="evaluation/agent-runs/alias",
                )

            status = run / "status.json"
            real_status = run / ".status-real.json"
            status.replace(real_status)
            status.symlink_to(real_status.name)
            with self.assertRaisesRegex(EvaluationProjectionError, "symbolic link"):
                self.project(
                    root=root,
                    run=run,
                    output=root / "evaluation/results",
                )

    def test_output_escape_and_symlinked_output_parent_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            run = self.build_release_run(root)
            with self.assertRaisesRegex(EvaluationProjectionError, "escapes"):
                self.project(
                    root=root,
                    run=run,
                    output=Path(outside) / "results",
                )

            linked_parent = root / "linked-output"
            linked_parent.symlink_to(Path(outside), target_is_directory=True)
            with self.assertRaisesRegex(EvaluationProjectionError, "symbolic link"):
                self.project(
                    root=root,
                    run=run,
                    output=linked_parent / "results",
                )


if __name__ == "__main__":
    unittest.main()
