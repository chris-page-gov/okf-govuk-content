# ADR-006: Partition skewed static-search postings without changing logical lookup

Date: 2026-07-14

## Status

Accepted implementation. The deterministic skew regression passes; the exact
836,998-record T0 capacity build must be rerun before full-corpus capacity can
be accepted.

## Context

The first complete T0 capacity attempt used the bounded SQLite compiler over
`corpus/inventory/T0-20260712/source-records-ef800a508c601cdb/index.json`.
That content-addressed source index declares 836,998 records and canonical
source hash
`ef800a508c601cdb2fbeef68267a55145deb6de14e19c0bed0f663e1fba245b2`.
After 44,342.28 seconds, compilation correctly stopped
because `data/search/postings/ca.json` was 6,712,946 bytes, exceeding the frozen
5,242,880-byte ordinary-shard budget. No target publication was installed.

The failure is a physical skew problem, not evidence that the `ca` logical
lexicon, its records, or its postings may be omitted. Relaxing the budget,
reducing the 2,000-row per-token cap, discarding tokens, narrowing the corpus or
silently changing the Explorer's two-character lookup would breach the
accepted publication and compatibility contracts.

The same capacity audit showed that a singleton document map would be the next
predictable scaling risk: 836,998 ordinal-to-route entries do not belong in one
ordinary shard.

## Decision

`okf-static-search.v1` and its two-character logical lexicon remain unchanged.
The publication declares two additive, exact contracts:

- `okf-search-postings-partitioning.v1` greedily groups complete tokens in
  lexical order using the exact UTF-8 pretty-JSON bytes that will be written.
  It never splits or truncates one token's posting list. A token that cannot fit
  alone fails closed. A logical shard with one physical partition keeps the
  legacy path such as `postings/co.json`; an overflow uses contiguous paths
  such as `postings/ca-00000.json` and `postings/ca-00001.json`.
- `okf-search-doc-map-partitioning.v1` writes contiguous ordinal blocks of at
  most 1,000 records to `doc-map-00000.json`, `doc-map-00001.json`, and so on.

Each lexicon entry already carries its exact `postings` pointer, so both the
Explorer worker and the Python discovery client continue to dereference a
token-specific physical file. The search manifest inventories every physical
path and declares the versioned algorithm. Per-shard metadata binds logical
shard, partition number/count, token or ordinal bounds, record/posting counts,
bytes and SHA-256. Publication validation requires canonical contiguous paths,
exact lexicon/postings token-set equality, exact document-map ordinal coverage,
the frozen 5 MiB limit and contract metadata. Release range-pack collection
continues to derive from that complete shard-metadata inventory.

Manifests without either additive declaration remain readable under the
legacy contract: one postings file per logical lexicon shard and one scalar
document-map path. A declaration that is unknown, modified or inconsistent
with its entrypoint shape fails closed in the validator, Explorer and Python
discovery client.

## Alternatives rejected

- Raising the 5 MiB budget would invalidate REQ-048 and browser/resource
  assumptions without solving future skew.
- Increasing logical shard width would change lookup routing for every
  consumer and make a data-distribution accident part of the public semantics.
- Hash partitioning would bound files but require probing or a second routing
  structure; contiguous token ranges preserve deterministic inspection and the
  existing per-token pointer.
- Further capping, dropping or sampling postings would change search semantics
  and corpus coverage.
- Publishing one very large document map would merely move the known capacity
  failure to the next build phase.

## Consequences

- Memory and SQLite compilers remain byte-identical for the same input.
- Existing single-partition bundles retain their historical postings paths;
  legacy manifests remain supported.
- Physical shard count may increase, but every reference is explicit,
  integrity-bound and included in release packs.
- Synthetic 64-token/2,048-document skew now proves overflow, byte bounds,
  deterministic routing, validation and discovery without a long network or
  model run.
- Lexicon, prefix, result, metadata and adjacency shards remain independently
  subject to the same fail-closed 5 MiB gate. The full T0 rerun is still
  required to demonstrate that no other distribution exceeds it and to record
  final shard/pack counts and Pages size.
