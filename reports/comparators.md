# Comparator catalogue and matched-evaluation plan

Status: **design complete; empirical comparison not run**  
Evidence cutoff: **2026-07-11**  
Source constraint: references inherited from the approved research plan passed URL-identity preflight only. Locator-level and claim-support verification, systematic search logs, independent screening and final delta search remain release evidence gates.

This catalogue defines what must be compared before the “What’s on GOV.UK” bundle can claim an effectiveness, efficiency or preference advantage. It deliberately does not turn system descriptions or generic benchmark scores into evidence that the bundle performs better on GOV.UK.

## Matched comparison rule

All machine systems must receive the same frozen T0 metadata corpus, question wording, independently verified gold judgements, hardware envelope and measurement code. Human-facing systems must be tested with the same preregistered tasks and balanced order, while preserving each system's normal interaction model. Discovery from metadata and answering from authoritative body content are scored separately.

No comparator result is currently reported because T0 has not been frozen, answerable gold content IDs and URLs have not been independently assigned, and human research is not authorised. Those are evidence gates, not zero scores.

## Mandatory families

| Family | Named comparators | What already exists or must be learned | Matched evidence required | Current status |
|---|---|---|---|---|
| Current GOV.UK discovery | GOV.UK site search, Search API v1, Search API v2, mainstream browse, topic taxonomy and organisation pages | Human and programmatic routes already provide search, browse and filtered discovery; the bundle must show a task-specific gain rather than relabel these capabilities. | Same tasks; target recall, nDCG, success, time, reformulations, click depth, bytes and failure analysis. | Protocol only; no result. |
| GOV.UK content graph and data | Content API, Publishing API model, content schemas, GovGraph and GovSearch | Official systems define content identities, editions, routes, relationships and analytical graph uses. The closest comparison is whether an independently reproducible public projection adds value while retaining source distinctions. | Identity/edge fidelity, public reproducibility, freshness, provenance completeness, query coverage and known access limits. | Source identities recorded; matched run pending. |
| GOV.UK generative discovery | GOV.UK Chat engineering and pilot evidence | Published work provides a relevant retrieval, grounding, reranking and citation comparator. Its reported population and system setting cannot be treated as a matched bundle evaluation. | Same frozen tasks where an accessible interface exists; otherwise a bounded comparison of published methods and evidence. | Published-evidence review pending claim-level verification. |
| Existing project baseline | OKF Explorer, its large-corpus CKAN profile and overview-context design | Existing static search, facets, routes and sharding are the migration baseline. GOV.UK-specific semantics must be measured as an extension, not credited for inherited behaviour. | Pinned Explorer commit, route/component gap matrix, static build size, search latency, accessibility and regression tests. | Implementation baseline handled separately; empirical task comparison pending. |
| Knowledge graphs and linked data | Wikidata, DBpedia, schema.org, JSON-LD/YAML-LD and SHACL-style validation | These provide identifiers, graph representations and validation patterns, but none is presumed to be the authoritative GOV.UK operational content model. | Crosswalk loss, source-native distinction retention, evidence granularity, round-trip determinism and validation coverage. | Semantic comparison required; no quality claim. |
| Public-service models | CPSV-AP, Core Public Service Vocabulary, life-event/service models and GOV.UK content schemas | Public-service vocabularies can inform service, channel, eligibility and evidence concepts. GOV.UK content formats remain a separate source-native layer. | Term-by-term crosswalk with exact, broader, narrower, related or no-match disposition and cited rationale. | Crosswalk research pending. |
| National government portals | Canada.ca and other official national navigation, content API and linked-data portals | Comparator portals can expose reusable information-architecture and accessibility practices without implying that national contexts are interchangeable. | Purpose, scope, semantics, acquisition, freshness, governance, language, accessibility, agent interface and published task evidence. | Systematic comparator sweep pending. |
| Data catalogues | data.gov.uk, DCAT-AP catalogues and official open-data portals | Dataset discovery patterns help with resources and provenance but do not cover all informational content or transactions. | Resource metadata, attachment/API discovery, licensing, versioning, publisher identity and boundaries of catalogue scope. | Protocol only. |
| Retrieval systems | Exact known-item rule, BM25/lexical retrieval, learned sparse, dense, late interaction, reciprocal-rank fusion and graph retrieval | No retrieval family wins by definition. BEIR and related studies inform methods, not expected GOV.UK results. | Recall@10, MRR@10, nDCG@10, latency, index/build cost, memory, language slices and deterministic rebuild. | Four local baseline families registered; runs pending T0/gold. |
| Knowledge-intensive QA | KILT-style provenance evaluation and retrieval-augmented answering | Provenance-aware evaluation informs citation and supported-answer scoring. Generic benchmark results are not transferable without a matched corpus. | Citation precision/completeness, supported-claim rate, answerability, abstention, jurisdiction and temporal accuracy. | Protocol only; body-content retrieval is outside the release-1 metadata corpus. |
| Agent and web-task benchmarks | GAIA, BrowseComp, WebArena and AssistantBench | End-task and tool-trace practices can inform reproducibility, adversarial tasks and efficiency reporting. Their scores do not establish performance on GOV.UK. | Tool calls, query steps, tokens, bytes/shards read, latency, cost, target correctness, trace evidence and failure taxonomy. | Method transfer only; no benchmark score claimed. |

