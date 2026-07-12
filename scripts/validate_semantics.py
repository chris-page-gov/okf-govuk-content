#!/usr/bin/env python3
"""Validate the bundle's YAML-LD, JSON-LD and sharded semantic projection."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.util import canonical_json_bytes, pretty_json, yaml_load_subset  # noqa: E402

HOME_ID = "https://chris-page-gov.github.io/okf-govuk-content/id/"
MAX_COMPRESSED_SHARD = 5 * 1024 * 1024
MAX_UNCOMPRESSED_SHARD = 64 * 1024 * 1024


class SemanticValidationError(RuntimeError):
    """Raised when any semantic release invariant fails."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_path(bundle: Path, relative_text: str) -> Path:
    relative = Path(relative_text)
    path = (bundle / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(bundle.resolve()):
        raise SemanticValidationError(f"unsafe semantic path: {relative_text}")
    if not path.is_file():
        raise SemanticValidationError(f"missing semantic artifact: {relative_text}")
    return path


def load_json(path: Path, *, compressed: bool = False) -> dict[str, Any]:
    if compressed:
        if path.stat().st_size > MAX_COMPRESSED_SHARD:
            raise SemanticValidationError(f"semantic shard exceeds 5 MiB compressed budget: {path}")
        with gzip.open(path, "rb") as stream:
            payload = stream.read(MAX_UNCOMPRESSED_SHARD + 1)
        if len(payload) > MAX_UNCOMPRESSED_SHARD:
            raise SemanticValidationError(f"semantic shard exceeds 64 MiB expansion budget: {path}")
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SemanticValidationError(f"invalid semantic JSON-LD shard {path}: {exc}") from exc
    else:
        if path.stat().st_size > MAX_UNCOMPRESSED_SHARD:
            raise SemanticValidationError(f"semantic JSON exceeds 64 MiB budget: {path}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SemanticValidationError(f"invalid semantic JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SemanticValidationError(f"semantic JSON-LD document must be an object: {path}")
    return value


def chunk_rows(manifest: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any] | None]]:
    shard_groups = manifest.get("shards")
    if isinstance(shard_groups, dict):
        seen_paths: set[str] = set()
        for family, rows in sorted(shard_groups.items()):
            if not isinstance(rows, list):
                raise SemanticValidationError(f"semantic shard family is not a list: {family}")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                    raise SemanticValidationError(f"invalid semantic shard metadata row in {family}")
                if row["path"] in seen_paths:
                    raise SemanticValidationError(f"duplicate semantic shard path: {row['path']}")
                seen_paths.add(row["path"])
                yield family, row["path"], row
        chunks = manifest.get("chunks")
        if not isinstance(chunks, dict):
            raise SemanticValidationError("semantic manifest has no backward-compatible chunks object")
        for family, rows in chunks.items():
            if not isinstance(rows, list) or any(not isinstance(row, str) for row in rows):
                raise SemanticValidationError(f"semantic chunk family is not a path list: {family}")
            metadata_paths = [row["path"] for row in shard_groups.get(family, [])]
            if rows != metadata_paths:
                raise SemanticValidationError(f"semantic chunk paths differ from shard metadata: {family}")
        return
    chunks = manifest.get("chunks")
    if not isinstance(chunks, dict):
        raise SemanticValidationError("semantic manifest has no chunks object")
    for family, rows in sorted(chunks.items()):
        if not isinstance(rows, list):
            raise SemanticValidationError(f"semantic chunk family is not a list: {family}")
        for row in rows:
            if isinstance(row, str):
                yield family, row, None
            elif isinstance(row, dict) and isinstance(row.get("path"), str):
                yield family, row["path"], row
            else:
                raise SemanticValidationError(f"invalid semantic chunk row in {family}")


def graph_nodes(document: dict[str, Any], path: str, expected_context: str) -> list[dict[str, Any]]:
    if document.get("@context") != expected_context:
        raise SemanticValidationError(f"semantic shard uses an unpinned context: {path}")
    graph = document.get("@graph")
    if not isinstance(graph, list) or any(not isinstance(node, dict) for node in graph):
        raise SemanticValidationError(f"semantic shard has no object @graph: {path}")
    return graph


