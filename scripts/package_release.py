#!/usr/bin/env python3
"""Gate, package or verify immutable Release/Pages bytes."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.release_packaging import (  # noqa: E402
    PackagingError,
    attach_pages_browser_evidence,
    check_pages_site,
    check_verified_release,
    package_verified_release,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", type=Path, metavar="VERIFIED_DIRECTORY")
    parser.add_argument("--check-site", type=Path, metavar="PAGES_SITE_DIRECTORY")
    parser.add_argument("--attach-pages-browser-evidence", type=Path, metavar="VERIFIED_DIRECTORY")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--bundle", type=Path, default=ROOT / "bundle")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--tag")
    parser.add_argument("--channel", choices=("release-candidate", "final"))
    parser.add_argument("--browser-evidence", type=Path)
    args = parser.parse_args()
    selected_modes = sum(bool(value) for value in (args.check, args.check_site, args.attach_pages_browser_evidence))
    if selected_modes > 1:
        parser.error("--check, --check-site and --attach-pages-browser-evidence are mutually exclusive")
    if args.attach_pages_browser_evidence:
        if not args.evidence:
            parser.error("--attach-pages-browser-evidence requires --evidence")
        try:
            attach_pages_browser_evidence(args.attach_pages_browser_evidence, args.evidence)
        except (OSError, PackagingError) as exc:
            print(f"attaching packed-site browser evidence failed: {exc}", file=sys.stderr)
            return 1
        print(f"packed-site browser evidence attached: {args.attach_pages_browser_evidence}")
        return 0
    if args.check_site:
        errors = check_pages_site(args.check_site)
        if errors:
            print("Pages control-plane validation failed:", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1
        print(f"Pages control plane passed: {args.check_site}")
        return 0
    if args.check:
        errors = check_verified_release(args.check)
        if errors:
            print("verified package validation failed:", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1
        print(f"verified release package passed: {args.check}")
        return 0
    if not args.output or not args.tag or not args.channel or not args.browser_evidence:
        parser.error("packaging requires --output, --tag, --channel and --browser-evidence")
    expected_candidate = "-rc." in args.tag
    if expected_candidate != (args.channel == "release-candidate"):
        parser.error("--channel must agree with the semantic-version tag")
    gate_arguments = [sys.executable, str(ROOT / "scripts" / "check_release.py")]
    gate_arguments.append("--publication-ready" if args.channel == "release-candidate" else "--finalized")
    gate = subprocess.run(
        gate_arguments,
        cwd=ROOT,
        check=False,
    )
    if gate.returncode:
        return gate.returncode
    try:
        manifest = package_verified_release(
            repository_root=ROOT,
            bundle=args.bundle,
            output=args.output,
            tag=args.tag,
            browser_evidence=args.browser_evidence,
        )
    except (OSError, PackagingError) as exc:
        print(f"release packaging failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(args.output), "root_sha256": manifest["root_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
