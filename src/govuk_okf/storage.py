"""Launch-authorised disk safety and external cache discovery."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class StoragePolicyError(RuntimeError):
    """Raised when the launch storage contract cannot be satisfied safely."""


_MINIMUM_FREE_PATTERN = re.compile(
    r"^\s*minimum_free_disk_gib:\s*([0-9]+)\s*$",
    re.MULTILINE,
)
_EXTERNAL_PERMISSION_PATTERN = re.compile(
    r"^external_storage_permission:\s*(true|false)\s*$",
    re.MULTILINE,
)
_EXTRACT_REQUIRED_PATTERN = re.compile(
    r"^\s*external_cache_required_for_body_extracts:\s*(true|false)\s*$",
    re.MULTILINE,
)
_EXTERNAL_RELATIVE_PATTERN = re.compile(
    r"^\s*external_cache_relative_path:\s*['\"]?([^'\"\n]+?)['\"]?\s*$",
    re.MULTILINE,
)
DEFAULT_EXTERNAL_VOLUME_NAMES = ("EXTSSD", "ExtSSD-Data")
DEFAULT_EXTERNAL_RELATIVE_PATH = "okf-govuk-content"


def _boolean(match: re.Match[str] | None, *, default: bool = False) -> bool:
    return default if match is None else match.group(1) == "true"


def _safe_relative(value: str) -> Path:
    path = Path(value.strip())
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise StoragePolicyError("external_cache_relative_path must be a safe relative path")
    return path


def nearest_existing_path(path: Path) -> Path:
    """Return the nearest existing ancestor without resolving through symlinks."""

    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            raise StoragePolicyError(f"no existing ancestor for storage target: {path}")
        current = parent
    if current.is_symlink():
        raise StoragePolicyError(f"storage target cannot resolve through a symbolic link: {current}")
    return current


def free_disk_bytes(path: Path) -> int:
    return int(shutil.disk_usage(nearest_existing_path(path)).free)


def assert_minimum_free_disk(
    path: Path,
    minimum_free_bytes: int,
    *,
    reserve_bytes: int = 0,
    phase: str,
) -> dict[str, int | str | bool]:
    if minimum_free_bytes < 1 or reserve_bytes < 0:
        raise StoragePolicyError("minimum free disk and reserve values must be positive")
    observed = free_disk_bytes(path)
    remaining = observed - reserve_bytes
    if remaining < minimum_free_bytes:
        raise StoragePolicyError(
            f"insufficient free disk for {phase}: {observed}-{reserve_bytes}<{minimum_free_bytes} bytes "
            f"at {nearest_existing_path(path)}"
        )
    return {
        "target": path.as_posix(),
        "observed_free_bytes": observed,
        "reserved_write_bytes": reserve_bytes,
        "minimum_free_disk_bytes": minimum_free_bytes,
        "projected_free_bytes": remaining,
        "minimum_free_disk_satisfied": True,
    }


@dataclass(frozen=True)
class StoragePolicy:
    root: Path
    minimum_free_bytes: int
    external_storage_permitted: bool
    external_cache_required_for_body_extracts: bool
    external_cache_relative_path: Path
    external_volume: Path | None
    external_cache_root: Path | None

    @property
    def external_cache_available(self) -> bool:
        return self.external_cache_root is not None

    def cache_root(self, label: str, purpose: str) -> Path:
        if not label or "/" in label or label in {".", ".."}:
            raise StoragePolicyError("cache snapshot label is unsafe")
        if not purpose or not re.fullmatch(r"[a-z][a-z0-9-]*", purpose):
            raise StoragePolicyError("cache purpose is unsafe")
        if self.external_cache_root is not None:
            return self.external_cache_root / "cache" / label / purpose
        return self.root / "corpus" / "cache" / label / purpose

    def extract_database(self, label: str) -> Path | None:
        if self.external_cache_root is None:
            if self.external_cache_required_for_body_extracts:
                raise StoragePolicyError(
                    "body-extract caching requires a mounted EXTSSD/ExtSSD-Data volume or "
                    "OKF_GOVUK_EXTERNAL_CACHE_ROOT"
                )
            return None
        return self.external_cache_root / "extracts" / label / "search-parts.sqlite"

    def preflight(
        self,
        *,
        reserve_bytes: int = 0,
        disclose_paths: bool = True,
    ) -> dict[str, object]:
        targets = [self.root]
        if self.external_cache_root is not None:
            targets.append(self.external_cache_root)
        checks = [
            assert_minimum_free_disk(
                target,
                self.minimum_free_bytes,
                reserve_bytes=reserve_bytes,
                phase="storage preflight",
            )
            for target in targets
        ]
        if not disclose_paths:
            for index, check in enumerate(checks):
                check["target"] = "repository" if index == 0 else "external-cache"
        return {
            "schema": "govuk-okf-storage-preflight.v1",
            "minimum_free_disk_bytes": self.minimum_free_bytes,
            "external_storage_permitted": self.external_storage_permitted,
            "external_cache_available": self.external_cache_available,
            "external_volume_name": self.external_volume.name if self.external_volume else None,
            "external_cache_root": (
                self.external_cache_root.as_posix()
                if disclose_paths and self.external_cache_root
                else "external-cache" if self.external_cache_root else None
            ),
            "checks": checks,
        }


def load_storage_policy(
    root: Path,
    *,
    volumes_root: Path = Path("/Volumes"),
    environ: Mapping[str, str] | None = None,
) -> StoragePolicy:
    root = root.resolve()
    launch = root / "governance" / "launch-manifest.yaml"
    if not launch.is_file():
        raise StoragePolicyError("launch manifest is required to authorise storage")
    document = launch.read_text(encoding="utf-8")
    minimum_match = _MINIMUM_FREE_PATTERN.search(document)
    if not minimum_match or int(minimum_match.group(1)) < 1:
        raise StoragePolicyError("launch manifest has no positive minimum_free_disk_gib value")
    permission = _boolean(_EXTERNAL_PERMISSION_PATTERN.search(document))
    required = _boolean(_EXTRACT_REQUIRED_PATTERN.search(document))
    relative_match = _EXTERNAL_RELATIVE_PATTERN.search(document)
    relative = _safe_relative(
        relative_match.group(1) if relative_match else DEFAULT_EXTERNAL_RELATIVE_PATH
    )
    environment = os.environ if environ is None else environ
    explicit = environment.get("OKF_GOVUK_EXTERNAL_CACHE_ROOT")
    external_volume: Path | None = None
    external_cache_root: Path | None = None
    if explicit:
        if not permission:
            raise StoragePolicyError("external cache root was configured without external-storage permission")
        candidate = Path(explicit).expanduser()
        if not candidate.is_absolute() or candidate.is_symlink():
            raise StoragePolicyError("OKF_GOVUK_EXTERNAL_CACHE_ROOT must be an absolute non-symlink path")
        external_cache_root = candidate
        external_volume = nearest_existing_path(candidate)
    elif permission and volumes_root.is_dir():
        mounted = {
            path.name.casefold(): path
            for path in volumes_root.iterdir()
            if path.is_dir() and not path.is_symlink() and os.path.ismount(path)
        }
        for name in DEFAULT_EXTERNAL_VOLUME_NAMES:
            volume = mounted.get(name.casefold())
            if volume is not None:
                external_volume = volume
                external_cache_root = volume / relative
                break
    policy = StoragePolicy(
        root=root,
        minimum_free_bytes=int(minimum_match.group(1)) * 1024**3,
        external_storage_permitted=permission,
        external_cache_required_for_body_extracts=required,
        external_cache_relative_path=relative,
        external_volume=external_volume,
        external_cache_root=external_cache_root,
    )
    if required and external_cache_root is None:
        raise StoragePolicyError(
            "launch manifest requires an external body-extract cache but no EXTSSD volume is mounted"
        )
    policy.preflight()
    return policy
