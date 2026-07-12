from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.acquisition import (  # noqa: E402
    AcquisitionError,
    read_jsonl_gzip,
    write_jsonl_gzip,
)
from govuk_okf.closing import (  # noqa: E402
    ClosingDelta,
    ClosingError,
    enumerator_fingerprint,
    iter_record_input,
    write_record_shards,
)
from govuk_okf.util import canonical_json_bytes  # noqa: E402


def record(
    path: str,
    title: str,
    *,
    source_memberships: list[str] | None = None,
    content_id: str | None = None,
    retrieved_at: str = "2026-07-12T00:00:00Z",
    evidence_sha256: str = "a" * 64,
    public_updated_at: str = "2026-07-11T00:00:00Z",
) -> dict[str, Any]:
    url = "https://www.gov.uk" + path
    value: dict[str, Any] = {
        "candidate_key": "enumerator-key-" + path,
        "entity_class": "route",
        "source_native_id": url,
        "source_id": "search-api-v1",
        "source_memberships": source_memberships or ["search-guidance-ascending", "sitemap"],
        "coverage_disposition": "represented",
        "canonical_url": url,
        "base_path": path,
        "title": title,
        "description": f"Metadata for {title}",
        "document_type": "guidance",
        "schema_name": "publication",
        "locale": "en",
        "public_updated_at": public_updated_at,
        "links": {},
        "retrieved_at": retrieved_at,
        "evidence_url": "https://www.gov.uk/api/search.json",
        "evidence_sha256": evidence_sha256,
        "evidence_locator": "/results/0",
    }
    if content_id:
        value["content_id"] = content_id
        value["source_native_id"] = content_id
        value["source_id"] = "content-api"
        value["source_memberships"] = source_memberships or ["structured-content-api"]
        value["evidence_url"] = "https://www.gov.uk/api/content" + path
    return value


class ClosingFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.t0_enum = root / "inputs" / "t0-enum.jsonl.gz"
        self.t0_hydrated = root / "inputs" / "t0-hydrated.jsonl.gz"
        self.t1_enum = root / "inputs" / "t1-enum.jsonl.gz"
        self.t0_reconciliation = root / "inputs" / "t0-reconciliation.json"
        self.t1_reconciliation = root / "inputs" / "t1-reconciliation.json"
        old_records = [
            record("/same", "Same"),
            record("/changed", "Old changed"),
            record("/linked", "Linked", source_memberships=["structured-linked-content"]),
            record("/removed-redirect", "Old redirect"),
            record("/removed-gone", "Old gone"),
            record("/removed-error", "Old error"),
        ]
        hydrated = [
            record(row["base_path"], row["title"], content_id=row["base_path"].strip("/") + "-id")
            for row in old_records
        ]
        hydrated[0]["details"] = {
            "attachments": [
                {
                    "id": "same-attachment",
                    "title": "Same attachment",
                    "url": "https://assets.publishing.service.gov.uk/same.pdf",
                    "content_type": "application/pdf",
                }
            ]
        }
        current_records = [
            record(
                "/same",
                "Same",
                retrieved_at="2026-07-13T00:00:00Z",
                evidence_sha256="b" * 64,
            ),
            record("/changed", "New changed", retrieved_at="2026-07-13T00:00:00Z"),
            record("/new", "New", retrieved_at="2026-07-13T00:00:00Z"),
            record(
                "/linked",
                "Linked",
                source_memberships=["structured-linked-content"],
                retrieved_at="2026-07-13T00:00:00Z",
                evidence_sha256="c" * 64,
            ),
        ]
        _, self.t0_enum_digest = write_jsonl_gzip(self.t0_enum, old_records)
        _, self.t0_hydrated_digest = write_jsonl_gzip(self.t0_hydrated, hydrated)
        _, self.t1_enum_digest = write_jsonl_gzip(self.t1_enum, current_records)
        self.t0_reconciliation.parent.mkdir(parents=True, exist_ok=True)
        self.t0_reconciliation.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "snapshot": "T0",
                    "sampled": False,
                    "hydrated": True,
                    "unexplained_omissions": 0,
                    "publication_records": len(hydrated),
                    "inventory_canonical_sha256": self.t0_enum_digest,
                    "hydrated_records_canonical_sha256": self.t0_hydrated_digest,
                    "hydration_proof": {"closed": True, "pending": 0},
                }
            ),
            encoding="utf-8",
        )
        self.write_t1_reconciliation()

    def write_t1_reconciliation(self, *, sampled: bool = False, unexplained: int = 0) -> None:
        self.t1_reconciliation.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "snapshot": "T1",
                    "sampled": sampled,
                    "unexplained_omissions": unexplained,
                    "publication_records": 4,
                    "inventory_canonical_sha256": self.t1_enum_digest,
                    "search_partitions_closed": True,
                    "search_partition_proofs": [
                        {
                            "partition": "guidance",
                            "expected": 4,
                            "passes": [
                                {
                                    "order": "public_timestamp",
                                    "returned_rows": 4,
                                    "unique_source_rows": 4,
                                    "unique_urls": 4,
                                    "canonical_alias_rows": 0,
                                    "closed": True,
                                },
                                {
                                    "order": "-public_timestamp",
                                    "returned_rows": 4,
                                    "unique_source_rows": 4,
                                    "unique_urls": 4,
                                    "canonical_alias_rows": 0,
                                    "closed": True,
                                },
                            ],
                        }
                    ],
                    "sitemap_byte_stable": True,
                    "sitemap_proof": {"closed": True},
                    "organisations_proof": {"closed": True},
                    "navigation_proof": {"closed": True},
                }
            ),
            encoding="utf-8",
        )

    def closing(
        self,
        *,
        label: str = "T1-closed",
        official_request_ceiling: int = 1_000_000,
    ) -> ClosingDelta:
        return ClosingDelta(
            self.root,
            "T0",
            "T1",
            self.t0_enum,
            self.t0_hydrated,
            self.t1_enum,
            self.t0_reconciliation,
            self.t1_reconciliation,
            closing_label=label,
            requests_per_second=100_000,
            www_requests_per_second=50_000,
            official_request_ceiling=official_request_ceiling,
            workers=3,
            batch_size=3,
        )

    @staticmethod
    def observation(url: str, **_: object) -> tuple[bytes, dict[str, Any]]:
        retrieved_at = "2026-07-13T01:00:00Z"
        if url == "https://www.gov.uk/robots.txt":
            body = b"User-agent: *\nDisallow:\n"
            return body, {
                "ok": True,
                "partial": False,
                "status": 200,
                "requested_url": url,
                "final_url": url,
                "retrieved_at": retrieved_at,
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        if url.startswith("https://www.gov.uk/api/content"):
            path = url.removeprefix("https://www.gov.uk/api/content") or "/"
            if path in {"/same", "/changed", "/new", "/linked"}:
                payload = {
                    "content_id": path.strip("/") + "-id",
                    "base_path": path,
                    "title": {
                        "/same": "Same",
                        "/changed": "New changed",
                        "/new": "New",
                        "/linked": "Linked",
                    }[path],
                    "description": "Current Content API metadata",
                    "document_type": "guidance",
                    "schema_name": "publication",
                    "locale": "en",
                    "public_updated_at": "2026-07-13T00:00:00Z",
                    "links": {},
                    "details": {"body": "transient and forbidden from output"},
                }
                body = json.dumps(payload).encode("utf-8")
                return body, {
                    "ok": True,
                    "partial": False,
                    "status": 200,
                    "requested_url": url,
                    "final_url": url,
                    "retrieved_at": retrieved_at,
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
            return b"", {
                "ok": False,
                "partial": False,
                "status": 404,
                "error": "HTTP 404",
                "requested_url": url,
                "final_url": url,
                "retrieved_at": retrieved_at,
                "sha256": hashlib.sha256(b"").hexdigest(),
            }
        if url.endswith("/removed-redirect"):
            return b"x", {
                "ok": True,
                "partial": True,
                "status": 200,
                "requested_url": url,
                "final_url": "https://www.gov.uk/replacement",
                "retrieved_at": retrieved_at,
                "sha256": hashlib.sha256(b"x").hexdigest(),
            }
        if url.endswith("/removed-gone"):
            status, error = 410, "HTTP 410"
        elif url.endswith("/removed-error"):
            status, error = 503, "HTTP 503"
        else:
            status, error = 404, "HTTP 404"
        return b"", {
            "ok": False,
            "partial": False,
            "status": status,
            "error": error,
            "requested_url": url,
            "final_url": url,
            "retrieved_at": retrieved_at,
            "sha256": hashlib.sha256(b"").hexdigest(),
        }


class ClosingTests(unittest.TestCase):
    def test_fingerprint_ignores_only_observation_fields_and_rejects_bodies(self) -> None:
        before = record("/example", "Example", retrieved_at="2026-07-12T00:00:00Z")
        after = dict(before)
        after["retrieved_at"] = "2026-07-13T00:00:00Z"
        after["evidence_sha256"] = "f" * 64
        self.assertEqual(enumerator_fingerprint(before), enumerator_fingerprint(after))
        after["title"] = "Changed title"
        self.assertNotEqual(enumerator_fingerprint(before), enumerator_fingerprint(after))
        before["details"] = {"body": "complete page text"}
        with self.assertRaises(ClosingError):
            enumerator_fingerprint(before)

    def test_closing_delta_reuses_only_exact_match_hydrates_changes_and_probes_removals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ClosingFixture(Path(directory))
            closing = fixture.closing()
            calls: list[str] = []

            def observe(url: str, **kwargs: object) -> tuple[bytes, dict[str, Any]]:
                calls.append(url)
                return fixture.observation(url, **kwargs)

            with patch("govuk_okf.closing.request_observation", side_effect=observe):
                progress = closing.run()
            self.assertTrue(progress["closed"])
            self.assertEqual(
                {"added": 1, "changed": 1, "removed": 3, "unchanged": 2},
                progress["delta_counts"],
            )
            self.assertEqual(
                {"exception": 1, "redirect": 1, "represented": 4, "tombstone": 1},
                progress["closing_dispositions"],
            )
            self.assertNotIn("https://www.gov.uk/api/content/same", calls)
            self.assertEqual(10, len(calls))
            accounting = progress["request_accounting"]
            self.assertEqual(10, accounting["used"])
            self.assertEqual(6, accounting["by_class"]["content_api"]["used"])
            self.assertEqual(4, accounting["by_class"]["www_public"]["used"])
            self.assertEqual(
                100_000,
                accounting["by_class"]["content_api"]["requests_per_second"],
            )
            self.assertEqual(
                50_000,
                accounting["by_class"]["www_public"]["requests_per_second"],
            )
            self.assertNotEqual(
                accounting["by_class"]["content_api"]["state_ledger"],
                accounting["by_class"]["www_public"]["state_ledger"],
            )
            self.assertEqual(0, accounting["reserved"])
            self.assertEqual(1_000_000, accounting["ceiling"])

            reconciliation = closing.export()
            self.assertEqual(0, reconciliation["pending"])
            self.assertEqual(0, reconciliation["unexplained_omissions"])
            self.assertEqual(
                3,
                reconciliation["closing_probe_proof"]["actively_probed"],
            )
            self.assertTrue(reconciliation["closing_probe_proof"]["closed"])
            self.assertEqual(3, reconciliation["closing_probe_proof"]["t0_routes_absent_from_t1"])
            self.assertEqual(
                hashlib.sha256(b"User-agent: *\nDisallow:\n").hexdigest(),
                reconciliation["closing_probe_proof"]["robots"]["sha256"],
            )
            self.assertEqual(1, reconciliation["fingerprint_proof"]["reused_records"])
            self.assertEqual(accounting, reconciliation["request_accounting"])
            self.assertEqual(
                {"added": 1, "changed": 1, "removed": 3, "unchanged": 2},
                {
                    name: row["count"]
                    for name, row in reconciliation["set_differences"].items()
                },
            )
            self.assertTrue(
                all(
                    len(row["identity_sha256"]) == 64
                    for row in reconciliation["set_differences"].values()
                )
            )
            self.assertEqual(7, reconciliation["entity_class_accounting"]["route"]["expected_candidate_keys"])
            self.assertEqual(
                1,
                reconciliation["entity_class_accounting"]["resource"]["expected_candidate_keys"],
            )
            self.assertTrue(
                all(row["accounting_closed"] for row in reconciliation["entity_class_accounting"].values())
            )
            self.assertFalse(
                (Path(directory) / "corpus/records/T1-closed/source-records.jsonl.gz").exists()
            )
            manifest_path = Path(directory) / reconciliation["hydrated_records_manifest"]
            self.assertTrue(manifest_path.is_file())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(manifest["shards"])
            self.assertTrue(all(row["records"] <= 10_000 for row in manifest["shards"]))
            self.assertTrue(all(row["bytes"] < 50 * 1024 * 1024 for row in manifest["shards"]))
            output = list(iter_record_input(manifest_path, Path(directory)))
            self.assertEqual(7, len(output))
            same = next(row for row in output if row["base_path"] == "/same")
            self.assertTrue(same["closing_reuse"]["matched"])
            self.assertIn("attachments", same["details"])
            self.assertTrue(all("body" not in row.get("details", {}) for row in output))

            first_reconciliation = (
                Path(directory) / "corpus/reconciliation/T1-closed.json"
            ).read_bytes()
            first_manifest = (Path(directory) / "corpus/records/T1-closed/manifest.json").read_bytes()
            global_budget = Path(directory) / ".tmp/request-budget/official-sources.count"
            global_budget.parent.mkdir(parents=True, exist_ok=True)
            global_budget.write_text("9999\n", encoding="utf-8")
            closing.export()
            self.assertEqual(
                first_reconciliation,
                (Path(directory) / "corpus/reconciliation/T1-closed.json").read_bytes(),
            )
            self.assertEqual(
                first_manifest,
                (Path(directory) / "corpus/records/T1-closed/manifest.json").read_bytes(),
            )

    def test_open_or_unreconciled_inputs_fail_closed_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ClosingFixture(Path(directory))
            closing = fixture.closing(label="T1-resume")
            with patch("govuk_okf.closing.request_observation", side_effect=fixture.observation):
                first = closing.run(work_limit=2)
            self.assertFalse(first["closed"])
            with self.assertRaises(ClosingError):
                closing.export()
            with patch("govuk_okf.closing.request_observation", side_effect=fixture.observation):
                second = closing.run()
            self.assertTrue(second["closed"])
            self.assertEqual(0, closing.export()["unexplained_omissions"])

        with tempfile.TemporaryDirectory() as directory:
            fixture = ClosingFixture(Path(directory))
            fixture.write_t1_reconciliation(sampled=True)
            with self.assertRaisesRegex(ClosingError, "sampled"):
                fixture.closing().prepare()
            fixture.write_t1_reconciliation(unexplained=1)
            with self.assertRaisesRegex(ClosingError, "pending or unexplained"):
                fixture.closing().prepare()
            fixture.write_t1_reconciliation()
            reconciliation = json.loads(fixture.t1_reconciliation.read_text(encoding="utf-8"))
            reconciliation["inventory_canonical_sha256"] = "0" * 64
            fixture.t1_reconciliation.write_text(json.dumps(reconciliation), encoding="utf-8")
            with self.assertRaisesRegex(ClosingError, "not bound to its reconciliation digest"):
                fixture.closing().prepare()

    def test_t1_search_proof_distinguishes_source_rows_from_canonical_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ClosingFixture(Path(directory))
            reconciliation = json.loads(fixture.t1_reconciliation.read_text(encoding="utf-8"))
            for row in reconciliation["search_partition_proofs"][0]["passes"]:
                row["unique_urls"] = 3
                row["canonical_alias_rows"] = 1
                row["identity_sha256"] = "a" * 64
            fixture.t1_reconciliation.write_text(json.dumps(reconciliation), encoding="utf-8")
            fixture.closing(label="T1-alias-proof").prepare()

            reconciliation["search_partition_proofs"][0]["passes"][1][
                "canonical_alias_rows"
            ] = 0
            fixture.t1_reconciliation.write_text(json.dumps(reconciliation), encoding="utf-8")
            with self.assertRaisesRegex(ClosingError, "did not close"):
                fixture.closing(label="T1-invalid-alias-proof").prepare()

    def test_exact_census_match_does_not_reuse_incomplete_t0_hydration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ClosingFixture(Path(directory))
            hydrated = list(read_jsonl_gzip(fixture.t0_hydrated))
            same = next(row for row in hydrated if row["base_path"] == "/same")
            same["hydration_status"] = "content_api_exception"
            same["constraints"] = [
                {"class": "content_api_hydration", "reason": "transient T0 failure"}
            ]
            _, digest = write_jsonl_gzip(fixture.t0_hydrated, hydrated)
            reconciliation = json.loads(fixture.t0_reconciliation.read_text(encoding="utf-8"))
            reconciliation["hydrated_records_canonical_sha256"] = digest
            fixture.t0_reconciliation.write_text(json.dumps(reconciliation), encoding="utf-8")
            calls: list[str] = []

            def observe(url: str, **kwargs: object) -> tuple[bytes, dict[str, Any]]:
                calls.append(url)
                return fixture.observation(url, **kwargs)

            with patch("govuk_okf.closing.request_observation", side_effect=observe):
                progress = fixture.closing(label="T1-retry-incomplete").run()
            self.assertEqual(0, progress["work_kinds"].get("reuse", 0))
            self.assertEqual(1, progress["work_kinds"]["hydrate_unreusable_t0"])
            self.assertIn("https://www.gov.uk/api/content/same", calls)

    def test_public_fallback_probe_respects_current_robots_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ClosingFixture(Path(directory))
            calls: list[str] = []

            def observe(url: str, **kwargs: object) -> tuple[bytes, dict[str, Any]]:
                calls.append(url)
                if url == "https://www.gov.uk/robots.txt":
                    body = b"User-agent: *\nDisallow: /removed-gone\n"
                    return body, {
                        "ok": True,
                        "partial": False,
                        "status": 200,
                        "requested_url": url,
                        "final_url": url,
                        "retrieved_at": "2026-07-13T01:00:00Z",
                        "sha256": hashlib.sha256(body).hexdigest(),
                    }
                return fixture.observation(url, **kwargs)

            closing = fixture.closing(label="T1-robots")
            with patch("govuk_okf.closing.request_observation", side_effect=observe):
                progress = closing.run()
            self.assertNotIn("https://www.gov.uk/removed-gone", calls)
            self.assertEqual(2, progress["closing_dispositions"]["exception"])
            self.assertNotIn("tombstone", progress["closing_dispositions"])
            reconciliation = closing.export()
            self.assertEqual(3, reconciliation["closing_probe_proof"]["actively_probed"])
            self.assertEqual(0, reconciliation["unexplained_omissions"])

    def test_official_request_ceiling_is_reserved_before_network_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ClosingFixture(Path(directory))
            calls: list[str] = []

            def observe(url: str, **kwargs: object) -> tuple[bytes, dict[str, Any]]:
                calls.append(url)
                body, evidence = fixture.observation(url, **kwargs)
                if url == "https://www.gov.uk/robots.txt":
                    evidence = {**evidence, "acquisition_attempt": 3}
                return body, evidence

            closing = fixture.closing(label="T1-budget", official_request_ceiling=5)
            with patch("govuk_okf.closing.request_observation", side_effect=observe):
                with self.assertRaisesRegex(ClosingError, "official request ceiling"):
                    closing.run()
            self.assertEqual(["https://www.gov.uk/robots.txt"], calls)
            connection = closing._connect()
            try:
                accounting = closing._request_accounting(connection)
            finally:
                connection.close()
            self.assertEqual(3, accounting["used"])
            self.assertEqual(2, accounting["remaining"])
            self.assertEqual(0, accounting["reserved"])

    def test_request_accounting_includes_prior_pipeline_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = ClosingFixture(root)
            budget = root / ".tmp/request-budget/official-sources.count"
            budget.parent.mkdir(parents=True)
            budget.write_text("100\n", encoding="utf-8")
            closing = fixture.closing(label="T1-prior-budget")
            with patch("govuk_okf.closing.request_observation", side_effect=fixture.observation):
                progress = closing.run(work_limit=1)
            accounting = progress["request_accounting"]
            self.assertEqual(100, accounting["prior_stage_used"])
            self.assertEqual(2, accounting["closing_stage_used"])
            self.assertEqual(102, accounting["used"])
            self.assertEqual(999_898, accounting["remaining"])
            # In production HostLimiter has already advanced the shared file
            # for the same two attempts. max(global, baseline + local) must not
            # add those attempts a second time.
            budget.write_text("102\n", encoding="utf-8")
            connection = closing._connect()
            try:
                reconciled = closing._request_accounting(connection)
            finally:
                connection.close()
            self.assertEqual(102, reconciled["used"])

    def test_observation_exception_is_charged_and_wrapped_without_dangling_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ClosingFixture(Path(directory))
            closing = fixture.closing(label="T1-observation-error", official_request_ceiling=100)
            with patch(
                "govuk_okf.closing.request_observation",
                side_effect=AcquisitionError("official-source request ceiling exhausted"),
            ):
                with self.assertRaisesRegex(ClosingError, "official-source request ceiling exhausted"):
                    closing.run()
            connection = closing._connect()
            try:
                accounting = closing._request_accounting(connection)
            finally:
                connection.close()
            self.assertEqual(5, accounting["closing_stage_used"])
            self.assertEqual(5, accounting["used"])
            self.assertEqual(0, accounting["reserved"])

    def test_manifest_input_and_record_and_byte_shard_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [record(f"/item-{index}", f"Item {index}") for index in range(5)]
            manifest = write_record_shards(
                root,
                root / "records",
                rows,
                schema="test-records.v1",
                snapshot="test",
                max_records=2,
                max_compressed_bytes=10_000,
            )
            self.assertEqual([2, 2, 1], [row["count"] for row in manifest["shards"]])
            loaded = list(iter_record_input(root / manifest["manifest_path"], root))
            self.assertEqual(rows, loaded)
            self.assertEqual(
                rows,
                list(iter_record_input((root / manifest["manifest_path"]).parent, root)),
            )
            digest = hashlib.sha256()
            for row in rows:
                digest.update(canonical_json_bytes(row))
            self.assertEqual(digest.hexdigest(), manifest["canonical_sha256"])
            with self.assertRaisesRegex(ClosingError, "compressed JSONL shard exceeds"):
                write_record_shards(
                    root,
                    root / "too-small",
                    [rows[0]],
                    schema="test-records.v1",
                    snapshot="test",
                    max_records=1,
                    max_compressed_bytes=1,
                )


if __name__ == "__main__":
    unittest.main()
