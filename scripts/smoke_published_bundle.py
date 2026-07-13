#!/usr/bin/env python3
"""Verify live Pages critical bytes against the packaged bundle checksums."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_RANGE_BYTES = 64 * 1024 * 1024
CRITICAL_PATHS = ("index.html", "okf-explorer.json", "data/manifest.json", "release-data-plane.json")


def _bounded_gunzip(payload: bytes, expected_bytes: int) -> bytes:
    if not 0 < expected_bytes <= MAX_RANGE_BYTES:
        raise ValueError("range index declares an invalid logical member size")
    with gzip.GzipFile(fileobj=BytesIO(payload), mode="rb") as compressed:
        decoded = compressed.read(expected_bytes + 1)
    if len(decoded) != expected_bytes:
        raise ValueError("range transport member decoded length differs from the index")
    return decoded


def _fetch(url: str) -> tuple[bytes, str, int, str]:
    request = Request(url, headers={"User-Agent": "okf-govuk-content-release-smoke/1.0"})
    with urlopen(request, timeout=30) as response:
        status = int(response.status)
        content_type = str(response.headers.get("content-type") or "")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        final_url = response.geturl()
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError(f"response exceeds {MAX_RESPONSE_BYTES} bytes: {url}")
    return payload, final_url, status, content_type


def _fetch_range(url: str, start: int, end: int) -> tuple[bytes, str, int, str, str, str]:
    if start < 0 or end < start or end - start + 1 > MAX_RANGE_BYTES:
        raise ValueError("live smoke range is invalid or exceeds its 64 MiB bound")
    request = Request(
        url,
        headers={
            "User-Agent": "okf-govuk-content-release-smoke/1.0",
            "Range": f"bytes={start}-{end}",
            "Accept-Encoding": "gzip, deflate, br",
        },
    )
    with urlopen(request, timeout=30) as response:
        status = int(response.status)
        content_range = str(response.headers.get("content-range") or "")
        content_encoding = str(response.headers.get("content-encoding") or "")
        content_type = str(response.headers.get("content-type") or "")
        payload = response.read(MAX_RANGE_BYTES + 1)
        final_url = response.geturl()
    if len(payload) > MAX_RANGE_BYTES:
        raise ValueError(f"range response exceeds {MAX_RANGE_BYTES} bytes: {url}")
    return payload, final_url, status, content_range, content_encoding, content_type


def run(base_url: str, verified: Path) -> dict[str, object]:
    base_url = base_url.rstrip("/") + "/"
    base = urlsplit(base_url)
    if base.scheme != "https" or not base.netloc:
        raise ValueError("live base URL must be absolute HTTPS")
    site = verified.resolve() / "site"
    checksums = json.loads((site / "checksums.json").read_text(encoding="utf-8"))
    checksum_rows = {row["path"]: row for row in checksums["files"]}
    local_manifest = json.loads((site / "data" / "manifest.json").read_text(encoding="utf-8"))
    local_index = json.loads((site / "release-data-plane.json").read_text(encoding="utf-8"))
    results = []
    range_results = []
    errors = []
    for relative in CRITICAL_PATHS:
        row = checksum_rows.get(relative)
        if not isinstance(row, dict):
            errors.append(f"critical path absent from packaged checksums: {relative}")
            continue
        requested = urljoin(base_url, relative)
        try:
            payload, final_url, status, content_type = _fetch(requested)
            final = urlsplit(final_url)
            digest = hashlib.sha256(payload).hexdigest()
            passed = (
                status == 200
                and final.scheme == "https"
                and final.netloc == base.netloc
                and digest == row["sha256"]
                and len(payload) == row["bytes"]
            )
            if not passed:
                errors.append(f"live byte verification failed: {relative}")
            results.append(
                {
                    "path": relative,
                    "requested_url": requested,
                    "final_url": final_url,
                    "status": status,
                    "content_type": content_type,
                    "bytes": len(payload),
                    "sha256": digest,
                    "expected_bytes": row["bytes"],
                    "expected_sha256": row["sha256"],
                    "passed": passed,
                }
            )
            if relative == "data/manifest.json":
                remote_manifest = json.loads(payload)
                if remote_manifest.get("snapshot") != local_manifest.get("snapshot"):
                    errors.append("live data manifest snapshot differs from packaged snapshot")
        except Exception as exc:  # evidence records bounded network failures
            errors.append(f"{relative}: {type(exc).__name__}: {exc}")
    entries_by_pack: dict[str, list[dict[str, object]]] = {}
    for entry in local_index.get("entries") or []:
        if isinstance(entry, dict) and isinstance(entry.get("pack"), str):
            entries_by_pack.setdefault(str(entry["pack"]), []).append(entry)
    for pack in local_index.get("packs") or []:
        if not isinstance(pack, dict) or not isinstance(pack.get("id"), str):
            errors.append("release data-plane pack row is malformed")
            continue
        candidates = entries_by_pack.get(str(pack["id"])) or []
        if not candidates:
            errors.append(f"release data-plane pack has no smoke candidate: {pack['id']}")
            continue
        entry = min(candidates, key=lambda row: int(row.get("packed_bytes") or MAX_RANGE_BYTES + 1))
        start = int(entry.get("offset") or 0)
        packed_bytes = int(entry.get("packed_bytes") or 0)
        end = start + packed_bytes - 1
        relative = str(pack.get("path") or "")
        requested = urljoin(base_url, relative)
        expected_content_range = f"bytes {start}-{end}/{int(pack.get('bytes') or 0)}"
        try:
            payload, final_url, status, content_range, content_encoding, content_type = _fetch_range(
                requested, start, end
            )
            final = urlsplit(final_url)
            packed_digest = hashlib.sha256(payload).hexdigest()
            source = (
                _bounded_gunzip(payload, int(entry.get("bytes") or 0))
                if entry.get("transport_compression") == "gzip"
                else payload
            )
            source_digest = hashlib.sha256(source).hexdigest()
            passed = (
                status == 206
                and final.scheme == "https"
                and final.netloc == base.netloc
                and not content_encoding
                and content_type.split(";", 1)[0].strip().lower() == "application/gzip"
                and payload.startswith(b"\x1f\x8b")
                and content_range == expected_content_range
                and len(payload) == packed_bytes
                and packed_digest == entry.get("packed_sha256")
                and len(source) == entry.get("bytes")
                and source_digest == entry.get("sha256")
            )
            if not passed:
                errors.append(f"live Pages range verification failed: {pack['id']}")
            range_results.append(
                {
                    "pack": pack["id"],
                    "path": entry.get("path"),
                    "requested_url": requested,
                    "final_url": final_url,
                    "status": status,
                    "content_range": content_range,
                    "content_type": content_type,
                    "bytes": len(payload),
                    "packed_sha256": packed_digest,
                    "source_sha256": source_digest,
                    "passed": passed,
                }
            )
        except Exception as exc:  # evidence records bounded network failures
            errors.append(f"{pack['id']}: {type(exc).__name__}: {exc}")
    return {
        "schema": "govuk-okf-pages-live-smoke.v1",
        "checked_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "snapshot": local_manifest.get("snapshot"),
        "passed": (
            not errors
            and len(results) == len(CRITICAL_PATHS)
            and len(range_results) == len(local_index.get("packs") or [])
        ),
        "results": results,
        "range_results": range_results,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--verified", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run(args.base_url, args.verified)
    except Exception as exc:
        report = {
            "schema": "govuk-okf-pages-live-smoke.v1",
            "checked_at": datetime.now(UTC).isoformat(),
            "base_url": args.base_url,
            "passed": False,
            "results": [],
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "passed": report["passed"]}, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
