"""Package and verify the bounded demonstrator for public preview."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

PREVIEW_SCHEMA = "govuk-okf-demonstrator-preview.v1"
PREVIEW_SNAPSHOT = "NEW-CHILD-20260715"
PREVIEW_RECORDS = 69
PREVIEW_SITE_BUDGET_BYTES = 50_000_000
IGNORED_NAMES = {".DS_Store"}
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class PreviewPublicationError(ValueError):
    """Raised when fixture bytes are unsafe or mislabelled for preview."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _load_object(path: Path, label: str, errors: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{label} is missing or invalid: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{label} must be a JSON object")
        return {}
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _site_files(site: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(site.rglob("*")):
        if path.is_symlink():
            raise PreviewPublicationError(f"symlinks are forbidden in preview bytes: {path}")
        if path.is_file() and path.name not in IGNORED_NAMES:
            files.append(path)
    return files


def _checksum_rows(site: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(site).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in _site_files(site)
        if path.name != "checksums.json"
    ]


def validate_preview_site(site: Path) -> list[str]:
    """Validate the self-contained public fixture without release controls."""

    site = site.resolve()
    errors: list[str] = []
    try:
        files = _site_files(site)
    except PreviewPublicationError as exc:
        return [str(exc)]
    total_bytes = sum(path.stat().st_size for path in files)
    if total_bytes >= PREVIEW_SITE_BUDGET_BYTES:
        errors.append(
            f"preview site is {total_bytes} bytes and exceeds the {PREVIEW_SITE_BUDGET_BYTES}-byte budget"
        )

    checksums = _load_object(site / "checksums.json", "bundle checksum manifest", errors)
    expected_rows = _checksum_rows(site)
    if checksums.get("schema") != "okf-checksums.v1" or checksums.get("algorithm") != "sha256":
        errors.append("bundle checksum manifest schema or algorithm is invalid")
    if checksums.get("file_count") != len(expected_rows) or checksums.get("files") != expected_rows:
        errors.append("bundle checksum manifest does not exactly cover the preview site")

    descriptor = _load_object(site / "okf-explorer.json", "Explorer descriptor", errors)
    data_manifest = _load_object(site / "data" / "manifest.json", "data manifest", errors)
    demonstrator = _load_object(site / "data" / "demonstrator.json", "demonstrator contract", errors)
    required_files = ("index.html", ".nojekyll", "404.html")
    for relative in required_files:
        if not (site / relative).is_file():
            errors.append(f"preview site lacks required file: {relative}")

    for label, value in (
        ("Explorer descriptor", descriptor.get("snapshot")),
        ("data manifest", data_manifest.get("snapshot")),
        ("demonstrator contract", demonstrator.get("snapshot")),
    ):
        if value != PREVIEW_SNAPSHOT:
            errors.append(f"{label} is not bound to {PREVIEW_SNAPSHOT}")
    if descriptor.get("status") != "bounded-demonstrator":
        errors.append("Explorer descriptor is not labelled bounded-demonstrator")
    description = str(descriptor.get("description") or "").lower()
    if "not a complete gov.uk corpus" not in description:
        errors.append("Explorer descriptor does not state that the preview is incomplete")
    if data_manifest.get("counts", {}).get("records") != PREVIEW_RECORDS:
        errors.append(f"data manifest does not contain exactly {PREVIEW_RECORDS} records")
    if demonstrator.get("schema") != "govuk-new-child-demonstrator.v1":
        errors.append("demonstrator contract schema is invalid")
    if demonstrator.get("status") != "bounded_demonstrator":
        errors.append("demonstrator contract is not labelled bounded_demonstrator")
    coverage = demonstrator.get("coverage") or {}
    if coverage.get("seed_expected") != PREVIEW_RECORDS:
        errors.append(f"demonstrator denominator is not {PREVIEW_RECORDS}")
    if coverage.get("seed_represented") != PREVIEW_RECORDS:
        errors.append(f"demonstrator represented count is not {PREVIEW_RECORDS}")
    if coverage.get("unexplained_seed_omissions") != 0:
        errors.append("demonstrator has unexplained seed omissions")
    return errors


def validate_preview_source(repository_root: Path, bundle: Path) -> list[str]:
    """Validate the site and prove release controls still reject promotion."""

    repository_root = repository_root.resolve()
    bundle = bundle.resolve()
    errors = validate_preview_site(bundle)
    release_manifest = _load_object(
        repository_root / "release" / "manifest.yaml", "release manifest", errors
    )
    release_status = _load_object(
        repository_root / "release" / "status.json", "release status", errors
    )
    snapshot = release_manifest.get("snapshot") or {}
    if snapshot.get("id") != PREVIEW_SNAPSHOT:
        errors.append(f"release manifest is not bound to {PREVIEW_SNAPSHOT}")
    if snapshot.get("kind") != "fixture" or snapshot.get("sampled") is not True:
        errors.append("release manifest must retain fixture and sampled labels")
    if release_status.get("release_id") != PREVIEW_SNAPSHOT:
        errors.append(f"release status is not bound to {PREVIEW_SNAPSHOT}")
    if release_status.get("status") != "checkpoint":
        errors.append("release status must remain checkpoint")
    for field in (
        "publication_ready",
        "machine_rc_complete",
        "programme_complete",
        "full_corpus_reconciled",
    ):
        if release_status.get(field) is not False:
            errors.append(f"release status {field} must remain false for the preview")
    if release_status.get("completion_statement") != "AFHF_GOVUK_OKF_CHECKPOINT_V1":
        errors.append("release status completion statement is not the checkpoint marker")
    return errors


def package_preview(
    *, repository_root: Path, bundle: Path, output: Path, commit: str
) -> dict[str, Any]:
    """Copy exact checked-in fixture bytes into a self-verifying preview package."""

    repository_root = repository_root.resolve()
    bundle = bundle.resolve()
    output = output.resolve()
    if not COMMIT_PATTERN.fullmatch(commit):
        raise PreviewPublicationError("commit must be a lowercase 40-character Git SHA")
    errors = validate_preview_source(repository_root, bundle)
    if errors:
        raise PreviewPublicationError("preview source validation failed: " + "; ".join(errors))
    if output.exists() and any(output.iterdir()):
        raise PreviewPublicationError(f"output must be absent or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    site = output / "site"
    shutil.copytree(
        bundle,
        site,
        ignore=lambda _directory, names: [name for name in names if name in IGNORED_NAMES],
    )
    files = _site_files(site)
    manifest = {
        "schema": PREVIEW_SCHEMA,
        "snapshot": PREVIEW_SNAPSHOT,
        "publication_tier": "bounded-demonstrator-preview",
        "commit": commit,
        "site": "site",
        "file_count": len(files),
        "site_bytes": sum(path.stat().st_size for path in files),
        "site_budget_bytes": PREVIEW_SITE_BUDGET_BYTES,
        "bundle_checksums_sha256": _sha256(site / "checksums.json"),
        "release_promotion": False,
        "full_corpus": False,
        "registry_entry": False,
    }
    (output / "verified-preview.json").write_text(_json(manifest), encoding="utf-8")
    package_errors = check_preview_package(output, expected_commit=commit)
    if package_errors:
        raise PreviewPublicationError("preview package verification failed: " + "; ".join(package_errors))
    return manifest


def check_preview_package(output: Path, *, expected_commit: str | None = None) -> list[str]:
    """Verify a transported preview package before Pages deployment."""

    output = output.resolve()
    errors: list[str] = []
    manifest = _load_object(output / "verified-preview.json", "verified preview manifest", errors)
    site = output / "site"
    errors.extend(validate_preview_site(site))
    if manifest.get("schema") != PREVIEW_SCHEMA:
        errors.append(f"verified preview schema must be {PREVIEW_SCHEMA}")
    if manifest.get("snapshot") != PREVIEW_SNAPSHOT:
        errors.append(f"verified preview is not bound to {PREVIEW_SNAPSHOT}")
    if manifest.get("publication_tier") != "bounded-demonstrator-preview":
        errors.append("verified preview has the wrong publication tier")
    commit = manifest.get("commit")
    if not isinstance(commit, str) or not COMMIT_PATTERN.fullmatch(commit):
        errors.append("verified preview commit is invalid")
    if expected_commit is not None and commit != expected_commit:
        errors.append("verified preview commit differs from the workflow commit")
    if manifest.get("release_promotion") is not False:
        errors.append("verified preview must not promote release status")
    if manifest.get("full_corpus") is not False:
        errors.append("verified preview must not claim full-corpus status")
    if manifest.get("registry_entry") is not False:
        errors.append("verified preview must not claim an Explorer registry entry")
    try:
        files = _site_files(site)
        site_bytes = sum(path.stat().st_size for path in files)
        if manifest.get("file_count") != len(files):
            errors.append("verified preview file count differs")
        if manifest.get("site_bytes") != site_bytes:
            errors.append("verified preview byte count differs")
        if manifest.get("site_budget_bytes") != PREVIEW_SITE_BUDGET_BYTES:
            errors.append("verified preview site budget differs")
        if manifest.get("bundle_checksums_sha256") != _sha256(site / "checksums.json"):
            errors.append("verified preview checksum-manifest digest differs")
    except (OSError, PreviewPublicationError) as exc:
        errors.append(f"verified preview files are unavailable: {exc}")
    return errors
