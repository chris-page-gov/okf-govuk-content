#!/usr/bin/env python3
"""Verify live Pages critical bytes against the packaged bundle checksums."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
CRITICAL_PATHS = ("index.html", "okf-explorer.json", "data/manifest.json")


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


def run(base_url: str, verified: Path) -> dict[str, object]:
    base_url = base_url.rstrip("/") + "/"
    base = urlsplit(base_url)
    if base.scheme != "https" or not base.netloc:
        raise ValueError("live base URL must be absolute HTTPS")
    site = verified.resolve() / "site"
    checksums = json.loads((site / "checksums.json").read_text(encoding="utf-8"))
    checksum_rows = {row["path"]: row for row in checksums["files"]}
    local_manifest = json.loads((site / "data" / "manifest.json").read_text(encoding="utf-8"))
    results = []
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
    return {
        "schema": "govuk-okf-pages-live-smoke.v1",
        "checked_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "snapshot": local_manifest.get("snapshot"),
        "passed": not errors and len(results) == len(CRITICAL_PATHS),
        "results": results,
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
