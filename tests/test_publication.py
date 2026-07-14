from __future__ import annotations

import gzip
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.publication import (
    PublicationError,
    build_publication,
    compile_records,
    load_jsonl,
    search_shard,
    semantic_descriptor,
    semantic_route_iri,
    tokenise,
)
from govuk_okf.util import adjacency_bucket, read_gzip_json, yaml_dump, yaml_load_subset


class PublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records = load_jsonl(ROOT / "tests" / "fixtures" / "corpus" / "source-records.jsonl")

    def build(self, output: Path) -> dict[str, object]:
        return build_publication(self.records, output, "2026-07-11T23:30:00Z", "fixture-2026-07-11")

    def test_explorer_and_semantic_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            result = self.build(output)
            descriptor = json.loads((output / "okf-explorer.json").read_text(encoding="utf-8"))
            semantic = json.loads((output / "okf-bundle.jsonld").read_text(encoding="utf-8"))
            self.assertEqual("okf-explorer-large-corpus.v1", descriptor["schema"])
            self.assertEqual("okf-large-corpus", descriptor["kind"])
            self.assertEqual("okf:Bundle", semantic["@type"])
            expected_semantic = semantic_descriptor(descriptor["counts"], "2026-07-11T23:30:00Z", "fixture-2026-07-11")
            self.assertEqual(expected_semantic, semantic)
            self.assertEqual(yaml_dump(expected_semantic) + "\n", (output / "okf-bundle.yamlld").read_text(encoding="utf-8"))
            self.assertEqual(semantic, yaml_load_subset((output / "okf-bundle.yamlld").read_text(encoding="utf-8")))
            self.assertEqual(result["semantic_projection_sha256"], descriptor["semantic_projection_sha256"])
            search_reference = descriptor["entrypoint_integrity"]["search_manifest"]
            self.assertEqual("data/search/manifest.json", search_reference["path"])
            self.assertEqual(search_reference["path"], descriptor["entrypoints"]["search_manifest"])
            self.assertEqual(
                hashlib.sha256((output / search_reference["path"]).read_bytes()).hexdigest(),
                search_reference["sha256"],
            )
            for name in (
                "data_manifest",
                "overview_index",
                "analysis_overview",
                "site_topology",
                "relationship_adjacency",
                "route_index",
            ):
                reference = descriptor["entrypoint_integrity"][name]
                self.assertEqual(reference["path"], descriptor["entrypoints"][name])
                self.assertEqual(
                    hashlib.sha256((output / reference["path"]).read_bytes()).hexdigest(),
                    reference["sha256"],
                )

    def test_publication_excludes_semantic_dependency_and_test_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            self.assertTrue((output / "semantic/context/govuk-okf-v1.jsonld").is_file())
            self.assertTrue((output / "semantic/shapes/govuk-okf-shapes.ttl").is_file())
            self.assertFalse((output / "semantic/node_modules").exists())
            self.assertFalse((output / "semantic/package-lock.json").exists())
            self.assertFalse((output / "semantic/tests").exists())
            self.assertFalse((output / "semantic/rdfc-equivalence.mjs").exists())

    def test_records_search_resources_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            manifest = json.loads((output / "data" / "manifest.json").read_text(encoding="utf-8"))
            datasets = []
            for path in manifest["chunks"]["datasets"]:
                datasets.extend(read_gzip_json(output / path))
            resources = read_gzip_json(output / manifest["chunks"]["resources"][0])
            relationships = []
            for path in manifest["chunks"]["relationships"]:
                relationships.extend(read_gzip_json(output / path))
            self.assertGreaterEqual(len(datasets), len(self.records))
            self.assertEqual(list(range(len(datasets))), [record["ordinal"] for record in datasets])
            self.assertTrue(any(record["language"] == "cy" for record in datasets))
            self.assertEqual(1, len(resources))
            self.assertEqual(resources[0]["attachment_id"], resources[0]["id"])
            self.assertTrue(resources[0]["dataset"])
            self.assertTrue(any(edge["kind"] == "redirects to" for edge in relationships))
            self.assertTrue(any(edge["kind"] == "has attachment" for edge in relationships))
            required = {
                "assertion_id",
                "source",
                "target",
                "kind",
                "source_native_predicate",
                "evidence_type",
                "evidence_url",
                "evidence_sha256",
                "evidence_locator",
                "observed_at",
                "derivation_method",
                "software_version",
                "snapshot_id",
                "assertion_status",
                "confidence",
            }
            for edge in relationships:
                self.assertTrue(required <= set(edge))
                self.assertRegex(edge["evidence_sha256"], r"^[0-9a-f]{64}$")
            for record in resources:
                self.assertTrue({"evidence_url", "evidence_sha256", "evidence_locator", "retrieved_at"} <= set(record))

            search = json.loads((output / "data" / "search" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(datasets), search["counts"]["documents"])
            result_docs = []
            for path in search["entrypoints"]["result_docs"]:
                result_docs.extend(json.loads((output / path).read_text(encoding="utf-8")))
            self.assertEqual(list(range(len(result_docs))), [record["ordinal"] for record in result_docs])
            self.assertEqual([record["open"] for record in datasets], [record["open"] for record in result_docs])
            redirect = next(record for record in datasets if record["url"].endswith("/dfe"))
            self.assertEqual("redirect", redirect["routing_kind"])
            self.assertEqual("content_identity", redirect["entity_class"])
            self.assertEqual("represented", redirect["coverage_disposition"])
            self.assertEqual(
                {
                    "ordinal": 0,
                    "path": "/dfe",
                    "destination": "/government/organisations/department-for-education",
                    "destination_url": "https://www.gov.uk/government/organisations/department-for-education",
                    "type": "exact",
                    "segments_mode": "unknown",
                },
                redirect["redirects"][0],
            )

    def test_site_topology_covers_hosts_and_routing_mechanisms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            topology = json.loads(
                (output / "data" / "site-topology.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (output / "data" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual("govuk-site-topology.v1", topology["schema"])
            self.assertEqual("fixture-2026-07-11", topology["snapshot"])
            self.assertEqual(
                manifest["counts"]["datasets"], topology["counts"]["published_records"]
            )
            self.assertEqual(
                manifest["counts"]["relationships"],
                topology["counts"]["relationship_assertions"],
            )
            self.assertEqual(
                ["www.gov.uk", "apply.example.service.gov.uk"],
                [row["hostname"] for row in topology["hosts"]],
            )
            self.assertEqual(1, topology["counts"]["redirect_rules"])
            self.assertTrue(topology["redirect_samples_complete"])
            self.assertEqual(
                "/government/organisations/department-for-education",
                topology["redirect_samples"][0]["destination"],
            )
            self.assertEqual(
                "external_boundary",
                next(
                    row
                    for row in topology["hosts"]
                    if row["hostname"] == "apply.example.service.gov.uk"
                )["routing_kinds"][0]["value"],
            )

    def test_route_index_retains_typed_matches_for_shared_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            manifest = json.loads((output / "data" / "routes" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("identifier-to-typed-matches", manifest["entry_shape"])
            content_id = "00000000-0000-4000-8000-000000000004"
            payload = read_gzip_json(output / manifest["buckets"][adjacency_bucket(content_id)])
            self.assertEqual({"datasets", "publishers"}, {match["kind"] for match in payload[content_id]})
            url = "https://www.gov.uk/government/organisations/department-for-work-pensions"
            payload = read_gzip_json(output / manifest["buckets"][adjacency_bucket(url)])
            self.assertEqual({"datasets", "publishers"}, {match["kind"] for match in payload[url]})

    def test_semantic_projection_is_lazy_complete_and_referential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            data_manifest = json.loads(
                (output / "data" / "manifest.json").read_text(encoding="utf-8")
            )
            relative = data_manifest["indexes"]["semantic_projection"]
            manifest = json.loads((output / relative).read_text(encoding="utf-8"))
            self.assertEqual("govuk-okf-semantic-projection.v1", manifest["schema"])
            self.assertEqual("lazy", manifest["startup"])
            self.assertEqual(
                data_manifest["counts"]["relationships"],
                manifest["counts"]["assertion_nodes"],
            )

            nodes = []
            vocabulary = json.loads(
                (output / manifest["entrypoints"]["vocabulary"]).read_text(
                    encoding="utf-8"
                )
            )
            nodes.extend(vocabulary["@graph"])
            for paths in manifest["chunks"].values():
                for path in paths:
                    document = read_gzip_json(output / path)
                    self.assertEqual(manifest["context"], document["@context"])
                    nodes.extend(document["@graph"])
            self.assertEqual(manifest["counts"]["total_nodes"], len(nodes))
            identifiers = {node["@id"] for node in nodes}
            types_by_identifier = {
                identifier: {
                    node["@type"] for node in nodes if node["@id"] == identifier
                }
                for identifier in identifiers
            }
            assertions = [
                node for node in nodes if node["@type"] == "govuk:Assertion"
            ]
            evidence = {node["@id"] for node in nodes if node["@type"] == "govuk:Evidence"}
            activity = {
                node["@id"]
                for node in nodes
                if node["@type"] == "govuk:InferenceActivity"
            }
            self.assertEqual(manifest["counts"]["assertion_nodes"], len(assertions))
            for assertion in assertions:
                self.assertIn(assertion["subject"]["@id"], identifiers)
                self.assertIn(assertion["object"]["@id"], identifiers)
                self.assertIn(assertion["evidence"][0]["@id"], evidence)
                self.assertIn(assertion["generatedBy"]["@id"], activity)
            for node in nodes:
                if node["@type"] not in {"govuk:Evidence", "govuk:Assertion"}:
                    for reference in node.get("evidence", []):
                        self.assertIn(reference["@id"], evidence)
                if node["@type"] == "govuk:Attachment":
                    self.assertIn(
                        "govuk:ContentItem",
                        types_by_identifier[node["contentItem"]["@id"]],
                    )
            dataset_routes = []
            for path in data_manifest["chunks"]["datasets"]:
                dataset_routes.extend(row["open"] for row in read_gzip_json(output / path))
            self.assertTrue(
                {semantic_route_iri(route) for route in dataset_routes} <= identifiers
            )
            for schema in ("entity_schema", "evidence_schema", "assertion_schema"):
                self.assertTrue((output / manifest["entrypoints"][schema]).is_file())
            shard_rows = [row for rows in manifest["shards"].values() for row in rows]
            self.assertTrue(shard_rows)
            required_shard_fields = {
                "path",
                "schema",
                "snapshot",
                "count",
                "compressed_bytes",
                "uncompressed_bytes",
                "sha256",
                "first_key",
                "last_key",
            }
            self.assertTrue(
                all(required_shard_fields <= set(row) for row in shard_rows)
            )

    def test_native_predicates_are_not_collapsed_into_one_assertion(self) -> None:
        record = copy.deepcopy(self.records[0])
        target = {
            "content_id": "00000000-0000-4000-8000-000000000099",
            "base_path": "/same-target",
            "title": "Same target",
            "locale": "en",
        }
        record["links"] = {"related": [target], "ordered_related_items": [target]}
        _datasets, _publishers, _resources, relationships = compile_records(
            [record], "2026-07-11T23:30:00Z", "fixture-2026-07-11"
        )
        self.assertEqual(2, len(relationships))
        self.assertEqual({"related", "ordered_related_items"}, {edge["source_native_predicate"] for edge in relationships})
        self.assertEqual(2, len({edge["assertion_id"] for edge in relationships}))

    def test_url_only_relationships_reuse_the_canonical_dataset_route(self) -> None:
        target = {
            "content_id": "00000000-0000-4000-8000-000000000088",
            "base_path": "/canonical-target",
            "title": "Canonical target",
            "locale": "en",
            "coverage_disposition": "represented",
        }
        source = {
            "content_id": "00000000-0000-4000-8000-000000000089",
            "base_path": "/source",
            "title": "Source",
            "locale": "en",
            "coverage_disposition": "represented",
            "links": {"related": [{"base_path": "/canonical-target", "title": "Canonical target"}]},
        }
        redirect = {
            "base_path": "/old-target",
            "title": "Old target",
            "locale": "en",
            "document_type": "redirect",
            "coverage_disposition": "redirect_only",
            "redirects": [{"destination": "/canonical-target"}],
        }
        datasets, _publishers, _resources, relationships = compile_records(
            [target, source, redirect], "2026-07-11T23:30:00Z", "fixture-2026-07-11"
        )
        target_route = next(row["open"] for row in datasets if row["canonical_content_id"] == target["content_id"])
        self.assertEqual(3, len(datasets))
        self.assertEqual(
            {target_route},
            {edge["target"] for edge in relationships if edge["kind"] in {"related to", "redirects to"}},
        )

    def test_adjacency_hash_vectors_and_dual_indexing(self) -> None:
        self.assertEqual("83", adjacency_bucket("dataset/dataset-one"))
        self.assertEqual("7f", adjacency_bucket("publisher/publisher-one"))
        self.assertEqual("1e", adjacency_bucket("é"))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            manifest = json.loads((output / "data" / "adjacency" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(256, len(manifest["buckets"]))
            for bucket, relative in manifest["buckets"].items():
                rows_by_route = read_gzip_json(output / relative)
                for route, rows in rows_by_route.items():
                    self.assertEqual(bucket, adjacency_bucket(route))
                    self.assertTrue(all(route in {edge["source"], edge["target"]} for edge in rows))

    def test_search_token_and_shard_vectors_match_explorer(self) -> None:
        self.assertEqual({"e-government", "can", "file.name", "under_score"}, tokenise("The e-government can't file.name under_score"))
        self.assertEqual("eg", search_shard("e-government"))
        self.assertEqual("ca", search_shard("can"))
        self.assertEqual("fi", search_shard("file.name"))

    def test_build_is_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "one"
            second = Path(directory) / "two"
            self.build(first)
            self.build(second)
            first_files = sorted(path.relative_to(first) for path in first.rglob("*") if path.is_file())
            second_files = sorted(path.relative_to(second) for path in second.rglob("*") if path.is_file())
            self.assertEqual(first_files, second_files)
            for relative in first_files:
                self.assertEqual((first / relative).read_bytes(), (second / relative).read_bytes(), relative)

    def test_protected_output_is_refused_without_deleting_it(self) -> None:
        marker = ROOT / "README.md"
        before = marker.read_bytes()
        with self.assertRaises(PublicationError):
            self.build(ROOT)
        self.assertEqual(before, marker.read_bytes())

    def test_failed_rebuild_preserves_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            self.build(output)
            descriptor_before = (output / "okf-explorer.json").read_bytes()
            marker = output / "operator-marker.txt"
            marker.write_text("preserve me\n", encoding="utf-8")
            duplicate_records = [*self.records, copy.deepcopy(self.records[0])]
            with self.assertRaises(PublicationError):
                build_publication(
                    duplicate_records,
                    output,
                    "2026-07-11T23:31:00Z",
                    "fixture-duplicate",
                )
            self.assertEqual("preserve me\n", marker.read_text(encoding="utf-8"))
            self.assertEqual(descriptor_before, (output / "okf-explorer.json").read_bytes())
            self.assertFalse(list(Path(directory).glob(".bundle.build-*")))

    def test_empty_fixture_does_not_claim_reconciliation_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            build_publication([], output, "2026-07-11T23:30:00Z", "fixture-empty")
            overview = json.loads((output / "data" / "overview.json").read_text(encoding="utf-8"))
            self.assertIsNone(overview["coverage"]["unexplained_omissions"])

    def test_route_only_attachment_parent_fails_semantic_projection_closed(self) -> None:
        record = {
            "base_path": "/route-only-parent",
            "title": "Route-only attachment parent",
            "document_type": "guidance",
            "schema_name": "publication",
            "locale": "en",
            "coverage_disposition": "represented",
            "links": {},
            "details": {
                "attachments": [
                    {
                        "id": "22222222-2222-4222-8222-222222222222",
                        "title": "Unsafe parent attachment",
                        "url": "https://assets.publishing.service.gov.uk/unsafe.pdf",
                    }
                ]
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                PublicationError, "no source-native ContentItem parent"
            ):
                build_publication(
                    [record],
                    Path(directory) / "bundle",
                    "2026-07-12T00:00:00Z",
                    "route-only-attachment",
                )


if __name__ == "__main__":
    unittest.main()
