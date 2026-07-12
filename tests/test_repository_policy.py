from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from govuk_okf.repository_policy import compare_api_capture, validate_repository_policy

ROOT = Path(__file__).resolve().parents[1]


def api_capture(branch: dict[str, object]) -> dict[str, object]:
    capture = copy.deepcopy(branch)
    for key in (
        "enforce_admins",
        "required_linear_history",
        "allow_force_pushes",
        "allow_deletions",
        "block_creations",
        "required_conversation_resolution",
        "lock_branch",
        "allow_fork_syncing",
    ):
        capture[key] = {"enabled": branch[key]}
    return capture


class RepositoryPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(prefix="repo-policy-"))
        shutil.copytree(ROOT / ".github", self.temporary / ".github")
        shutil.copyfile(ROOT / "CITATION.cff", self.temporary / "CITATION.cff")
        shutil.copyfile(ROOT / "pyproject.toml", self.temporary / "pyproject.toml")

    def tearDown(self) -> None:
        shutil.rmtree(self.temporary)

    def test_checked_in_policy_passes_without_external_state(self) -> None:
        report = validate_repository_policy(ROOT)
        self.assertTrue(report["passed"], report["errors"])
        self.assertFalse(report["api_capture_compared"])
        self.assertTrue(report["checks"]["citation"])

    def test_solo_owner_policy_requires_pr_controls_without_self_approval(self) -> None:
        branch = json.loads((ROOT / ".github" / "branch-protection.json").read_text(encoding="utf-8"))
        reviews = branch["required_pull_request_reviews"]
        self.assertEqual(reviews["required_approving_review_count"], 0)
        self.assertFalse(reviews["require_code_owner_reviews"])
        self.assertTrue(branch["required_status_checks"]["strict"])
        self.assertTrue(branch["required_conversation_resolution"])
        self.assertFalse(branch["allow_force_pushes"])
        self.assertFalse(branch["allow_deletions"])

    def test_release_policy_separates_candidate_and_final_without_mandatory_key(self) -> None:
        policy = json.loads((ROOT / ".github" / "repository-policy.json").read_text(encoding="utf-8"))
        release = policy["release"]
        self.assertEqual(release["signature_policy"], "verify_if_present")
        self.assertNotIn("require_valid_signature", release)
        self.assertIn("-rc", release["candidate_tag_pattern"])
        self.assertNotIn("-rc", release["final_tag_pattern"])
        self.assertEqual(release["final_gate"], "scripts/check_release.py --finalized")

    def test_raw_api_capture_matches_nested_enabled_shape(self) -> None:
        branch = json.loads((self.temporary / ".github" / "branch-protection.json").read_text(encoding="utf-8"))
        capture = api_capture(branch)
        self.assertEqual(compare_api_capture(branch, capture), [])
        capture_path = self.temporary / "capture.json"
        capture_path.write_text(json.dumps(capture), encoding="utf-8")
        report = validate_repository_policy(self.temporary, capture_path)
        self.assertTrue(report["passed"], report["errors"])
        self.assertTrue(report["api_capture_compared"])

    def test_api_drift_and_protection_weakening_fail_closed(self) -> None:
        branch_path = self.temporary / ".github" / "branch-protection.json"
        branch = json.loads(branch_path.read_text(encoding="utf-8"))
        capture = api_capture(branch)
        capture["allow_force_pushes"] = {"enabled": True}
        self.assertIn("API capture differs: allow_force_pushes", compare_api_capture(branch, capture))
        branch["allow_force_pushes"] = True
        branch_path.write_text(json.dumps(branch), encoding="utf-8")
        report = validate_repository_policy(self.temporary)
        self.assertFalse(report["passed"])
        self.assertIn("branch protection allow_force_pushes must be false", report["errors"])

    def test_pages_rebuild_and_citation_version_drift_are_rejected(self) -> None:
        pages = self.temporary / ".github" / "workflows" / "pages.yml"
        pages.write_text(pages.read_text(encoding="utf-8") + "\n# build_bundle.py\n", encoding="utf-8")
        citation = self.temporary / "CITATION.cff"
        citation.write_text(citation.read_text(encoding="utf-8").replace("version: 0.1.0", "version: 9.9.9"), encoding="utf-8")
        report = validate_repository_policy(self.temporary)
        self.assertFalse(report["passed"])
        self.assertTrue(any("build_bundle.py" in error for error in report["errors"]))
        self.assertIn("CITATION.cff version differs from pyproject.toml", report["errors"])


if __name__ == "__main__":
    unittest.main()
