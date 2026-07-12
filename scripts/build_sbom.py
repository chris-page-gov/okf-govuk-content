#!/usr/bin/env python3
"""Build or check the deterministic CycloneDX SBOM from locked dependencies."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import tomllib
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PYTHON_LOCK = ROOT / "uv.lock"
DEFAULT_NODE_LOCK = ROOT / "semantic" / "package-lock.json"
DEFAULT_PROJECT = ROOT / "pyproject.toml"
DEFAULT_OUTPUT = ROOT / "release" / "sbom.cdx.json"
SBOM_NAMESPACE = uuid.UUID("bce1365d-0e9d-56f8-b2bf-f9575f0d115f")


class SbomError(ValueError):
    """Raised when a lock file cannot be represented without guessing."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _pypi_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _pypi_purl(name: str, version: str) -> str:
    return f"pkg:pypi/{quote(_pypi_name(name), safe='-')}@{quote(version, safe='.-')}"


def _npm_purl(name: str, version: str) -> str:
    if name.startswith("@"):
        namespace, package = name.split("/", 1)
        return f"pkg:npm/{quote(namespace, safe='')}/{quote(package, safe='-._')}@{quote(version, safe='.-')}"
    return f"pkg:npm/{quote(name, safe='-._')}@{quote(version, safe='.-')}"


def _source_hash(package: dict[str, Any]) -> dict[str, str] | None:
    candidates = [package.get("sdist"), *(package.get("wheels") or [])]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        value = candidate.get("hash")
        if isinstance(value, str) and value.startswith("sha256:"):
            digest = value.removeprefix("sha256:")
            if re.fullmatch(r"[0-9a-f]{64}", digest):
                return {"alg": "SHA-256", "content": digest}
    return None


def _npm_hash(value: object) -> dict[str, str] | None:
    if not isinstance(value, str) or not value.startswith("sha512-"):
        return None
    try:
        digest = base64.b64decode(value.removeprefix("sha512-"), validate=True).hex()
    except (ValueError, base64.binascii.Error) as exc:
        raise SbomError(f"invalid npm sha512 integrity value: {value}") from exc
    if len(digest) != 128:
        raise SbomError("npm sha512 integrity did not decode to 64 bytes")
    return {"alg": "SHA-512", "content": digest}


def _dependency_name(value: object) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        raise SbomError(f"invalid uv dependency row: {value!r}")
    return _pypi_name(value["name"])


