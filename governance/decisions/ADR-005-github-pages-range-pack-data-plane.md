# ADR-005: Keep browser delivery on byte-stable GitHub Pages range packs

Date: 2026-07-13

## Status

Accepted implementation, with final full-corpus capacity unresolved until the
hydrated closing snapshot is packaged.

## Context

GitHub Pages limits a published site to 1 GB. GitHub Releases permits up to
1,000 assets per release, each strictly smaller than 2 GiB, but a live probe of
a public Release asset followed its redirect to a response without
`Access-Control-Allow-Origin`. Browser JavaScript on Pages therefore cannot use
Release assets as its fetch data plane. A third-party CORS relay or storage
service is outside the launch authority.

Pages itself supports same-origin byte ranges. On 2026-07-13 a live request to
the existing Explorer Pages site returned HTTP 206 with exact `Content-Range`,
`Content-Length`, `Accept-Ranges: bytes` and `Access-Control-Allow-Origin: *`.
A browser-style request to a `.json.gz` shard returned the same byte range with
no `Content-Encoding`; ordinary JSON could instead be transparently encoded,
changing range coordinates. The published representation must therefore be an
already-compressed gzip resource.

Primary platform constraints:

- [GitHub Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)
- [GitHub Release asset limits](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
- [Immutable Releases and draft-first publication](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)

## Decision

Release packaging preserves every virtual shard path and original SHA-256. An
identity JSON shard becomes one deterministic gzip transport member; an
original gzip shard remains byte-identical. Members are concatenated into
deterministic `.pack.gz` files no larger than 64 MiB. The Pages index binds each
virtual path to its member range, transport hash/compression, original byte
length/hash/compression, pack hash and frozen snapshot.

Real-browser release evidence additionally records the byte hashes of the
exact range index and complete Pages checksum manifest. Evidence attachment
requires both bindings, so a report cannot be replayed onto another packed site
that happens to use the same snapshot identifier.

The Explorer requests the pack only from the descriptor's same Pages origin,
requires HTTP 206, exact `Content-Range`, no `Content-Encoding`, exact member
length and both hashes, then reverses transport compression before applying the
source shard's original compression. The static search worker uses the same
contract. Release URLs are recorded only as an immutable offline mirror and
are never fetched by browser JavaScript.

Packaging fails if the complete Pages site reaches 950,000,000 bytes. This
conservative ceiling leaves headroom below the official 1 GB site limit. It is
not evidence that the full hydrated corpus fits: only the final closing
snapshot can resolve that gate, and failure blocks publication rather than
narrowing the corpus.

Immutable Releases are created as drafts. The workflow uploads and verifies
the exact asset names, sizes and API-reported SHA-256 digests while mutable,
deploys and live-smokes the exact Pages artifact, re-verifies the same draft,
and only then publishes it. The versioned API read-back must report
`draft:false` and `immutable:true`. A failed Pages job leaves a deletable draft.
This is a staged, recoverable cross-system sequence rather than an atomic
transaction. If Pages succeeds but final Release publication fails, Pages is
live while the Release remains a verified draft; release status and the
publication provenance terminal remain pending. Rerunning the final job
re-fetches and re-verifies the attested expectation before publishing. If
publication succeeded but its final read-back failed, a rerun accepts only the
already immutable release with the exact same asset set and digests.

## Consequences

- Browser delivery remains public, repository-native and same-origin.
- Logical routes, search shards and adjacency paths do not change.
- Pages and Release bytes share one deterministic pack representation, but the
  Release mirror is not a browser fallback.
- Every release pays a bounded transport-compression and pack-verification cost.
- Full-corpus publication remains honestly blocked if the measured Pages site
  is 950,000,000 bytes or larger.
