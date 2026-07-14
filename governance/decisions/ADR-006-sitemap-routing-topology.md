# ADR-006: Publish a sitemap and routing topology without expanding the release boundary

Date: 2026-07-14

## Status

Accepted implementation for fixture and snapshot projections. Full-corpus and
release completeness remain unresolved until hydration, T1 closing and release
reconciliation pass.

## Context

The GOV.UK developer manual describes `https://www.gov.uk/sitemap.xml` as a
daily Search API-derived index split into roughly 30 sitemaps and excluding
recommended links. GOV.UK's reuse guidance separately describes that sitemap
as listing the majority of pages. It is a valuable official enumerator, but it
is not by itself a complete model of canonical content identities, redirects,
independently operated service hosts, structured navigation or boundary links.

The release contract is also narrower than a DNS or web crawl of every host
ending in `.gov.uk`: release 1 covers the bounded public metadata estate of
`www.gov.uk`, with associated representations and evidenced boundary links.
Independently operated `*.gov.uk` sites are boundary destinations, not sites to
mirror. Expanding that boundary would require a requirements and source-policy
change and is not authorised by this work.

The closed T0 source union already provides a useful hackathon-scale opening
census. The larger Content API hydration is resumable but cannot be completed
in time for the demonstration. Waiting for it would delay a topology surface
without making a release completeness claim valid.

## Decision

The compiler deterministically emits `data/site-topology.json` using the
`govuk-site-topology.v1` contract. It is a compact control-plane projection
over the same records and evidence-bearing relationships as the full bundle.
It contains:

- every observed host in the compiled snapshot, classified as the main
  publishing estate, a GOV.UK-domain boundary, or another external boundary;
- record counts and routing kinds for each host;
- canonical URL, stable content-identifier, redirect-rule,
  external-boundary and typed-relationship mechanism counts;
- complete source-native redirect fields on each record, `redirects to`
  adjacency, and a bounded deterministic redirect sample in the topology;
- precise links to the full record, route, search, adjacency and semantic data
  planes; and
- an explicit status that this is a snapshot projection, not a release
  completeness claim.

The Explorer lazily loads that control plane only when the user opens the
`Sitemap & routing` view. Host rows lead into static search; redirect sources
open the canonical record and show every admitted source-native redirect rule.
Startup therefore remains overview-first and within the frozen bootstrap
budget.

The fixture is used for immediate implementation and browser verification. A
T0 compilation may be used as a clearly labelled opening-census preview after
the active hydrator no longer competes for local CPU and disk. Only the
hydrated, T1-closed source union may become the release projection.

## Requirements and evidence mapping

- Boundary, non-authoritative status and body exclusion: REQ-002, REQ-008,
  REQ-009 through REQ-012.
- Canonical URLs, lifecycle, redirects, resources, source evidence and search:
  REQ-014, REQ-018 through REQ-023.
- Official-source audit, reconciliation, canonicalisation and explicit
  constraints: REQ-024 through REQ-036.
- Descriptor, manifest, static search, adjacency, URL loading, validation and
  budgets: REQ-043 through REQ-048.
- Deterministic counts, fail-closed scope, recorded decisions and status:
  REQ-088 and REQ-093 through REQ-095.

Evidence is provided by `src/govuk_okf/publication.py`,
`src/govuk_okf/publication_validation.py`, `explorer/src/app.js`,
`tests/test_publication.py`, `explorer/tests/core.test.mjs` and
`explorer/tests/data.test.mjs`.

## Consequences

- The hackathon can demonstrate a working routing information architecture
  without interrupting hydration or disguising the fixture as the corpus.
- The full record plane remains the authoritative bundle detail; the topology
  is an index, not a lossy replacement.
- A host appearing in the topology proves only that it was observed in the
  admitted snapshot and classification. It does not assert a complete crawl of
  that host.
- Release claims remain fail-closed until the frozen source union is hydrated,
  T1 is closed and `unexplained_omissions = 0` is verified for the release
  snapshot.
