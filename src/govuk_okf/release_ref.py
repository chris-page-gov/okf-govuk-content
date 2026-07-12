"""Fail-closed validation for annotated semantic-version release tags."""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

SEMVER_CORE = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
TAG_PATTERN = re.compile(rf"^v(?P<version>{SEMVER_CORE})(?P<candidate>-rc\.(?P<rc>[1-9][0-9]*))?$")


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def validate_tag_name(tag: str, project_version: str) -> list[str]:
    errors: list[str] = []
    match = TAG_PATTERN.fullmatch(tag)
    if not match:
        errors.append("release tag must use vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-rc.N")
    elif match.group("version") != project_version:
        errors.append(f"release tag {tag} differs from project version {project_version}")
    return errors


def release_channel(tag: str) -> str | None:
    """Return the publication channel encoded by a validated tag name."""

    match = TAG_PATTERN.fullmatch(tag)
    if not match:
        return None
    return "release-candidate" if match.group("candidate") else "final"


def _tag_signature_present(tag_object: str) -> bool:
    return any(
        marker in tag_object
        for marker in (
            "-----BEGIN PGP SIGNATURE-----",
            "-----BEGIN PGP MESSAGE-----",
            "-----BEGIN SSH SIGNATURE-----",
            "-----BEGIN SIGNED MESSAGE-----",
        )
    )


def validate_release_ref(
    root: Path,
    *,
    tag: str,
    expected_commit: str,
    main_ref: str = "origin/main",
    verify_signature: bool = True,
) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project_version = str(project["project"]["version"])
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        return {"passed": False, "errors": [f"project version is unavailable: {exc}"]}
    errors.extend(validate_tag_name(tag, project_version))
    reference = f"refs/tags/{tag}"
    object_type = _git(root, "cat-file", "-t", reference)
    if object_type.returncode or object_type.stdout.strip() != "tag":
        errors.append("release tag must exist locally as an annotated tag object")
    target = _git(root, "rev-parse", f"{reference}^{{commit}}")
    resolved_commit = target.stdout.strip() if target.returncode == 0 else ""
    expected = _git(root, "rev-parse", expected_commit)
    expected_resolved = expected.stdout.strip() if expected.returncode == 0 else ""
    if not resolved_commit or not expected_resolved or resolved_commit != expected_resolved:
        errors.append("release tag target differs from the checked commit")
    main = _git(root, "rev-parse", "--verify", f"{main_ref}^{{commit}}")
    if main.returncode:
        errors.append(f"protected main reference is unavailable: {main_ref}")
    elif resolved_commit:
        ancestor = _git(root, "merge-base", "--is-ancestor", resolved_commit, main.stdout.strip())
        if ancestor.returncode:
            errors.append("release tag target is not reachable from protected main")
    tag_object = _git(root, "cat-file", "tag", reference)
    signature_present = tag_object.returncode == 0 and _tag_signature_present(tag_object.stdout)
    signature_status = "absent_optional"
    if signature_present and verify_signature:
        signature = _git(root, "verify-tag", tag)
        signature_status = "valid" if signature.returncode == 0 else "invalid"
        if signature.returncode:
            errors.append("release tag contains a signature that could not be verified")
    elif signature_present:
        signature_status = "present_not_checked"
    return {
        "schema": "govuk-okf-release-ref-validation.v1",
        "passed": not errors,
        "tag": tag,
        "channel": release_channel(tag),
        "project_version": project_version,
        "tag_commit": resolved_commit or None,
        "expected_commit": expected_resolved or None,
        "main_ref": main_ref,
        "signature_present": signature_present,
        "signature_status": signature_status,
        "errors": errors,
    }
