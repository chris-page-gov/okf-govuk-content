# Publication compiler scale evidence

Date: 2026-07-12
Compiler contract: `sqlite-bounded-v1`

The fixture build remains the small in-memory reference implementation. Gzip
inputs, source-shard directories and plain JSONL inputs of at least 32 MiB use
the SQLite compiler automatically. `--compiler memory|disk` permits an explicit
choice. Both compilers share the final descriptor/manifest emitter, and the
complete fixture output is byte-identical across them, including deterministic
gzip bytes.

## Measured deterministic spike

One local ARM64 macOS run used Python 3.14.5 and SQLite 3.53.0. The synthetic
input contained 5,000 source records, one shared publisher, 50 attachments,
10,049 relationships, 89,990 token-document postings and a high-document-
frequency term that exercised the 2,000-posting cap. The semantic projection
contained 40,204 JSON-LD node occurrences.

| Measure | Observed |
|---|---:|
| Compiler wall time | 10.13 seconds |
| Validator wall time | 5.40 seconds |
| Peak traced compiler Python heap | 10,668,406 bytes (10.2 MiB) |
| Peak traced validator Python heap | 15,299,998 bytes (14.6 MiB) |
| Peak process RSS reported by `ru_maxrss` | 121,126,912 bytes (115.5 MiB) |
| SQLite compiler spill database | 57,610,240 bytes (54.9 MiB) |
| Published output | 15,968,653 bytes (15.2 MiB) |

This is a compiler capacity sample, not a full-corpus forecast. Record shape,
relationship density and vocabulary cardinality materially affect storage and
runtime. The CI scale test independently builds and validates 2,048 linked
records and requires both traced Python heaps to remain below 64 MiB.

## Bounded-memory design

- Source JSONL is read one size-limited line at a time.
- Normalised records, entity conflicts, relationship assertions, identifiers,
  adjacency membership and token-document postings spill to SQLite with a
  24 MiB page-cache ceiling and file-backed temporary storage.
- Flat entity and result shards materialise at most 1,000 records; flat
  relationship shards materialise at most 1,000 assertions. Semantic entity
  shards use 500 source rows and semantic assertion shards use 1,000
  assertions.
- Search lexicons, prefix maps, document maps, route buckets and adjacency
  buckets stream in deterministic key order. A single posting list is capped
  at 2,000 rows in published output while its uncapped document frequency
  remains recorded.
- Both compilers emit byte-identical per-shard schema/snapshot/count/key-bound,
  size and SHA-256 metadata. Gzip uncompressed sizes and file hashes are counted
  in bounded streams. Search shard metadata is a lazy side manifest, preserving
  the existing Explorer path arrays without spending the bootstrap budget on
  thousands of integrity rows.
- Release checksums hash each file in 1 MiB blocks; no publication shard is
  loaded wholesale merely to calculate SHA-256.
- The candidate tree is atomically installed only after required descriptors
  exist; a failed rebuild preserves the prior publication.

## Limits and release implications

- The spill database scales with the uncapped token-document pairs and may be
  several times larger than the compressed publication. Capacity planning must
  budget temporary local disk as well as final hosting storage.
- A single source envelope may be at most 64 MiB. Release 1 is metadata-led, so
  an envelope that large is treated as malformed rather than an invitation to
  retain a body.
- The compiler preserves the two-character Explorer search-shard contract.
  Highly skewed vocabulary can still make an individual published shard exceed
  the 5 MiB release budget; compilation and publication validation both fail
  that snapshot, and a versioned contract change is required rather than
  silently changing routes.
- One exceptionally high-degree route can make its adjacency bucket large.
  Emission remains bounded-memory, but the same release-size gate applies.
- A directory containing multiple snapshot-level `*-source-records` files is
  rejected as ambiguous. Multi-file inputs must be an explicit `records-*` or
  `part-*` shard set so T0 and T1 cannot be accidentally combined.
- Release packaging keeps the logical shard contract but maps it to
  deterministic, gzip-framed Pages range packs no larger than 64 MiB. The
  linked 300-record fixture measured 1,666,697 indexed source bytes and 855,210
  packed bytes (51.31%). That small-fixture ratio and this 5,000-record compiler
  spike do not establish full-corpus fit. Only a measured closing-snapshot site
  below the fail-closed 950,000,000-byte budget may be deployed.
