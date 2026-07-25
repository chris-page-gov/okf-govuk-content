# ADR-010 — Adopt OKF v0.2 as the core without discarding GOV.UK extensions

- Status: accepted
- Date: 2026-07-25
- Decision owner: repository maintainer
- Supersedes: the OKF v0.1 format baseline in the July 2026 planning documents

## Context

The controlling plan was written against OKF v0.1. Upstream OKF v0.2 now makes
structured provenance, producer and verifier identity, lifecycle, freshness and
attested-computation contracts part of the core Markdown format. The user's
later instruction requires v0.2 conformance while preserving the GOV.UK
semantic, provenance, federation, static-search and Explorer work that is
already further developed than the minimal base format.

The normative migration target is
`GoogleCloudPlatform/knowledge-catalog@3fcbb9f828c2f23d109c855ee403c3a4c81f3a96`,
`okf/SPEC.md`.

## Decision

1. `bundle/index.md` declares only `okf_version: "0.2"` in frontmatter.
2. Every non-reserved Markdown file in the publication is a v0.2 concept with
   parseable frontmatter and a non-empty `type`. Nested `index.md` and every
   `log.md` remain reserved and have no frontmatter.
3. The generated `concepts/` tree is the canonical OKF layer. YAML-LD, JSON-LD,
   the Explorer descriptor, semantic assertion shards, search shards, route
   indexes, facets, checksums and snapshot controls remain deterministic profile
   projections.
4. Generated concepts use `generated.by: govuk-okf/0.1.0` and the explicit
   publication build time. A GOV.UK content update is recorded separately as
   `sources[].last_modified`; retrieval time remains a GOV.UK profile field.
5. No `verified` event is created. All current projection concepts are
   `status: draft`, so their derived v0.2 trust tier is unverified. No
   `stale_after` is created without an approved review policy.
6. The Explorer descriptor exposes a governed-snapshot/live distinction:
   snapshot ID, compilation time, latest source observation, authoritative live
   GOV.UK destination and the expectation that drift can occur.
7. Attested Computation is supported by validation when present, but this
   discovery bundle contains no computation contract and nothing is executed
   automatically on load.
8. Unknown types and fields remain permitted and preserved at the bundle
   boundary. The GOV.UK profile extensions remain explicitly versioned.
9. The project package and first release train remain version `0.1.0`. That is
   the software/release version, not the OKF format version.

## Consequences

- A conforming v0.2 consumer can read the Markdown tree without understanding
  the GOV.UK or Explorer extensions.
- Existing Explorer consumers keep their current entrypoints and data-plane
  schemas, with additive `okf_version`, canonical-concept and snapshot-state
  metadata.
- Snapshot generation, source modification and retrieval times cannot be
  silently conflated.
- Trust remains conservative until an evidenced verification workflow records a
  real event.
- The July planning documents remain immutable historical controls; this ADR
  records why their v0.1 format statement is superseded.
