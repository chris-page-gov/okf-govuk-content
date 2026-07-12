"""Reproducibly package verified bundle bytes for Release and Pages."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import tarfile
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

SEMVER_CORE = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
TAG_PATTERN = re.compile(rf"^v{SEMVER_CORE}(?:-rc\.[1-9][0-9]*)?$")
IGNORED_NAMES = {".DS_Store"}


class PackagingError(ValueError):
    """Raised when verified bytes cannot be packaged safely."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _safe_relative(value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise PackagingError(f"unsafe repository-relative path: {value}")
    return relative


def _files(root: Path) -> list[Path]:
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise PackagingError(f"symlinks are forbidden in verified artifacts: {path}")
        if path.is_file() and path.name not in IGNORED_NAMES:
            result.append(path)
    return result


def _manifest_rows(root: Path, files: Iterable[Path]) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        for path in sorted(files)
    ]


def _manifest(schema: str, root: Path, files: Iterable[Path], **metadata: Any) -> dict[str, Any]:
    rows = _manifest_rows(root, files)
    material = "".join(f"{row['path']}\0{row['bytes']}\0{row['sha256']}\n" for row in rows)
    return {
        "schema": schema,
        **metadata,
        "file_count": len(rows),
        "files": rows,
        "root_sha256": hashlib.sha256(material.encode("utf-8")).hexdigest(),
    }


def _write_reproducible_tar(site: Path, target: Path) -> None:
    files = _files(site)
    directories = {Path("bundle")}
    for path in files:
        relative = path.relative_to(site)
        for parent in relative.parents:
            if parent != Path("."):
                directories.add(Path("bundle") / parent)
    with target.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for directory in sorted(directories, key=lambda item: item.as_posix()):
                    info = tarfile.TarInfo(directory.as_posix().rstrip("/") + "/")
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    archive.addfile(info)
                for path in files:
                    payload = path.read_bytes()
                    info = tarfile.TarInfo(f"bundle/{path.relative_to(site).as_posix()}")
                    info.size = len(payload)
                    info.mode = 0o644
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    archive.addfile(info, BytesIO(payload))


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise PackagingError(f"bundle directory is missing: {source}")
    _files(source)
    shutil.copytree(
        source,
        destination,
        ignore=lambda _directory, names: [name for name in names if name in IGNORED_NAMES],
    )


def _asset_name(key: str, source: Path) -> str:
    suffixes = "".join(source.suffixes) or ".json"
    return f"evidence-{key}{suffixes}"


