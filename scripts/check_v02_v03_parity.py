#!/usr/bin/env python3
"""Compare the v0.3 candidate with the committed v0.2 generated snapshot."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = "cbc06f20c3a64c7cce233c5fd666d03ca3478c2c"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def rows(bundle: Path, category: str) -> list[dict[str, Any]]:
    manifest = json_file(bundle / "data/manifest.json")
    result: list[dict[str, Any]] = []
    for relative in manifest["chunks"][category]:
        with gzip.open(bundle / relative, "rt", encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, list):
            raise ValueError(f"record shard is not an array: {relative}")
        result.extend(value)
    return result


def semantic_nodes(bundle: Path) -> dict[str, dict[str, Any]]:
    manifest = json_file(bundle / "data/semantic/manifest.json")
    result: dict[str, dict[str, Any]] = {}
    for family, paths in manifest["chunks"].items():
        for relative in paths:
            path = bundle / relative
            if path.suffix == ".gz":
                with gzip.open(path, "rt", encoding="utf-8") as stream:
                    document = json.load(stream)
            else:
                document = json_file(path)
            for node in document.get("@graph", []):
                identifier = str(node["@id"])
                if identifier in result:
                    raise ValueError(f"duplicate semantic node identifier: {identifier}")
                result[identifier] = node
    return result


def normalise_graph_node(node: dict[str, Any]) -> dict[str, Any]:
    value = dict(node)
    value.pop("core_role", None)
    if node.get("@type") == "govuk:InferenceActivity":
        value["evidence"] = [{"@id": "PROFILE_EVIDENCE"}]
    return value


def baseline_archive(revision: str, destination: Path) -> None:
    result = subprocess.run(
        ["git", "archive", revision, "bundle"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        timeout=60,
    )
    with tarfile.open(fileobj=BytesIO(result.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError("baseline archive contains an unexpected path type")
        archive.extractall(destination, filter="data")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-revision", default=DEFAULT_BASELINE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="govuk-okf-v02-parity-") as temporary:
        baseline_root = Path(temporary)
        baseline_archive(args.baseline_revision, baseline_root)
        baseline = baseline_root / "bundle"
        candidate = ROOT / "bundle"
        old_descriptor = json_file(baseline / "okf-explorer.json")
        new_descriptor = json_file(candidate / "okf-explorer.json")
        for field in ("snapshot", "generated_at"):
            if old_descriptor.get(field) != new_descriptor.get(field):
                raise ValueError(f"source snapshot field changed: {field}")

        record_counts: dict[str, int] = {}
        for category in ("datasets", "publishers", "resources"):
            old_by_key = {row["open"]: row for row in rows(baseline, category)}
            new_by_key = {row["open"]: {key: value for key, value in row.items() if key != "core_role"} for row in rows(candidate, category)}
            if old_by_key != new_by_key:
                raise ValueError(f"source record identity or content changed in {category}")
            record_counts[category] = len(old_by_key)
        old_edges = rows(baseline, "relationships")
        new_edges = rows(candidate, "relationships")
        if old_edges != new_edges:
            raise ValueError("source relationships or evidence changed")

        concept_paths = sorted((baseline / "concepts").glob("dataset/*.md")) + sorted((baseline / "concepts").glob("publisher/*.md"))
        markdown_hashes: dict[str, str] = {}
        for old_path in concept_paths:
            relative = old_path.relative_to(baseline)
            new_path = candidate / relative
            if not new_path.is_file() or old_path.read_bytes() != new_path.read_bytes():
                raise ValueError(f"typed source concept changed: {relative}")
            markdown_hashes[relative.as_posix()] = sha256(new_path)

        old_nodes = semantic_nodes(baseline)
        new_nodes = semantic_nodes(candidate)
        old_profile = {key: node for key, node in old_nodes.items() if node.get("sourceSystem") == "govuk_okf_profile"}
        new_profile = {key: node for key, node in new_nodes.items() if node.get("sourceSystem") == "govuk_okf_profile"}
        old_rest = {key: normalise_graph_node(node) for key, node in old_nodes.items() if node.get("sourceSystem") != "govuk_okf_profile"}
        new_rest = {key: normalise_graph_node(node) for key, node in new_nodes.items() if node.get("sourceSystem") != "govuk_okf_profile"}
        if old_rest != new_rest:
            raise ValueError("semantic identifiers or node content changed beyond additive core_role and profile evidence")
        old_assertions = {
            key: (node["subject"], node["predicate"], node["object"])
            for key, node in old_nodes.items()
            if node.get("@type") == "govuk:Assertion"
        }
        new_assertions = {
            key: (node["subject"], node["predicate"], node["object"])
            for key, node in new_nodes.items()
            if node.get("@type") == "govuk:Assertion"
        }
        if old_assertions != new_assertions:
            raise ValueError("semantic assertion IDs or directed triples changed")
        receipt = {
            "schema": "govuk-okf-v02-v03-parity.v1",
            "baseline_revision": args.baseline_revision,
            "candidate_head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
            "snapshot": new_descriptor["snapshot"],
            "baseline_core_version": old_descriptor["okf_version"],
            "candidate_core_version": new_descriptor["okf_version"],
            "source_record_counts": record_counts,
            "relationships": len(old_edges),
            "assertion_ids_and_directed_triples": len(old_assertions),
            "semantic_node_count_including_profile_evidence": {"baseline": len(old_nodes), "candidate": len(new_nodes)},
            "non_profile_semantic_nodes_preserved": len(old_rest),
            "profile_evidence_ids": {"baseline": sorted(old_profile), "candidate": sorted(new_profile)},
            "typed_data_markdown_files_preserved_byte_identically": len(markdown_hashes),
            "typed_data_markdown_sha256": markdown_hashes,
            "result": "passed",
        }
    payload = json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
        os.chmod(output, 0o600)
    print(json.dumps({key: value for key, value in receipt.items() if key != "typed_data_markdown_sha256"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
