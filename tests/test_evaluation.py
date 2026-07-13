from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.evaluation import (  # noqa: E402
    SYSTEMS,
    canonical_json,
    run_evaluation,
    sha256_file,
    sha256_text,
    validate_input_contract,
)
from govuk_okf.util import write_gzip_json, yaml_dump  # noqa: E402


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def checked(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result["checksum"] = sha256_text(canonical_json(value))
    return result


class EvaluationHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = Path(tempfile.mkdtemp(prefix=".test-evaluation-", dir=ROOT))
        cls.questions = cls.temporary / "questions"
        cls.bundle = cls.temporary / "bundle"
        cls.output = cls.temporary / "run"
        cls._build_bundle()
        cls._build_questions()
        cls.result = run_evaluation(
            questions=cls.questions,
            bundle=cls.bundle,
            output=cls.output,
            run_id="fixture-evaluation-v1",
            mode="fixture",
            trace_shard_records=7,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.temporary)

    @classmethod
    def _build_bundle(cls) -> None:
        records = [
            {
                "@id": "https://www.gov.uk/alpha",
                "canonical_content_id": "00000000-0000-4000-8000-000000000001",
                "confidence": "source-declared",
                "description": "Alpha service guidance for England.",
                "document_type": "guidance",
                "evidence_sha256": "a" * 64,
                "evidence_url": "https://content-api.publishing.service.gov.uk/content/alpha",
                "jurisdiction": ["England"],
                "language": "en",
                "lifecycle": "published",
                "open": "dataset/alpha-en",
                "publisher_title": "Department Alpha",
                "schema_name": "guidance",
                "source_memberships": ["search-v1", "sitemap"],
                "tags": ["alpha", "service"],
                "title": "Alpha service",
                "url": "https://www.gov.uk/alpha",
            },
            {
                "@id": "https://www.gov.uk/beta",
                "canonical_content_id": "00000000-0000-4000-8000-000000000002",
                "confidence": "source-declared",
                "description": "Beta collection related to Alpha.",
                "document_type": "document_collection",
                "evidence_sha256": "b" * 64,
                "evidence_url": "https://content-api.publishing.service.gov.uk/content/beta",
                "jurisdiction": ["United Kingdom"],
                "language": "en",
                "lifecycle": "published",
                "open": "dataset/beta-en",
                "publisher_title": "Department Beta",
                "schema_name": "document_collection",
                "source_memberships": ["sitemap"],
                "tags": ["beta", "collection"],
                "title": "Beta collection",
                "url": "https://www.gov.uk/beta",
            },
            {
                "@id": "https://www.gov.uk/near-alpha",
                "canonical_content_id": "00000000-0000-4000-8000-000000000003",
                "confidence": "source-declared",
                "description": "A distinct near match.",
                "document_type": "guidance",
                "evidence_sha256": "c" * 64,
                "evidence_url": "https://content-api.publishing.service.gov.uk/content/near-alpha",
                "jurisdiction": ["England"],
                "language": "en",
                "lifecycle": "published",
                "open": "dataset/near-alpha-en",
                "publisher_title": "Department Alpha",
                "schema_name": "guidance",
                "source_memberships": ["search-v1"],
                "tags": ["alpha", "near"],
                "title": "Alpha nearby information",
                "url": "https://www.gov.uk/near-alpha",
            },
        ]
        relationships = [
            {
                "assertion_status": "source-declared",
                "evidence_sha256": "a" * 64,
                "evidence_url": "https://content-api.publishing.service.gov.uk/content/alpha",
                "kind": "related to",
                "source": "dataset/alpha-en",
                "source_native_predicate": "related",
                "target": "dataset/beta-en",
            }
        ]
        write_gzip_json(cls.bundle / "data" / "records-0.json.gz", records)
        write_gzip_json(cls.bundle / "data" / "relationships-0.json.gz", relationships)
        manifest = {
            "chunks": {
                "datasets": ["data/records-0.json.gz"],
                "relationships": ["data/relationships-0.json.gz"],
            },
            "counts": {"datasets": 3, "relationships": 1},
            "snapshot": "fixture-evaluation-v1",
        }
        dump(cls.bundle / "data" / "manifest.json", manifest)
        dump(
            cls.bundle / "okf-explorer.json",
            {
                "counts": manifest["counts"],
                "entrypoints": {"data_manifest": "data/manifest.json"},
                "schema": "okf-explorer-large-corpus.v1",
            },
        )
        semantic = {"schemaVersion": 1, "snapshot": "fixture-evaluation-v1"}
        dump(cls.bundle / "okf-bundle.jsonld", semantic)
        (cls.bundle / "okf-bundle.yamlld").write_text(yaml_dump(semantic) + "\n", encoding="utf-8")

    @classmethod
    def _question(cls, identifier: str, title: str, content_id: str, *, unanswerable: bool = False) -> dict[str, Any]:
        primary = {
            "content_id": content_id,
            "identity": f"content:{content_id}:en",
            "source_evidence_sha256": ("a" if title == "Alpha service" else "b") * 64,
            "source_evidence_url": f"https://content-api.publishing.service.gov.uk/content/{title.split()[0].casefold()}",
            "title": title,
            "url": f"https://www.gov.uk/{title.split()[0].casefold()}",
        }
        wording = f"Where is the official GOV.UK item “{title}”?"
        if unanswerable:
            wording += " Does GOV.UK guarantee my personal outcome even though no case facts have been supplied?"
        return checked(
            {
                "challenge": "unsupported_premise" if unanswerable else "direct",
                "difficulty": "adversarial" if unanswerable else "easy",
                "discovery_stage": "metadata_only_discovery",
                "expected_unanswerable": unanswerable,
                "gold": {
                    "classification": "deliberately_unanswerable" if unanswerable else "answerable",
                    "content_ids": [] if unanswerable else [content_id],
                    "expected_paths": [] if unanswerable else [
                        {
                            "nodes": [primary, {"identity": "content-type:guidance"}],
                            "edges": [
                                {
                                    "subject": primary["identity"],
                                    "predicate": "has_content_type",
                                    "object": "content-type:guidance",
                                }
                            ],
                        }
                    ],
                    "primary_targets": [] if unanswerable else [primary],
                    "snapshot_id": "fixture-evaluation-v1",
                    "unanswerable_rationale": "A guaranteed personal outcome cannot be established from metadata.",
                    "urls": [] if unanswerable else [primary["url"]],
                },
                "locale": "en",
                "operation": "handle_ambiguity" if unanswerable else "locate_known_item",
                "persona_ids": ["persona-fixture"],
                "question_id": identifier,
                "risk": "high" if unanswerable else "low",
                "split": "held_out" if identifier.endswith("3") else "development",
                "split_group": f"group-{identifier}",
                "story_id": f"story-{identifier}",
                "story_role": "known-item",
                "target_relationships": [] if unanswerable else ["has_content_type"],
                "wording": wording,
                "jurisdiction": ["United Kingdom"],
            }
        )

    @classmethod
    def _build_questions(cls) -> None:
        questions = [
            cls._question("q-1", "Alpha service", "00000000-0000-4000-8000-000000000001"),
            cls._question("q-2", "Beta collection", "00000000-0000-4000-8000-000000000002"),
            cls._question("q-3", "Alpha service", "00000000-0000-4000-8000-000000000001", unanswerable=True),
        ]
        binding = cls.questions / "bindings" / "fixture.jsonl"
        binding.parent.mkdir(parents=True, exist_ok=True)
        binding.write_text("".join(canonical_json(item) + "\n" for item in questions), encoding="utf-8")
        gold_path = cls.questions / "gold" / "catalogue.jsonl"
        gold_path.parent.mkdir(parents=True, exist_ok=True)
        gold_records = [
            checked(
                {
                    "schema_version": 2,
                    "question_id": item["question_id"],
                    "question_checksum": item["checksum"],
                    "gold": item["gold"],
                }
            )
            for item in questions
        ]
        gold_path.write_text(
            "".join(canonical_json(item) + "\n" for item in gold_records),
            encoding="utf-8",
        )
        matrix_path = cls.questions / "matrix.json"
        dump(matrix_path, {"schema_version": 2, "legacy_development_gold": "gold/catalogue.jsonl"})
        files = [
            {"path": path.relative_to(cls.questions).as_posix(), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
            for path in (binding, gold_path, matrix_path)
        ]
        material = "".join(f"{item['path']}\0{item['sha256']}\n" for item in files)
        dump(
            cls.questions / "manifest.json",
            {
                "artifact_tier": "development_only",
                "counts": {"primary_personas": 1, "questions": 3, "stories": 3},
                "files": files,
                "publication_ready_candidate": False,
                "root_sha256": sha256_text(material),
                "snapshot_id": "fixture-evaluation-v1",
            },
        )
        dump(
            cls.questions / "contract.json",
            {
                "artifact_tier": "development_only",
                "publication_ready_candidate": False,
                "snapshot": {"snapshot_id": "fixture-evaluation-v1"},
            },
        )

    def test_release_mode_rejects_a_fixture_before_execution(self) -> None:
        with self.assertRaisesRegex(ValueError, "bounded sharded gold catalogue"):
            validate_input_contract(self.questions, self.bundle, "release")

    def test_inputs_and_completed_runs_are_immutable(self) -> None:
        with self.assertRaisesRegex(ValueError, "disjoint"):
            run_evaluation(
                questions=self.questions,
                bundle=self.bundle,
                output=self.bundle / "evaluation-output",
                run_id="unsafe-output",
                mode="fixture",
            )
        self.assertFalse((self.bundle / "evaluation-output").exists())
        with self.assertRaisesRegex(ValueError, "immutable"):
            run_evaluation(
                questions=self.questions,
                bundle=self.bundle,
                output=self.output,
                run_id="fixture-evaluation-v1",
                mode="fixture",
                resume=True,
            )

    def test_complete_fixture_run_writes_matched_zero_cost_evidence(self) -> None:
        status = self.result["status"]
        self.assertEqual(status["questions"], 3)
        self.assertEqual(status["systems"], len(SYSTEMS))
        self.assertEqual(status["outcomes"], 3 * len(SYSTEMS))
        self.assertEqual(status["human_evaluation_status"], "not_authorised")
        self.assertEqual(status["human_ui_of_choice_status"], "not_yet_testable")
        self.assertFalse(status["full_evaluation_complete"])
        self.assertFalse(status["programme_complete"])
        self.assertEqual(status["model_usage"]["cost_gbp"], 0.0)
        self.assertEqual(status["network_requests"], 0)
        self.assertTrue(status["serialization_invariance"]["passed"])
        trace_manifest = json.loads((self.output / "trace-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(trace_manifest["records"], 3 * len(SYSTEMS))
        self.assertTrue(all(item["records"] <= 7 for item in trace_manifest["shards"]))
        for item in trace_manifest["shards"]:
            path = self.output / item["path"]
            self.assertEqual(sha256_file(path), item["file_sha256"])
            payload = gzip.open(path, "rb").read()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), item["canonical_sha256"])
            for line in payload.splitlines():
                trace = json.loads(line)
                self.assertEqual(trace["usage"]["model_calls"], 0)
                self.assertEqual(trace["usage"]["network_requests"], 0)
                self.assertEqual(trace["usage"]["cost_gbp"], 0.0)
        self.assertFalse((self.output / ".work").exists())

    def test_metrics_include_required_effectiveness_efficiency_and_paired_slices(self) -> None:
        metrics = json.loads((self.output / "metrics.json").read_text(encoding="utf-8"))
        proposal = metrics["systems"]["proposal-okf-graph"]["metrics"]
        for key in (
            "recall_at_10",
            "mrr_at_10",
            "ndcg_at_10",
            "relationship_f1",
            "citation_correctness",
            "provenance_completeness",
            "answerability_accuracy",
            "latency_ms_p95",
            "tool_calls",
            "query_steps",
            "bytes_read",
            "shards_read",
            "model_calls",
            "input_tokens",
            "output_tokens",
            "network_requests",
            "cost_gbp",
        ):
            self.assertIn(key, proposal)
        paired = json.loads((self.output / "paired-comparisons.json").read_text(encoding="utf-8"))
        self.assertTrue(paired["comparisons"])
        self.assertTrue(all(item["independent_clusters"] >= 1 for item in paired["comparisons"]))
        self.assertEqual(paired["method"]["multiplicity"], "Bonferroni simultaneous intervals")
        slices = json.loads((self.output / "slices.json").read_text(encoding="utf-8"))
        self.assertIn("persona_id", slices["dimensions"])
        self.assertIn("challenge", slices["dimensions"])
        failures = json.loads((self.output / "failure-analysis.json").read_text(encoding="utf-8"))
        self.assertTrue(failures["failures"])


if __name__ == "__main__":
    unittest.main()