def package_verified_release(
    *,
    repository_root: Path,
    bundle: Path,
    output: Path,
    tag: str,
    browser_evidence: Path | None = None,
) -> dict[str, Any]:
    repository_root = repository_root.resolve()
    bundle = bundle.resolve()
    output = output.resolve()
    if not TAG_PATTERN.fullmatch(tag):
        raise PackagingError("tag must use vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-rc.N")
    channel = "release-candidate" if "-rc." in tag else "final"
    if output.exists() and any(output.iterdir()):
        raise PackagingError(f"output must be absent or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    site = output / "site"
    assets = output / "assets"
    assets.mkdir()
    _copy_tree(bundle, site)
    checksum_source = site / "checksums.json"
    if not checksum_source.is_file():
        raise PackagingError("verified bundle has no checksums.json")
    shutil.copyfile(checksum_source, assets / "bundle-checksums.json")

    release_manifest_path = repository_root / "release" / "manifest.yaml"
    try:
        release_manifest = json.loads(release_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackagingError(f"release manifest is missing or invalid: {exc}") from exc
    shutil.copyfile(release_manifest_path, assets / "release-manifest.json")
    copied_sources: set[Path] = {release_manifest_path.resolve()}
    for key, value in sorted((release_manifest.get("artifacts") or {}).items()):
        if not isinstance(value, str) or not value:
            continue
        relative = _safe_relative(value)
        source = (repository_root / relative).resolve()
        if source in copied_sources or source == bundle or bundle in source.parents:
            continue
        if not source.is_file():
            raise PackagingError(f"declared release evidence is missing: {relative.as_posix()}")
        shutil.copyfile(source, assets / _asset_name(key, source))
        copied_sources.add(source)
    if browser_evidence is not None:
        browser_evidence = browser_evidence.resolve()
        if not browser_evidence.is_file():
            raise PackagingError(f"browser evidence is missing: {browser_evidence}")
        shutil.copyfile(browser_evidence, assets / "evidence-browser-workflow.json")

    archive = assets / f"okf-govuk-content-{tag}.tar.gz"
    _write_reproducible_tar(site, archive)
    release_asset_manifest_path = assets / "release-assets.json"
    release_asset_files = [path for path in _files(assets) if path != release_asset_manifest_path]
    release_asset_manifest_path.write_text(
        _json(
            _manifest(
                "govuk-okf-release-assets.v1",
                assets,
                release_asset_files,
                tag=tag,
                channel=channel,
                archive=archive.name,
                source_bundle_checksums_sha256=_sha256_file(checksum_source),
            )
        ),
        encoding="utf-8",
    )
    verified_manifest_path = output / "verified-artifact.json"
    verified_files = [path for path in _files(output) if path != verified_manifest_path]
    verified = _manifest(
        "govuk-okf-verified-artifact.v1",
        output,
        verified_files,
        tag=tag,
        channel=channel,
        release_assets_manifest="assets/release-assets.json",
        site_checksums="site/checksums.json",
    )
    verified_manifest_path.write_text(_json(verified), encoding="utf-8")
    return verified


def _verify_rows(root: Path, document: dict[str, Any], excluded: set[Path]) -> list[str]:
    errors: list[str] = []
    actual = [path for path in _files(root) if path not in excluded]
    expected_rows = _manifest_rows(root, actual)
    if document.get("files") != expected_rows:
        errors.append(f"{document.get('schema')}: file rows differ")
    material = "".join(f"{row['path']}\0{row['bytes']}\0{row['sha256']}\n" for row in expected_rows)
    if document.get("file_count") != len(expected_rows):
        errors.append(f"{document.get('schema')}: file count differs")
    if document.get("root_sha256") != hashlib.sha256(material.encode("utf-8")).hexdigest():
        errors.append(f"{document.get('schema')}: root hash differs")
    return errors


def _verify_site_checksums(site: Path, copied_checksum: Path) -> list[str]:
    errors: list[str] = []
    checksum_path = site / "checksums.json"
    try:
        document = json.loads(checksum_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"site checksum manifest is missing or invalid: {exc}"]
    if document.get("schema") != "okf-checksums.v1" or document.get("algorithm") != "sha256":
        errors.append("site checksum manifest schema or algorithm is invalid")
        return errors
    actual = [path for path in _files(site) if path != checksum_path]
    expected_rows = _manifest_rows(site, actual)
    if document.get("files") != expected_rows or document.get("file_count") != len(expected_rows):
        errors.append("site checksum manifest does not exactly cover the Pages tree")
    try:
        if checksum_path.read_bytes() != copied_checksum.read_bytes():
            errors.append("release checksum asset differs from the Pages checksum manifest")
    except OSError as exc:
        errors.append(f"release checksum asset is unavailable: {exc}")
    return errors


def check_verified_release(output: Path) -> list[str]:
    output = output.resolve()
    errors: list[str] = []
    try:
        verified_path = output / "verified-artifact.json"
        verified = json.loads(verified_path.read_text(encoding="utf-8"))
        asset_manifest_path = output / "assets" / "release-assets.json"
        asset_manifest = json.loads(asset_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"verified package manifest is missing or invalid: {exc}"]
    if verified.get("schema") != "govuk-okf-verified-artifact.v1":
        errors.append("verified artifact schema is invalid")
    if asset_manifest.get("schema") != "govuk-okf-release-assets.v1":
        errors.append("release asset schema is invalid")
    tag = verified.get("tag")
    expected_channel = "release-candidate" if isinstance(tag, str) and "-rc." in tag else "final"
    if not isinstance(tag, str) or not TAG_PATTERN.fullmatch(tag):
        errors.append("verified artifact tag is invalid")
    if verified.get("channel") != expected_channel:
        errors.append("verified artifact channel differs from its tag")
    if asset_manifest.get("tag") != tag or asset_manifest.get("channel") != expected_channel:
        errors.append("release asset identity differs from the verified artifact")
    try:
        errors.extend(_verify_rows(output, verified, {verified_path}))
        errors.extend(_verify_rows(output / "assets", asset_manifest, {asset_manifest_path}))
        errors.extend(_verify_site_checksums(output / "site", output / "assets" / "bundle-checksums.json"))
    except PackagingError as exc:
        errors.append(str(exc))
    archive_name = asset_manifest.get("archive")
    if not isinstance(archive_name, str):
        errors.append("release asset manifest has no archive")
        return errors
    archive = output / "assets" / archive_name
    if isinstance(tag, str) and archive_name != f"okf-govuk-content-{tag}.tar.gz":
        errors.append("release archive name differs from the verified tag")
    site = output / "site"
    checksum_path = site / "checksums.json"
    if checksum_path.is_file() and asset_manifest.get("source_bundle_checksums_sha256") != _sha256_file(
        checksum_path
    ):
        errors.append("release asset manifest checksum binding differs from the Pages tree")
    try:
        with tarfile.open(archive, "r:gz") as package:
            members = [member for member in package.getmembers() if member.isfile()]
            names = [member.name for member in members]
            expected_names = [f"bundle/{path.relative_to(site).as_posix()}" for path in _files(site)]
            if names != expected_names:
                errors.append("release archive file list differs from the verified site")
            for member, path in zip(members, _files(site), strict=False):
                extracted = package.extractfile(member)
                if extracted is None or hashlib.sha256(extracted.read()).hexdigest() != _sha256_file(path):
                    errors.append(f"release archive differs: {member.name}")
    except (OSError, PackagingError, tarfile.TarError) as exc:
        errors.append(f"release archive is invalid: {exc}")
    return errors
