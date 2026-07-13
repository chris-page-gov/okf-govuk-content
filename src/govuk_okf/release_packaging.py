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

from .release_data_plane import (
    GITHUB_RELEASE_ASSET_LIMIT,
    GITHUB_RELEASE_ASSET_MAX_BYTES,
    build_release_packs,
    collect_data_plane_rows,
    data_plane_paths,
    verify_release_packs,
    write_pack_index,
)

SEMVER_CORE = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
TAG_PATTERN = re.compile(rf"^v{SEMVER_CORE}(?:-rc\.[1-9][0-9]*)?$")
IGNORED_NAMES = {".DS_Store"}
PAGES_SITE_BUDGET_BYTES = 950_000_000


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


def _copy_control_plane(source: Path, destination: Path, excluded: set[str]) -> None:
    """Copy exact bundle control bytes while excluding packed data shards."""

    if not source.is_dir():
        raise PackagingError(f"bundle directory is missing: {source}")
    _files(source)
    destination.mkdir(parents=True)
    for path in _files(source):
        relative = path.relative_to(source).as_posix()
        if relative in excluded or relative == "checksums.json":
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def _write_site_checksums(site: Path) -> None:
    checksum_path = site / "checksums.json"
    files = [path for path in _files(site) if path != checksum_path]
    checksum_path.write_text(
        _json(
            {
                "schema": "okf-checksums.v1",
                "algorithm": "sha256",
                "file_count": len(files),
                "files": _manifest_rows(site, files),
            }
        ),
        encoding="utf-8",
    )


def _install_distribution_entrypoint(site: Path, index_path: Path) -> None:
    descriptor_path = site / "okf-explorer.json"
    try:
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackagingError(f"Explorer descriptor is missing or invalid: {exc}") from exc
    if not isinstance(descriptor, dict):
        raise PackagingError("Explorer descriptor must be an object")
    entrypoints = descriptor.setdefault("entrypoints", {})
    if not isinstance(entrypoints, dict):
        raise PackagingError("Explorer descriptor entrypoints must be an object")
    entrypoints["release_data_plane"] = {
        "path": index_path.relative_to(site).as_posix(),
        "sha256": _sha256_file(index_path),
    }
    descriptor["distribution"] = {
        "control_plane": "github-pages",
        "data_plane": "github-pages-same-origin-range-packs",
        "release_mirror": "immutable-github-release-assets",
        "browser_release_asset_fetch": False,
        "immutable_release_required": True,
    }
    descriptor_path.write_text(_json(descriptor), encoding="utf-8")


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
    try:
        shard_rows = collect_data_plane_rows(bundle)
        pack_index = build_release_packs(
            bundle=bundle,
            assets=assets,
            repository="chris-page-gov/okf-govuk-content",
            tag=tag,
        )
    except ValueError as exc:
        raise PackagingError(f"cannot construct release data plane: {exc}") from exc
    _copy_control_plane(bundle, site, data_plane_paths(shard_rows))
    site_packs = site / "data-packs"
    site_packs.mkdir()
    for pack in pack_index["packs"]:
        shutil.copyfile(assets / str(pack["asset_name"]), site_packs / str(pack["asset_name"]))
    index_path = site / "release-data-plane.json"
    write_pack_index(pack_index, index_path)
    _install_distribution_entrypoint(site, index_path)
    _write_site_checksums(site)
    site_bytes = sum(path.stat().st_size for path in _files(site))
    if site_bytes >= PAGES_SITE_BUDGET_BYTES:
        raise PackagingError(
            f"Pages artifact is {site_bytes} bytes and exceeds the {PAGES_SITE_BUDGET_BYTES}-byte publication budget"
        )
    checksum_source = bundle / "checksums.json"
    if not checksum_source.is_file():
        raise PackagingError("verified bundle has no checksums.json")
    shutil.copyfile(checksum_source, assets / "bundle-checksums.json")
    shutil.copyfile(site / "checksums.json", assets / "pages-checksums.json")
    shutil.copyfile(index_path, assets / "release-data-plane.json")

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

    archive = assets / f"okf-govuk-content-{tag}-pages-site.tar.gz"
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
                data_plane_index="release-data-plane.json",
                data_plane_index_sha256=_sha256_file(index_path),
                pages_site_bytes=site_bytes,
                pages_site_budget_bytes=PAGES_SITE_BUDGET_BYTES,
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