def local_references(value: Any, *, root: bool = True) -> Iterator[str]:
    if isinstance(value, dict):
        identifier = value.get("@id")
        if not root and isinstance(identifier, str) and (
            identifier.startswith("urn:govuk:") or identifier.startswith(HOME_ID)
        ):
            yield identifier
        for child in value.values():
            yield from local_references(child, root=False)
    elif isinstance(value, list):
        for child in value:
            yield from local_references(child, root=False)


def schema_validators(bundle: Path):
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry, Resource
    except ImportError as exc:
        raise SemanticValidationError("run `uv sync --frozen` to install semantic validators") from exc
    schema_root = bundle / "semantic" / "schemas"
    documents = {
        name: json.loads((schema_root / name).read_text(encoding="utf-8"))
        for name in ("common.schema.json", "entity.schema.json", "evidence.schema.json", "assertion.schema.json")
    }
    for document in documents.values():
        Draft202012Validator.check_schema(document)
    registry = Registry().with_resources(
        [(document["$id"], Resource.from_contents(document)) for document in documents.values()]
    )
    checker = FormatChecker()
    entity = documents["entity.schema.json"]
    entities = {}
    for name, definition in entity["$defs"].items():
        selected = {"$id": entity["$id"], **definition}
        entities[f"govuk:{name}"] = Draft202012Validator(selected, registry=registry, format_checker=checker)
    return {
        **entities,
        "govuk:Evidence": Draft202012Validator(
            documents["evidence.schema.json"], registry=registry, format_checker=checker
        ),
        "govuk:Assertion": Draft202012Validator(
            documents["assertion.schema.json"], registry=registry, format_checker=checker
        ),
    }


def validate_node_schema(node: dict[str, Any], validators: dict[str, Any]) -> list[str]:
    node_type = node.get("@type")
    validator = validators.get(node_type)
    if validator is None:
        return [f"unsupported @type {node_type!r}"]
    errors = sorted(validator.iter_errors(node), key=lambda error: list(error.absolute_path))
    return [
        f"{'/'.join(map(str, error.absolute_path)) or '$'}: {error.message}"
        for error in errors
    ]


