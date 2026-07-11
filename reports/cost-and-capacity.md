# Cost and capacity preflight

Date: 2026-07-11

## Observed volume

- Search API v1: 715,465 results and 137 document types.
- Sitemap: 869,875 raw entries, 683,070 unique URLs across 35 shards.
- Organisations API: 1,256 records.
- Content schemas: 83 source schema families at the pinned commit.

At the configured 8 Content API requests/s, 683,070 unique sitemap paths alone
have a theoretical minimum hydration time of 85,384 seconds (23 h 43 m 4 s),
before Search-only routes, relationships, retries or T1. Full hydration is
therefore checkpointed and resumable rather than one CI job.

## Cost authority and recorded use

No external paid model budget was authorised and no paid model API was called.
Deterministic HTTP, parsing, hashing and validation perform the corpus work.
The current Codex product session and three same-provider subagents were used
for bounded implementation, convention inspection, official-source research
and independent challenge. The product does not expose exact backend version,
tokens or marginal cost to this run; those fields are recorded as unavailable,
not estimated as zero.

## Storage/browser decision

The initial control plane remains in the public repository and Pages. Record,
search and adjacency output uses immutable deterministic gzip shards and no
body mirror. External storage is not authorised; if a measured release exceeds
repository/Pages capacity, the affected publication is checkpointed rather
than silently reducing corpus scope.