def render(
    python_lock: Path,
    node_lock: Path,
    project_file: Path = DEFAULT_PROJECT,
) -> str:
    """Return a byte-stable CycloneDX 1.6 document for two lock files."""

    with python_lock.open("rb") as stream:
        python_document = tomllib.load(stream)
    with project_file.open("rb") as stream:
        project_document = tomllib.load(stream)
    node_document = json.loads(node_lock.read_text(encoding="utf-8"))
    python_packages = python_document.get("package")
    node_packages = node_document.get("packages")
    if not isinstance(python_packages, list) or not python_packages:
        raise SbomError("uv.lock contains no packages")
    if node_document.get("lockfileVersion") != 3 or not isinstance(node_packages, dict):
        raise SbomError("semantic/package-lock.json must use npm lockfileVersion 3")
    project = project_document.get("project")
    if not isinstance(project, dict):
        raise SbomError("pyproject.toml has no project table")
    project_name = project.get("name")
    project_version = project.get("version")
    project_license = project.get("license")
    if not isinstance(project_name, str) or not isinstance(project_version, str):
        raise SbomError("pyproject.toml requires a project name and version")
    license_text = (
        project_license.get("text") if isinstance(project_license, dict) else None
    )

    components: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    python_refs: dict[str, str] = {}
    for package in python_packages:
        if not isinstance(package, dict):
            raise SbomError("uv package row must be an object")
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise SbomError("uv package requires string name and version")
        canonical_name = _pypi_name(name)
        reference = _pypi_purl(name, version)
        if canonical_name in python_refs:
            raise SbomError(f"multiple locked Python versions are ambiguous: {canonical_name}")
        python_refs[canonical_name] = reference
        component: dict[str, Any] = {
            "bom-ref": reference,
            "type": "library",
            "name": name,
            "version": version,
            "purl": reference,
            "properties": [
                {"name": "govuk-okf:ecosystem", "value": "python"},
                {"name": "govuk-okf:lockfile", "value": python_lock.name},
            ],
        }
        source_hash = _source_hash(package)
        if source_hash:
            component["hashes"] = [source_hash]
        if canonical_name == _pypi_name(project_name) and isinstance(license_text, str):
            component["licenses"] = [{"license": {"id": license_text}}]
        source = package.get("source")
        if isinstance(source, dict) and isinstance(source.get("registry"), str):
            component["externalReferences"] = [
                {"type": "distribution", "url": source["registry"]}
            ]
        components.append(component)

    for package in python_packages:
        name = _pypi_name(str(package["name"]))
        child_refs = []
        for dependency in package.get("dependencies") or []:
            dependency_name = _dependency_name(dependency)
            if dependency_name not in python_refs:
                raise SbomError(f"uv dependency is not locked: {dependency_name}")
            child_refs.append(python_refs[dependency_name])
        dependencies.append(
            {"ref": python_refs[name], "dependsOn": sorted(set(child_refs))}
        )

    node_root = node_packages.get("")
    if not isinstance(node_root, dict) or not isinstance(node_root.get("name"), str):
        raise SbomError("npm lock root package requires a name")
    node_application_ref = "urn:govuk-okf:component:semantic-validation"
    node_lock_label = (
        node_lock.relative_to(ROOT).as_posix()
        if node_lock.is_relative_to(ROOT)
        else node_lock.name
    )
    components.append(
        {
            "bom-ref": node_application_ref,
            "type": "application",
            "name": node_root["name"],
            "version": "private-lock-v3",
            "properties": [
                {"name": "govuk-okf:ecosystem", "value": "npm"},
                {"name": "govuk-okf:lockfile", "value": node_lock_label},
            ],
        }
    )
    node_refs: dict[str, str] = {}
    for path, package in sorted(node_packages.items()):
        if path == "":
            continue
        if not path.startswith("node_modules/") or not isinstance(package, dict):
            raise SbomError(f"unsupported npm package path: {path}")
        name = path.removeprefix("node_modules/")
        version = package.get("version")
        if not isinstance(version, str):
            raise SbomError(f"npm package has no version: {name}")
        reference = _npm_purl(name, version)
        node_refs[name] = reference
        component = {
            "bom-ref": reference,
            "type": "library",
            "name": name,
            "version": version,
            "purl": reference,
            "properties": [
                {"name": "govuk-okf:ecosystem", "value": "npm"},
                {"name": "govuk-okf:lockfile", "value": node_lock_label},
            ],
        }
        integrity = _npm_hash(package.get("integrity"))
        if integrity:
            component["hashes"] = [integrity]
        if isinstance(package.get("resolved"), str):
            component["externalReferences"] = [
                {"type": "distribution", "url": package["resolved"]}
            ]
        if isinstance(package.get("license"), str):
            component["licenses"] = [{"license": {"id": package["license"]}}]
        components.append(component)

    def node_dependency_refs(package: dict[str, Any]) -> list[str]:
        result = []
        for name in (package.get("dependencies") or {}):
            if name not in node_refs:
                raise SbomError(f"npm dependency is not locked: {name}")
            result.append(node_refs[name])
        return sorted(set(result))

    dependencies.append(
        {"ref": node_application_ref, "dependsOn": node_dependency_refs(node_root)}
    )
    for path, package in sorted(node_packages.items()):
        if path == "":
            continue
        name = path.removeprefix("node_modules/")
        dependencies.append(
            {"ref": node_refs[name], "dependsOn": node_dependency_refs(package)}
        )

    python_lock_sha256 = sha256_file(python_lock)
    node_lock_sha256 = sha256_file(node_lock)
    project_sha256 = sha256_file(project_file)
    serial = uuid.uuid5(
        SBOM_NAMESPACE,
        python_lock_sha256 + ":" + node_lock_sha256 + ":" + project_sha256,
    )
    root_ref = "urn:govuk-okf:application:okf-govuk-content"
    project_ref = python_refs.get("govuk-okf")
    if not project_ref:
        raise SbomError("uv.lock does not contain the govuk-okf project")
    dependencies.append(
        {"ref": root_ref, "dependsOn": sorted([project_ref, node_application_ref])}
    )
    document = {
        "$schema": "https://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "component": {
                "bom-ref": root_ref,
                "type": "application",
                "name": "okf-govuk-content",
                "version": project_version,
                "licenses": [{"license": {"id": license_text}}]
                if isinstance(license_text, str)
                else [],
            },
            "properties": [
                {"name": "govuk-okf:lock:uv.sha256", "value": python_lock_sha256},
                {"name": "govuk-okf:lock:semantic-package-lock.sha256", "value": node_lock_sha256},
                {"name": "govuk-okf:input:pyproject.sha256", "value": project_sha256},
                {"name": "govuk-okf:generator", "value": "scripts/build_sbom.py"},
            ],
        },
        "components": sorted(components, key=lambda row: str(row["bom-ref"])),
        "dependencies": sorted(dependencies, key=lambda row: str(row["ref"])),
    }
    validate(document, python_packages=python_packages, node_packages=node_packages)
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def validate(
    document: dict[str, Any],
    *,
    python_packages: list[dict[str, Any]] | None = None,
    node_packages: dict[str, Any] | None = None,
) -> None:
    """Apply the release-critical subset of the CycloneDX contract."""

    if document.get("bomFormat") != "CycloneDX" or document.get("specVersion") != "1.6":
        raise SbomError("SBOM must be CycloneDX 1.6")
    components = document.get("components")
    dependencies = document.get("dependencies")
    if not isinstance(components, list) or not components:
        raise SbomError("SBOM has no components")
    if not isinstance(dependencies, list) or not dependencies:
        raise SbomError("SBOM has no dependency graph")
    references = [row.get("bom-ref") for row in components if isinstance(row, dict)]
    root_reference = document.get("metadata", {}).get("component", {}).get("bom-ref")
    if not isinstance(root_reference, str):
        raise SbomError("SBOM root component has no bom-ref")
    if len(references) != len(set(references)) or any(not isinstance(item, str) for item in references):
        raise SbomError("SBOM component references are missing or duplicated")
    all_references = set(references) | {root_reference}
    dependency_refs = set()
    for row in dependencies:
        if not isinstance(row, dict) or not isinstance(row.get("ref"), str):
            raise SbomError("invalid SBOM dependency row")
        dependency_refs.add(row["ref"])
        children = row.get("dependsOn")
        if not isinstance(children, list) or any(child not in all_references for child in children):
            raise SbomError(f"SBOM dependency has an unresolved component: {row}")
    if dependency_refs != all_references:
        raise SbomError("SBOM dependency graph does not cover every component")
    if python_packages is not None and node_packages is not None:
        expected = len(python_packages) + len(node_packages)
        if len(components) != expected:
            raise SbomError(
                f"SBOM component count {len(components)} differs from locked count {expected}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-lock", type=Path, default=DEFAULT_PYTHON_LOCK)
    parser.add_argument("--node-lock", type=Path, default=DEFAULT_NODE_LOCK)
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        expected = render(
            args.python_lock.resolve(),
            args.node_lock.resolve(),
            args.project.resolve(),
        )
        if args.check:
            if not args.output.is_file() or args.output.read_text(encoding="utf-8") != expected:
                print("SBOM is missing or stale", file=sys.stderr)
                return 1
            print(f"SBOM verified: {len(json.loads(expected)['components'])} locked components")
            return 0
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(expected, encoding="utf-8")
        print(f"wrote SBOM: {len(json.loads(expected)['components'])} locked components")
        return 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, tomllib.TOMLDecodeError, SbomError) as exc:
        print(f"SBOM generation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
