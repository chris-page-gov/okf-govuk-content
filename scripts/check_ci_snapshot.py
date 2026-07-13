#!/usr/bin/env python3
"""Dispatch CI validation by the checked release snapshot contract.

Development fixtures are rebuilt byte-for-byte.  A promoted full-corpus
snapshot instead validates the immutable clean-room evidence and every current
release/publication binding; it never pretends that the archived official
source tree is present in a fresh Git checkout.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_release  # noqa: E402
import reproduce_release  # noqa: E402


class SnapshotCheckError(ValueError):
    """Raised when no safe CI validation route matches the checked snapshot."""


def _load(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotCheckError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SnapshotCheckError(f"{label} must be an object")
    return value


def snapshot_mode(manifest: dict[str, object]) -> str:
    snapshot = manifest.get("snapshot")
    if not isinstance(snapshot, dict):
        raise SnapshotCheckError("release manifest has no snapshot contract")
    kind = snapshot.get("kind")
    sampled = snapshot.get("sampled")
    release_kind = manifest.get("release_kind")
    if kind == "fixture" and sampled is True and release_kind == "fixture":
        return "fixture"
    if (
        kind == "full_corpus"
        and sampled is False
        and release_kind == "machine_release_candidate"
        and manifest.get("publication_ready") is True
    ):
        promotion = manifest.get("promotion")
        if not isinstance(promotion, dict) or promotion.get("finalized") not in {False, True}:
            raise SnapshotCheckError("full-corpus candidate has no valid promotion state")
        return "finalized" if promotion["finalized"] else "candidate"
    raise SnapshotCheckError(
        "CI snapshot dispatch accepts only the exact development fixture or a promoted "
        "unsampled full-corpus candidate/final"
    )


def _run(command: list[str], root: Path) -> None:
    result = subprocess.run(command, cwd=root, check=False)
    if result.returncode != 0:
        raise SnapshotCheckError(f"snapshot command failed ({result.returncode}): {' '.join(command)}")


def check_snapshot(
    root: Path,
    *,
    runner: Callable[[list[str], Path], None] = _run,
) -> str:
    root = root.resolve()
    manifest = _load(root / check_release.MANIFEST_RELATIVE, "release manifest")
    mode = snapshot_mode(manifest)
    if mode == "fixture":
        runner([sys.executable, "scripts/build_bundle.py", "--check"], root)
        runner([sys.executable, "scripts/reproduce_release.py", "--check"], root)
        runner([sys.executable, "scripts/check_release.py"], root)
        return mode

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise SnapshotCheckError("full-corpus candidate has no artifact contract")
    clean_relative = artifacts.get("clean_room_reproduction")
    if not isinstance(clean_relative, str):
        raise SnapshotCheckError("full-corpus candidate has no clean-room evidence path")
    clean = _load(root / clean_relative, "clean-room evidence")
    errors = reproduce_release.validate_evidence(clean, require_release=True)
    errors.extend(
        check_release.validate_release(
            root,
            require_publication_ready=True,
            require_finalized=mode == "finalized",
            allow_missing_archived_inputs=True,
        )
    )
    if errors:
        raise SnapshotCheckError("full-corpus snapshot validation failed: " + "; ".join(errors))
    return mode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        mode = check_snapshot(args.root)
    except SnapshotCheckError as exc:
        print(f"CI snapshot validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"CI snapshot validation passed: {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
