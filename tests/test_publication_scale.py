from __future__ import annotations

import gzip
import json
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.publication import (  # noqa: E402
    build_publication,
    compare_trees,
    load_jsonl,
    select_compiler,
    synchronize,
)
from govuk_okf.acquisition import write_jsonl_gzip_shards  # noqa: E402
from govuk_okf.publication_disk import build_publication_from_path  # noqa: E402
from govuk_okf.publication_validation import validate_bundle  # noqa: E402


class PublicationScaleTests(unittest.TestCase):
    generated_at = "2026-07-11T23:30:00Z"
    fixture_snapshot = "fixture-2026-07-11"
    fixture = ROOT / "tests" / "fixtures" / "corpus" / "source-records.jsonl"

    def test_disk_compiler_is_byte_identical_to_fixture_compiler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected"
            first = root / "first"
            second = root / "second"
            automatic = root / "automatic"
            sharded = root / "sharded"
            gzip_source = root / "fixture.jsonl.gz"
            build_publication(
                load_jsonl(self.fixture),
                expected,
                self.generated_at,
                self.fixture_snapshot,
            )
            first_result = build_publication_from_path(
                self.fixture, first, self.generated_at, self.fixture_snapshot
            )
            build_publication_from_path(
                self.fixture, second, self.generated_at, self.fixture_snapshot
            )
            with gzip.open(gzip_source, "wb") as stream:
                stream.write(self.fixture.read_bytes())
            self.assertEqual(
                [],
                synchronize(
                    gzip_source,
                    automatic,
                    self.generated_at,
                    self.fixture_snapshot,
                ),
            )
            self.assertEqual([], compare_trees(expected, first))
            self.assertEqual([], compare_trees(first, second))
            self.assertEqual([], compare_trees(first, automatic))
            shard_output = write_jsonl_gzip_shards(
                root / "source-shards",
                "records",
                load_jsonl(self.fixture),
                max_records=3,
            )
            self.assertEqual(
                [],
                synchronize(
                    Path(shard_output["root"]),
                    sharded,
                    self.generated_at,
                    self.fixture_snapshot,
                ),
            )
            self.assertEqual([], compare_trees(first, sharded))
            self.assertEqual("sqlite-bounded-v1", first_result["compiler"]["engine"])
            self.assertEqual(
                len(load_jsonl(self.fixture)),
                first_result["compiler"]["tables"]["source_records"],
            )

    def test_auto_selection_uses_disk_for_full_corpus_shapes(self) -> None:
        self.assertEqual("memory", select_compiler(self.fixture))
        self.assertEqual("disk", select_compiler(Path("snapshot.jsonl.gz")))
        self.assertEqual("memory", select_compiler(Path("snapshot.jsonl.gz"), "memory"))
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual("disk", select_compiler(Path(directory)))

    def test_higher_volume_build_caps_postings_with_bounded_python_heap(self) -> None:
        record_count = 2_048
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source-records.jsonl.gz"
            output = root / "bundle"
            with gzip.open(source, "wt", encoding="utf-8") as stream:
                for index in range(record_count):
                    links: dict[str, list[dict[str, object]]] = {
                        "primary_publishing_organisation": [
                            {
                                "content_id": "11111111-1111-4111-8111-111111111111",
                                "base_path": "/government/organisations/synthetic-office",
                                "slug": "synthetic-office",
                                "title": "Synthetic Office",
                            }
                        ]
                    }
                    if index:
                        links["related"] = [
                            {
                                "content_id": f"00000000-0000-4000-8000-{index - 1:012d}",
                                "base_path": f"/synthetic/{index - 1}",
                                "title": f"Synthetic record {index - 1}",
                                "locale": "en",
                            }
                        ]
                    record: dict[str, object] = {
                        "source_id": "synthetic-scale-test",
                        "coverage_disposition": "represented",
                        "content_id": f"00000000-0000-4000-8000-{index:012d}",
                        "base_path": f"/synthetic/{index}",
                        "title": f"Synthetic record {index} common government guidance",
                        "description": "Shared terms for a bounded-memory scale test.",
                        "document_type": "guidance",
                        "schema_name": "publication",
                        "locale": "en",
                        "links": links,
                    }
                    if index % 100 == 0:
                        record["details"] = {
                            "attachments": [
                                {
                                    "id": f"aaaaaaaa-aaaa-4aaa-8aaa-{index:012d}",
                                    "title": f"Attachment {index}",
                                    "url": (
                                        "https://assets.publishing.service.gov.uk/"
                                        f"synthetic/{index}.pdf"
                                    ),
                                    "content_type": "application/pdf",
                                }
                            ]
                        }
                    stream.write(
                        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
                    )

            tracemalloc.start()
            result = build_publication_from_path(
                source, output, "2026-07-12T00:00:00Z", "synthetic-2048"
            )
            _current_heap, compiler_peak_heap = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            tracemalloc.start()
            validation = validate_bundle(output)
            _current_heap, validator_peak_heap = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            self.assertLess(compiler_peak_heap, 64 * 1024 * 1024)
            self.assertLess(validator_peak_heap, 64 * 1024 * 1024)
            self.assertEqual((), validation.errors)
            self.assertTrue(validation.passed)
            self.assertEqual(record_count, result["counts"]["datasets"])
            self.assertEqual(1, result["counts"]["publishers"])
            self.assertEqual(21, result["counts"]["resources"])
            self.assertEqual(4_116, result["counts"]["relationships"])
            self.assertGreater(
                result["compiler"]["tables"]["search_postings"], record_count
            )

            search = json.loads(
                (output / "data" / "search" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(record_count, search["counts"]["documents"])
            self.assertGreater(
                search["counts"]["uncapped_postings"], search["counts"]["postings"]
            )
            postings = json.loads(
                (output / "data" / "search" / "postings" / "co.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(2_000, len(postings["tokens"]["common"]))
            route_manifest = json.loads(
                (output / "data" / "routes" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            adjacency_manifest = json.loads(
                (output / "data" / "adjacency" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(256, len(route_manifest["buckets"]))
            self.assertEqual(256, len(adjacency_manifest["buckets"]))


if __name__ == "__main__":
    unittest.main()