class RdfcStream:
    def __init__(self, context_path: Path) -> None:
        self.process = subprocess.Popen(
            ["node", "rdfc-stream.mjs", str(context_path)],
            cwd=ROOT / "semantic",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

    def canonicalise(self, identifier: str, document: dict[str, Any]) -> dict[str, Any]:
        if self.process.stdin is None or self.process.stdout is None:
            raise SemanticValidationError("RDFC worker pipe is unavailable")
        self.process.stdin.write(json.dumps({"id": identifier, "document": document}, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        response = self.process.stdout.readline()
        if not response:
            error = self.process.stderr.read() if self.process.stderr else ""
            raise SemanticValidationError(f"RDFC worker stopped before {identifier}: {error}")
        value = json.loads(response)
        if value.get("id") != identifier or not value.get("ok"):
            raise SemanticValidationError(f"RDFC-1.0 failed for {identifier}: {value.get('error')}")
        return value

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        status = self.process.wait(timeout=30)
        error = self.process.stderr.read() if self.process.stderr else ""
        if status:
            raise SemanticValidationError(f"RDFC worker failed ({status}): {error}")


def descriptor_equivalence(bundle: Path) -> dict[str, Any]:
    yaml_path = bundle / "okf-bundle.yamlld"
    json_path = bundle / "okf-bundle.jsonld"
    parsed_yaml = yaml_load_subset(yaml_path.read_text(encoding="utf-8"))
    parsed_json = json.loads(json_path.read_text(encoding="utf-8"))
    if parsed_yaml != parsed_json:
        raise SemanticValidationError("YAML-LD and JSON-LD descriptors are not structurally equivalent")
    with tempfile.TemporaryDirectory(prefix="govuk-okf-descriptor-") as temporary:
        yaml_json = Path(temporary) / "yaml-projection.jsonld"
        yaml_json.write_text(pretty_json(parsed_yaml), encoding="utf-8")
        result = subprocess.run(
            [
                "node",
                "rdfc-equivalence.mjs",
                str(yaml_json),
                str(json_path),
                str(bundle / "context" / "govuk-okf-v1.jsonld"),
            ],
            cwd=ROOT / "semantic",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    if result.returncode:
        raise SemanticValidationError(f"descriptor RDFC-1.0 equivalence failed: {result.stderr}")
    report = json.loads(result.stdout)
    if not report.get("equivalent"):
        raise SemanticValidationError("descriptor canonical N-Quads digests differ")
    report["yamlLdSha256"] = sha256_file(yaml_path)
    report["jsonLdSha256"] = sha256_file(json_path)
    return report


def run_validation(bundle: Path, *, require_shard_metadata: bool) -> dict[str, Any]:
    bundle = bundle.resolve()
    descriptor = json.loads((bundle / "okf-explorer.json").read_text(encoding="utf-8"))
    semantic_relative = descriptor.get("entrypoints", {}).get("semantic_projection") or "data/semantic/manifest.json"
    semantic_manifest_path = safe_path(bundle, str(semantic_relative))
    manifest = load_json(semantic_manifest_path)
    if manifest.get("schema") != "govuk-okf-semantic-projection.v1":
        raise SemanticValidationError("unsupported semantic projection manifest")
    expected_context = str(manifest.get("context") or "")
    local_context = safe_path(bundle, "context/govuk-okf-v1.jsonld")
    copied_context = safe_path(bundle, "semantic/context/govuk-okf-v1.jsonld")
    if sha256_file(local_context) != sha256_file(copied_context):
        raise SemanticValidationError("published and profile JSON-LD contexts differ")

    entries = list(chunk_rows(manifest))
    vocabulary_relative = str(manifest.get("entrypoints", {}).get("vocabulary") or "")
    if not vocabulary_relative:
        raise SemanticValidationError("semantic manifest has no vocabulary entrypoint")
    if not any(family == "vocabulary" and relative == vocabulary_relative for family, relative, _ in entries):
        entries.append(("vocabulary", vocabulary_relative, None))
    validators = schema_validators(bundle)
    schema_counts: Counter[str] = Counter()
    errors: list[str] = []
    rdfc_entries: list[dict[str, Any]] = []
    rdfc_root = hashlib.sha256()

    with tempfile.TemporaryDirectory(prefix="govuk-okf-semantic-index-") as temporary:
        database = sqlite3.connect(Path(temporary) / "semantic.sqlite")
        database.executescript(
            """
            CREATE TABLE ids (identifier TEXT PRIMARY KEY, node_type TEXT NOT NULL, source_path TEXT NOT NULL);
            CREATE TABLE refs (source_path TEXT NOT NULL, identifier TEXT NOT NULL);
            CREATE INDEX refs_source ON refs(source_path, identifier);
            """
        )
        documents: dict[str, dict[str, Any]] = {}
        worker = RdfcStream(local_context)
        try:
            for family, relative, metadata in entries:
                path = safe_path(bundle, relative)
                if metadata is not None:
                    if metadata.get("compressed_bytes") is not None and int(metadata["compressed_bytes"]) != path.stat().st_size:
                        raise SemanticValidationError(f"semantic shard byte count mismatch: {relative}")
                    expected_hash = metadata.get("sha256") or metadata.get("file_sha256")
                    if expected_hash and expected_hash != sha256_file(path):
                        raise SemanticValidationError(f"semantic shard hash mismatch: {relative}")
                    if require_shard_metadata:
                        required = {"path", "count", "compressed_bytes", "uncompressed_bytes", "sha256", "first_key", "last_key", "snapshot", "schema", "compression", "source_row_count"}
                        missing = required - set(metadata)
                        if missing:
                            raise SemanticValidationError(f"semantic shard metadata missing {sorted(missing)}: {relative}")
                        if metadata.get("schema") != "govuk-okf-semantic-shard.v1":
                            raise SemanticValidationError(f"semantic shard schema is unsupported: {relative}")
                        if metadata.get("snapshot") != manifest.get("snapshot"):
                            raise SemanticValidationError(f"semantic shard snapshot mismatch: {relative}")
                elif require_shard_metadata and family != "vocabulary":
                    raise SemanticValidationError(f"semantic release shard has no metadata row: {relative}")
                document = load_json(path, compressed=path.suffix == ".gz")
                nodes = graph_nodes(document, relative, expected_context)
                if metadata is not None and metadata.get("count") is not None and int(metadata["count"]) != len(nodes):
                    raise SemanticValidationError(f"semantic shard node count mismatch: {relative}")
                if metadata is not None:
                    identifiers = sorted(str(node.get("@id") or "") for node in nodes)
                    first = identifiers[0] if identifiers else None
                    last = identifiers[-1] if identifiers else None
                    if metadata.get("first_key") != first or metadata.get("last_key") != last:
                        raise SemanticValidationError(f"semantic shard key bounds mismatch: {relative}")
                    if path.suffix == ".gz":
                        with gzip.open(path, "rb") as stream:
                            uncompressed = len(stream.read(MAX_UNCOMPRESSED_SHARD + 1))
                    else:
                        uncompressed = path.stat().st_size
                    if uncompressed != int(metadata.get("uncompressed_bytes", -1)):
                        raise SemanticValidationError(f"semantic shard uncompressed byte count mismatch: {relative}")
                documents[relative] = document
                canonical = worker.canonicalise(relative, document)
                rdfc_entries.append(
                    {
                        "path": relative,
                        "expanded_sha256": canonical["expandedSha256"],
                        "canonical_nquads_sha256": canonical["canonicalNQuadsSha256"],
                        "canonical_nquads_statements": canonical["canonicalNQuadsStatements"],
                    }
                )
                rdfc_root.update(
                    f"{relative}\0{canonical['canonicalNQuadsSha256']}\n".encode("utf-8")
                )
                for ordinal, node in enumerate(nodes):
                    identifier = node.get("@id")
                    node_type = node.get("@type")
                    if not isinstance(identifier, str) or not identifier:
                        errors.append(f"{relative}:{ordinal}: missing @id")
                        continue
                    try:
                        database.execute(
                            "INSERT INTO ids(identifier, node_type, source_path) VALUES (?, ?, ?)",
                            (identifier, str(node_type), relative),
                        )
                    except sqlite3.IntegrityError:
                        errors.append(f"duplicate semantic @id: {identifier}")
                    for reference in set(local_references(node)):
                        database.execute(
                            "INSERT INTO refs(source_path, identifier) VALUES (?, ?)",
                            (relative, reference),
                        )
                    node_errors = validate_node_schema(node, validators)
                    schema_counts[str(node_type)] += 1
                    errors.extend(f"{relative}:{identifier}: {message}" for message in node_errors)
                    if len(errors) > 500:
                        raise SemanticValidationError("semantic schema errors exceeded 500: " + "; ".join(errors[:20]))
                database.commit()
        finally:
            worker.close()
        missing_refs = [
            row[0]
            for row in database.execute(
                "SELECT DISTINCT refs.identifier FROM refs LEFT JOIN ids USING(identifier) "
                "WHERE ids.identifier IS NULL ORDER BY refs.identifier LIMIT 501"
            )
        ]
        if missing_refs:
            errors.extend(f"unresolved local semantic reference: {identifier}" for identifier in missing_refs[:500])
        if errors:
            raise SemanticValidationError("semantic validation failed: " + "; ".join(errors[:30]))

        try:
            from pyshacl import validate as shacl_validate
            from rdflib import Graph, RDF, URIRef
        except ImportError as exc:
            raise SemanticValidationError("run `uv sync --frozen` to install SHACL validation") from exc
        shapes = Graph().parse(bundle / "semantic" / "shapes" / "govuk-okf-shapes.ttl", format="turtle")
        context_value = load_json(local_context)["@context"]
        vocabulary_document = documents[vocabulary_relative]
        local_vocabulary = {**vocabulary_document, "@context": context_value}
        vocabulary_graph = Graph().parse(data=json.dumps(local_vocabulary), format="json-ld")
        shacl_runs: list[dict[str, Any]] = []
        for run_index, (family, relative, _metadata) in enumerate(entries):
            document = {**documents[relative], "@context": context_value}
            graph = Graph().parse(data=json.dumps(document), format="json-ld")
            if family != "vocabulary":
                for triple in vocabulary_graph:
                    graph.add(triple)
            focus = [URIRef(str(node["@id"])) for node in document["@graph"]]
            references = [
                row[0]
                for row in database.execute(
                    "SELECT DISTINCT identifier FROM refs WHERE source_path=? ORDER BY identifier",
                    (relative,),
                )
            ]
            for reference in references:
                row = database.execute(
                    "SELECT node_type FROM ids WHERE identifier=?", (reference,)
                ).fetchone()
                if row and str(row[0]).startswith("govuk:"):
                    graph.add(
                        (
                            URIRef(reference),
                            RDF.type,
                            URIRef("https://chris-page-gov.github.io/okf-govuk-content/ns#" + str(row[0]).split(":", 1)[1]),
                        )
                    )
            conforms, _report_graph, report_text = shacl_validate(
                data_graph=graph,
                shacl_graph=shapes,
                advanced=True,
                meta_shacl=run_index == 0,
                focus_nodes=focus,
                allow_infos=False,
                allow_warnings=False,
            )
            shacl_runs.append({"path": relative, "focus_nodes": len(focus), "conforms": bool(conforms)})
            if not conforms:
                raise SemanticValidationError(f"SHACL failed for {relative}: {str(report_text)[:20_000]}")
        database.close()

    descriptor_report = descriptor_equivalence(bundle)
    manifest_counts = manifest.get("counts") or {}
    observed_total = sum(schema_counts.values())
    if int(manifest_counts.get("total_nodes", -1)) != observed_total:
        raise SemanticValidationError(
            f"semantic manifest total_nodes mismatch: {manifest_counts.get('total_nodes')} != {observed_total}"
        )
    return {
        "schema": "govuk-okf-semantic-validation.v1",
        "bundle": str(bundle.relative_to(ROOT) if bundle.is_relative_to(ROOT) else bundle),
        "snapshot": manifest.get("snapshot"),
        "semantic_manifest": {
            "path": semantic_relative,
            "sha256": sha256_file(semantic_manifest_path),
            "schema": manifest.get("schema"),
        },
        "passed": True,
        "yaml_json_ld_equivalence": descriptor_report,
        "json_schema": {
            "draft": "2020-12",
            "passed": True,
            "nodes": observed_total,
            "by_type": dict(sorted(schema_counts.items())),
        },
        "reference_integrity": {"passed": True, "unresolved_local_references": 0},
        "rdfc": {
            "passed": True,
            "algorithm": "RDFC-1.0",
            "implementation": {"jsonld": "9.0.0", "rdf-canonize": "5.0.0"},
            "offline_context": True,
            "max_work_factor": 1,
            "timeout_ms_per_shard": 30_000,
            "aggregate_sha256": rdfc_root.hexdigest(),
            "shards": rdfc_entries,
        },
        "shacl": {
            "passed": True,
            "shape_graph": "semantic/shapes/govuk-okf-shapes.ttl",
            "shape_graph_sha256": sha256_file(bundle / "semantic" / "shapes" / "govuk-okf-shapes.ttl"),
            "bounded_focus_runs": shacl_runs,
        },
    }


def write_atomic(path: Path, value: dict[str, Any]) -> None:
    material = pretty_json(value)
    output = {**value, "report_sha256": hashlib.sha256(material.encode("utf-8")).hexdigest()}
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(pretty_json(output))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT / "bundle")
    parser.add_argument("--output", type=Path, default=ROOT / "release" / "semantic-validation.json")
    parser.add_argument("--require-shard-metadata", action="store_true")
    parser.add_argument("--check", action="store_true", help="validate without rewriting the evidence report")
    args = parser.parse_args()
    try:
        report = run_validation(args.bundle, require_shard_metadata=args.require_shard_metadata)
    except (SemanticValidationError, OSError, ValueError, json.JSONDecodeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"semantic validation failed closed: {exc}", file=sys.stderr)
        return 1
    if not args.check:
        write_atomic(args.output.resolve(), report)
    print(json.dumps({"passed": True, "snapshot": report["snapshot"], "nodes": report["json_schema"]["nodes"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
