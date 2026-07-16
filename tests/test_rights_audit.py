from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from govuk_okf.rights_audit import (
    AuditLimits,
    RightsAuditError,
    audit_contract_has_missing_corpus_inputs,
    audit_from_input_contract,
    audit_release,
    rebind_audit_release,
    validate_audit_evidence,
)
from govuk_okf.util import canonical_json_bytes


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_gzip(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
            stream.write(payload)


def policy_files(root: Path, *, metadata_link_default: bool = True) -> None:
    write_json(
        root / "research/source-preflight.json",
        {
            "completed_at": "2026-07-12T00:00:00Z",
            "official_sources": [
                {"id": name, "ok": True, "status": 200}
                for name in ("robots", "reuse", "terms", "ogl-v3", "ogl-exceptions")
            ],
        },
    )
    disposition = (
        "Metadata-and-link default plus per-item rights evidence and exception triggers."
        if metadata_link_default
        else "Every trigger requires an explicit review disposition."
    )
    write_json(
        root / "research/source-constraints.json",
        {
            "constraints": [
                {
                    "id": "SRC-CONSTRAINT-006",
                    "class": "item_specific_rights",
                    "disposition": disposition,
                }
            ]
        },
    )


def comparator_files(root: Path) -> list[Path]:
    walkthrough = root / "evaluation/govuk-chat/new-parent-multi-service.json"
    published = root / "evaluation/govuk-chat/official-published-example.json"
    common = {
        "schema": "govuk-chat-comparator-rights-disposition.v1",
        "not_a_legal_conclusion": True,
    }
    write_json(
        walkthrough,
        {
            "schema": "govuk-chat-comparison-walkthrough.v1",
            "rights_and_reuse": {
                **common,
                "fair_use_or_fair_dealing_trigger":
                "review_required_before_retaining_or_republishing_any_chat_answer_or_source_asset",
                "disposition": "links_and_minimal_source_metadata_only",
                "published_material_retained": False,
                "official_context_rights_status": "not_independently_verified_for_each_linked_item",
            },
        },
    )
    write_json(
        published,
        {
            "schema": "govuk-chat-published-observation.v1",
            "capture": {"asset_retained": False, "asset_sha256": "a" * 64},
            "answer": {"short_verbatim_excerpt": "A short bounded excerpt."},
            "rights_and_reuse": {
                **common,
                "fair_use_or_fair_dealing_trigger":
                "item_level_review_required_before_expanding_the_excerpt_or_copying_the_image",
                "answer": {
                    "disposition": "short_attributed_excerpt_and_structured_paraphrase_only",
                    "rights_status":
                    "not_independently_verified_for_republication_beyond_this_bounded_evidence_use",
                },
                "image": {
                    "bytes_retained": False,
                    "bytes_published": False,
                    "disposition": "source_url_and_sha256_only",
                    "rights_status": "not_independently_verified",
                },
                "source_cards": {
                    "destination_content_copied": False,
                    "disposition": "ordered_title_and_url_metadata_only",
                    "rights_status": "linked_GOV.UK_items_may_contain_item_level_exceptions",
                },
            },
        },
    )
    return [walkthrough, published]


def make_release(
    root: Path,
    records: list[dict[str, object]],
    *,
    snapshot: str = "T1-20260712",
    snapshot_kind: str = "full_corpus",
    sampled: bool = False,
    metadata_link_default: bool = True,
    reviews: list[dict[str, object]] | None = None,
) -> tuple[Path, Path]:
    policy_files(root, metadata_link_default=metadata_link_default)
    comparator_files(root)
    bundle = root / "bundle"
    shard = bundle / "data/records-0.json.gz"
    raw = canonical_json_bytes(records) + b"\n"
    write_gzip(shard, raw)
    row = {
        "compressed_bytes": shard.stat().st_size,
        "count": len(records),
        "kind": "datasets",
        "path": "data/records-0.json.gz",
        "sha256": hashlib.sha256(shard.read_bytes()).hexdigest(),
        "snapshot": snapshot,
        "uncompressed_bytes": len(raw),
    }
    write_json(
        bundle / "data/manifest.json",
        {
            "generated_at": "2026-07-12T00:00:00Z",
            "indexes": {},
            "schema": "okf-data-manifest.v1",
            "shards": {"datasets": [row]},
            "snapshot": snapshot,
        },
    )
    write_json(
        bundle / "okf-explorer.json",
        {"entrypoints": {"data_manifest": "data/manifest.json"}},
    )
    corpus_record = records[0] if records else {"canonical_url": "https://www.gov.uk/empty"}
    corpus_line = canonical_json_bytes(corpus_record) + b"\n"
    corpus_shard = root / "corpus/records/source/part-00000.jsonl.gz"
    write_gzip(corpus_shard, corpus_line)
    write_json(
        root / "corpus/records/source/index.json",
        {
            "canonical_sha256": hashlib.sha256(canonical_json_bytes(corpus_record)).hexdigest(),
            "records": 1,
            "schema": "govuk-okf-jsonl-shards.v1",
            "shards": [
                {
                    "bytes": corpus_shard.stat().st_size,
                    "canonical_sha256": hashlib.sha256(canonical_json_bytes(corpus_record)).hexdigest(),
                    "file_sha256": hashlib.sha256(corpus_shard.read_bytes()).hexdigest(),
                    "path": "part-00000.jsonl.gz",
                    "records": 1,
                }
            ],
        },
    )
    corpus_manifest = root / "corpus/records/manifest.json"
    write_json(
        corpus_manifest,
        {
            "complete_page_bodies_retained": False,
            "metadata_only": True,
            "snapshot": snapshot,
            "source_record_manifest": "corpus/records/source/index.json",
        },
    )
    ledger = root / "governance/rights-review-ledger.json"
    write_json(
        ledger,
        {
            "reviews": reviews or [],
            "schema": "afhf-govuk-okf-rights-review-ledger.v1",
            "snapshot": snapshot,
        },
    )
    write_json(
        root / "release/manifest.yaml",
        {
            "artifacts": {"bundle": "bundle", "descriptor": "bundle/okf-explorer.json"},
            "snapshot": {"id": snapshot, "kind": snapshot_kind, "sampled": sampled},
            "promotion_contract": {
                "reproduction": {
                    "source": "corpus/records",
                    "source_binding": {
                        "path": "corpus/records",
                        "kind": "directory",
                        "file_count": 3,
                        "bytes": sum(
                            path.stat().st_size
                            for path in (root / "corpus/records").rglob("*")
                            if path.is_file()
                        ),
                        "tree_sha256": "a" * 64,
                    },
                }
            },
        },
    )
    return corpus_manifest, ledger


class RightsAuditTests(unittest.TestCase):
    def test_repository_rights_state_matches_checked_snapshot_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "release/manifest.yaml").read_text(encoding="utf-8"))
        snapshot = manifest["snapshot"]
        if snapshot["kind"] == "fixture":
            result = audit_release(root)
            self.assertTrue(result["mechanical_controls_passed"], result["errors"])
            self.assertTrue(result["comparator_evidence"]["controls_passed"])
            self.assertEqual(2, len(result["comparator_evidence"]["files"]))
            self.assertTrue(
                all(
                    disposition["rights_verified"] is False
                    for disposition in result["comparator_evidence"]["dispositions"]
                )
            )
            self.assertTrue(result["retention_and_secret_findings"]["passed"])
            self.assertFalse(result["rights_privacy_audit_passed"])
            self.assertFalse(result["snapshot_binding"]["full_unsampled_snapshot"])
            self.assertFalse(result["snapshot_binding"]["corpus_snapshot_bound"])
            self.assertGreaterEqual(result["classification"]["item_review_triggered_items"], 0)
            self.assertGreaterEqual(result["classification"]["ogl_default_candidate_items"], 1)
        else:
            self.assertEqual("full_corpus", snapshot["kind"])
            self.assertFalse(snapshot["sampled"])
            result = json.loads(
                (root / manifest["artifacts"]["rights_privacy_audit"]).read_text(encoding="utf-8")
            )
            errors = validate_audit_evidence(
                root,
                result,
                require_release=result.get("rights_privacy_audit_passed") is True,
                allow_missing_corpus_inputs=True,
            )
            self.assertEqual([], errors)
        for trigger in result["classification"]["triggers"].values():
            for fingerprint in trigger["example_record_fingerprints"]:
                self.assertRegex(fingerprint, r"^sha256:[0-9a-f]{64}$")

    def test_input_contract_rebuild_rejects_missing_or_changed_corpus_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, ledger = make_release(root, [{"canonical_url": "https://www.gov.uk/example"}])
            result = audit_release(root, corpus_manifest_paths=[corpus], review_ledger_path=ledger)
            self.assertFalse(
                audit_contract_has_missing_corpus_inputs(root, result["audit_input_contract"])
            )
            rebuilt = audit_from_input_contract(root, result["audit_input_contract"])
            self.assertEqual(result, rebuilt)
            original = corpus.read_bytes()
            corpus.write_bytes(original + b" ")
            with self.assertRaisesRegex(RightsAuditError, "content differs"):
                audit_from_input_contract(root, result["audit_input_contract"])
            with self.assertRaisesRegex(RightsAuditError, "content differs"):
                audit_contract_has_missing_corpus_inputs(
                    root,
                    result["audit_input_contract"],
                )
            with self.assertRaisesRegex(RightsAuditError, "content differs"):
                rebind_audit_release(
                    root,
                    result,
                    allow_missing_corpus_inputs=True,
                )
            corpus.write_bytes(original)
            real_corpus = corpus.with_name("real-manifest.json")
            corpus.rename(real_corpus)
            corpus.symlink_to(real_corpus.name)
            with self.assertRaisesRegex(RightsAuditError, "symbolic link"):
                audit_from_input_contract(root, result["audit_input_contract"])
            corpus.unlink()
            real_corpus.rename(corpus)
            corpus.unlink()
            self.assertTrue(
                audit_contract_has_missing_corpus_inputs(root, result["audit_input_contract"])
            )
            with self.assertRaisesRegex(RightsAuditError, "does not exist"):
                audit_from_input_contract(root, result["audit_input_contract"])

            release_path = root / "release/manifest.yaml"
            release = json.loads(release_path.read_text(encoding="utf-8"))
            release["release_kind"] = "machine_release_candidate"
            write_json(release_path, release)
            rebound = rebind_audit_release(
                root,
                result,
                allow_missing_corpus_inputs=True,
            )
            self.assertEqual(
                "static_archived_input_validation",
                rebound["release_binding_refresh"]["mode"],
            )
            self.assertEqual(
                [],
                validate_audit_evidence(
                    root,
                    rebound,
                    require_release=True,
                    allow_missing_corpus_inputs=True,
                ),
            )

    def test_input_contract_does_not_auto_adopt_a_later_review_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, _ = make_release(
                root,
                [{"canonical_url": "https://www.gov.uk/example"}],
            )
            result = audit_release(
                root,
                corpus_manifest_paths=[corpus],
                auto_review_ledger=False,
            )
            self.assertIsNone(result["audit_input_contract"]["review_ledger"])
            self.assertEqual(result, audit_from_input_contract(root, result["audit_input_contract"]))

    def test_comparator_rights_dispositions_are_hash_bound_and_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, ledger = make_release(root, [{"canonical_url": "https://www.gov.uk/example"}])
            comparators = comparator_files(root)
            result = audit_release(
                root,
                corpus_manifest_paths=[corpus],
                review_ledger_path=ledger,
                comparator_evidence_paths=comparators,
            )
            self.assertTrue(result["comparator_evidence"]["controls_passed"], result["errors"])
            self.assertEqual(2, len(result["audit_input_contract"]["comparator_evidence"]))
            self.assertEqual(result, audit_from_input_contract(root, result["audit_input_contract"]))

            comparators[0].write_bytes(comparators[0].read_bytes() + b" ")
            with self.assertRaisesRegex(RightsAuditError, "content differs"):
                audit_from_input_contract(root, result["audit_input_contract"])
            with self.assertRaisesRegex(RightsAuditError, "content differs"):
                rebind_audit_release(root, result)

    def test_comparator_rights_dispositions_reject_semantic_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, ledger = make_release(root, [{"canonical_url": "https://www.gov.uk/example"}])
            comparators = comparator_files(root)
            cases = (
                (comparators[0], ("rights_and_reuse", "fair_use_or_fair_dealing_trigger")),
                (comparators[0], ("rights_and_reuse", "disposition")),
                (comparators[0], ("rights_and_reuse", "official_context_rights_status")),
                (comparators[1], ("rights_and_reuse", "fair_use_or_fair_dealing_trigger")),
                (comparators[1], ("rights_and_reuse", "answer", "rights_status")),
                (comparators[1], ("rights_and_reuse", "image", "disposition")),
                (comparators[1], ("rights_and_reuse", "source_cards", "disposition")),
                (comparators[1], ("rights_and_reuse", "source_cards", "rights_status")),
            )
            originals = {
                path: json.loads(path.read_text(encoding="utf-8")) for path in comparators
            }
            for path, keys in cases:
                with self.subTest(path=path.name, keys=keys):
                    document = json.loads(json.dumps(originals[path]))
                    target = document
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = "unsafe_overstatement"
                    write_json(path, document)
                    result = audit_release(
                        root,
                        corpus_manifest_paths=[corpus],
                        review_ledger_path=ledger,
                    )
                    self.assertFalse(result["comparator_evidence"]["controls_passed"])
                    self.assertTrue(
                        any("comparator rights controls" in error for error in result["errors"]),
                        result["errors"],
                    )
                    write_json(path, originals[path])

    def test_comparator_contract_requires_both_fixed_repository_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, ledger = make_release(root, [{"canonical_url": "https://www.gov.uk/example"}])
            comparators = comparator_files(root)
            comparators[1].unlink()
            result = audit_release(
                root,
                corpus_manifest_paths=[corpus],
                review_ledger_path=ledger,
            )
            self.assertFalse(result["comparator_evidence"]["controls_passed"])
            self.assertTrue(
                any("comparator rights evidence" in error for error in result["errors"]),
                result["errors"],
            )

            write_json(comparators[1], json.loads(comparators[0].read_text(encoding="utf-8")))
            result = audit_release(
                root,
                corpus_manifest_paths=[corpus],
                review_ledger_path=ledger,
                comparator_evidence_paths=[comparators[0], comparators[0]],
            )
            self.assertTrue(
                any("must bind both" in error for error in result["errors"]),
                result["errors"],
            )

    def test_structural_fields_trigger_but_narrative_words_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, ledger = make_release(
                root,
                [
                    {
                        "canonical_url": "https://www.gov.uk/example",
                        "title": "Personal data, logos, patents and third-party copyright",
                        "description": "Contact someone using name@example.com.",
                    }
                ],
            )
            result = audit_release(root, corpus_manifest_paths=[corpus], review_ledger_path=ledger)
            self.assertTrue(result["rights_privacy_audit_passed"], result["errors"])
            self.assertEqual(0, result["classification"]["item_review_triggered_items"])

            record_path = root / "bundle/data/records-0.json.gz"
            record = {
                "canonical_url": "https://www.gov.uk/example",
                "details": {"logo": {"crest": "department"}, "contact": {"email": "name@example.com"}},
            }
            raw = canonical_json_bytes([record]) + b"\n"
            write_gzip(record_path, raw)
            manifest_path = root / "bundle/data/manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            row = manifest["shards"]["datasets"][0]
            row.update(
                {
                    "compressed_bytes": record_path.stat().st_size,
                    "sha256": hashlib.sha256(record_path.read_bytes()).hexdigest(),
                    "uncompressed_bytes": len(raw),
                }
            )
            write_json(manifest_path, manifest)
            result = audit_release(root, corpus_manifest_paths=[corpus], review_ledger_path=ledger)
            self.assertGreater(result["classification"]["triggers"]["personal_data_indicator"]["items"], 0)
            self.assertGreater(
                result["classification"]["triggers"]["logo_crest_royal_arms_or_insignia"]["items"], 0
            )

    def test_metadata_policy_separates_unresolved_trigger_from_release_blocker(self) -> None:
        record = {
            "canonical_url": "https://outside.example.gov.uk/service",
            "details": {"attachments": [{"url": "https://assets.publishing.service.gov.uk/a.pdf"}]},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, ledger = make_release(root, [record], metadata_link_default=True)
            result = audit_release(root, corpus_manifest_paths=[corpus], review_ledger_path=ledger)
            self.assertTrue(result["rights_privacy_audit_passed"], result["errors"])
            self.assertGreater(result["review"]["unresolved_triggered_items"], 0)
            self.assertEqual(0, result["review"]["release_blocking_unresolved_triggered_items"])
            self.assertFalse(result["review"]["unresolved_triggers_are_release_blocking"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, ledger = make_release(root, [record], metadata_link_default=False)
            result = audit_release(root, corpus_manifest_paths=[corpus], review_ledger_path=ledger)
            self.assertFalse(result["rights_privacy_audit_passed"])
            self.assertGreater(result["review"]["release_blocking_unresolved_triggered_items"], 0)

    def test_snapshot_bound_review_can_resolve_trigger(self) -> None:
        record = {"canonical_url": "https://outside.example.gov.uk/service"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, ledger = make_release(root, [record], metadata_link_default=False)
            initial = audit_release(root, corpus_manifest_paths=[corpus], review_ledger_path=ledger)
            packet = root / "release/review-packet.jsonl"
            initial = audit_release(
                root,
                corpus_manifest_paths=[corpus],
                review_ledger_path=ledger,
                review_packet_path=packet,
            )
            row = json.loads(packet.read_text(encoding="utf-8").splitlines()[0])
            self.assertFalse(row["source_values_retained"])
            write_json(
                ledger,
                {
                    "reviews": [
                        {
                            "record_fingerprint": row["record_fingerprint"],
                            "trigger_ids": row["trigger_ids"],
                            "disposition": "metadata_only_safe",
                            "reviewed_by": "rights-review-role",
                            "reviewed_at": "2026-07-12T00:00:00Z",
                            "evidence_ids": ["SRC-CONSTRAINT-006"],
                        }
                    ],
                    "schema": "afhf-govuk-okf-rights-review-ledger.v1",
                    "snapshot": "T1-20260712",
                },
            )
            result = audit_release(root, corpus_manifest_paths=[corpus], review_ledger_path=ledger)
            self.assertTrue(result["rights_privacy_audit_passed"], result["errors"])
            self.assertEqual(0, result["review"]["unresolved_triggered_items"])

    def test_body_and_credentials_are_hard_failures_with_redacted_examples(self) -> None:
        record = {
            "canonical_url": "https://www.gov.uk/example",
            "body": "complete copied page body",
            "client_secret": "do-not-report-this-value",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, ledger = make_release(root, [record])
            result = audit_release(root, corpus_manifest_paths=[corpus], review_ledger_path=ledger)
            self.assertFalse(result["mechanical_controls_passed"])
            self.assertFalse(result["rights_privacy_audit_passed"])
            findings = result["retention_and_secret_findings"]
            # The same unsafe item is independently observed in publication and corpus inputs.
            self.assertEqual(4, findings["finding_count"])
            encoded = json.dumps(findings)
            self.assertNotIn("complete copied page body", encoded)
            self.assertNotIn("do-not-report-this-value", encoded)
            self.assertTrue(
                all(
                    example["value_retained"] == "false"
                    for values in findings["examples"].values()
                    for example in values
                )
            )

    def test_bounded_scanner_rejects_oversized_record(self) -> None:
        record = {"canonical_url": "https://www.gov.uk/example", "description": "x" * 2048}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, ledger = make_release(root, [record])
            limits = AuditLimits(max_record_bytes=256)
            result = audit_release(
                root, corpus_manifest_paths=[corpus], review_ledger_path=ledger, limits=limits
            )
            self.assertFalse(result["rights_privacy_audit_passed"])
            self.assertTrue(any("record exceeds" in error for error in result["errors"]), result["errors"])


if __name__ == "__main__":
    unittest.main()