## Primary evidence entry points

- GOV.UK [Content API](https://content-api.publishing.service.gov.uk/) and [publishing architecture](https://docs.publishing.service.gov.uk/manual/architecture-deep-dive.html)
- GOV.UK [content schemas](https://github.com/alphagov/publishing-api/tree/b1e987aa7b3e62c105ff2b2db87667f7638726f8/content_schemas) and [document types](https://docs.publishing.service.gov.uk/document-types.html)
- GOV.UK [site-search overview](https://docs.publishing.service.gov.uk/manual/govuk-search.html), [Search API usage](https://docs.publishing.service.gov.uk/repos/search-api/using-the-search-api.html) and [search-quality metrics](https://docs.publishing.service.gov.uk/repos/search-api/search-quality-metrics.html)
- GOV.UK [Knowledge Graph query guide](https://docs.publishing.service.gov.uk/repos/govuk-knowledge-graph-gcp/how-to-write-queries.html), [technical-debt note](https://docs.publishing.service.gov.uk/repos/govuk-knowledge-graph-gcp/technical-debt.html) and [GovSearch application](https://docs.publishing.service.gov.uk/repos/govuk-knowledge-graph-search.html)
- GOV.UK Chat [engineering account](https://insidegovuk.blog.gov.uk/2026/05/15/developing-gov-uk-chat-our-data-science-and-ai-engineering-journey/) and [public testing account](https://insidegovuk.blog.gov.uk/2026/03/16/5-things-we-learned-testing-gov-uk-chat-an-ai-assistant-for-government/)
- [BEIR](https://arxiv.org/abs/2104.08663), [KILT](https://aclanthology.org/2021.naacl-main.200.pdf), [WebArena](https://arxiv.org/abs/2307.13854) and [GAIA](https://proceedings.iclr.cc/paper_files/paper/2024/hash/25ae35b5b1738d80f1f03a8713e405ec-Abstract-Conference.html)
- Canada.ca [content organisation](https://design.canada.ca/specifications/information-findability/organizing-content.html) and [research and prototyping](https://design.canada.ca/continuous-improvement/research.html)

## Decision and failure meanings

- Explorer is not successful merely because it beats a weak lexical baseline. It must at least be non-inferior on the preregistered primary discovery tasks and show a measurable semantic, provenance, accessibility or efficiency benefit.
- Agent improvement cannot compensate for a human-accessibility failure, and human preference cannot compensate for incorrect targets or citations.
- A null result is publishable: the bundle may improve auditability and machine traversal without materially improving ordinary human discovery.
- Inaccessible, internal-only or unmatched comparators remain visible with an access-status reason; they are not silently removed or assigned synthetic results.
- “UI of choice” requires accessible research with people in the claimed populations. Synthetic questions, automated accessibility checks and agent traces cannot establish that preference claim.

The executable evaluation contract is in `evaluation/protocol/preregistration.json`; baseline run status is in `evaluation/baselines/catalogue.json` and `evaluation/results/status.json`.
