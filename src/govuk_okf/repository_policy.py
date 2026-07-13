"""Deterministic validation of checked-in GitHub repository governance."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

POLICY_SCHEMA = "govuk-okf-repository-policy.v1"
REQUIRED_BRANCH_BOOLEANS = {
    "enforce_admins": True,
    "required_linear_history": True,
    "allow_force_pushes": False,
    "allow_deletions": False,
    "required_conversation_resolution": True,
}
REQUIRED_REVIEW_BOOLEANS = {
    "dismiss_stale_reviews": True,
    "require_code_owner_reviews": False,
}
WORKFLOW_MARKERS = {
    "ci.yml": {
        "required": (
            "permissions:\n  contents: read",
            "scripts/check_repository_policy.py",
            "scripts/check_release.py",
            "git diff --check",
        ),
        "forbidden": ("pull_request_target:", "permissions: write-all"),
    },
    "release.yml": {
        "required": (
            "tags:",
            "scripts/check_release_ref.py",
            "scripts/check_repository_policy.py",
            "scripts/check_provenance.py --require-candidate",
            "scripts/check_provenance.py --require-release",
            "scripts/check_release.py --publication-ready",
            "scripts/check_release.py --finalized",
            "scripts/package_release.py",
            "actions/upload-artifact@",
            "actions/attest@",
            "attestations: write",
            "artifact-metadata: write",
            "gh release create",
            "gh release upload",
            "gh release edit",
            "gh release verify",
            "scripts/verify_github_release_assets.py",
            "--attach-pages-browser-evidence",
            "release-asset-expectation.json",
            "X-GitHub-Api-Version: 2026-03-10",
            "immutable-releases",
            "--draft",
            "--verify-tag",
            "--prerelease",
            "verified-pages-${{ github.ref_name }}-${{ github.sha }}",
            "uses: ./.github/workflows/pages.yml",
            "cancel-in-progress: false",
        ),
        "forbidden": (
            "pull_request_target:",
            "permissions: write-all",
            "persist-credentials: true",
            "build_bundle.py",
            "git verify-tag",
            "--clobber",
        ),
    },
    "pages.yml": {
        "required": (
            "workflow_call:",
            "pages: write",
            "id-token: write",
            "actions/download-artifact@",
            "scripts/package_release.py --check-site",
            "browser-evidence.mjs",
            "actions/upload-pages-artifact@",
            "actions/deploy-pages@",
            "scripts/smoke_published_bundle.py",
            "cancel-in-progress: false",
        ),
        "forbidden": (
            "workflow_dispatch:",
            "pull_request_target:",
            "permissions: write-all",
            "persist-credentials: true",
            "build_bundle.py",
        ),
    },
}
ACTION_PATTERN = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)\s*$", re.MULTILINE)
REMOTE_ACTION_PATTERN = re.compile(r"^(?P<owner>[^/]+)/[^@]+@(?P<ref>.+)$")


def _load_json(path: Path, label: str, errors: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{label} is missing or invalid: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{label} must be a JSON object")
        return {}
    return value


def _api_enabled(value: Any) -> Any:
    if isinstance(value, dict) and "enabled" in value:
        return value["enabled"]
    return value


def _normalise_restrictions(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict) and all(not value.get(key) for key in ("users", "teams", "apps")):
        return None
    return value


def compare_api_capture(local: dict[str, Any], capture: dict[str, Any]) -> list[str]:
    """Compare a raw branch-protection API capture with the checked policy."""

    errors: list[str] = []
    if isinstance(capture.get("branch_protection"), dict):
        capture = capture["branch_protection"]
    local_checks = local.get("required_status_checks") or {}
    api_checks = capture.get("required_status_checks") or {}
    if bool(api_checks.get("strict")) != bool(local_checks.get("strict")):
        errors.append("API capture differs: required_status_checks.strict")
    if sorted(api_checks.get("contexts") or []) != sorted(local_checks.get("contexts") or []):
        errors.append("API capture differs: required_status_checks.contexts")
    if bool(_api_enabled(capture.get("enforce_admins"))) != bool(local.get("enforce_admins")):
        errors.append("API capture differs: enforce_admins")
    local_reviews = local.get("required_pull_request_reviews") or {}
    api_reviews = capture.get("required_pull_request_reviews") or {}
    for key in (
        "dismiss_stale_reviews",
        "require_code_owner_reviews",
        "required_approving_review_count",
        "require_last_push_approval",
    ):
        if api_reviews.get(key) != local_reviews.get(key):
            errors.append(f"API capture differs: required_pull_request_reviews.{key}")
    for key in (
        "required_linear_history",
        "allow_force_pushes",
        "allow_deletions",
        "block_creations",
        "required_conversation_resolution",
        "lock_branch",
        "allow_fork_syncing",
    ):
        if _api_enabled(capture.get(key)) != local.get(key):
            errors.append(f"API capture differs: {key}")
    if _normalise_restrictions(capture.get("restrictions")) != _normalise_restrictions(local.get("restrictions")):
        errors.append("API capture differs: restrictions")
    return errors


def compare_publication_api_capture(policy: dict[str, Any], capture: dict[str, Any]) -> list[str]:
    """Compare immutable-release and Pages API read-backs with policy."""

    errors: list[str] = []
    release_contract = (policy.get("release") or {}).get("immutable_releases") or {}
    immutable = capture.get("immutable_releases")
    if not isinstance(immutable, dict):
        errors.append("publication API capture has no immutable-releases response")
    elif immutable.get("enabled") is not release_contract.get("enabled_required"):
        errors.append("publication API capture differs: immutable releases enabled")
    pages_contract = policy.get("pages") or {}
    pages = capture.get("pages")
    if not isinstance(pages, dict):
        errors.append("publication API capture has no Pages response")
        return errors
    expected = {
        "html_url": pages_contract.get("expected_url"),
        "build_type": pages_contract.get("build_type"),
        "public": pages_contract.get("public"),
        "https_enforced": pages_contract.get("https_enforced"),
    }
    for key, value in expected.items():
        if pages.get(key) != value:
            errors.append(f"publication API capture differs: Pages {key}")
    return errors


def _validate_branch_protection(branch: dict[str, Any], required_contexts: list[str], errors: list[str]) -> None:
    checks = branch.get("required_status_checks")
    if not isinstance(checks, dict) or checks.get("strict") is not True:
        errors.append("branch protection must require strict status checks")
    contexts = checks.get("contexts") if isinstance(checks, dict) else None
    if not isinstance(contexts, list) or sorted(contexts) != sorted(required_contexts):
        errors.append("branch protection required contexts differ from repository policy")
    for key, expected in REQUIRED_BRANCH_BOOLEANS.items():
        if branch.get(key) is not expected:
            errors.append(f"branch protection {key} must be {str(expected).lower()}")
    reviews = branch.get("required_pull_request_reviews")
    if not isinstance(reviews, dict):
        errors.append("branch protection must require pull-request reviews")
        return
    for key, expected in REQUIRED_REVIEW_BOOLEANS.items():
        if reviews.get(key) is not expected:
            errors.append(f"branch protection review setting {key} must be {str(expected).lower()}")
    count = reviews.get("required_approving_review_count")
    if count != 0 or isinstance(count, bool):
        errors.append("solo-owner branch protection must require zero approvals without bypassing pull requests")


def _validate_codeowners(path: Path, contract: dict[str, Any], errors: list[str]) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        errors.append(f"CODEOWNERS is missing: {exc}")
        return
    entries: dict[str, set[str]] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            errors.append(f"invalid CODEOWNERS row: {stripped}")
            continue
        entries.setdefault(parts[0], set()).update(parts[1:])
    owner = contract.get("required_owner")
    for pattern in contract.get("required_patterns") or []:
        if owner not in entries.get(pattern, set()):
            errors.append(f"CODEOWNERS does not assign {pattern} to {owner}")


def _validate_workflow(path: Path, name: str, allowed_owners: set[str], forbidden_tokens: list[str], errors: list[str]) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"workflow {name} is missing: {exc}")
        return
    contract = WORKFLOW_MARKERS[name]
    for marker in contract["required"]:
        if marker not in text:
            errors.append(f"workflow {name} lacks required contract marker: {marker}")
    for marker in (*contract["forbidden"], *forbidden_tokens):
        if marker in text:
            errors.append(f"workflow {name} contains forbidden contract marker: {marker}")
    for uses in ACTION_PATTERN.findall(text):
        if uses.startswith("./"):
            continue
        match = REMOTE_ACTION_PATTERN.fullmatch(uses)
        if not match:
            errors.append(f"workflow {name} has an invalid action reference: {uses}")
            continue
        if match.group("owner") not in allowed_owners:
            errors.append(f"workflow {name} uses an unapproved action owner: {uses}")
        reference = match.group("ref")
        if not (re.fullmatch(r"[0-9a-f]{40}", reference) or re.fullmatch(r"v[0-9]+", reference)):
            errors.append(f"workflow {name} action reference is not a major tag or commit SHA: {uses}")


def _validate_citation(root: Path, contract: dict[str, Any], policy: dict[str, Any], errors: list[str]) -> None:
    path = root / str(contract.get("citation_path") or "CITATION.cff")
    try:
        text = path.read_text(encoding="utf-8")
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project_version = str(project["project"]["version"])
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        errors.append(f"release citation metadata is unavailable: {exc}")
        return
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([a-z][a-z0-9-]*):\s*(.*?)\s*$", line)
        if not match or not match.group(2):
            continue
        value = match.group(2)
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                errors.append(f"CITATION.cff has invalid quoted value for {match.group(1)}")
                continue
        values[match.group(1)] = value
    expected_repository = f"https://github.com/{policy.get('repository')}"
    for key in ("cff-version", "message", "title", "type", "version", "repository-code", "url", "license"):
        if not values.get(key):
            errors.append(f"CITATION.cff lacks required top-level field: {key}")
    if values.get("cff-version") != "1.2.0":
        errors.append("CITATION.cff must use CFF 1.2.0")
    if values.get("version") != project_version:
        errors.append("CITATION.cff version differs from pyproject.toml")
    if values.get("repository-code") != expected_repository:
        errors.append("CITATION.cff repository-code differs from repository identity")
    if contract.get("doi") is None and "doi" in values:
        errors.append("CITATION.cff must not invent a DOI")


def validate_repository_policy(
    root: Path,
    api_capture: Path | None = None,
    publication_api_capture: Path | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    policy = _load_json(root / ".github" / "repository-policy.json", "repository policy", errors)
    if policy.get("schema") != POLICY_SCHEMA:
        errors.append(f"repository policy schema must be {POLICY_SCHEMA}")
    if policy.get("default_branch") != "main":
        errors.append("repository policy default branch must be main")
    release = policy.get("release") or {}
    if release.get("candidate_tag_pattern") != (
        r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)-rc\.[1-9][0-9]*$"
    ):
        errors.append("candidate tag pattern must be strict vMAJOR.MINOR.PATCH-rc.N")
    if release.get("final_tag_pattern") != (
        r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
    ):
        errors.append("final tag pattern must be strict vMAJOR.MINOR.PATCH")
    for key in ("require_annotated_tag", "require_main_ancestry", "artifact_attestations_required", "checksums_required"):
        if release.get(key) is not True:
            errors.append(f"release policy {key} must be true")
    if release.get("signature_policy") != "verify_if_present":
        errors.append("release signatures must be verified when present without making a signing key mandatory")
    if release.get("candidate_gate") != "scripts/check_release.py --publication-ready":
        errors.append("candidate release policy must require the publication-ready gate")
    if release.get("final_gate") != "scripts/check_release.py --finalized":
        errors.append("final release policy must require the finalized gate")
    if release.get("immutable_artifact_name") != "verified-pages-${{ github.ref_name }}-${{ github.sha }}":
        errors.append("release artifact identity must include both tag and commit")
    immutable = release.get("immutable_releases") or {}
    if immutable != {
        "api_endpoint": "/repos/chris-page-gov/okf-govuk-content/immutable-releases",
        "api_version": "2026-03-10",
        "enabled_required": True,
        "draft_first": True,
        "verify_exact_assets_before_publish": True,
        "verify_immutable_after_publish": True,
    }:
        errors.append("immutable-release policy must require versioned draft-first exact-asset verification")
    pages = policy.get("pages") or {}
    if pages.get("rebuild_forbidden") is not True or pages.get("post_deploy_smoke_required") is not True:
        errors.append("Pages policy must forbid rebuilds and require post-deploy smoke")
    expected_pages = {
        "api_endpoint": "/repos/chris-page-gov/okf-govuk-content/pages",
        "api_version": "2026-03-10",
        "expected_url": "https://chris-page-gov.github.io/okf-govuk-content/",
        "build_type": "workflow",
        "public": True,
        "https_enforced": True,
        "site_budget_bytes": 950_000_000,
        "data_plane": "same-origin-github-pages-range-packs",
        "pack_suffix": ".pack.gz",
        "browser_release_asset_fetch": False,
        "live_range_smoke_required": True,
    }
    for key, expected in expected_pages.items():
        if pages.get(key) != expected:
            errors.append(f"Pages policy {key} differs from the required publication contract")
    _validate_citation(root, policy.get("release_metadata") or {}, policy, errors)

    branch_contract = policy.get("branch_protection") or {}
    branch_path = root / str(branch_contract.get("policy_path") or ".github/branch-protection.json")
    branch = _load_json(branch_path, "branch-protection policy", errors)
    _validate_branch_protection(branch, list(branch_contract.get("required_check_contexts") or []), errors)
    codeowners_contract = policy.get("codeowners") or {}
    _validate_codeowners(root / str(codeowners_contract.get("path") or ".github/CODEOWNERS"), codeowners_contract, errors)

    workflow_policy = policy.get("workflow_policy") or {}
    allowed_owners = set(workflow_policy.get("allowed_action_owners") or [])
    forbidden_tokens = list(workflow_policy.get("forbidden_tokens") or [])
    for name in WORKFLOW_MARKERS:
        _validate_workflow(root / ".github" / "workflows" / name, name, allowed_owners, forbidden_tokens, errors)
    for event in workflow_policy.get("forbidden_events") or []:
        for name in WORKFLOW_MARKERS:
            path = root / ".github" / "workflows" / name
            if path.is_file() and f"{event}:" in path.read_text(encoding="utf-8"):
                errors.append(f"workflow {name} uses forbidden event {event}")

    api_compared = api_capture is not None
    if api_capture is not None:
        capture = _load_json(api_capture, "GitHub API capture", errors)
        errors.extend(compare_api_capture(branch, capture))
    publication_api_compared = publication_api_capture is not None
    if publication_api_capture is not None:
        capture = _load_json(publication_api_capture, "publication API capture", errors)
        errors.extend(compare_publication_api_capture(policy, capture))
    return {
        "schema": "govuk-okf-repository-policy-validation.v1",
        "passed": not errors,
        "repository": policy.get("repository"),
        "default_branch": policy.get("default_branch"),
        "api_capture_compared": api_compared,
        "publication_api_capture_compared": publication_api_compared,
        "checks": {
            "branch_protection": bool(branch),
            "codeowners": (root / ".github" / "CODEOWNERS").is_file(),
            "workflows": len(WORKFLOW_MARKERS),
            "citation": (root / "CITATION.cff").is_file(),
        },
        "errors": errors,
    }
