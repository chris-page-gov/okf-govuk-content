from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.new_child_demo import (  # noqa: E402
    MAX_RETRY_AFTER_SECONDS,
    NewChildDemoAcquirer,
    NewChildDemoError,
    _bounded_retry_after_seconds,
    combined_search_params,
    dedupe_search_seeds,
    increment_request_counter,
    load_contract,
    rebuild_snapshot,
    safe_content_payload,
    safe_search_payload,
    search_url,
    validate_snapshot,
)


def content_id(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


class FrozenOfficialFixture:
    def __init__(
        self,
        *,
        excessive_closure: bool = False,
        drift_group: bool = False,
        failed_closure: bool = False,
        concurrent_ledger: Path | None = None,
    ) -> None:
        self.contract = load_contract()
        self.excessive_closure = excessive_closure
        self.drift_group = drift_group
        self.failed_closure = failed_closure
        self.concurrent_ledger = concurrent_ledger
        self.group_calls: dict[str, int] = {}
        self.rows = [
            {
                "_id": content_id(index),
                "content_id": content_id(index),
                "link": f"/new-child-demo/item-{index:02d}",
                "title": f"New child item {index:02d}",
                "description": "Metadata-only fixture.",
                "content_store_document_type": "guide" if index < 32 else "answer",
                "format": "guide" if index < 32 else "answer",
                "public_timestamp": "2026-07-15T00:00:00Z",
                "organisation_content_ids": [content_id(900)],
            }
            for index in range(69)
        ]
        paths = self.contract["search"]["browse_paths"]
        self.groups = {
            paths[0]: self.rows[:15],
            paths[1]: self.rows[:47],
            paths[2]: self.rows[46:],
        }

    @staticmethod
    def _response(url: str, payload: dict[str, object], *, ok: bool = True, status: int = 200):
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return body, {
            "requested_url": url,
            "final_url": url,
            "status": status,
            "ok": ok,
            "retrieved_at": "2026-07-15T06:00:00+00:00",
            "sha256": hashlib.sha256(body).hexdigest(),
            "acquisition_attempt": 1,
            "headers": {"content-type": "application/json"},
        }

    def __call__(self, url: str):
        if self.concurrent_ledger is not None:
            increment_request_counter(self.concurrent_ledger)
        parsed = urlparse(url)
        if parsed.path == "/api/search.json":
            query = parse_qs(parsed.query)
            filters = query.get("filter_mainstream_browse_pages", [])
            count = int(query.get("count", ["0"])[0])
            if len(filters) == 3:
                rows = self.rows
            elif len(filters) == 1 and filters[0] in self.groups:
                rows = self.groups[filters[0]]
                self.group_calls[filters[0]] = self.group_calls.get(filters[0], 0) + 1
                if self.drift_group and self.group_calls[filters[0]] > 1:
                    rows = rows[:-1]
            else:
                raise AssertionError(f"unexpected Search query: {url}")
            return self._response(
                url,
                {"total": len(rows), "start": 0, "results": rows[:count] if count else []},
            )
        if parsed.path.startswith("/api/content"):
            base_path = parsed.path[len("/api/content") :] or "/"
            if base_path == "/government/organisations/demo-department":
                if self.failed_closure:
                    return self._response(
                        url,
                        {"error": "temporary closure failure"},
                        ok=False,
                        status=503,
                    )
                return self._response(
                    url,
                    {
                        "content_id": content_id(900),
                        "base_path": base_path,
                        "title": "Demo department",
                        "description": "Organisation metadata.",
                        "document_type": "organisation",
                        "schema_name": "organisation",
                        "locale": "en",
                        "links": {},
                    },
                )
            if base_path.startswith("/government/organisations/demo-"):
                suffix = int(base_path.rsplit("-", 1)[-1])
                return self._response(
                    url,
                    {
                        "content_id": content_id(1000 + suffix),
                        "base_path": base_path,
                        "title": f"Demo organisation {suffix}",
                        "document_type": "organisation",
                        "schema_name": "organisation",
                        "locale": "en",
                        "links": {},
                    },
                )
            index = int(base_path.rsplit("-", 1)[-1])
            links: dict[str, list[dict[str, object]]] = {
                "primary_publishing_organisation": [
                    {
                        "content_id": content_id(900),
                        "base_path": "/government/organisations/demo-department",
                        "title": "Demo department",
                        "document_type": "organisation",
                        "schema_name": "organisation",
                        "locale": "en",
                    }
                ],
                "related": [
                    {
                        "content_id": content_id((index + 1) % 69),
                        "base_path": f"/new-child-demo/item-{(index + 1) % 69:02d}",
                        "title": "Next in-cohort item",
                        "document_type": "guide",
                        "schema_name": "guide",
                        "locale": "en",
                        "details": {"body": "nested narrative must be removed"},
                        "links": {
                            "related": [
                                {
                                    "content_id": content_id(5000 + index),
                                    "base_path": f"/nested/unclassified-{index:02d}",
                                }
                            ]
                        },
                    }
                ],
                "ordered_related_items": [
                    {
                        "web_url": "https://example.test/external-service",
                        "title": "External service",
                        "document_type": "external_content",
                    }
                ],
            }
            if self.excessive_closure:
                links["organisations"] = [
                    {
                        "content_id": content_id(1000 + index * 3 + offset),
                        "base_path": f"/government/organisations/demo-{index * 3 + offset}",
                        "title": "Unique demo organisation",
                        "document_type": "organisation",
                    }
                    for offset in range(3)
                ]
            return self._response(
                url,
                {
                    "content_id": content_id(index),
                    "base_path": base_path,
                    "title": f"New child item {index:02d}",
                    "description": "Metadata-only Content API fixture.",
                    "document_type": "guide" if index < 32 else "answer",
                    "schema_name": "guide" if index < 32 else "answer",
                    "locale": "en",
                    "first_published_at": "2025-01-01T00:00:00Z",
                    "public_updated_at": "2026-07-15T00:00:00Z",
                    "links": links,
                    "details": {
                        "body": "must never be retained",
                        "attachments": [
                            {
                                "id": f"attachment-{index}",
                                "title": "Metadata only attachment",
                                "url": "https://assets.publishing.service.gov.uk/demo.pdf",
                                "content_type": "application/pdf",
                            }
                        ],
                    },
                },
            )
        raise AssertionError(f"unexpected URL: {url}")


class NewChildDemoTests(unittest.TestCase):
    def test_retry_after_is_finite_and_fails_closed_above_ceiling(self) -> None:
        self.assertEqual(30.0, _bounded_retry_after_seconds("30"))
        self.assertEqual(0.0, _bounded_retry_after_seconds(""))
        self.assertEqual(0.0, _bounded_retry_after_seconds("not-a-delta"))
        self.assertEqual(0.0, _bounded_retry_after_seconds("nan"))
        self.assertEqual(0.0, _bounded_retry_after_seconds("inf"))
        self.assertEqual(0.0, _bounded_retry_after_seconds("-1"))
        with self.assertRaisesRegex(NewChildDemoError, "bounded acquisition ceiling"):
            _bounded_retry_after_seconds(str(MAX_RETRY_AFTER_SECONDS + 1))

    def test_exact_combined_query_repeats_three_or_filters_in_contract_order(self) -> None:
        contract = load_contract()
        url = search_url(contract["search"]["endpoint"], combined_search_params(contract, count=0))
        query = parse_qs(urlparse(url).query)
        self.assertEqual(["0"], query["count"])
        self.assertEqual(
            contract["search"]["browse_paths"],
            query["filter_mainstream_browse_pages"],
        )

    def test_deduplication_is_content_id_then_canonical_link_and_conflicts_fail(self) -> None:
        first = {"_id": content_id(1), "link": "/example", "title": "One"}
        duplicate = {"content_id": content_id(1), "link": "https://www.gov.uk/example", "title": "Duplicate"}
        self.assertEqual(1, len(dedupe_search_seeds([first, duplicate])))
        conflict = {"content_id": content_id(2), "link": "/example", "title": "Conflict"}
        with self.assertRaisesRegex(NewChildDemoError, "conflicting content IDs"):
            dedupe_search_seeds([first, conflict])
        idless = {"link": "/x", "title": "ID-less first"}
        identified = {"content_id": content_id(3), "link": "/x", "title": "Identified"}
        moved = {"content_id": content_id(3), "link": "/y", "title": "Conflicting route"}
        with self.assertRaisesRegex(NewChildDemoError, "conflicting canonical links"):
            dedupe_search_seeds([idless, identified, moved])
        with self.assertRaisesRegex(NewChildDemoError, "conflicting canonical links"):
            dedupe_search_seeds([moved, identified, idless])

    def test_search_allowlist_drops_undeclared_scalars_and_rejects_structures(self) -> None:
        safe = safe_search_payload(
            {
                "total": 1,
                "start": 0,
                "results": [
                    {
                        "_id": content_id(1),
                        "title": "Allowed",
                        "link": "/allowed",
                        "future_scalar": "drop",
                    }
                ],
            },
            ["title", "link"],
        )
        self.assertNotIn("future_scalar", safe["results"][0])
        with self.assertRaisesRegex(NewChildDemoError, "undeclared structured field"):
            safe_search_payload(
                {"results": [{"title": "Allowed", "future": {"body_like": "drop"}}]},
                ["title"],
            )
        with self.assertRaisesRegex(NewChildDemoError, "not shallow metadata"):
            safe_search_payload(
                {"results": [{"title": "Allowed", "mainstream_browse_pages": [{"title": "nested"}]}]},
                ["title", "mainstream_browse_pages"],
            )

    def test_content_allowlist_drops_body_recursively_and_keeps_attachment_metadata(self) -> None:
        safe = safe_content_payload(
            {
                "content_id": content_id(1),
                "base_path": "/example",
                "details": {
                    "body": "drop",
                    "attachments": [{"id": "a", "title": "A", "body": "drop", "url": "https://x/a"}],
                    "step_by_step_nav": {
                        "title": "Get childcare",
                        "introduction": [{"content": "drop narrative"}],
                        "steps": [
                            {
                                "title": "Check help",
                                "contents": [
                                    {"type": "paragraph", "text": "drop narrative"},
                                    {"type": "link", "text": "Check eligibility", "href": "/check"},
                                ],
                            }
                        ],
                    },
                },
                "links": {
                    "related": [
                        {
                            "base_path": "/other",
                            "html": "drop",
                            "details": {"body": "drop nested"},
                            "links": {"related": [{"base_path": "/unclassified"}]},
                        }
                    ]
                },
            }
        )
        rendered = json.dumps(safe)
        self.assertNotIn('"body"', rendered)
        self.assertNotIn('"html"', rendered)
        self.assertNotIn("drop narrative", rendered)
        self.assertNotIn('"details": {"body"', rendered)
        self.assertEqual({"base_path": "/other"}, safe["links"]["related"][0])
        self.assertEqual("a", safe["details"]["attachments"][0]["id"])
        self.assertEqual(
            {"title": "Check eligibility", "url": "/check"},
            safe["details"]["step_by_step_nav"]["steps"][0]["links"][0],
        )

    def test_frozen_acquisition_emits_exact_seed_projection_boundaries_and_offline_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "DEMO-20260715"
            programme_ledger = root / "programme-official-sources.count"
            programme_ledger.write_text("76016\n", encoding="utf-8")
            result = NewChildDemoAcquirer(
                fetcher=FrozenOfficialFixture(),
                rate_state_path=root / "rate.timestamp",
                request_ledger_path=programme_ledger,
            ).acquire("DEMO-20260715", snapshot)
            self.assertEqual(69, result["seed_records"])
            self.assertTrue(result["frozen_rebuild_identical"])

            source_records = [
                json.loads(line)
                for line in (snapshot / "publication" / "source-records.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(69, len(source_records))
            self.assertTrue(all(row["demo"]["is_seed"] for row in source_records))
            self.assertTrue(all(row["demo"]["journey_groups"][0] == "new-child-overview" for row in source_records))
            self.assertTrue(all(row["boundary_references"] for row in source_records))
            self.assertNotIn("body", json.dumps(source_records))
            for record in source_records:
                for targets in record["links"].values():
                    for target in targets:
                        self.assertFalse(any(isinstance(value, (dict, list)) for value in target.values()))
                        self.assertNotIn("links", target)
                        self.assertNotIn("details", target)
            first = source_records[0]
            self.assertIn("related", first["links"])
            self.assertIn("primary_publishing_organisation", first["links"])
            self.assertNotIn("ordered_related_items", first["links"])
            self.assertEqual(
                {"external-host", "typed-one-hop-metadata"},
                {row["boundary_class"] for row in first["boundary_references"]},
            )

            manifest = json.loads(
                (snapshot / "publication" / "cohort-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(69, manifest["counts"]["seed_denominator"])
            self.assertEqual(0, manifest["counts"]["unexplained_seed_omissions"])
            self.assertEqual(79, manifest["retrieval"]["official_request_attempts"])
            self.assertEqual([76017, 76095], manifest["retrieval"]["global_request_attempt_interval"])
            self.assertEqual(76095, manifest["retrieval"]["programme_request_count_after"])
            self.assertEqual("76095", programme_ledger.read_text(encoding="utf-8").strip())
            self.assertEqual("programme-official-source-request-counter", manifest["retrieval"]["programme_request_ledger"])
            self.assertNotIn(str(root), json.dumps(manifest))
            receipts = [
                json.loads(line)
                for line in (snapshot / "frozen" / "request-attempt-receipts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(158, len(receipts))
            self.assertEqual(79, sum(row["event"] == "request-reserved" for row in receipts))
            self.assertEqual(79, sum(row["event"] == "request-result" for row in receipts))
            self.assertFalse(manifest["raw_metadata"]["complete_page_bodies_retained"])

            rebuilt = root / "rebuilt"
            rebuilt_manifest = rebuild_snapshot(snapshot, rebuilt)
            self.assertEqual(69, rebuilt_manifest["counts"]["seed_records"])
            self.assertEqual(
                (snapshot / "publication" / "source-records.jsonl").read_bytes(),
                (rebuilt / "source-records.jsonl").read_bytes(),
            )
            self.assertEqual("pass", validate_snapshot(snapshot)["status"])
            with self.assertRaisesRegex(NewChildDemoError, "outside the immutable source snapshot"):
                rebuild_snapshot(snapshot, snapshot / "nested-rebuild")

            contract_path = snapshot / "contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["title"] = "Tampered after freeze"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            with self.assertRaisesRegex(NewChildDemoError, "embedded cohort contract"):
                rebuild_snapshot(snapshot, root / "tampered-rebuild")

    def test_group_membership_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "DRIFT"
            with self.assertRaisesRegex(NewChildDemoError, "group membership changed"):
                NewChildDemoAcquirer(
                    fetcher=FrozenOfficialFixture(drift_group=True),
                    rate_state_path=root / "rate.timestamp",
                ).acquire("DRIFT-DEMO", output)
            self.assertFalse(output.exists())
            self.assertEqual(1, len(list(root.glob(".DRIFT-DEMO.*.failed"))))

    def test_failed_one_hop_observation_is_bound_to_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "FAILED-CLOSURE"
            NewChildDemoAcquirer(
                fetcher=FrozenOfficialFixture(failed_closure=True),
                rate_state_path=root / "rate.timestamp",
            ).acquire("FAILED-CLOSURE", snapshot)
            records = [
                json.loads(line)
                for line in (snapshot / "publication" / "source-records.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            publisher = next(
                boundary
                for boundary in records[0]["boundary_references"]
                if boundary["predicate"] == "primary_publishing_organisation"
            )
            self.assertEqual("content-api-unavailable-or-non-content-route", publisher["boundary_class"])
            self.assertEqual(503, publisher["closure_observation"]["status"])

    def test_concurrent_programme_counter_is_attributed_by_exact_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "programme.count"
            ledger.write_text("100\n", encoding="utf-8")
            snapshot = root / "INTERLEAVED"
            NewChildDemoAcquirer(
                fetcher=FrozenOfficialFixture(concurrent_ledger=ledger),
                rate_state_path=root / "rate.timestamp",
                request_ledger_path=ledger,
            ).acquire("INTERLEAVED", snapshot)
            manifest = json.loads(
                (snapshot / "publication" / "cohort-manifest.json").read_text(encoding="utf-8")
            )
            retrieval = manifest["retrieval"]
            self.assertEqual(79, retrieval["official_request_attempts"])
            self.assertIsNone(retrieval["global_request_attempt_interval"])
            self.assertEqual(79, len(retrieval["programme_request_sequences"]))
            self.assertEqual(list(range(101, 259, 2)), retrieval["programme_request_sequences"])
            self.assertEqual("258", ledger.read_text(encoding="utf-8").strip())

    def test_staged_tree_is_validated_before_immutable_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "NOT-PROMOTED"
            with patch(
                "govuk_okf.new_child_demo.validate_snapshot",
                side_effect=NewChildDemoError("synthetic staged validation failure"),
            ):
                with self.assertRaisesRegex(NewChildDemoError, "synthetic staged validation failure"):
                    NewChildDemoAcquirer(
                        fetcher=FrozenOfficialFixture(),
                        rate_state_path=root / "rate.timestamp",
                    ).acquire("NOT-PROMOTED", output)
            self.assertFalse(output.exists())
            failed = list(root.glob(".NOT-PROMOTED.*.failed"))
            self.assertEqual(1, len(failed))
            self.assertTrue((failed[0] / "snapshot-manifest.json").is_file())

    def test_retained_one_hop_metadata_ceiling_fails_before_fetching_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            programme_ledger = root / "programme.count"
            programme_ledger.write_text("76016\n", encoding="utf-8")
            with self.assertRaisesRegex(NewChildDemoError, "exceeds retained-record ceiling"):
                NewChildDemoAcquirer(
                    fetcher=FrozenOfficialFixture(excessive_closure=True),
                    rate_state_path=root / "rate.timestamp",
                    request_ledger_path=programme_ledger,
                ).acquire("DEMO-OVER-CAP", root / "DEMO-OVER-CAP")
            failed = list(root.glob(".DEMO-OVER-CAP.*.failed"))
            self.assertEqual(1, len(failed))
            self.assertEqual("74", (failed[0] / "frozen" / "official-request-attempts.count").read_text().strip())
            failure = json.loads((failed[0] / "failure.json").read_text(encoding="utf-8"))
            self.assertEqual(74, failure["local_official_request_attempts"])
            self.assertEqual("NewChildDemoError", failure["error_type"])
            self.assertEqual([76017, 76090], failure["global_request_attempt_interval"])
            self.assertEqual("76090", programme_ledger.read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    unittest.main()
