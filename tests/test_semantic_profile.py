from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
SEMANTIC = ROOT / "semantic"
SCHEMAS = SEMANTIC / "schemas"
PROFILE_PATH = SEMANTIC / "profile" / "govuk-okf-profile-v1.yamlld"
ENTITY_CROSSWALK_PATH = SEMANTIC / "crosswalks" / "entity-crosswalk.yamlld"
RELATIONSHIP_CROSSWALK_PATH = SEMANTIC / "crosswalks" / "relationship-crosswalk.yamlld"
SHAPES_PATH = SEMANTIC / "shapes" / "govuk-okf-shapes.ttl"

REQUIRED_CLASSES = (
    "ContentItem",
    "Document",
    "Edition",
    "Route",
    "BasePath",
    "RenderedPage",
    "Part",
    "ContentType",
    "SchemaFamily",
    "Organisation",
    "Taxon",
    "WorldTaxon",
    "Browse",
    "MainstreamBrowsePage",
    "StepByStep",
    "Service",
    "GovernmentService",
    "Collection",
    "Attachment",
    "Distribution",
    "MachineRepresentation",
    "Redirect",
    "Withdrawal",
    "Replacement",
    "Tombstone",
    "EvidenceSource",
    "AcquisitionActivity",
    "InferenceActivity",
    "ConstraintRecord",
    "Evidence",
    "Assertion",
)
ENTITY_CLASSES = REQUIRED_CLASSES[:-2]
REQUIRED_RELATIONSHIPS = (
    "publishedBy",
    "ownedBy",
    "partOf",
    "parentOf",
    "childOf",
    "classifiedUnder",
    "relatedTo",
    "linksTo",
    "replaces",
    "replacedBy",
    "redirectsTo",
    "hasAttachment",
    "hasContentType",
    "availableInLanguage",
    "hasBasePath",
    "renders",
    "hasDistribution",
    "hasMachineRepresentation",
    "withdrawnBy",
    "constrainedBy",
)
AUTHORITY_CLASSES = {"source_native", "normalized", "inferred", "model_derived"}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_refs(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref":
                yield str(item)
            else:
                yield from iter_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_refs(item)


def resolve_pointer(document: Any, pointer: str) -> Any:
    current = document
    for raw_part in pointer.removeprefix("/").split("/") if pointer else []:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


def property_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for item in value.values():
            names.update(property_names(item))
    elif isinstance(value, list):
        for item in value:
            names.update(property_names(item))
    return names


def terms_in_yaml(text: str) -> set[str]:
    return set(re.findall(r"^\s*- term: ([A-Za-z][A-Za-z0-9]*)\s*$", text, flags=re.MULTILINE))


class SemanticProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.context = load_json(SEMANTIC / "context" / "govuk-okf-v1.jsonld")
        cls.schemas = {path.name: load_json(path) for path in sorted(SCHEMAS.glob("*.json"))}
        cls.profile = PROFILE_PATH.read_text(encoding="utf-8")
        cls.entity_crosswalk = ENTITY_CROSSWALK_PATH.read_text(encoding="utf-8")
        cls.relationship_crosswalk = RELATIONSHIP_CROSSWALK_PATH.read_text(encoding="utf-8")
        cls.shapes = SHAPES_PATH.read_text(encoding="utf-8")

    def test_context_is_local_pinned_and_covers_every_class(self) -> None:
        context = self.context["@context"]
        self.assertEqual(1.1, context["@version"])
        self.assertTrue(context["@protected"])
        self.assertEqual(
            "https://chris-page-gov.github.io/okf-govuk-content/ns#",
            context["govuk"],
        )
        for class_name in REQUIRED_CLASSES:
            self.assertEqual(f"govuk:{class_name}", context[class_name])
        self.assertIn('"@context": ../context/govuk-okf-v1.jsonld', self.profile)
        self.assertIn('"@context": ../context/govuk-okf-v1.jsonld', self.entity_crosswalk)
        self.assertIn('"@context": ../context/govuk-okf-v1.jsonld', self.relationship_crosswalk)
        self.assertFalse(self.profile.lstrip().startswith("{"), "profile must be readable YAML-LD, not JSON-shaped YAML")

    def test_every_json_schema_is_draft_2020_12_and_references_resolve(self) -> None:
        self.assertEqual(
            {"assertion.schema.json", "common.schema.json", "entity.schema.json", "evidence.schema.json"},
            set(self.schemas),
        )
        for filename, schema in self.schemas.items():
            self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"], filename)
            self.assertTrue(schema["$id"].endswith(f"/{filename}"), filename)
            for reference in iter_refs(schema):
                self.assertFalse(reference.startswith("http"), f"runtime schema ref must be local: {reference}")
                file_part, _, fragment = reference.partition("#")
                target_name = file_part or filename
                self.assertIn(target_name, self.schemas, reference)
                resolve_pointer(self.schemas[target_name], fragment)

    def test_entity_schema_discriminates_every_source_entity(self) -> None:
        schema = self.schemas["entity.schema.json"]
        self.assertEqual(set(ENTITY_CLASSES), set(schema["$defs"]))
        one_of = {entry["$ref"].rsplit("/", 1)[-1] for entry in schema["oneOf"]}
        self.assertEqual(set(ENTITY_CLASSES), one_of)
        for class_name in ENTITY_CLASSES:
            branches = schema["$defs"][class_name]["allOf"]
            self.assertEqual("common.schema.json#/$defs/commonEntity", branches[0]["$ref"])
            self.assertEqual(f"govuk:{class_name}", branches[1]["properties"]["@type"]["const"])
            self.assertTrue(branches[1]["required"], class_name)

    def test_evidence_schema_requires_retrievable_precise_provenance(self) -> None:
        schema = self.schemas["evidence.schema.json"]
        required = set(schema["required"])
        self.assertTrue(
            {
                "evidenceUrl",
                "sourceSystem",
                "sourceAuthority",
                "locator",
                "retrievedAt",
                "sha256",
                "mediaType",
                "license",
                "rightsStatus",
                "snapshotId",
                "authority",
            }.issubset(required)
        )
        self.assertEqual("govuk:Evidence", schema["properties"]["@type"]["const"])
        self.assertEqual("source_native", schema["properties"]["authority"]["const"])
        self.assertEqual(
            {"locatorKind", "locatorValue"},
            set(schema["properties"]["locator"]["required"]),
        )

    def test_assertion_schema_reifies_one_edge_and_separates_authority(self) -> None:
        schema = self.schemas["assertion.schema.json"]
        required = set(schema["required"])
        self.assertTrue(
            {
                "subject",
                "predicate",
                "object",
                "sourceNativePredicate",
                "evidence",
                "retrievedAt",
                "generatedBy",
                "derivationMethod",
                "assertionStatus",
                "authority",
                "confidence",
                "snapshotId",
            }.issubset(required)
        )
        self.assertEqual("govuk:Assertion", schema["properties"]["@type"]["const"])
        for field in ("subject", "predicate", "object"):
            self.assertEqual("common.schema.json#/$defs/iriReference", schema["properties"][field]["$ref"])
        conditional_statuses = {
            branch["if"]["properties"]["assertionStatus"]["const"] for branch in schema["allOf"]
        }
        self.assertEqual(AUTHORITY_CLASSES, conditional_statuses)
        derived_required = {
            branch["if"]["properties"]["assertionStatus"]["const"]: set(branch["then"].get("required", []))
            for branch in schema["allOf"]
        }
        self.assertIn("derivedFrom", derived_required["normalized"])
        for status in ("inferred", "model_derived"):
            self.assertTrue({"derivedFrom", "reviewStatus", "reviewer"}.issubset(derived_required[status]))

    def test_profile_crosswalks_and_shapes_cover_every_class(self) -> None:
        self.assertEqual(set(REQUIRED_CLASSES), terms_in_yaml(self.profile))
        self.assertEqual(set(REQUIRED_CLASSES), terms_in_yaml(self.entity_crosswalk))
        for class_name in REQUIRED_CLASSES:
            self.assertIn(f"govuk:{class_name}Shape", self.shapes)
            self.assertIn(f"sh:targetClass govuk:{class_name}", self.shapes)

    def test_relationship_crosswalk_is_directional_and_provenance_aware(self) -> None:
        terms = terms_in_yaml(self.relationship_crosswalk)
        self.assertEqual(set(REQUIRED_RELATIONSHIPS), terms)
        self.assertIn("sourceFields:", self.relationship_crosswalk)
        self.assertIn("direction:", self.relationship_crosswalk)
        self.assertIn("mappingStatus: mixed_explicit_authority", self.relationship_crosswalk)
        for field in ("subject", "predicate", "object", "sourceNativePredicate", "evidence"):
            self.assertIn(f"sh:path govuk:{field}", self.shapes)

    def test_authority_classes_are_identical_across_contracts(self) -> None:
        common_authority = set(self.schemas["common.schema.json"]["$defs"]["authority"]["enum"])
        self.assertEqual(AUTHORITY_CLASSES, common_authority)
        for status in AUTHORITY_CLASSES:
            self.assertIn(f"  - {status}\n", self.profile)
            self.assertIn(f'"{status}"', self.shapes)
        narrative = (SEMANTIC / "profile" / "index.md").read_text(encoding="utf-8")
        self.assertIn("Normalization never upgrades a statement to source-native", narrative)
        self.assertIn("cannot overwrite source-native values", narrative)

    def test_profile_validation_artifacts_exist(self) -> None:
        block_match = re.search(
            r"^validationArtifact:\n(?P<items>(?:  - .+\n)+)",
            self.profile,
            flags=re.MULTILINE,
        )
        self.assertIsNotNone(block_match)
        assert block_match is not None
        paths = [line.removeprefix("  - ") for line in block_match.group("items").splitlines()]
        self.assertGreaterEqual(len(paths), 6)
        for relative in paths:
            self.assertTrue((PROFILE_PATH.parent / relative).resolve().is_file(), relative)

    def test_released_semantic_contract_has_no_page_body_field(self) -> None:
        forbidden = {"body", "fullBody", "renderedBody", "pageBody", "contentBody"}
        context_terms = set(self.context["@context"])
        self.assertTrue(forbidden.isdisjoint(context_terms))
        for filename, schema in self.schemas.items():
            self.assertTrue(forbidden.isdisjoint(property_names(schema)), filename)


if __name__ == "__main__":
    unittest.main()
