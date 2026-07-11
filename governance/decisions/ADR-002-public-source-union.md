# ADR-002: Frozen multi-source union is the corpus denominator

Status: accepted  
Date: 2026-07-11

No public source enumerates all current and historic GOV.UK publishing state.
The release denominator is therefore the frozen union of deduplicated sitemap
routes, all public Search API v1 results, Organisations API records, recursive
Content API roots and typed links, declared curated route families/resources,
and bounded feed/rendered-link gap detectors.

The sitemap and Search API are independent observations with different
omissions; neither is authoritative alone. Every admitted candidate receives
one disposition and every publicly non-enumerable class remains an explicit
constraint. Internal Publishing API, Search v2, GovGraph and Content Data are
not hidden dependencies.

