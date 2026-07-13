#!/usr/bin/env python3
"""Verify local release bytes against a GitHub Release API response."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.release_ref import release_channel  # noqa: E402

EXPECTATION_SCHEMA = "govuk-okf-github-release-asset-expectation.v2"
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_channel(tag: str, channel: str | None = None) -> str:
    encoded = release_channel(tag)
    if encoded is None:
        raise ValueError("release tag does not encode a valid RC or final channel")
    if channel is not None and channel != encoded:
        raise ValueError("release channel differs from the semantic-version tag")
    return encoded


def _root_material(
    rows: list[dict[str, Any]],
    *,
    tag: str,
    channel: str,
    prerelease: bool,
) -> str:
    header = f"tag\0{tag}\nchannel\0{channel}\nprerelease\0{str(prerelease).lower()}\n"
    assets = "".join(f"{row['name']}\0{row['bytes']}\0{row['digest']}\n" for row in rows)
    return header + assets


def build_expectation(
    assets: list[Path],
    *,
    tag: str,
    channel: str | None = None,
) -> dict[str, Any]:
    expected_channel = _validated_channel(tag, channel)
    prerelease = expected_channel == "release-candidate"
    local: dict[str, dict[str, Any]] = {}
    for path in assets:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"local release asset is missing or unsafe: {path}")
        if path.name in local:
            raise ValueError(f"duplicate local release asset name: {path.name}")
        local[path.name] = {"bytes": path.stat().st_size, "digest": f"sha256:{_sha256(path)}"}
    rows = [{"name": name, **local[name]} for name in sorted(local)]
    material = _root_material(
        rows,
        tag=tag,
        channel=expected_channel,
        prerelease=prerelease,
    )
    return {
        "schema": EXPECTATION_SCHEMA,
        "tag": tag,
        "channel": expected_channel,
        "prerelease": prerelease,
        "asset_count": len(rows),
        "assets": rows,
        "root_sha256": hashlib.sha256(material.encode()).hexdigest(),
    }


def verify_release_expectation(
    document: dict[str, Any],
    expectation: dict[str, Any],
    *,
    tag: str,
    published: bool,
    channel: str | None = None,
) -> list[str]:
    errors: list[str] = []
    encoded_channel = release_channel(tag)
    if encoded_channel is None:
        errors.append("release tag does not encode a valid RC or final channel")
        expected_channel = "invalid"
    else:
        expected_channel = encoded_channel
        if channel is not None and channel != encoded_channel:
            errors.append("release channel differs from the semantic-version tag")
    expected_prerelease = expected_channel == "release-candidate"
    if expectation.get("schema") != EXPECTATION_SCHEMA or expectation.get("tag") != tag:
        errors.append("release asset expectation schema or tag differs")
    if (
        expectation.get("channel") != expected_channel
        or expectation.get("prerelease") is not expected_prerelease
    ):
        errors.append("release asset expectation channel or prerelease state differs")
    expected_rows = expectation.get("assets")
    if not isinstance(expected_rows, list):
        return [*errors, "release asset expectation rows are not an array"]
    normalized_rows: list[dict[str, Any]] = []
    names: list[str] = []
    for row in expected_rows:
        if not isinstance(row, dict):
            errors.append("release asset expectation row is not an object")
            continue
        name = row.get("name")
        size = row.get("bytes")
        digest = row.get("digest")
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in {".", ".."}
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or not DIGEST_PATTERN.fullmatch(digest)
        ):
            errors.append("release asset expectation row is malformed")
            continue
        names.append(name)
        normalized_rows.append({"name": name, "bytes": size, "digest": digest})
    if names != sorted(names) or len(set(names)) != len(names):
        errors.append("release asset expectation names are not sorted and unique")
    material = _root_material(
        normalized_rows,
        tag=tag,
        channel=expected_channel,
        prerelease=expected_prerelease,
    )
    if expectation.get("asset_count") != len(expected_rows) or expectation.get("root_sha256") != hashlib.sha256(
        material.encode()
    ).hexdigest():
        errors.append("release asset expectation count or root differs")
    if document.get("tag_name") != tag:
        errors.append("GitHub Release tag differs from the requested tag")
    if document.get("prerelease") is not expected_prerelease:
        errors.append("GitHub Release prerelease state differs from the attested release channel")
    if published:
        if document.get("draft") is not False:
            errors.append("GitHub Release is not published")
        if document.get("immutable") is not True:
            errors.append("published GitHub Release is not immutable")
    elif document.get("draft") is not True:
        errors.append("GitHub Release must remain a draft during asset verification")

    local = {
        row["name"]: {"bytes": row["bytes"], "digest": row["digest"]}
        for row in normalized_rows
    }

    remote: dict[str, dict[str, Any]] = {}
    rows = document.get("assets")
    if not isinstance(rows, list):
        return [*errors, "GitHub Release assets are not an array"]
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            errors.append("GitHub Release asset row is malformed")
            continue
        name = row["name"]
        if name in remote:
            errors.append(f"duplicate GitHub Release asset name: {name}")
            continue
        remote[name] = row
    if set(remote) != set(local):
        errors.append("GitHub Release asset names differ from the verified local set")
    for name in sorted(set(remote) & set(local)):
        row = remote[name]
        expected = local[name]
        if row.get("state") != "uploaded":
            errors.append(f"GitHub Release asset is not uploaded: {name}")
        if row.get("size") != expected["bytes"]:
            errors.append(f"GitHub Release asset size differs: {name}")
        if row.get("digest") != expected["digest"]:
            errors.append(f"GitHub Release asset SHA-256 differs: {name}")
    return errors


def verify_release_assets(
    document: dict[str, Any],
    assets: list[Path],
    *,
    tag: str,
    published: bool,
    channel: str | None = None,
) -> list[str]:
    try:
        expectation = build_expectation(assets, tag=tag, channel=channel)
    except ValueError as exc:
        return [str(exc)]
    return verify_release_expectation(
        document,
        expectation,
        tag=tag,
        published=published,
        channel=channel,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-json", type=Path)
    parser.add_argument("--assets", type=Path, nargs="+")
    parser.add_argument("--expectation", type=Path)
    parser.add_argument("--write-expectation", type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--channel", choices=("release-candidate", "final"), required=True)
    parser.add_argument("--published", action="store_true")
    args = parser.parse_args()
    try:
        if args.write_expectation:
            if not args.assets or args.expectation:
                parser.error("--write-expectation requires --assets and forbids --expectation")
            expectation = build_expectation(args.assets, tag=args.tag, channel=args.channel)
            args.write_expectation.parent.mkdir(parents=True, exist_ok=True)
            args.write_expectation.write_text(
                json.dumps(expectation, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(json.dumps({"passed": True, "expectation": str(args.write_expectation)}, sort_keys=True))
            return 0
        if bool(args.assets) == bool(args.expectation):
            parser.error("verification requires exactly one of --assets or --expectation")
        if not args.release_json:
            parser.error("verification requires --release-json")
        document = json.loads(args.release_json.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("release JSON is not an object")
        if args.expectation:
            expectation = json.loads(args.expectation.read_text(encoding="utf-8"))
            if not isinstance(expectation, dict):
                raise ValueError("release expectation is not an object")
            errors = verify_release_expectation(
                document,
                expectation,
                tag=args.tag,
                published=args.published,
                channel=args.channel,
            )
        else:
            errors = verify_release_assets(
                document,
                args.assets or [],
                tag=args.tag,
                published=args.published,
                channel=args.channel,
            )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        errors = [f"GitHub Release evidence is unavailable: {exc}"]
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(json.dumps({"passed": True, "tag": args.tag}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
