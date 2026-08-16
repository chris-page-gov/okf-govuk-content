# ADR-011 — Publish the bounded demonstrator as a separate preview tier

Date: 16 August 2026
Status: accepted

## Context

The controlling programme keeps a representative fixture separate from Release
1. Release 1 still requires the complete snapshot-bounded GOV.UK metadata
corpus and all publication gates. The checked-in 69-record new-child fixture is
nevertheless useful for public review of the Explorer, semantic model,
provenance, search and AI handoff before that release is ready.

The repository already had a Pages URL and a full-release deployment workflow,
but no release tag or Pages deployment. Reusing the release-candidate channel
would incorrectly imply that the fixture had passed the full-corpus gate.

On 16 August 2026 the repository owner explicitly confirmed that the intention
was to make the 69-record demonstrator publicly reviewable before the full
release and instructed that this be implemented. That instruction authorises
publication of the bounded demonstrator, not promotion of Release 1 or a claim
that Explorer is a preferred human interface.

## Decision

Add a separate `bounded-demonstrator-preview` publication tier with these
controls:

- publication is manual and can run only from protected `main`;
- the workflow deploys the exact checked-in `bundle/` bytes without rebuilding;
- deterministic packaging requires snapshot `NEW-CHILD-20260715`, the fixture
  and sampled labels, checkpoint release status, `publication_ready: false`,
  exactly 69 expected and represented seeds, and zero unexplained seed
  omissions;
- the transported artefact is verified again before deployment;
- real-Chromium checks exercise the exact preview shell and data before and
  after artefact transport;
- live smoke checks compare critical public bytes with the transported
  checksums and recheck the bounded-demonstrator labels;
- preview and release deployments share one Pages concurrency group;
- the preview does not create a tag, GitHub Release, release-candidate marker or
  OKF Explorer registry entry.

The existing tag-triggered candidate/final workflow and publication-ready gates
remain unchanged. A later verified release may replace the preview at the same
stable Pages origin.

## Consequences

The demonstrator becomes reviewable at
`https://chris-page-gov.github.io/okf-govuk-content/` without overstating its
coverage or acceptance state. Reviewers can also load its public descriptor in
the generic OKF Explorer by URL.

The Pages origin is a channel whose current tier must be read from the content
labels and deployment evidence. It is not, by itself, evidence of a release.
The canonical Explorer registry remains reserved for the independently verified
release publication terminal.
