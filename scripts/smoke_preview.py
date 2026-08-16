#!/usr/bin/env python3
"""Verify live demonstrator preview bytes against the transported package."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
CRITICAL_PATHS = (
    "index.html",
    "okf-explorer.json",
    "data/manifest.json",
    "data/demonstrator.json",
    "checksums.json",
)


def _fetch(url: str) -> tuple[bytes, str, int, str]:
    request = Request(url, headers={"User-Agent": "okf-govuk-content-preview-smoke/1.0"})
    with urlopen(request, timeout=30) as response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        final_url = response.geturl()
        status = int(response.status)
        content_type = str(response.headers.get("content-type") or "")
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError(f"response exceeds {MAX_RESPONSE_BYTES} bytes: {url}")
    return payload, final_url, status, content_type


def run(base_url: str, preview: Path) -> dict[str, object]:
    base_url = base_url.rstrip("/") + "/"
    base = urlsplit(base_url)
    if base.scheme != "https" or not base.netloc:
        raise ValueError("live base URL must be absolute HTTPS")
    preview = preview.resolve()
    site = preview / "site"
    package_manifest = json.loads((preview / "verified-preview.json").read_text(encoding="utf-8"))
    checksums = json.loads((site / "checksums.json").read_text(encoding="utf-8"))
    checksum_rows = {row["path"]: row for row in checksums["files"]}
    expected_checksums = {
        "bytes": (site / "checksums.json").stat().st_size,
        "sha256": package_manifest["bundle_checksums_sha256"],
    }
    results: list[dict[str, object]] = []
    errors: list[str] = []
    responses: dict[str, bytes] = {}
    for relative in CRITICAL_PATHS:
        row = expected_checksums if relative == "checksums.json" else checksum_rows.get(relative)
        if not isinstance(row, dict):
            errors.append(f"critical path absent from preview checksums: {relative}")
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
            responses[relative] = payload
        except Exception as exc:  # bounded network failures belong in the evidence
            errors.append(f"{relative}: {type(exc).__name__}: {exc}")

    try:
        descriptor = json.loads(responses["okf-explorer.json"])
        data_manifest = json.loads(responses["data/manifest.json"])
        demonstrator = json.loads(responses["data/demonstrator.json"])
        coverage = demonstrator.get("coverage") or {}
        if descriptor.get("snapshot") != package_manifest.get("snapshot"):
            errors.append("live Explorer descriptor snapshot differs from the preview package")
        if descriptor.get("status") != "bounded-demonstrator":
            errors.append("live Explorer descriptor lost its bounded-demonstrator label")
        if data_manifest.get("snapshot") != package_manifest.get("snapshot"):
            errors.append("live data manifest snapshot differs from the preview package")
        if data_manifest.get("counts", {}).get("records") != 69:
            errors.append("live data manifest does not contain exactly 69 records")
        if coverage.get("seed_expected") != 69 or coverage.get("seed_represented") != 69:
            errors.append("live demonstrator does not retain the 69-of-69 seed contract")
        if coverage.get("unexplained_seed_omissions") != 0:
            errors.append("live demonstrator reports unexplained seed omissions")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        errors.append(f"live preview control documents are invalid: {type(exc).__name__}: {exc}")

    return {
        "schema": "govuk-okf-demonstrator-preview-live-smoke.v1",
        "checked_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "snapshot": package_manifest.get("snapshot"),
        "commit": package_manifest.get("commit"),
        "publication_tier": package_manifest.get("publication_tier"),
        "passed": not errors and len(results) == len(CRITICAL_PATHS),
        "results": results,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--preview", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run(args.base_url, args.preview)
    except Exception as exc:
        report = {
            "schema": "govuk-okf-demonstrator-preview-live-smoke.v1",
            "checked_at": datetime.now(UTC).isoformat(),
            "base_url": args.base_url,
            "passed": False,
            "results": [],
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "passed": report["passed"]}, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
