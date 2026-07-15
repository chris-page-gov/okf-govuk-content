# ADR-008: Use expanded Search metadata before selective Content API enrichment

Date: 2026-07-15

## Status

Accepted for the next opening census and development preview. Full-corpus and
release completeness remain unresolved until the new census, selective
enrichment, T1 closing and release reconciliation pass.

## Context

The closed `T0-20260712` census proved the public source union but its Search
projection requested too few fields. Universal path-by-path Content API
hydration was therefore doing expensive work before exploiting metadata that
the public Search API can return in bulk. At eight requests per second, that
made the opening queue a multi-day critical path and was unsuitable for a
useful pre-meeting demonstration.

Search v1 currently accepts `content_id`, organisations, taxons, world
locations, parts, historic state and content-purpose fields. For some
multi-part documents, `parts[].body` is a short search extract rather than a
complete page body. Those extracts can support local concept, topic and
relationship analysis, but publishing them would unnecessarily expand the
release rights and body-retention boundary.

The former 10 GiB retained-metadata ceiling also treated abundant external
storage as a prohibition. The user has instead authorised the mounted EXTSSD
drive as a cache and requires at least 10 GiB free to remain on every active
filesystem.

## Decision

The next census requests the expanded, live-verified Search field set. Public
source records retain only allowlisted metadata and part identity fields; no
`parts[].body` value enters the corpus, bundle, release package or checksum
tree.

Raw resumable response caches and a SQLite/FTS5 extract database live under
`okf-govuk-content/` on a mounted volume named `EXTSSD` or `ExtSSD-Data`. An
explicit absolute `OKF_GOVUK_EXTERNAL_CACHE_ROOT` may be used on another
authorised machine. The extract database stores normalised snippets, source
hashes, evidence, stable content and route identifiers, content-purpose fields
and Search-native organisation, taxon and world-location relationships. It is
local-only analytical state, not a release or clean-reproduction dependency.

Acquisition and hydration fail closed unless each active repository/cache
filesystem will retain at least 10 GiB free after a reserved write. The old
retained-size ceiling is removed. Cache loss may require reacquisition but
cannot invalidate already frozen body-free source records.

Content API work is selected deterministically and versioned. The immediate
tier covers sitemap-only or identity-poor routes, structural/navigation
documents, explicit redirect/tombstone dispositions, structured-link closure
and a stable one-percent audit sample. Attachment/resource families and
historic Search records are explicitly deferred until an authoritative bulk
source or a separately scheduled targeted pass is available; they remain
represented by bulk metadata with machine-visible deferred status. No record
is misreported as Content API enriched. The old universal-hydration checkpoint
is preserved, and a new census label/cache root is required for the new policy.

Authenticated GovSearch, GovGraph or Publishing API bulk access remains
unauthorised and is not a hidden dependency. A human bulk-source request is
planned for the GOV.UK team meeting; any later source admission requires a new
decision, frozen evidence and reconciliation.

## Requirements and evidence mapping

- Source boundary, body exclusion and identity preservation: REQ-002,
  REQ-008 through REQ-023.
- Official sources, source constraints, rights and reconciliation: REQ-024
  through REQ-038.
- Static publication, discovery and capacity: REQ-043 through REQ-048.
- Determinism, provenance, budgets and honest status: REQ-086 through REQ-095.

Evidence is provided by `src/govuk_okf/storage.py`,
`src/govuk_okf/search_extracts.py`, `src/govuk_okf/hydration_policy.py`,
`scripts/check_storage.py`, `scripts/probe_search_extracts.py`,
`scripts/query_extracts.py`, `scripts/plan_hydration.py`, the acquisition and
hydration implementations, and `tests/test_bulk_first.py`.

## Consequences

- A useful bulk-metadata OKF preview can be built without waiting for a
  universal Content API walk.
- Every field-level enrichment gap remains machine-visible and can be reduced
  when an authoritative bulk source is authorised.
- The deferred attachment/resource and historic populations are release gaps,
  not omissions from the admitted source union; final acceptance still needs
  their declared field-level contract to be resolved or explicitly accepted.
- The external extract index can be queried locally for concepts and
  relationships, but it must never be copied into a release asset or treated
  as authoritative text.
- Complete release claims remain fail-closed until the newly frozen source
  union is closed with `unexplained_omissions = 0` and every release gate passes.