def attach_pages_browser_evidence(output: Path, evidence: Path) -> None:
    """Bind a successful packed-site browser run into the release manifests."""

    output = output.resolve()
    evidence = evidence.resolve()
    target = output / "assets" / "evidence-pages-pack-browser.json"
    if not evidence.is_file() or evidence.is_symlink():
        raise PackagingError(f"packed-site browser evidence is missing or unsafe: {evidence}")
    if target.exists():
        raise PackagingError("packed-site browser evidence is already attached")
    try:
        payload = json.loads(evidence.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackagingError(f"packed-site browser evidence is invalid: {exc}") from exc
    try:
        data_plane = json.loads((output / "site" / "release-data-plane.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackagingError(f"packed-site data-plane index is unavailable: {exc}") from exc
    expected_snapshot = data_plane.get("snapshot") if isinstance(data_plane, dict) else None
    data_plane_index_sha256 = _sha256_file(output / "site" / "release-data-plane.json")
    site_checksums_sha256 = _sha256_file(output / "site" / "checksums.json")
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "govuk-okf-explorer-browser-evidence.v1"
        or not isinstance(expected_snapshot, str)
        or not expected_snapshot
        or payload.get("snapshot") != expected_snapshot
        or payload.get("data_plane_index_sha256") != data_plane_index_sha256
        or payload.get("site_checksums_sha256") != site_checksums_sha256
        or payload.get("overall_status") != "automated_full_release_evidence_pass"
        or payload.get("artifact_tier") != "full_release_snapshot"
        or payload.get("publication_ready") is not True
        or payload.get("accessibility", {}).get("pass") is not True
        or payload.get("routing_and_data", {}).get("pass") is not True
        or payload.get("performance", {}).get("pass") is not True
        or payload.get("full_release_gates", {}).get("full_corpus_browser_measurement") != "passed"
        or payload.get("console_exceptions") != []
    ):
        raise PackagingError("packed-site browser evidence did not pass the full-release schema and snapshot contract")
    asset_manifest_path = output / "assets" / "release-assets.json"
    verified_manifest_path = output / "verified-artifact.json"
    try:
        asset_manifest = json.loads(asset_manifest_path.read_text(encoding="utf-8"))
        verified = json.loads(verified_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackagingError(f"verified package manifest is unavailable: {exc}") from exc
    shutil.copyfile(evidence, target)
    asset_metadata = {
        key: asset_manifest[key]
        for key in (
            "tag",
            "channel",
            "archive",
            "source_bundle_checksums_sha256",
            "data_plane_index",
            "data_plane_index_sha256",
            "pages_site_bytes",
            "pages_site_budget_bytes",
        )
    }
    asset_files = [path for path in _files(output / "assets") if path != asset_manifest_path]
    asset_manifest_path.write_text(
        _json(_manifest("govuk-okf-release-assets.v1", output / "assets", asset_files, **asset_metadata)),
        encoding="utf-8",
    )
    verified_metadata = {
        key: verified[key]
        for key in ("tag", "channel", "release_assets_manifest", "site_checksums")
    }
    verified_files = [path for path in _files(output) if path != verified_manifest_path]
    verified_manifest_path.write_text(
        _json(_manifest("govuk-okf-verified-artifact.v1", output, verified_files, **verified_metadata)),
        encoding="utf-8",
    )


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


def _verify_distribution_descriptor(site: Path, index: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        descriptor = json.loads((site / "okf-explorer.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"packaged Explorer descriptor is missing or invalid: {exc}"]
    entry = (descriptor.get("entrypoints") or {}).get("release_data_plane")
    if entry != {
        "path": "release-data-plane.json",
        "sha256": _sha256_file(site / "release-data-plane.json"),
    }:
        errors.append("Explorer descriptor does not hash-bind the release data-plane index")
    if descriptor.get("distribution") != {
        "control_plane": "github-pages",
        "data_plane": "github-pages-same-origin-range-packs",
        "release_mirror": "immutable-github-release-assets",
        "browser_release_asset_fetch": False,
        "immutable_release_required": True,
    }:
        errors.append("Explorer descriptor distribution contract differs")
    for row in index.get("entries") or []:
        if isinstance(row, dict) and isinstance(row.get("path"), str) and (site / row["path"]).exists():
            errors.append(f"Pages site duplicates a virtual shard outside its range pack: {row['path']}")
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
        errors.extend(_verify_site_checksums(output / "site", output / "assets" / "pages-checksums.json"))
    except PackagingError as exc:
        errors.append(str(exc))
    archive_name = asset_manifest.get("archive")
    if not isinstance(archive_name, str):
        errors.append("release asset manifest has no archive")
        return errors
    archive = output / "assets" / archive_name
    if isinstance(tag, str) and archive_name != f"okf-govuk-content-{tag}-pages-site.tar.gz":
        errors.append("release archive name differs from the verified tag")
    site = output / "site"
    checksum_path = output / "assets" / "bundle-checksums.json"
    if checksum_path.is_file() and asset_manifest.get("source_bundle_checksums_sha256") != _sha256_file(checksum_path):
        errors.append("release asset manifest checksum binding differs from the source bundle checksum asset")
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
    index_path = output / "site" / "release-data-plane.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        copied_index = output / "assets" / "release-data-plane.json"
        if copied_index.read_bytes() != index_path.read_bytes():
            errors.append("release data-plane index differs between Pages and Release assets")
        errors.extend(verify_release_packs(index, output / "assets"))
        errors.extend(verify_release_packs(index, output / "site" / "data-packs"))
        errors.extend(_verify_distribution_descriptor(output / "site", index))
        if asset_manifest.get("data_plane_index_sha256") != _sha256_file(index_path):
            errors.append("release asset manifest data-plane index hash differs")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"release data-plane index is invalid: {exc}")
    release_asset_files = [path for path in _files(output / "assets") if path != asset_manifest_path]
    published_asset_count = len(_files(output / "assets")) + 1  # verified-artifact.json is uploaded too
    if published_asset_count > GITHUB_RELEASE_ASSET_LIMIT:
        errors.append("verified package exceeds GitHub's 1000-assets-per-release limit")
    for path in release_asset_files:
        if path.stat().st_size >= GITHUB_RELEASE_ASSET_MAX_BYTES:
            errors.append(f"verified release asset reaches GitHub's 2 GiB limit: {path.name}")
    site_bytes = sum(path.stat().st_size for path in _files(output / "site"))
    if site_bytes >= PAGES_SITE_BUDGET_BYTES:
        errors.append("verified Pages artifact exceeds its conservative 950 MB budget")
    if asset_manifest.get("pages_site_bytes") != site_bytes or asset_manifest.get("pages_site_budget_bytes") != PAGES_SITE_BUDGET_BYTES:
        errors.append("release asset manifest Pages capacity evidence differs")
    return errors


def check_pages_site(site: Path) -> list[str]:
    """Verify the small Pages control-plane artifact without Release packs."""

    site = site.resolve()
    checksum_path = site / "checksums.json"
    try:
        document = json.loads(checksum_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"Pages checksum manifest is missing or invalid: {exc}"]
    if document.get("schema") != "okf-checksums.v1" or document.get("algorithm") != "sha256":
        return ["Pages checksum manifest schema or algorithm is invalid"]
    actual = [path for path in _files(site) if path != checksum_path]
    expected = _manifest_rows(site, actual)
    errors: list[str] = []
    if document.get("files") != expected or document.get("file_count") != len(expected):
        errors.append("Pages checksum manifest does not exactly cover the control plane")
    if not (site / "release-data-plane.json").is_file():
        errors.append("Pages control plane has no release data-plane index")
        return errors
    try:
        index = json.loads((site / "release-data-plane.json").read_text(encoding="utf-8"))
        errors.extend(verify_release_packs(index, site / "data-packs"))
        errors.extend(_verify_distribution_descriptor(site, index))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"Pages release data-plane index is invalid: {exc}")
    if sum(path.stat().st_size for path in _files(site)) >= PAGES_SITE_BUDGET_BYTES:
        errors.append("Pages artifact exceeds its conservative 950 MB budget")
    return errors
