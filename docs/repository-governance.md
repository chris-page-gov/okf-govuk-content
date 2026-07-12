# Repository and publication governance

The checked-in policy is the reproducible source for the live GitHub settings.
Run the local contract without network access:

```sh
.venv/bin/python scripts/check_repository_policy.py
```

`.github/branch-protection.json` requires pull requests, the strict `validate`
status check, resolved review conversations, linear history and administrator
enforcement. Force pushes and branch deletion are disabled. The repository has
one owner, so the rule deliberately requires zero approvals and does not
require a CODEOWNER approval: requiring the pull-request author to approve
their own change is impossible and would block unattended maintenance. The PR,
CI and conversation gates still apply to the owner.

After applying the policy, capture and compare the live API response:

```sh
gh api repos/chris-page-gov/okf-govuk-content/branches/main/protection \
  > /tmp/okf-govuk-content-main-protection.json
.venv/bin/python scripts/check_repository_policy.py \
  --api-capture /tmp/okf-govuk-content-main-protection.json
```

`CITATION.cff` takes its version from `pyproject.toml`, identifies this
repository and the Pages publication, uses the MIT software licence, and does
not claim a DOI.

## Two publication channels

The release workflow accepts only two annotated-tag classes on protected-main
ancestry:

- `vMAJOR.MINOR.PATCH-rc.N` is a release candidate. It must pass
  `check_provenance.py --require-candidate` and
  `check_release.py --publication-ready`. Candidate provenance requires ten of
  the eleven terminal activities; only the external publication/Pages/Explorer
  terminal may remain pending. GitHub publishes this channel as a prerelease
  and deploys its exact verified site bytes to Pages.
- `vMAJOR.MINOR.PATCH` is final. It must pass
  `check_provenance.py --require-release` and
  `check_release.py --finalized`, including the appended publication terminal
  and finalized two-stage promotion record. A candidate result cannot satisfy
  this tag.

Tags must be annotated. A local GPG or SSH signing key is not mandatory; when a
tag contains a signature, the ref validator verifies it and fails if the
signature is invalid. This keeps unattended publication possible without
silently accepting a bad supplied signature.

## Immutable bytes and least privilege

The tag workflow runs the locked tests and snapshot gates, measures the full
browser contract over the checked-out release bundle, and packages without
calling the bundle builder. The package contains the exact Pages tree, a
reproducible `tar.gz`, the release evidence, CycloneDX SBOM, bundle checksums
and nested SHA-256 manifests. Its artifact name includes both tag and commit.

GitHub's short-lived OIDC identity creates signed SLSA provenance attestations
for the verified manifest and every release asset. The publishing job downloads
the artifact, rechecks every digest and archive member, verifies each
attestation against this repository, workflow, ref and commit, and refuses to
replace an existing release. Candidate releases receive `--prerelease`; final
releases do not.

The reusable Pages workflow receives only that verified artifact. It never
rebuilds the bundle. It reruns package validation and the full browser smoke,
deploys the exact `site/` directory, then compares live critical-file bytes and
the snapshot identifier with the packaged checksums. Release, Pages and
attestation permissions are scoped to only the jobs that need them, and both
workflows use non-cancelling concurrency groups.
