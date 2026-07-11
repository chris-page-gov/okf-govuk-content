# Official-source audit

Status: verified source contract  
Observation window: 2026-07-11 22:45:09Z–23:21:00Z  
Machine evidence: `source-preflight.json`

## Conclusion and bounded completeness

No public authoritative bulk export of every Publishing API document, route,
redirect, unpublishing and link was found. Release completeness therefore means
complete reconciliation of the frozen public-source union—not parity with
internal GOV.UK state.

The admitted union is deduplicated sitemap URLs, every Search API v1 result,
all Organisations API records, recursive Content API graphs rooted at `/`,
`/world/all` and `/browse`, structured links/parts/translations/attachments
found while hydrating, documented curated route families, and bounded Atom or
rendered-link gap detection. Each candidate becomes a record, alias, redirect,
tombstone, external boundary link or explicit exception.

## Live source results

| Source | Observed result | Contract and constraint |
|---|---|---|
| [Content API overview](https://content-api.publishing.service.gov.uk/) and [v1.0.0 reference](https://content-api.publishing.service.gov.uk/reference.html) | Public path lookup at `https://www.gov.uk/api/content/{path}`; no authentication; root exposes 20 level-one taxons; documented 10 requests/s/client | Path-addressed, beta, `www.gov.uk` HTML-backed content only; no enumeration, direct asset bytes, dynamic or historic-content API. Shared acquisition ceiling is 8 requests/s. |
| [Search API v1](https://www.gov.uk/api/search.json) and [usage documentation](https://docs.publishing.service.gov.uk/repos/search-api/using-the-search-api.html) | `total=715465`; 137 content-store document types; 461 external records; maximum page size 1,500 | Unsupported public interface, English/selected formats, no redirects/gone inventory and no immutable cursor. Use opposing timestamp passes, overlaps, deduplication and T1 repeat; 477 maximum-sized requests is only an offset-walk estimate. |
| [Sitemap index](https://www.gov.uk/sitemap.xml) and [generation documentation](https://docs.publishing.service.gov.uk/manual/govuk-sitemap.html) | 35 shards; 869,875 raw entries; 683,070 unique URLs; 186,759 duplicate URL keys | GOV.UK describes coverage as the “majority”. Shard 35 changed during the preflight; store/hash/refetch every shard and reject mixed snapshots. |
| [Organisations API](https://www.gov.uk/api/organisations) and [documentation](https://docs.publishing.service.gov.uk/manual/organisations-api.html) | 1,256 records, 63 pages of 20; includes live, closed and exempt bodies plus hierarchy/supersession | Use as organisation inventory. Preserve the smaller public navigation index as a separate view, not a denominator. |
| [Topic taxonomy](https://docs.publishing.service.gov.uk/manual/taxonomy.html) | Content API root has 20 `level_one_taxons` | Recursively traverse typed parent/child/translation fields. Search’s taxon count is a gap detector only. |
| [World taxonomy](https://docs.publishing.service.gov.uk/manual/world-taxonomy.html) | `/api/content/world/all` has 233 `child_taxons` | Published prose counts are stale; retain the live response and pinned documentation as conflicting evidence. |
| [Collections](https://docs.publishing.service.gov.uk/repos/collections.html) | `/api/content/browse` has 16 top-level browse pages | Preserve browse, taxonomy, step-by-step and collection classes separately. Old documented routes may now be redirects. |
| Structured attachment sample | Publication metadata exposed attachment ID, title, MIME type, filename, bytes, pages, accessibility flag and `assets.publishing.service.gov.uk` URL | Publish metadata and canonical link, not the attachment bytes. Perform item-specific rights review. |
| Public Atom feeds | Two tested feeds returned 20 entries and no next link | Recent-delta corroboration only; not a census. |
| Search API v2, GovSearch/GovGraph, Content Data | Documentation is public; query/data surfaces require internal or authenticated access | Comparator-only. They are not hidden dependencies and are not admitted without explicit authority. |

At the configured 8 requests/s, hydrating only the 683,070 sitemap-unique
paths has a theoretical network floor of roughly 23 hours 43 minutes before
retries or Search-only additions. The acquisition pipeline is resumable and
checkpointed accordingly.

## Robots, rate and operational policy

[Robots.txt](https://www.gov.uk/robots.txt) was retrieved with SHA-256
`459835d29527b9c00f00f6af00f45c73a909af1944e81fefa153f585e1857305`.
For the default agent it disallows print routes and `/search/all*`, and declares
the sitemap index. Ordinary Content API and canonical routes are not disallowed.
The generator uses one descriptive User-Agent and one shared host limiter;
429/5xx responses back off and resume from checkpoint.

## Rights, licence and fair-use triggers

The [reuse guidance](https://www.gov.uk/help/reuse-govuk-content), [GOV.UK
terms](https://www.gov.uk/help/terms-conditions), [OGL
v3](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)
and [OGL exceptions](https://www.nationalarchives.gov.uk/information-management/re-using-public-sector-information/uk-government-licensing-framework/open-government-licence/exceptions-to-ogl/)
support a default classification of `ogl_v3_except_where_otherwise_stated`, not
an unconditional licence claim.

Item-level exception/fair-dealing review is triggered by personal data,
third-party credits, logos/crests/Royal Arms/insignia, patents/trademarks/design
rights, identity documents, complete page/attachment/image bytes or a specific
licence notice. The safe release default is metadata, source-native
relationships and authoritative links with a derived/non-official notice.

## Pinned official source versions

| Source | Commit |
|---|---|
| Content API documentation | `dc46726bfb41238576ee52c5e5b0a491c10710fd` |
| Content Store/OpenAPI | `35f086d811ebf72f7a2f0e02e6f8b132b7efd45d` |
| Search API | `c37530f7cd01aa53c58283588b7d136f954c86e0` |
| Publishing API/content schemas | `b1e987aa7b3e62c105ff2b2db87667f7638726f8` |
| Collections | `532a54cfa04305265abc087e15249cdfdc0b8e61` |
| Data Community/GovGraph docs | `3f308dad463fba772ea09bb119fbd8d8eee02ea8` |
| GovGraph pipeline | `fa870c312fd768c7aa98fcbc28b827028408abe9` |

The pinned Publishing API tree contains 83 actual source schema families while
the live Search index exposes 137 document types. These remain distinct fields.

## Plan-source preflight

The execution preflight opened all 93 URLs from
`planning/PLAN_SOURCE_PREFLIGHT.json` with a bounded, identified client. Ninety
two passed. The Pirolli PDF host negotiates a legacy Diffie-Hellman key that the
current Python/OpenSSL client rejects (`DH_KEY_TOO_SMALL`); it is retained as an
explicit citation-access constraint and is not treated as verified by fallback.

