# Cost, capacity and execution-approval record

Initial preflight date: 2026-07-11

Implementation usage checkpoint: 2026-07-13T07:59:00Z

Official-source request checkpoint: 2026-07-12T08:20:20Z

## Observed volume

- Search API v1: 715,467 source rows, 715,463 canonical routes and 137
  document types. Three within-partition and one cross-partition source-row
  aliases share canonical routes; opposing source-identity passes agree.
- Sitemap: 869,875 raw entries, 663,331 unique URLs across 35 byte-stable
  double-read shards.
- Organisations API: 1,256 records.
- Content schemas: 83 source schema families at the pinned commit.
- T0 union: 848,977 candidate keys and 836,998 publication records, comprising
  836,543 GOV.UK routes and 455 external-boundary records. All candidates are
  accounted for: 848,971 represented and six redirect-only, with zero
  exceptions or unexplained omissions.

At the configured 8 Content API requests/s, the 836,543 initial GOV.UK routes
have a theoretical minimum hydration time of 104,568 seconds (29 h 2 m 48 s),
before linked-only discoveries, retries or T1. The 75,000-page rendered-link
detector has a separate theoretical minimum of 37,500 seconds (10 h 25 m) at
2 requests/s and runs through the same checkpointed work queue. Full hydration
is therefore resumable rather than one CI job.

## Model authority and recorded use

No external paid model budget was authorised and no paid model API was called.
Deterministic HTTP, parsing, hashing and validation perform the corpus work.
At this implementation checkpoint the activity ledger contains 26 activities:
four preserved historical rows and 22 SHA-256-chained v2 rows. Six activities
are classified as deterministic and 20 as model-assisted or mixed. Twenty
activities have unavailable product-session tokens and marginal cost. The
external paid-model totals are exact: 0 API calls, 0 input tokens, 0 output
tokens and GBP 0.

The current Codex product session and same-provider subagents were used for
bounded implementation, source research, evaluation implementation, citation
collection, review and security work. Process separation is recorded, but a
same-provider/model-family subagent is not described as an independent-provider
judge. The product does not expose exact backend version, inference parameters,
tokens or marginal cost to this run; those fields remain unavailable rather
than being estimated as zero.

The user reported that the Codex product usage limit reset on 12 July 2026.
That operational reset authorises continued product-session execution only
while the product permits it. It does not expose a numeric token ceiling, set a
price, authorise paid external APIs or imply unlimited spend.

One delegated allocation reported `Selected model is at capacity`. The work was
reassigned to already-running same-product agents and the root session; no
external model, paid API or weaker validation fallback was used. Exact product-
session tokens and marginal cost remain unavailable rather than being reported
as zero.

## Official-source request authority

Official-source requests have a separate 1,000,000-attempt execution ceiling.
The completed T0 census consumed 5,752 cumulative attempts by
2026-07-12T08:20:20Z, including 127 preflight attempts, leaving 994,248. All
5,421 acquisition observations returned HTTP 200. The T0 rendered-link detector
is frozen at 75,000 pages by ADR-004, leaving a deterministic lower-bound
reserve of 76,952 attempts after the initial Content API routes, one robots
check and a T1 census projected at the observed T0 census cost. That reserve is
for retries, linked discoveries and closing; it is not a promise that all will
be consumed. T0 hydration, T1 and closing activities must append their exact
counter intervals; citation verification publishes its own aggregate from
per-source evidence. Source requests are never included in model token or cost
totals.

## Source access and fallback record

| Original | Result | Allowed fallback | Final disposition |
|---|---|---|---|
| ACM RRF DOI landing page | Automated HTTP 403 | Author-hosted University of Waterloo PDF | Successful evidence source; DOI retained as metadata |
| CMU Information Foraging PDF | `DH_KEY_TOO_SMALL` under strict TLS | ResearchGate record and exact author PDF, then Crossref | ResearchGate returned HTTP 403 after three bounded attempts; Crossref HTTP 200 supports bibliographic identity only |
| National Archives OGL and exceptions HTML | Self-signed certificate chain under Python strict TLS; no HTTP redirect chain | National Archives CDN guidance PDF, then GOV.UK Knowledge Asset guide | CDN had the same certificate failure; GOV.UK guide HTTP 200 supports the released OGL claims |
| OpenAI BrowseComp article | HTTP 403 | Official `cdn.openai.com` paper | HTTP 200 under strict TLS; paper supplies release evidence |

All failed originals, timestamps and evidence/source IDs are retained in
`research/citation-policy.json` and
`provenance/reproduction-declarations.json`. No TLS downgrade, certificate
override, anti-bot workaround or access-control bypass was attempted.

## Storage/browser decision

The publication remains entirely on GitHub under the explicit repository,
Pages and Release authority. GitHub documents a 1 GB Pages site limit and
Release limits of 1,000 assets with each asset smaller than 2 GiB. Direct
browser use of a live Release asset was rejected after its final redirect
returned no CORS permission.

The implemented browser transport instead concatenates gzip-framed logical
members into same-origin Pages `.pack.gz` files of at most 64 MiB. The index
binds transport and original hashes, lengths, compression and ranges. A live
2026-07-13 Pages probe returned HTTP 206, exact 32-byte `Content-Range` and
`Content-Length`, `Accept-Ranges: bytes` and CORS permission. A browser-style
probe also showed why the gzip representation is mandatory: ordinary JSON was
served with `Content-Encoding: gzip` and different range coordinates, whereas
an existing `.json.gz` shard retained identical bytes and no content encoding.

On the linked 300-record fixture, 653 indexed shard files occupied 1,666,697
source bytes and 855,210 packed transport bytes (51.31%). This is not a
full-corpus forecast. The 5,000-record compiler spike is also explicitly not a
forecast because record shape, relationship density and vocabulary cardinality
vary. Final capacity remains unresolved until the hydrated closing snapshot is
packaged. Packaging fails closed at 950,000,000 total Pages bytes; it does not
fall back to Release CORS, external storage or a narrower corpus.

Primary constraints are [GitHub Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits),
[GitHub Release limits](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
and [immutable Release draft-first behaviour](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases).
