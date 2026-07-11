# What's on GOV.UK — proposed OKF Bundle Wiki

Status: initial project brief  
Prepared: 11 July 2026  
Proposed repository: `chris-page-gov/okf-govuk-content`

## Purpose

Create an independently published OKF Bundle Wiki that explains and maps what
is on GOV.UK: the structure and organisation of the website, its content and
navigation models, publishing organisations, metadata, taxonomies and the
relationships between them.

The bundle should help:

- people navigate and understand GOV.UK through OKF Explorer;
- content-serving systems understand GOV.UK identifiers, types, provenance,
  hierarchy, lifecycle and relationships;
- agents retrieve or cite GOV.UK content without flattening the site into an
  unstructured document collection;
- teams compare source metadata, public presentation and search/discovery
  behaviour;
- future GOV.UK-derived bundles share one explicit semantic and provenance
  layer.

## Working scope

The first release should be metadata-led rather than a mirror of page bodies.
It should model:

1. canonical content items and public URLs;
2. GOV.UK content types and schema families;
3. publishing and owning organisations;
4. mainstream browse, topics, taxonomies and navigation nodes;
5. parent, child, related, collection and part-of relationships;
6. lifecycle state, first publication, updates, redirects and replacement;
7. attachments, downloadable resources and machine-readable representations;
8. language, jurisdiction, audience and service/content distinctions;
9. source systems, evidence URLs, retrieval times and confidence;
10. search/discovery fields useful to a static Explorer index.

## Candidate source families to verify

Source discovery must begin with a fresh official-source audit. Candidate
families include the GOV.UK Content API, search/index interfaces, public
sitemaps and robots policy, publishing/content-store schemas, organisation and
taxonomy pages, and documented bulk or feed surfaces. Their current contracts,
coverage, access controls and reuse terms must be verified before choosing the
acquisition design.

No feature class should be silently omitted because of fair-use, access or
licensing concerns. Constraints should be retained in the same machine-readable
escalation ledger pattern used by the existing bundles.

## Proposed semantic model

Example node types:

- `govuk:ContentItem`
- `govuk:ContentType`
- `govuk:Organisation`
- `govuk:Taxon`
- `govuk:MainstreamBrowsePage`
- `govuk:Service`
- `govuk:Collection`
- `govuk:Attachment`
- `govuk:Redirect`
- `schema:WebPage`
- `schema:GovernmentService`

Example relationship kinds:

- `published by`
- `owned by`
- `part of`
- `parent of` / `child of`
- `classified under`
- `related to`
- `links to`
- `replaces` / `replaced by`
- `redirects to`
- `has attachment`
- `has content type`
- `available in language`

Every generated relationship should retain its evidence type, evidence URL,
retrieval time, derivation method and confidence.

## Publication shape

Follow the implemented federated profile:

- independent repository, CI, release cadence and GitHub Pages publication;
- Markdown plus YAML-LD for human-maintained concepts and narrative;
- canonical `okf-bundle.yamlld` and equivalent JSON-LD;
- compiled Explorer descriptor and data manifest;
- static search shards for large-corpus discovery;
- deterministic route-scoped relationship adjacency;
- registry entry in `okf-explorer`, while remaining directly loadable by URL;
- compatibility and provenance checks from the start.

## Recommended delivery phases

1. **Official-source audit** — identify interfaces, schemas, counts, licences,
   rate limits and stable identifiers.
2. **Profile and crosswalk** — map GOV.UK fields to OKF, Schema.org, DCAT and
   relevant government vocabularies without forcing unlike content into one
   class.
3. **Representative fixture** — generate a small cross-section covering
   guidance, services, organisations, taxonomies, collections and attachments.
4. **Explorer evaluation** — test reader, graph, links, type, timeline,
   resources, narrative and search behaviours against real user questions.
5. **Full metadata corpus** — shard records, search and adjacency indexes with
   completeness and drift checks.
6. **Publication and registry** — protected main, Pages, checksums, release and
   canonical Explorer example.
7. **Model-assisted enrichment** — only after source-native structure is
   preserved; label every inferred assertion and evaluate precision.

## Decisions for the next implementation turn

- Confirm `okf-govuk-content` as the repository name.
- Decide whether the first corpus represents all public GOV.UK content or a
  deliberately representative cross-section followed by scale-up.
- Choose the initial user questions and evaluation personas.
- Confirm whether authenticated/internal GOV.UK interfaces may supplement the
  public reproducible source boundary.
