from __future__ import annotations

import hashlib
import gzip
import copy
from email.message import Message
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

from govuk_okf.citations import (
    _best_locator,
    _decode_content_encoding,
    _RecordingRedirect,
    CitationError,
    collect_citations,
    digest_text,
    fetch_evidence,
    normalise_url,
    stable_id,
    verify_release,
)


class CitationTests(unittest.TestCase):
    def policy(self) -> dict:
        return {
            "allowed_authority_classes": ["normative_standard"],
            "authority_rules": [
                {
                    "host_suffix": "example.test",
                    "authority_class": "normative_standard",
                    "publisher": "Example Standards Body",
                }
            ],
            "non_citation_prefixes": [],
            "source_overrides": {},
            "url_replacements": {},
        }

    def test_normalise_url_rejects_userinfo(self) -> None:
        with self.assertRaises(CitationError):
            normalise_url("https://secret@example.test/source")

    def test_inventory_maps_claim_source_and_citation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "planning").mkdir()
            (root / "planning/example.md").write_text(
                "A material statement cites the [Example Standard](https://example.test/spec).\n",
                encoding="utf-8",
            )
            inventory = collect_citations(root, self.policy())
            self.assertEqual(len(inventory["claims"]), 1)
            self.assertEqual(len(inventory["sources"]), 1)
            self.assertEqual(len(inventory["citations"]), 1)
            self.assertEqual(inventory["citations"][0]["claim_id"], inventory["claims"][0]["claim_id"])

    def test_multiline_list_link_does_not_absorb_neighbouring_bullets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "docs/example.md").write_text(
                "- unrelated claim\n"
                "- cited claim continued on the next\n"
                "  line with [Example Standard](https://example.test/spec) evidence\n"
                "- another unrelated claim\n",
                encoding="utf-8",
            )
            inventory = collect_citations(root, self.policy())
            self.assertEqual(len(inventory["claims"]), 1)
            claim = inventory["claims"][0]
            self.assertIn("cited claim", claim["text"])
            self.assertNotIn("unrelated claim", claim["text"])
            self.assertNotIn("another unrelated", claim["text"])

    def test_markdown_link_split_across_lines_is_collected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "research").mkdir()
            (root / "research/example.md").write_text(
                "The [Example\nStandard](https://example.test/spec) supports this claim.\n",
                encoding="utf-8",
            )
            inventory = collect_citations(root, self.policy())
            self.assertEqual(len(inventory["claims"]), 1)
            self.assertEqual(len(inventory["citations"]), 1)
            self.assertEqual(inventory["citations"][0]["link_label"], "Example Standard")

    def test_dependency_markdown_is_not_a_released_citation_surface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependency = root / "semantic/node_modules/example"
            dependency.mkdir(parents=True)
            (dependency / "README.md").write_text(
                "[build badge](https://untrusted.example.test/badge.svg)\n",
                encoding="utf-8",
            )
            (root / "semantic/profile").mkdir()
            (root / "semantic/profile/README.md").write_text(
                "[Example Standard](https://example.test/spec)\n",
                encoding="utf-8",
            )
            inventory = collect_citations(root, self.policy())
            self.assertEqual(len(inventory["citations"]), 1)
            self.assertEqual(inventory["citations"][0]["requested_url"], "https://example.test/spec")

    def test_govuk_chat_json_urls_are_structured_citations_with_json_pointers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            comparison = root / "evaluation/govuk-chat"
            comparison.mkdir(parents=True)
            (comparison / "new-parent-multi-service.json").write_text(
                json.dumps(
                    {
                        "schema": "govuk-chat-comparison-walkthrough.v1",
                        "official_context": [
                            {
                                "claim": "The official source documents the bounded comparator.",
                                "url": "https://example.test/context",
                            }
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (comparison / "official-published-example.json").write_text(
                json.dumps(
                    {
                        "schema": "govuk-chat-published-observation.v1",
                        "source_page_url": "https://example.test/page",
                        "source_image_url": "https://example.test/image.png",
                        "capture": {"asset_sha256": "a" * 64},
                        "question": "What does the example show?",
                        "answer": {
                            "short_verbatim_excerpt": "A bounded excerpt.",
                            "structured_summary": [
                                "The first structured point.",
                                "The second structured point.",
                            ],
                        },
                        "source_cards": [
                            {"position": 1, "title": "Source card", "url": "https://example.test/card"}
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            inventory = collect_citations(root, self.policy())
            self.assertEqual(len(inventory["claims"]), 8)
            self.assertEqual(len(inventory["citations"]), 9)
            self.assertEqual(
                {source["requested_url"] for source in inventory["sources"]},
                {
                    "https://example.test/context",
                    "https://example.test/page",
                    "https://example.test/image.png",
                    "https://example.test/card",
                },
            )
            image_source = next(
                source for source in inventory["sources"] if source["requested_url"].endswith("image.png")
            )
            self.assertEqual(image_source["expected_document_sha256"], "a" * 64)
            self.assertEqual(image_source["locator_hint"]["kind"], "binary_sha256")
            self.assertTrue(all(citation["structured_source"] for citation in inventory["citations"]))
            pointers = {claim["source_location"]["json_pointer"] for claim in inventory["claims"]}
            self.assertEqual(
                pointers,
                {
                    "/official_context/0/url",
                    "/source_page_url",
                    "/source_image_url",
                    "/question",
                    "/answer/short_verbatim_excerpt",
                    "/answer/structured_summary/0",
                    "/answer/structured_summary/1",
                    "/source_cards/0",
                },
            )
            card_claim = next(
                claim for claim in inventory["claims"] if claim["source_location"]["json_pointer"] == "/source_cards/0"
            )
            self.assertIn("position 1", card_claim["text"])
            self.assertIn("Source card", card_claim["text"])
            self.assertIn("https://example.test/card", card_claim["text"])
            card_citations = [
                citation
                for citation in inventory["citations"]
                if citation["claim_id"] == card_claim["claim_id"]
            ]
            self.assertEqual(
                {citation["requested_url"] for citation in card_citations},
                {"https://example.test/image.png", "https://example.test/card"},
            )

    def test_govuk_chat_source_card_positions_must_match_array_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            comparison = root / "evaluation/govuk-chat"
            comparison.mkdir(parents=True)
            (comparison / "official-published-example.json").write_text(
                json.dumps(
                    {
                        "schema": "govuk-chat-published-observation.v1",
                        "source_page_url": "https://example.test/page",
                        "source_image_url": "https://example.test/image.png",
                        "capture": {"asset_sha256": "a" * 64},
                        "question": "Question?",
                        "answer": {
                            "short_verbatim_excerpt": "A bounded excerpt.",
                            "structured_summary": ["A structured point."],
                        },
                        "source_cards": [
                            {"position": 2, "title": "Second", "url": "https://example.test/two"},
                            {"position": 1, "title": "First", "url": "https://example.test/one"},
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CitationError, "unique, contiguous and match array order"):
                collect_citations(root, self.policy())

    def test_binary_comparator_evidence_is_verified_by_exact_sha256(self) -> None:
        body = b"\x89PNG\r\n\x1a\nexact-test-image"
        digest = hashlib.sha256(body).hexdigest()
        headers = Message()
        headers["Content-Type"] = "image/png"

        class Response:
            status = 200

            def read(self, _limit: int) -> bytes:
                return body

            def geturl(self) -> str:
                return "https://example.test/image.png"

            def getcode(self) -> int:
                return 200

            def close(self) -> None:
                return None

        response = Response()
        response.headers = headers

        class Opener:
            def open(self, _request: object, timeout: float) -> Response:
                self.timeout = timeout
                return response

        source = {
            "source_id": "SRC-BINARY",
            "requested_url": "https://example.test/image.png",
            "publisher": "Example Standards Body",
            "expected_hosts": ["example.test"],
            "expected_document_sha256": digest,
            "locator": {"kind": "binary_sha256", "value": digest},
        }
        contexts = [
            {
                "citation_id": "CIT-BINARY",
                "claim_sha256": "a" * 64,
                "claim_text": "The source image is hash bound.",
                "link_label": "source image",
            }
        ]
        with patch("urllib.request.build_opener", return_value=Opener()):
            evidence = fetch_evidence(
                source,
                citation_contexts=contexts,
                timeout=1,
                max_bytes=1024,
                user_agent="test",
            )
        self.assertEqual(evidence["document_sha256"], digest)
        self.assertEqual(evidence["evidence_excerpt"], f"sha256:{digest}")
        self.assertEqual(evidence["checks"]["identity_matches"], "pass")
        self.assertEqual(evidence["checks"]["locator_found"], "pass")
        self.assertEqual(evidence["citation_evidence"][0]["checks"]["locator_found"], "pass")

        source["expected_document_sha256"] = "0" * 64
        with patch("urllib.request.build_opener", return_value=Opener()):
            mismatch = fetch_evidence(
                source,
                citation_contexts=contexts,
                timeout=1,
                max_bytes=1024,
                user_agent="test",
            )
        self.assertEqual(mismatch["checks"]["identity_matches"], "fail")
        self.assertEqual(mismatch["checks"]["locator_found"], "fail")

    def fixture(self, *, material: bool = True) -> tuple[dict, list, list]:
        source_url = "https://example.test/spec"
        source_id = stable_id("SRC", source_url)
        claim_text = "Example claim."
        claim_id = stable_id("CLM", "planning/example.md", claim_text)
        citation_id = stable_id("CIT", claim_id, source_id)
        document_hash = hashlib.sha256(b"source").hexdigest()
        inventory = {
            "claims": [
                {
                    "claim_id": claim_id,
                    "claim_sha256": digest_text(claim_text),
                    "text": claim_text,
                    "release_material": material,
                }
            ],
            "sources": [{"source_id": source_id, "requested_url": source_url}],
            "citations": [
                {"citation_id": citation_id, "claim_id": claim_id, "source_id": source_id}
            ],
        }
        evidence = [
            {
                "schema_version": "1.0",
                "evidence_id": stable_id("EVD", source_id, document_hash),
                "source_id": source_id,
                "requested_url": source_url,
                "verification_url": source_url,
                "final_url": source_url,
                "retrieved_at": "2026-07-12T00:00:00Z",
                "redirect_chain": [],
                "tls": {"verified": True, "policy": "python_default_strict_context"},
                "document_sha256": document_hash,
                "checks": {
                    "reachable": "pass",
                    "secure_transport": "pass",
                    "redirect_source_identity": "pass",
                    "identity_matches": "pass",
                    "locator_found": "pass",
                    "excerpt_matches": "pass",
                },
                "citation_evidence": [
                    {
                        "citation_id": citation_id,
                        "claim_sha256": digest_text(claim_text),
                        "locator": "locator",
                        "locator_sha256": digest_text(json.dumps("locator", separators=(",", ":"))),
                        "evidence_excerpt": "excerpt",
                        "excerpt_sha256": digest_text("excerpt"),
                        "checks": {"locator_found": "pass", "excerpt_matches": "pass"},
                    }
                ],
            }
        ]
        reviews = [
            {
                "review_id": "REV-1",
                "reviewer_id": "independent-reviewer-1",
                "reviewer_kind": "independent_agent_configuration",
                "reviewed_at": "2026-07-12T00:00:00Z",
                "rationale": "The located source directly supports the exact bounded claim.",
                "independence_limitations": "Test fixture reviewer.",
                "citation_id": citation_id,
                "claim_sha256": digest_text(claim_text),
                "document_sha256": document_hash,
                "locator_sha256": digest_text(json.dumps("locator", separators=(",", ":"))),
                "excerpt_sha256": digest_text("excerpt"),
                "verdict": "entailed",
                "reviewer_independent_from_claim_author": True,
                "method": "manual_locator_review",
                "numbers_dates_named_entities_checked": True,
                "contrary_evidence_checked": True,
            }
        ]
        return inventory, evidence, reviews

    def test_release_passes_only_with_hash_bound_manual_review(self) -> None:
        inventory, evidence, reviews = self.fixture()
        report = verify_release(
            inventory=inventory,
            evidence=evidence,
            reviews=reviews,
            claim_reviews=[],
            waivers=[],
            snapshot_id="T1-20260712",
        )
        self.assertTrue(report["citation_verification_passed"])

        reviews[0]["document_sha256"] = "0" * 64
        report = verify_release(
            inventory=inventory,
            evidence=evidence,
            reviews=reviews,
            claim_reviews=[],
            waivers=[],
            snapshot_id="T1-20260712",
        )
        self.assertFalse(report["citation_verification_passed"])
        self.assertIn("not bound", " ".join(report["failures"][0]["problems"]))

    def test_offline_verifier_recomputes_locator_and_excerpt_hashes(self) -> None:
        inventory, evidence, reviews = self.fixture()
        evidence[0]["citation_evidence"][0]["evidence_excerpt"] = "tampered"
        report = verify_release(
            inventory=inventory,
            evidence=evidence,
            reviews=reviews,
            claim_reviews=[],
            waivers=[],
            snapshot_id="T1-20260712",
        )
        self.assertFalse(report["citation_verification_passed"])
        self.assertIn("excerpt hash does not match", " ".join(report["failures"][0]["problems"]))

    def test_material_claim_cannot_use_non_dependent_waiver(self) -> None:
        inventory, evidence, _ = self.fixture(material=True)
        citation_id = inventory["citations"][0]["citation_id"]
        waiver = {
            "waiver_id": "W-1",
            "citation_id": citation_id,
            "reason": "test",
            "owner": "owner",
            "approved_at": "2026-07-12",
            "review_at": "2026-08-12",
            "evidence": "record",
            "non_dependent": True,
            "dependent_conclusions": [],
        }
        report = verify_release(
            inventory=inventory,
            evidence=evidence,
            reviews=[],
            claim_reviews=[],
            waivers=[waiver],
            snapshot_id="T1-20260712",
        )
        self.assertFalse(report["citation_verification_passed"])
        self.assertIn("material released claim", " ".join(report["failures"][0]["problems"]))

    def test_multi_source_claim_requires_joint_manual_review(self) -> None:
        inventory, evidence, reviews = self.fixture()
        claim = inventory["claims"][0]
        source_url = "https://example.test/second"
        source_id = stable_id("SRC", source_url)
        citation_id = stable_id("CIT", claim["claim_id"], source_id)
        inventory["sources"].append({"source_id": source_id, "requested_url": source_url})
        inventory["citations"].append(
            {"citation_id": citation_id, "claim_id": claim["claim_id"], "source_id": source_id}
        )
        second_evidence = copy.deepcopy(evidence[0])
        second_evidence["source_id"] = source_id
        second_evidence["evidence_id"] = stable_id(
            "EVD", source_id, second_evidence["document_sha256"]
        )
        second_evidence["requested_url"] = source_url
        second_evidence["verification_url"] = source_url
        second_evidence["final_url"] = source_url
        second_evidence["citation_evidence"][0]["citation_id"] = citation_id
        evidence.append(second_evidence)
        second_review = copy.deepcopy(reviews[0])
        second_review["review_id"] = "REV-2"
        second_review["citation_id"] = citation_id
        second_review["verdict"] = "partly_supported"
        second_review["supported_claim_spans"] = [claim["text"]]
        reviews.append(second_review)

        report = verify_release(
            inventory=inventory,
            evidence=evidence,
            reviews=reviews,
            claim_reviews=[],
            waivers=[],
            snapshot_id="T1-20260712",
        )
        self.assertFalse(report["citation_verification_passed"])
        self.assertTrue(
            any("joint semantic-support" in " ".join(item["problems"]) for item in report["failures"])
        )

        joint_review = {
            "claim_review_id": "JREV-1",
            "claim_id": claim["claim_id"],
            "claim_sha256": claim["claim_sha256"],
            "citation_ids": sorted(value["citation_id"] for value in inventory["citations"]),
            "citation_review_ids": sorted(value["review_id"] for value in reviews),
            "verdict": "entailed_jointly",
            "method": "manual_joint_support_review",
            "reviewer_id": "independent-reviewer-1",
            "reviewer_kind": "independent_agent_configuration",
            "reviewed_at": "2026-07-12T00:00:00Z",
            "rationale": "The two located sources jointly cover the bounded claim.",
            "independence_limitations": "Test fixture reviewer.",
            "reviewer_independent_from_claim_author": True,
            "coverage_complete": True,
            "uncovered_claim_spans": [],
            "numbers_dates_named_entities_checked": True,
            "contrary_evidence_checked": True,
        }
        report = verify_release(
            inventory=inventory,
            evidence=evidence,
            reviews=reviews,
            claim_reviews=[joint_review],
            waivers=[],
            snapshot_id="T1-20260712",
        )
        self.assertTrue(report["citation_verification_passed"])

    def test_snapshot_must_be_explicit(self) -> None:
        inventory, evidence, reviews = self.fixture()
        with self.assertRaises(CitationError):
            verify_release(
                inventory=inventory,
                evidence=evidence,
                reviews=reviews,
                claim_reviews=[],
                waivers=[],
                snapshot_id="latest",
            )

    def test_commit_locator_records_exact_line_hash(self) -> None:
        source = {
            "locator_hint": {
                "kind": "commit_lines",
                "value": "project@" + "a" * 40 + ":SPEC.md",
            },
            "expected_identity_terms": ["Open Knowledge Format"],
        }
        locator, excerpt, found = _best_locator(
            source,
            [("document", "Open Knowledge Format")],
            "preamble\n# Open Knowledge Format\nbody\n",
        )
        self.assertTrue(found)
        self.assertEqual(locator["line_start"], 2)
        self.assertEqual(locator["line_end"], 2)
        self.assertEqual(locator["line_sha256"], digest_text("# Open Knowledge Format"))
        self.assertIn("Open Knowledge Format", excerpt)

    def test_gzip_decode_is_bounded(self) -> None:
        payload = gzip.compress(b"x" * 100)
        self.assertEqual(_decode_content_encoding(payload, "gzip", 100), b"x" * 100)
        with self.assertRaises(CitationError):
            _decode_content_encoding(payload, "gzip", 99)

    def test_json_pointer_locator_hashes_exact_resolved_value(self) -> None:
        source = {"locator_hint": {"kind": "json_pointer", "value": "/facts/total"}}
        locator, excerpt, found = _best_locator(
            source,
            [("document", '{"facts":{"total":715465}}')],
            '{"facts":{"total":715465}}',
        )
        self.assertTrue(found)
        self.assertEqual(excerpt, "715465")
        self.assertEqual(locator["resolved_value_sha256"], digest_text("715465"))

    def test_heading_set_locator_requires_every_declared_section(self) -> None:
        source = {
            "locator_hint": {
                "kind": "heading_set_fingerprint",
                "values": ["Search API (unsupported)", "Scraping the site", "Sitemap"],
            }
        }
        blocks = [
            ("h3", "Search API (unsupported)"),
            ("p", "This interface may change without notice."),
            ("h2", "Scraping the site"),
            ("h2", "Sitemap"),
        ]
        locator, excerpt, found = _best_locator(source, blocks, "")
        self.assertTrue(found)
        self.assertEqual(len(locator["members"]), 3)
        self.assertIn("Search API", excerpt)

        incomplete = blocks[:-1]
        _, _, found = _best_locator(source, incomplete, "")
        self.assertFalse(found)

    def test_https_redirect_downgrade_is_rejected(self) -> None:
        handler = _RecordingRedirect(require_https=True)
        request = urllib.request.Request("https://example.test/source")
        with self.assertRaises(CitationError):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "http://example.test/source",
            )


if __name__ == "__main__":
    unittest.main()
