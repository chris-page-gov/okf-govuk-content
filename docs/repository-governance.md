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

Publication settings have a separate versioned read-back. The required state
is immutable releases enabled plus a public, HTTPS-enforced Pages site built by
Actions at `https://chris-page-gov.github.io/okf-govuk-content/`:

```sh
gh api -H "X-GitHub-Api-Version: 2026-03-10" \
  repos/chris-page-gov/okf-govuk-content/immutable-releases \
  > /tmp/immutable-releases.json
gh api -H "X-GitHub-Api-Version: 2026-03-10" \
  repos/chris-page-gov/okf-govuk-content/pages \
  > /tmp/pages.json
jq -n --slurpfile immutable /tmp/immutable-releases.json \
  --slurpfile pages /tmp/pages.json \
  '{immutable_releases: $immutable[0], pages: $pages[0]}' \
  > /tmp/publication-settings.json
.venv/bin/python scripts/check_repository_policy.py \
  --publication-api-capture /tmp/publication-settings.json
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

For the initial repository publication these classes resolve to
`v0.1.0-rc.1` and `v0.1.0`. The `v1.0.0` series is not used for the first
release.

Tags must be annotated. A local GPG or SSH signing key is not mandatory; when a
tag contains a signature, the ref validator verifies it and fails if the
signature is invalid. This keeps unattended publication possible without
silently accepting a bad supplied signature.

## Immutable bytes and least privilege

The tag workflow runs the locked tests and snapshot gates, measures the full
browser contract over both the checked-out release bundle and the packaged
Pages site, and packages without calling the bundle builder. The package
contains a sub-950,000,000-byte Pages site with same-origin `.pack.gz` byte
ranges, a reproducible `tar.gz`, release evidence, CycloneDX SBOM, bundle
checksums and nested SHA-256 manifests. Its transport artifact name includes
both tag and commit.

The ordinary pull-request workflow uses the same snapshot-aware gate. It
rebuilds only the exact development fixture; a promoted full-corpus candidate
or final is validated from its clean-room and hash-bound archived-input
evidence. A checkpoint or mixed state cannot select either path. Its packed
single-pack browser regression generates a dedicated tiny fixture, while the
tag workflow alone measures the checked full release bytes.

GitHub's short-lived OIDC identity creates signed SLSA provenance attestations
for the verified manifest, every Release asset and the exact-asset expectation.
That expectation binds the annotated tag, its independently validated
`release-candidate` or `final` channel, the required GitHub `prerelease` state,
and the sorted asset names, sizes and digests under one root hash. Pack
verification retains and reads only bounded regular files whose basename and
resolved location are inside the release asset directory; unsafe, symlinked or
oversized candidates are rejected before hashing or range reads.
The first job creates an editable draft, uploads all assets, and compares the
versioned API's asset names, sizes and `sha256:` digests with local verified
bytes. It does not publish. A partial draft may be deterministically recreated;
an existing published immutable release must exactly match instead.

The reusable Pages workflow receives only the verified site artifact. It never
rebuilds the bundle. It checks the site-specific checksums and pack index,
reruns the full browser smoke, deploys the exact `site/` directory, then
compares live critical bytes, snapshot and one byte-stable range from every
pack. Only after that job succeeds does the final job reverify the attested
draft expectation, publish it, require `draft:false` and `immutable:true`, and
run GitHub's immutable-release verification. A Pages failure therefore leaves
a mutable/deletable draft. Release, Pages and attestation permissions remain
scoped to only the jobs that need them.

The Pages and Release services do not provide one atomic commit. If Pages is
already live and the final publish job fails, the externally visible partial
state is recorded as `Pages live / Release draft`; the publication provenance
terminal and finalized status remain pending. The final job is replay-safe: it
downloads the attested exact-asset expectation, re-fetches the versioned draft
response and publishes only an exact match. If the previous attempt actually
published before losing its response, the rerun requires the same exact assets
and `immutable:true` rather than creating or replacing a release.
