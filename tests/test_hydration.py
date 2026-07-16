from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import govuk_okf.hydration as hydration_module  # noqa: E402
from govuk_okf.hydration import CorpusHydrator, HydrationError, read_source_records  # noqa: E402


class HydrationTests(unittest.TestCase):
    @staticmethod
    def record(path: str = "/", *, description: str = "") -> dict[str, object]:
        url = f"https://www.gov.uk{path}" if path != "/" else "https://www.gov.uk/"
        return {
            "candidate_key": f"route-{path}",
            "entity_class": "route",
            "source_native_id": url,
            "source_id": "govuk-sitemap",
            "source_memberships": ["sitemap"],
            "coverage_disposition": "represented",
            "canonical_url": url,
            "base_path": path,
            "title": path.strip("/").title() or "GOV.UK",
            "description": description,
            "document_type": "homepage" if path == "/" else "guidance",
            "schema_name": "homepage" if path == "/" else "publication",
            "locale": "en",
            "links": {},
        }

    def write_source(self, root: Path, records: list[dict[str, object]]) -> Path:
        launch = root / "governance" / "launch-manifest.yaml"
        launch.parent.mkdir(parents=True, exist_ok=True)
        if not launch.exists():
            launch.write_text("ceilings:\n  minimum_free_disk_gib: 1\n", encoding="utf-8")
        path = root / "corpus" / "inventory" / "T0-source-records.jsonl.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")
        return path

    def source(self, root: Path) -> Path:
        launch = root / "governance" / "launch-manifest.yaml"
        launch.parent.mkdir(parents=True, exist_ok=True)
        launch.write_text("ceilings:\n  minimum_free_disk_gib: 1\n", encoding="utf-8")
        return self.write_source(root, [self.record()])

    @staticmethod
    def observation(url: str, **_: object):
        path = url.removeprefix("https://www.gov.uk/api/content") or "/"
        if path == "/":
            payload = {
                "content_id": "root-id",
                "base_path": "/",
                "title": "GOV.UK",
                "document_type": "homepage",
                "schema_name": "homepage",
                "locale": "en",
                "links": {
                    "child_taxons": [
                        {
                            "content_id": "child-id",
                            "base_path": "/child",
                            "title": "Child",
                            "document_type": "taxon",
                            "schema_name": "taxon",
                            "locale": "en",
                        }
                    ]
                },
                "details": {"body": "must not survive"},
            }
        else:
            payload = {
                "content_id": "child-id",
                "base_path": "/child",
                "title": "Child",
                "document_type": "taxon",
                "schema_name": "taxon",
                "locale": "en",
                "links": {},
                "details": {
                    "body": "must not survive",
                    "attachments": [
                        {
                            "id": "attachment-id",
                            "title": "Attachment",
                            "url": "https://assets.publishing.service.gov.uk/file.pdf",
                            "content_type": "application/pdf",
                        }
                    ],
                },
            }
        body = json.dumps(payload).encode()
        return body, {
            "ok": True,
            "status": 200,
            "partial": False,
            "requested_url": url,
            "final_url": url,
            "retrieved_at": "2026-07-12T00:00:00Z",
            "sha256": "a" * 64,
        }

    def test_recursive_hydration_is_resumable_metadata_only_and_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hydrator = CorpusHydrator(root, "T0", self.source(root), requests_per_second=100000, workers=2)
            with patch("govuk_okf.hydration.request_observation", side_effect=self.observation):
                progress = hydrator.run(request_limit=1)
            self.assertFalse(progress["closed"])
            checkpoint = root / "corpus/cache/T0/hydration/checkpoint.sqlite"
            with sqlite3.connect(checkpoint) as connection:
                rows = connection.execute(
                    "SELECT state, input_json IS NULL, record_json IS NULL FROM queue ORDER BY url"
                ).fetchall()
            self.assertEqual(
                [("complete", 1, 0), ("pending", 0, 1)],
                rows,
            )

            resumed = CorpusHydrator(root, "T0", self.source(root), requests_per_second=100000, workers=2)
            with patch("govuk_okf.hydration.request_observation", side_effect=self.observation):
                progress = resumed.run()
            self.assertTrue(progress["closed"])
            self.assertEqual(2, progress["queue_records"])
            self.assertEqual(1, progress["processed_this_run"])
            reconciliation = resumed.export()
            self.assertEqual(0, reconciliation["unexplained_omissions"])
            self.assertEqual(2, reconciliation["publication_records"])
            self.assertEqual(1, reconciliation["entity_class_counts"]["resource"])
            rows = list(read_source_records(root / reconciliation["hydrated_records_path"]))
            self.assertEqual(
                ["https://www.gov.uk/", "https://www.gov.uk/child"],
                [row["canonical_url"] for row in rows],
            )
            self.assertTrue(all("body" not in row.get("details", {}) for row in rows))
            self.assertTrue(reconciliation["storage_accounting"]["minimum_free_disk_satisfied"])
            with sqlite3.connect(checkpoint) as connection:
                self.assertEqual(
                    [("complete", 1), ("complete", 1)],
                    connection.execute(
                        "SELECT state, input_json IS NULL FROM queue ORDER BY url"
                    ).fetchall(),
                )
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])

    def test_bounded_run_cannot_export_an_open_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hydrator = CorpusHydrator(root, "T0", self.source(root), requests_per_second=100000, workers=1)
            with patch("govuk_okf.hydration.request_observation", side_effect=self.observation):
                progress = hydrator.run(request_limit=1)
            self.assertFalse(progress["closed"])
            with self.assertRaises(HydrationError):
                hydrator.export()

    def test_legacy_queue_migrates_and_clears_only_completed_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.source(root)
            database = root / "corpus/cache/T0/hydration/checkpoint.sqlite"
            database.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE queue (
                        url TEXT NOT NULL,
                        locale TEXT NOT NULL,
                        input_json TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('pending', 'complete')),
                        hydration_status TEXT,
                        record_json TEXT,
                        PRIMARY KEY (url, locale)
                    );
                    CREATE INDEX queue_state_url ON queue(state, url, locale);
                    """
                )
                connection.execute(
                    "INSERT INTO queue VALUES (?, 'en', '{}', 'complete', 'represented', '{}')",
                    ("https://www.gov.uk/complete",),
                )
                connection.execute(
                    "INSERT INTO queue VALUES (?, 'en', '{}', 'pending', NULL, NULL)",
                    ("https://www.gov.uk/pending",),
                )

            hydrator = CorpusHydrator(root, "T0", source, requests_per_second=100000)
            connection = hydrator._connect()
            try:
                input_column = next(
                    row for row in connection.execute("PRAGMA table_info(queue)") if row[1] == "input_json"
                )
                self.assertEqual(0, input_column[3])
                self.assertEqual(
                    [("complete", 1, 0), ("pending", 0, 1)],
                    connection.execute(
                        "SELECT state, input_json IS NULL, record_json IS NULL FROM queue ORDER BY url"
                    ).fetchall(),
                )
            finally:
                connection.close()

    def test_minimum_free_disk_stops_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hydrator = CorpusHydrator(
                root,
                "T0",
                self.source(root),
                requests_per_second=100000,
                minimum_free_disk_bytes=1024,
            )
            with patch("govuk_okf.storage.free_disk_bytes", return_value=1023), patch(
                "govuk_okf.hydration.request_observation"
            ) as request:
                with self.assertRaisesRegex(HydrationError, "insufficient free disk"):
                    hydrator.run()
            request.assert_not_called()

    def test_changed_unstarted_source_replaces_pending_queue_and_rendered_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.source(root)
            hydrator = CorpusHydrator(root, "T0", source, requests_per_second=100000)
            connection = hydrator._connect()
            try:
                hydrator.prepare(connection)
                connection.executescript(
                    """
                    CREATE TABLE rendered_selection (
                        url TEXT NOT NULL,
                        locale TEXT NOT NULL,
                        stratum TEXT NOT NULL,
                        selection_sha256 TEXT NOT NULL,
                        PRIMARY KEY (url, locale)
                    );
                    INSERT INTO rendered_selection VALUES (
                        'https://www.gov.uk/', 'en', 'old', 'old-digest'
                    );
                    INSERT INTO meta(key, value) VALUES ('rendered_selection_limit', '1');
                    """
                )
                connection.commit()
            finally:
                connection.close()

            self.write_source(root, [self.record("/new")])
            replacement = CorpusHydrator(root, "T0", source, requests_per_second=100000)
            connection = replacement._connect()
            try:
                replacement.prepare(connection)
                self.assertEqual(
                    [("https://www.gov.uk/new", "pending")],
                    connection.execute("SELECT url, state FROM queue ORDER BY url").fetchall(),
                )
                self.assertEqual(
                    0,
                    connection.execute("SELECT COUNT(*) FROM rendered_selection").fetchone()[0],
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT value FROM meta WHERE key='rendered_selection_limit'"
                    ).fetchone()
                )
                self.assertEqual(
                    "1",
                    connection.execute(
                        "SELECT value FROM meta WHERE key='source_records'"
                    ).fetchone()[0],
                )
            finally:
                connection.close()

    def test_prepare_reserves_variable_source_payload_before_sqlite_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_source(
                root,
                [self.record(description="x" * (4 * 1024 * 1024))],
            )
            hydrator = CorpusHydrator(
                root,
                "T0",
                source,
                requests_per_second=100000,
                minimum_free_disk_bytes=16 * 1024 * 1024,
            )
            original_assert = hydrator._assert_retained_storage

            def reject_prepare(*, phase: str, **kwargs: object) -> int:
                if phase == "hydration preparation pre-write":
                    raise HydrationError("hydration preparation pre-write")
                return original_assert(phase=phase, **kwargs)

            with patch.object(
                hydrator, "_assert_retained_storage", side_effect=reject_prepare
            ), patch("govuk_okf.hydration.request_observation") as request:
                with self.assertRaisesRegex(HydrationError, "hydration preparation pre-write"):
                    hydrator.run()
            request.assert_not_called()
            with sqlite3.connect(hydrator.database_path) as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM queue").fetchone()[0])

    def test_conflicting_duplicate_route_fails_without_erasing_source_distinction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.record("/duplicate", description="first observation")
            second = self.record("/duplicate", description="different observation")
            source = self.write_source(root, [first, second])
            hydrator = CorpusHydrator(
                root,
                "T0",
                source,
                requests_per_second=100000,
                minimum_free_disk_bytes=1024**3,
            )
            with patch("govuk_okf.hydration.request_observation") as request:
                with self.assertRaises(sqlite3.IntegrityError):
                    hydrator.run()
            request.assert_not_called()
            with sqlite3.connect(hydrator.database_path) as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM queue").fetchone()[0])

    def test_identical_duplicate_route_also_fails_strict_source_identity_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            duplicate = self.record("/duplicate")
            source = self.write_source(root, [duplicate, dict(duplicate)])
            hydrator = CorpusHydrator(
                root,
                "T0",
                source,
                requests_per_second=100000,
                minimum_free_disk_bytes=1024**3,
            )
            with patch("govuk_okf.hydration.request_observation") as request:
                with self.assertRaises(sqlite3.IntegrityError):
                    hydrator.run()
            request.assert_not_called()
            with sqlite3.connect(hydrator.database_path) as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM queue").fetchone()[0])

    def test_legacy_migration_reserves_duplicate_checkpoint_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.source(root)
            database = root / "corpus/cache/T0/hydration/checkpoint.sqlite"
            database.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE queue (
                        url TEXT NOT NULL,
                        locale TEXT NOT NULL,
                        input_json TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('pending', 'complete')),
                        hydration_status TEXT,
                        record_json TEXT,
                        PRIMARY KEY (url, locale)
                    );
                    CREATE INDEX queue_state_url ON queue(state, url, locale);
                    """
                )
                connection.execute(
                    "INSERT INTO queue VALUES (?, 'en', ?, 'pending', NULL, NULL)",
                    ("https://www.gov.uk/large", "x" * (2 * 1024 * 1024)),
                )

            hydrator = CorpusHydrator(
                root,
                "T0",
                source,
                requests_per_second=100000,
                minimum_free_disk_bytes=12 * 1024 * 1024,
            )
            original_assert = hydrator._assert_retained_storage

            def reject_migration(*, phase: str, **kwargs: object) -> int:
                if phase == "legacy hydration queue migration reservation":
                    raise HydrationError("legacy hydration queue migration reservation")
                return original_assert(phase=phase, **kwargs)

            with patch.object(
                hydrator, "_assert_retained_storage", side_effect=reject_migration
            ), self.assertRaisesRegex(
                HydrationError, "legacy hydration queue migration reservation"
            ):
                hydrator._connect()
            with sqlite3.connect(database) as connection:
                input_column = next(
                    row for row in connection.execute("PRAGMA table_info(queue)") if row[1] == "input_json"
                )
                self.assertEqual(1, input_column[3])
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM queue").fetchone()[0])

    def test_spool_rejects_symbolic_link_root_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            source = self.source(root)
            temporary = root / ".tmp"
            temporary.mkdir()
            (temporary / "hydration-spool").symlink_to(Path(outside), target_is_directory=True)
            hydrator = CorpusHydrator(root, "T0", source, requests_per_second=100000)
            with patch("govuk_okf.hydration.request_observation") as request:
                with self.assertRaisesRegex(HydrationError, "symbolic-link root"):
                    hydrator.run()
            request.assert_not_called()

    def test_spool_disk_admission_stops_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hydrator = CorpusHydrator(root, "T0", self.source(root), requests_per_second=100000)
            usage = shutil.disk_usage(root)
            constrained_usage = type(usage)(
                usage.total,
                usage.used,
                hydrator.minimum_free_disk_bytes
                + hydration_module._TRANSIENT_SPOOL_MINIMUM_FREE
                + hydration_module._TRANSIENT_SPOOL_RESERVE_PER_REQUEST
                - 1,
            )
            with patch("govuk_okf.hydration.shutil.disk_usage", return_value=constrained_usage), patch(
                "govuk_okf.hydration.request_observation"
            ) as request:
                with self.assertRaisesRegex(HydrationError, "durably spool"):
                    hydrator.run()
            request.assert_not_called()

    def test_successful_sibling_request_is_spooled_when_one_future_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_source(root, [self.record("/good"), self.record("/bad")])
            hydrator = CorpusHydrator(
                root,
                "T0",
                source,
                requests_per_second=100000,
                workers=2,
                batch_size=2,
                minimum_free_disk_bytes=1024**3,
            )

            def one_failure(url: str, **_: object):
                if url.endswith("/bad"):
                    raise RuntimeError("injected request failure")
                return self.observation(url)

            with patch("govuk_okf.hydration.request_observation", side_effect=one_failure):
                with self.assertRaisesRegex(RuntimeError, "injected request failure"):
                    hydrator.run()
            self.assertEqual(1, len(list(hydrator.spool_root.glob("*.json"))))

            requested: list[str] = []

            def resume_observation(url: str, **_: object):
                requested.append(url)
                return self.observation(url)

            resumed = CorpusHydrator(
                root,
                "T0",
                source,
                requests_per_second=100000,
                workers=2,
                batch_size=2,
                minimum_free_disk_bytes=1024**3,
            )
            with patch("govuk_okf.hydration.request_observation", side_effect=resume_observation):
                self.assertTrue(resumed.run()["closed"])
            self.assertEqual(1, len(requested))
            self.assertTrue(requested[0].endswith("/bad"))
            self.assertFalse(resumed.spool_root.exists())

    def test_oversized_spool_result_is_rejected_without_partial_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hydrator = CorpusHydrator(root, "T0", self.source(root), requests_per_second=100000)
            result = ({"description": "x" * 2048}, "represented", [])
            with patch.object(hydration_module, "_MAX_SPOOL_DOCUMENT_BYTES", 1024):
                with self.assertRaisesRegex(HydrationError, "transient spool envelope"):
                    hydrator._write_spool("https://www.gov.uk/", "en", "{}", result)
            self.assertFalse(hydrator.spool_root.exists())

    def test_spooled_result_prevents_repeat_request_after_storage_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.source(root)

            def large_observation(url: str, **_: object):
                body = json.dumps(
                    {
                        "content_id": "root-id",
                        "base_path": "/",
                        "title": "GOV.UK",
                        "description": "x" * (5 * 1024 * 1024),
                        "document_type": "homepage",
                        "schema_name": "homepage",
                        "locale": "en",
                        "links": {},
                    }
                ).encode()
                return body, {
                    "ok": True,
                    "status": 200,
                    "partial": False,
                    "requested_url": url,
                    "final_url": url,
                    "retrieved_at": "2026-07-13T00:00:00Z",
                    "sha256": "b" * 64,
                }

            constrained = CorpusHydrator(
                root,
                "T0",
                source,
                requests_per_second=100000,
                minimum_free_disk_bytes=20 * 1024 * 1024,
            )
            original_assert = constrained._assert_retained_storage

            def reject_batch(*, phase: str, **kwargs: object) -> int:
                if phase == "hydration batch reservation":
                    raise HydrationError("hydration batch reservation")
                return original_assert(phase=phase, **kwargs)

            with patch.object(
                constrained, "_assert_retained_storage", side_effect=reject_batch
            ), patch(
                "govuk_okf.hydration.request_observation", side_effect=large_observation
            ) as request:
                with self.assertRaisesRegex(HydrationError, "hydration batch reservation"):
                    constrained.run()
            self.assertEqual(1, request.call_count)
            self.assertEqual(1, len(list(constrained.spool_root.glob("*.json"))))

            resumed = CorpusHydrator(
                root,
                "T0",
                source,
                requests_per_second=100000,
                minimum_free_disk_bytes=100 * 1024 * 1024,
            )
            with patch("govuk_okf.hydration.request_observation") as request:
                progress = resumed.run()
            request.assert_not_called()
            self.assertTrue(progress["closed"])
            self.assertFalse(resumed.spool_root.exists())

    def test_large_candidate_batch_is_rejected_before_candidate_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self.record()
            record.update(
                {
                    "canonical_url": "https://outside.example/document",
                    "base_path": "/document",
                    "entity_class": "external_boundary",
                    "source_native_id": "shared-content-id",
                    "content_id": "shared-content-id",
                    "public_updated_at": "2026-07-13T00:00:00Z",
                    "constraints": [{"reason": "x" * (2 * 1024 * 1024)}],
                    "details": {
                        "attachments": [
                            {
                                "id": f"attachment-{index}",
                                "title": f"Attachment {index}",
                                "url": f"https://assets.example/attachment-{index}.pdf",
                            }
                            for index in range(6)
                        ]
                    },
                }
            )
            hydrator = CorpusHydrator(
                root,
                "T0",
                self.write_source(root, [record]),
                requests_per_second=100000,
                minimum_free_disk_bytes=90 * 1024 * 1024,
            )
            with patch("govuk_okf.hydration.request_observation") as request:
                self.assertTrue(hydrator.run()["closed"])
            request.assert_not_called()
            original_reservation = hydrator._assert_sqlite_write_storage

            def reject_candidate(*, phase: str, **kwargs: object) -> int:
                if phase == "candidate materialization pre-write":
                    raise HydrationError("candidate materialization pre-write")
                return original_reservation(phase=phase, **kwargs)

            with patch.object(
                hydrator, "_assert_sqlite_write_storage", side_effect=reject_candidate
            ), self.assertRaisesRegex(HydrationError, "candidate materialization pre-write"):
                hydrator.export()
            self.assertFalse(any(hydrator.records_root.glob("source-records-*")))
            with sqlite3.connect(hydrator.database_path) as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])

    def test_alias_updates_are_reserved_before_sqlite_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = []
            for path in ("a", "b"):
                record = self.record(f"/{path}")
                record.update(
                    {
                        "canonical_url": f"https://outside.example/{path}",
                        "entity_class": "external_boundary",
                        "source_native_id": "shared-content-id",
                        "content_id": "shared-content-id",
                        "public_updated_at": "2026-07-13T00:00:00Z",
                    }
                )
                records.append(record)
            hydrator = CorpusHydrator(
                root,
                "T0",
                self.write_source(root, records),
                requests_per_second=100000,
                minimum_free_disk_bytes=1024**3,
            )
            with patch("govuk_okf.hydration.request_observation") as request:
                self.assertTrue(hydrator.run()["closed"])
            request.assert_not_called()
            original_reservation = hydrator._assert_sqlite_write_storage

            def reject_alias(*, phase: str, **kwargs: object) -> int:
                if phase == "candidate alias pre-write":
                    raise HydrationError("injected alias reservation")
                return original_reservation(phase=phase, **kwargs)

            with patch.object(
                hydrator,
                "_assert_sqlite_write_storage",
                side_effect=reject_alias,
            ):
                with self.assertRaisesRegex(HydrationError, "injected alias reservation"):
                    hydrator.export()
            self.assertFalse(any(hydrator.records_root.glob("source-records-*")))
            with sqlite3.connect(hydrator.database_path) as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])

    def test_failed_export_removes_new_outputs_and_candidate_working_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hydrator = CorpusHydrator(root, "T0", self.source(root), requests_per_second=100000)
            with patch("govuk_okf.hydration.request_observation", side_effect=self.observation):
                self.assertTrue(hydrator.run()["closed"])
            before_records = set(hydrator.records_root.glob("source-records-*"))
            before_candidates = set(
                (hydrator.inventory_root / hydrator.label).glob("hydrated-candidates-*")
            )
            original_assert = hydrator._assert_retained_storage

            def fail_after_source(*, phase: str, **kwargs: object) -> int:
                if phase == "candidate materialization reservation":
                    raise HydrationError("injected export ceiling")
                return original_assert(phase=phase, **kwargs)

            with patch.object(hydrator, "_assert_retained_storage", side_effect=fail_after_source):
                with self.assertRaisesRegex(HydrationError, "injected export ceiling"):
                    hydrator.export()
            self.assertEqual(before_records, set(hydrator.records_root.glob("source-records-*")))
            self.assertEqual(
                before_candidates,
                set((hydrator.inventory_root / hydrator.label).glob("hydrated-candidates-*")),
            )
            self.assertFalse((hydrator.records_root / "manifest.json").exists())
            self.assertFalse((hydrator.reconciliation_root / "T0-hydrated.json").exists())
            with sqlite3.connect(hydrator.database_path) as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])

    def test_export_reclaims_only_contained_orphan_build_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hydrator = CorpusHydrator(root, "T0", self.source(root), requests_per_second=100000)
            with patch("govuk_okf.hydration.request_observation", side_effect=self.observation):
                self.assertTrue(hydrator.run()["closed"])
            source_orphan = hydrator.records_root / ".source-records.building-interrupted"
            candidate_orphan = (
                hydrator.inventory_root
                / hydrator.label
                / ".hydrated-candidates.building-interrupted"
            )
            for orphan in (source_orphan, candidate_orphan):
                orphan.mkdir(parents=True, exist_ok=True)
                (orphan / "partial.jsonl.gz").write_bytes(b"partial")
            unrelated = hydrator.records_root / ".unrelated.building-keep"
            unrelated.mkdir()

            reconciliation = hydrator.export()
            self.assertEqual(0, reconciliation["unexplained_omissions"])
            self.assertFalse(source_orphan.exists())
            self.assertFalse(candidate_orphan.exists())
            self.assertTrue(unrelated.is_dir())

    def test_export_rejects_symbolic_link_orphan_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            hydrator = CorpusHydrator(root, "T0", self.source(root), requests_per_second=100000)
            with patch("govuk_okf.hydration.request_observation", side_effect=self.observation):
                self.assertTrue(hydrator.run()["closed"])
            target = Path(outside) / "preserved"
            target.mkdir()
            marker = target / "marker.txt"
            marker.write_text("keep", encoding="utf-8")
            hydrator.records_root.mkdir(parents=True, exist_ok=True)
            orphan = hydrator.records_root / ".source-records.building-symlink"
            orphan.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(HydrationError, "unsafe orphan export build path"):
                hydrator.export()
            self.assertEqual("keep", marker.read_text(encoding="utf-8"))
            self.assertTrue(orphan.is_symlink())

    def test_export_rejects_symbolic_link_corpus_root_without_touching_outside_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            outside_root = Path(outside)
            orphan = outside_root / "records/T0/.source-records.building-outside"
            orphan.mkdir(parents=True)
            marker = orphan / "marker.txt"
            marker.write_text("keep", encoding="utf-8")
            (root / "corpus").symlink_to(outside_root, target_is_directory=True)
            launch = root / "governance" / "launch-manifest.yaml"
            launch.parent.mkdir(parents=True)
            launch.write_text("ceilings:\n  minimum_free_disk_gib: 1\n", encoding="utf-8")
            source = root / "source.jsonl"
            source.write_text(json.dumps(self.record()) + "\n", encoding="utf-8")
            hydrator = CorpusHydrator(
                root,
                "T0",
                source,
                requests_per_second=100000,
                minimum_free_disk_bytes=1024**3,
            )

            with self.assertRaisesRegex(HydrationError, "root cannot be a symbolic link"):
                hydrator.export()
            self.assertEqual("keep", marker.read_text(encoding="utf-8"))
            self.assertTrue(orphan.is_dir())


if __name__ == "__main__":
    unittest.main()
