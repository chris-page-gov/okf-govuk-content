# ADR-008: bounded new-child demonstrator

- Status: accepted
- Date: 2026-07-15
- Decision owner: repository maintainer
- Scope: demonstrator only; this does not change the full-corpus release contract

## Context

The complete GOV.UK metadata hydration cannot produce a reviewed demonstration
within the available delivery window. The repository already preserves the
closed T0 census, resumable acquisition design and full-release acceptance
criteria. Those assets remain useful, but waiting for every GOV.UK record would
delay evidence about the more important product question: whether a federated
OKF bundle can help a person navigate a cross-department public-service
journey, retain provenance and provide safe machine-readable context.

The selected journey is **having a new child**. It crosses pregnancy and birth,
financial support and childcare, and therefore exposes relationships between
GOV.UK content published by several organisations. On 15 July 2026 one exact
combined Search API query over the three declared mainstream browse paths had a
deduplicated denominator of 69 content identities.

## Decision

We will publish a separately identified, metadata-only
`govuk-new-child-demonstrator.v1` projection with these invariants:

1. The seed set is the union of the three declared browse-path filters in one
   Search API query. Repeated values use the Search API's OR semantics.
2. The acquisition must observe exactly 69 identities after deterministic
   `content_id`, then canonical-link, deduplication or fail without replacing
   the checked-in bundle.
3. Every seed must have one valid Content API metadata record. Content API
   links to another seed may become internal relationships. All other typed
   content targets remain evidence-bearing boundary references and are not
   recursively hydrated.
4. The run retains source-native public metadata, typed relationships,
   attachment metadata, response hashes and retrieval evidence. It does not
   retain rendered pages, complete page bodies or attachment bytes.
5. The run is hard-bounded to 250 retained records and 500 official-source
   request attempts. It shares the programme request ledger and records its
   exact interval in the frozen cohort manifest.
6. The bundle and Explorer must label the result as derived,
   non-authoritative and complete only for the frozen 69-record seed
   denominator. GOV.UK remains authoritative, and time-sensitive or personal
   guidance must be checked against the live linked page.
7. AI access is portable rather than model-specific: a small context document
   supports file upload to any capable assistant; the deterministic discovery
   library and CLI support local automation; and a read-only, closed-world MCP
   server exposes search, fetch, traversal, citation and bounded context
   assembly without arbitrary URL fetching or writes.

## Consequences

- The demonstrator can be acquired, rebuilt, reviewed and shown within one day
  while still exercising the semantic source, JSON-LD projection, static
  search, adjacency, provenance and Explorer contracts.
- Exactly 69 seed records is a bounded denominator, not a claim that the
  demonstrator contains all information relevant to a new child or all GOV.UK
  content.
- Boundary references make out-of-cohort discovery visible without allowing a
  one-hop graph to grow silently into an uncontrolled crawl.
- The checked-in demonstrator remains a fixture/checkpoint. It cannot satisfy
  the release-v2 question, T1 closing, zero-omission full-corpus, participant
  research or immutable Release gates in the unattended execution contract.
- Resuming the full programme later requires a fresh source observation and the
  existing T1 closing process; this ADR neither discards nor promotes the
  incomplete hydration checkpoint.

## Alternatives rejected

- **Wait for complete hydration.** Rejected for the demonstration window; it
  provides scale evidence later but delays product learning now.
- **Choose 69 hand-picked pages.** Rejected because selection would be neither
  reproducible nor denominator-complete.
- **Recursively crawl all linked content.** Rejected because the scope and
  request cost would be unstable and source-policy review would be harder.
- **Copy full GOV.UK page text into an AI corpus.** Rejected because it conflicts
  with the metadata-led rights boundary, increases staleness and prompt-
  injection risk, and is unnecessary for discovery and citation.
- **Build a model-specific retrieval application.** Rejected because a portable
  context pack plus standard, read-only MCP and deterministic local interfaces
  makes the evidence usable by a wider range of AI systems.
