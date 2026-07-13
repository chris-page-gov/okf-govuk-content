const EXPECTED_SCHEMA = "govuk-okf-github-release-pack-index.v1";
const EXPECTED_REPOSITORY = "chris-page-gov/okf-govuk-content";
const RELEASE_TAG = /^v\d+\.\d+\.\d+(?:-rc\.\d+)?$/;

export function releaseEvidenceUrl(index) {
  if (
    !index ||
    index.schema !== EXPECTED_SCHEMA ||
    index.repository !== EXPECTED_REPOSITORY ||
    typeof index.tag !== "string" ||
    !RELEASE_TAG.test(index.tag)
  ) {
    return null;
  }
  return `https://github.com/${EXPECTED_REPOSITORY}/releases/download/${encodeURIComponent(index.tag)}/evidence-browser-workflow.json`;
}

async function installReleaseEvidenceLink() {
  const row = document.getElementById("current-release-evidence");
  const link = document.getElementById("current-release-evidence-link");
  if (!row || !link) return;
  try {
    const response = await fetch("release-data-plane.json", {
      cache: "no-store",
      credentials: "same-origin"
    });
    if (!response.ok) return;
    const url = releaseEvidenceUrl(await response.json());
    if (!url) return;
    link.href = url;
    row.hidden = false;
  } catch {
    // A development fixture has no release data plane; its historical evidence
    // remains available without turning absence into a release claim.
  }
}

if (typeof document !== "undefined") installReleaseEvidenceLink();
