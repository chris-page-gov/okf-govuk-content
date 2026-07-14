# Sitemap and routing topology

The bundle's `Sitemap & routing` view is a snapshot-bounded map of GOV.UK URLs,
observed hosts, redirects and other routing mechanisms. It is derived and
non-authoritative: follow the recorded destination to GOV.UK for current
guidance or a transaction.

## Why this is more than the XML sitemap

The official [GOV.UK sitemap developer
manual](https://docs.publishing.service.gov.uk/manual/govuk-sitemap.html) says
that `https://www.gov.uk/sitemap.xml` is generated each morning from the GOV.UK
Search API index, split across multiple sitemap files, and excludes recommended
links. The [reuse
guidance](https://www.gov.uk/help/reuse-govuk-content) calls it a list of the
majority of GOV.UK pages. The [robots policy](https://www.gov.uk/robots.txt)
advertises the sitemap while also declaring crawler exclusions.

The OKF projection therefore uses the sitemap as one official enumerator in a
reconciled union. It also retains source-native Content API redirect rules,
Search and navigation discoveries, stable content IDs, typed relationships and
external-boundary records. Complete page bodies are neither retained nor
published.

## Scope

The release-1 denominator is the bounded public metadata estate of
`www.gov.uk`, its associated representations and evidenced boundary links. It
is not a DNS inventory or complete crawl of every independently operated host
ending in `.gov.uk`. Boundary hosts are listed with observed records and routes
so that the hand-off is visible, but they are not represented as fully mirrored
sites.

`bundle/data/site-topology.json` contains the compact full-snapshot index. Each
record shard retains the complete admitted redirect array and routing
classification; route and relationship indexes provide identifier lookup and
route-scoped adjacency.

## Hackathon path

Build the deterministic fixture and open the routing view:

```sh
python3 scripts/build_bundle.py
python3 scripts/check_publication.py
python3 -m http.server 8000 --directory bundle
```

Then open `http://127.0.0.1:8000/?view=sitemap`. The fixture proves the contract,
lazy browser path and validation gates. It must remain labelled as fixture
evidence.

The closed unsampled T0 census can later supply a substantially larger opening-
census preview, including the observed external-boundary records and known
redirects. That preview is still not a release: the active Content API
hydration, T1 re-enumeration, closing delta and final reconciliation must finish
before the topology may claim complete bounded release representation.

## Machine contract

`govuk-site-topology.v1` includes:

- snapshot ID, generated time, status and explicit scope;
- source/published record, host, redirect, relationship and stable-ID counts;
- all deterministically ordered observed hosts and their routing kinds;
- routing mechanisms and relationship-kind summaries;
- up to 100 deterministic redirect samples with a completeness flag; and
- paths to complete records, search, identifiers, adjacency and semantics.

The descriptor keeps Explorer-compatible string entrypoints and hash-binds the
topology through the matching `entrypoint_integrity` reference. Publication validation
checks schema and snapshot agreement, host order and uniqueness, full host-to-
record accounting, relationship totals, redirect-sample semantics and manifest
agreement.
