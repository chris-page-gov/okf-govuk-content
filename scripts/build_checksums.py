#!/usr/bin/env python3
"""Build or check the complete publication checksum manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE = ROOT / "bundle"
HASH_CHUNK_SIZE = 1024 * 1024


def hash_file(path: Path) -> tuple[int, str]:
    """Hash one publication file without materialising a shard in memory."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK_SIZE):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def render(bundle: Path) -> str:
    files = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.name in {"checksums.json", ".DS_Store"}:
            continue
        size, digest = hash_file(path)
        files.append(
            {
                "path": path.relative_to(bundle).as_posix(),
                "bytes": size,
                "sha256": digest,
            }
        )
    document = {
        "schema": "okf-checksums.v1",
        "algorithm": "sha256",
        "file_count": len(files),
        "files": files,
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = args.bundle / "checksums.json"
    expected = render(args.bundle)
    if args.check:
        if not target.is_file() or target.read_text(encoding="utf-8") != expected:
            print("publication checksums are missing or out of date", file=sys.stderr)
            return 1
        print(f"checksums verified for {json.loads(expected)['file_count']} files")
        return 0
    target.write_text(expected, encoding="utf-8")
    print(f"wrote checksums for {json.loads(expected)['file_count']} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
