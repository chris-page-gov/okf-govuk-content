# Agent First, Human Friendly GOV.UK OKF — requirements register

Status: execution baseline  
Prepared: 11 July 2026  
Controlling brief: `WHATS_ON_GOVUK_OKF.md`  
Working repository: `chris-page-gov/okf-govuk-content`

## Scope decisions and operational definitions

1. **ADR-000 — Release 1 coverage.** Decision source: the user's exact later instruction, “The initial implementation should include the entire gov.uk contents”, which supersedes the open choice at brief lines 123–124. In the context of the brief's explicit metadata-led boundary at lines 28–40, Release 1 is interpreted as complete, snapshot-bounded inventory and metadata coverage, not republication of every body or every independently operated `*.gov.uk` site. This interpretation must be shown to the user at release and changes only through a later explicit scope decision. A representative fixture is a pipeline test, not the released corpus.
2. **Entire GOV.UK.** For this programme, this means every canonical public content item discoverable through the union of verified official public enumerators at a declared snapshot watermark. Live, withdrawn, redirected and translated items and discoverable attachment metadata are included. Every discovered item must become either a metadata record or an explicit constraint/exception record; unexplained omissions must be zero.
3. **Host boundary.** Unless a later decision expands it, `GOV.UK` means the main `www.gov.uk` publishing estate and its associated public attachments and machine-readable representations. It does not mean every independently operated `*.gov.uk` domain. Links to external transactional services remain relationships, not mirrored content.
4. **Metadata-led.** Release 1 includes titles, summaries, identifiers, canonical URLs, types, organisations, taxonomy, relationships, lifecycle, redirects, attachments, language, jurisdiction, evidence and indexing fields. It does not mirror complete page bodies. Evaluation therefore reports discovery separately from optional retrieval of authoritative source content.
5. **Every use.** No finite artefact can enumerate every present, future or idiosyncratic use. The testable requirement is an exhaustive, dated taxonomy of evidenced use classes across actor, goal, journey stage, content/service type, relationship need, jurisdiction, language, accessibility need, device/context, urgency/risk and degree of agent involvement. Completeness is demonstrated through coverage and saturation.
6. **One hundred questions.** To satisfy both reasonable readings without blocking unattended execution, every approved user story has 100 deterministically renderable question instances, and every approved primary persona has a curated, non-duplicative 100-question evaluation suite drawn from its story-bound instances. Shared 10 × 10 operation/challenge archetypes avoid paying for or hand-authoring repetitive variants.
7. **Agent First.** The canonical layer is machine-readable, schema-valid, identifier-stable, deterministic, provenance-complete and efficient for structured search, graph traversal and citation.
8. **Human Friendly.** Explorer supports accessible search, browse, facets, graph, lifecycle, resources and provenance with progressive disclosure. “Human UI of choice” is a comparative hypothesis for named populations and discovery tasks, not a universal claim that Explorer should replace GOV.UK for reading guidance or completing transactions.
9. **Public reproducibility.** Unattended acquisition defaults to public interfaces. Authenticated/internal sources may be used only when separately authorised, labelled and kept from becoming a hidden dependency of the reproducible public build.

## Atomic requirements

### Purpose and boundary

- **REQ-001** Treat AF/HF as a falsifiable proposal and report where it succeeds, partly succeeds, fails or remains untested.
- **REQ-002** Publish an independent OKF Bundle Wiki mapping GOV.UK structure, organisation, content/navigation models, publishers, metadata, taxonomies and relationships.
- **REQ-003** Support people navigating and understanding GOV.UK through OKF Explorer.
- **REQ-004** Support systems in understanding identifiers, types, provenance, hierarchy, lifecycle and relationships.
- **REQ-005** Support agents retrieving and citing authoritative GOV.UK content without flattening the site into an unstructured collection.
- **REQ-006** Enable comparison of source metadata, public presentation and search/discovery behaviour.
- **REQ-007** Establish a reusable semantic and provenance layer for later GOV.UK-derived bundles.
- **REQ-008** Identify the bundle prominently as derived and non-authoritative; GOV.UK remains the authoritative destination.
- **REQ-009** Cover the complete bounded public metadata corpus in release 1.
- **REQ-010** Declare an acquisition window and closing watermark so completeness is measurable on a changing site.
- **REQ-011** Do not mirror complete page bodies in release 1.
- **REQ-012** Publish the precise host, subdomain, campaign, form, API, asset and external-service boundary.
- **REQ-013** Keep a small representative fixture solely for pre-release pipeline and Explorer QA.

### Required metadata coverage

- **REQ-014** Model canonical content items and public URLs.
- **REQ-015** Model GOV.UK content types and schema families.
- **REQ-016** Model publishing and owning organisations distinctly.
- **REQ-017** Model mainstream browse, topics, taxonomies and navigation nodes.
- **REQ-018** Model parent, child, related, collection and part-of relationships.
- **REQ-019** Model lifecycle state, first publication, updates, redirects and replacement.
- **REQ-020** Model attachments, downloads and machine-readable representations.
- **REQ-021** Model language, jurisdiction, audience and service/content distinctions.
- **REQ-022** Model source systems, evidence URLs, retrieval times and confidence.
- **REQ-023** Provide fields suitable for full-corpus static search and discovery indexes.

### Sources, completeness and semantics

- **REQ-024** Begin with a fresh, dated audit of official source families.
- **REQ-025** Verify each source's current contract, schema and version.
- **REQ-026** Verify source coverage and reconcile independent counts.
- **REQ-027** Verify access controls, authentication, rate limits, robots policy and operational constraints.
- **REQ-028** Verify copyright, licensing and reuse terms before acquisition design.
- **REQ-029** Establish stable identifier and canonicalisation rules before deduplication.
- **REQ-030** Silently omit no feature class because of access, reuse or licensing constraints.
- **REQ-031** Record every constraint and exception in a machine-readable escalation ledger.
- **REQ-032** Use public reproducible sources as the unattended default.
- **REQ-033** Reconcile the union of official public enumerators so each discovered identifier maps to a record or an explicit exception and `unexplained_omissions = 0`.
- **REQ-034** Produce closing-watermark and subsequent drift/delta reports.
- **REQ-035** Define an extensible type and relationship vocabulary covering all examples in the brief without treating unlike classes as identical.
- **REQ-036** Give every generated relationship evidence type, evidence URL, retrieval time, derivation method and confidence.
- **REQ-037** Crosswalk GOV.UK fields to OKF, Schema.org, DCAT and relevant government vocabularies.
- **REQ-038** Preserve source-native structure before enrichment and label each inferred assertion.
- **REQ-039** Validate YAML-LD/JSON-LD equivalence, context expansion, identifiers, referential integrity and relationship direction.

### Publication and Explorer

- **REQ-040** Use an independent repository, CI, release cadence and GitHub Pages publication.
- **REQ-041** Provide human-maintained Markdown plus YAML-LD concepts and narrative.
- **REQ-042** Generate canonical `okf-bundle.yamlld` and semantically equivalent JSON-LD.
- **REQ-043** Generate the Explorer descriptor and data manifest.
- **REQ-044** Generate static search shards for the full corpus.
- **REQ-045** Generate deterministic route-scoped relationship adjacency.
- **REQ-046** Remain directly loadable by URL and provide an OKF Explorer registry entry.
- **REQ-047** Run compatibility and provenance checks from the first fixture onwards.
- **REQ-048** Set and test budgets for shard size, first useful render, query latency, browser memory and relationship expansion.

### Personas and user stories

- **REQ-049** Create a detailed taxonomy of human, organisational, intermediary and agent use archetypes.
- **REQ-050** Ground every persona in cited evidence rather than invented demographics.
- **REQ-051** Prefer needs, tasks and situational characteristics over stereotyping.
- **REQ-052** Include accessibility, assisted-digital, low-literacy, multilingual, mobile, urgent/high-stakes and professional/research contexts.
- **REQ-053** Include agents acting for individuals, agents supporting staff, content-serving systems, audit/governance agents and bulk-research agents.
- **REQ-054** Give each persona stable IDs, evidence, goals, contexts, capabilities, constraints, risks, success criteria and applicable content families.
- **REQ-055** Express stories consistently as actor/context–need–outcome statements with acceptance criteria and failure harms.
- **REQ-056** Map every official content/schema family and every evidenced use-class dimension to at least one story.
- **REQ-057** Run independent gap-challenge and saturation passes and retain residual gaps.
- **REQ-058** Version personas and stories as GOV.UK, public needs and agent capabilities change.

### Evaluation-question corpus

- **REQ-059** Generate 100 valid, deterministically renderable question instances per approved story and exactly 100 curated canonical questions per approved primary persona for each evaluation release.
- **REQ-060** Ensure every story for each persona is represented in its curated persona suite.
- **REQ-061** Store persona/story IDs, wording, intent, target entities and relationships, answer shape, difficulty, ambiguity, time, jurisdiction, language and provenance requirements.
- **REQ-062** Store verified gold content IDs/URLs or an explicit deliberately-unanswerable classification, with evidence and snapshot date.
- **REQ-063** Stratify questions across lookup, browse, taxonomy, publisher/owner, lifecycle, redirects, attachments, language, jurisdiction, multi-hop, comparison and negative tasks.
- **REQ-064** Distinguish metadata-only discovery from discovery-plus-authoritative-source retrieval.
- **REQ-065** Include ambiguous, stale, adversarial, unsupported-premise and no-result cases.
- **REQ-066** Detect semantic duplicates and leakage into implementation prompts or tuning data.
- **REQ-067** Freeze and checksum question manifests before comparative testing.
- **REQ-068** Validate every gold target independently of the model that generated the question.

### Evaluation, UI and comparison

- **REQ-069** Test effectiveness and efficiency separately for humans and agents.
- **REQ-070** Compare Explorer with GOV.UK search/navigation for humans and with raw official metadata/search surfaces for agents.
- **REQ-071** Measure agent target recall, ranking, relationship accuracy, citation correctness, provenance completeness, abstention and answerability classification.
- **REQ-072** Measure agent latency, tool calls, tokens, bytes/shards read, query steps and cost.
- **REQ-073** Measure human task success, time, errors, backtracking, reformulations, click depth, comprehension and preference.
- **REQ-074** Test accessibility against WCAG 2.2 AA using automation, expert review and representative users where available.
- **REQ-075** Audit Explorer's reader, search, facets, graph, links, type, timeline, resources and narrative behaviours.
- **REQ-076** Turn every evidenced UI gap into a prioritised change with affected stories, rationale, component, acceptance test and dependency.
- **REQ-077** Pre-register “UI of choice” for defined people and tasks and do not infer it from agent or synthetic tests.
- **REQ-078** Conduct a reproducible review of comparable government catalogues, knowledge graphs, linked-data portals, content APIs, search systems, data catalogues and agent/GraphRAG discovery layers.
- **REQ-079** Compare purpose, scope, semantics, acquisition, provenance, freshness, scale, agent interface, human UI, governance and published evidence.
- **REQ-080** Assess each stated aim independently as fulfilled, partly fulfilled, not fulfilled or not yet testable, with evidence and confidence.

### Evidence and unattended execution

- **REQ-081** Use a dated SOTA protocol with reproducible searches, inclusion/exclusion rules and primary-source preference.
- **REQ-082** Maintain a complete bibliography with direct URLs wherever available.
- **REQ-083** Give each citation its strongest stable locator: PDF page/section; repository commit/path/lines; HTML heading plus paragraph ordinal and snapshot; or API JSON Pointer.
- **REQ-084** Verify every citation's URL, redirect destination, source identity, locator and semantic support for the linked claim.
- **REQ-085** Record retrieval date, version/commit, content hash and a lawful evidence snapshot where feasible.
- **REQ-086** Express implementation as a dependency-aware multi-agent DAG with safe parallel workstreams.
- **REQ-087** Assign model tiers by task: browsing/research, frontier synthesis/ontology, coding, economical constrained generation and independent adjudication.
- **REQ-088** Use deterministic code—not an LLM—for counts, schema checks, hashes, URL checks, deduplication and metric calculation.
- **REQ-089** Give every agent a bounded input/output schema, source policy, budget, retry rule, completion gate and escalation condition.
- **REQ-090** Make runs resumable and idempotent using checkpoints, immutable manifests and content-addressed artefacts.
- **REQ-091** Record prompts, exact model/version, parameters, tool calls, timestamps, source snapshots, outputs, validation and cost.
- **REQ-092** Prevent the same model/run from being the sole generator and judge of evidence, questions or conclusions.
- **REQ-093** Fail closed on the affected scope for unresolved access, rights, robots, security, citation or completeness issues; continue independent workstreams safely.
- **REQ-094** Record decisions, design changes, commits, tests, evaluations, failures and waivers in human- and machine-readable forms.
- **REQ-095** Produce a final machine-readable status report mapping every requirement to tests, evidence, exceptions and unresolved dependencies.

## Acceptance gates

1. **Requirements:** every brief/prompt clause maps to a requirement; no orphan requirement or hidden blocking decision.
2. **Sources:** every candidate source has verified contract, coverage, count, access, reuse, identifier and retrieval evidence.
3. **Full corpus:** the frozen union of verified public enumerators is reconciled; every discovered item is a record or explicit exception; unexplained omissions are zero; counts, watermark, checksums and drift are published.
4. **Semantics:** artefacts validate; identifiers resolve; YAML-LD and JSON-LD are semantically equivalent; 100% of generated relationships have complete provenance.
5. **Personas:** every persona is evidenced; the dimensional coverage matrix has no unexplained gap; challenge passes reach documented saturation.
6. **Questions:** exactly 100 valid instances exist per approved story and exactly 100 validated curated questions exist per approved primary persona; every story is represented in its persona suite; all gold targets and citations verify; duplicate and leakage tests pass.
7. **Evaluation:** all machine-applicable frozen questions run against proposal and machine baselines under matched conditions; a preregistered, powered and coverage-balanced subset is used for human comparison, including representatives of every high-harm class; paired effectiveness/efficiency results, failures and confidence intervals are retained.
8. **Human evidence:** no “UI of choice” claim is made from synthetic evidence alone; it requires pre-registered accessible tests with people from the claimed populations.
9. **Citations:** every released citation passes link, locator and claim-support checks; redirects and inaccessible evidence are visible.
10. **Reproducibility:** a clean run from pinned inputs reproduces manifests, corpus checksums, tests and reports within declared tolerances.
11. **Assessment:** each original aim receives an evidenced status and confidence, including negative findings.

## Traceability record

The canonical relation is:

`brief clause → requirement → research question/hypothesis → workstream/run → artefact → test → evidence/citation → result/confidence/exception`.

The generated review matrix must contain at least:

| Field group | Required fields |
|---|---|
| Origin | `brief_clause_id`, `source_locator`, `clause_summary` |
| Requirement | `requirement_id`, `interpretation`, `boundary`, `decision_id` |
| Inquiry | `research_question_id`, `hypothesis_id` |
| Coverage | `persona_ids`, `story_ids`, `question_ids` |
| Execution | `workstream_id`, `agent_role`, `model_id`, `run_id`, `dependency_ids` |
| Artefact | `artefact_id`, `path_or_uri`, `version`, `sha256` |
| Test | `test_id`, `baseline_id`, `metric`, `threshold`, `result` |
| Evidence | `evidence_id`, `source_url`, `precise_locator`, `retrieved_at`, `evidence_sha256` |
| Verification | `link_status`, `locator_status`, `support_status`, `verifier_run_id` |
| Disposition | `confidence`, `status`, `exception_id`, `owner`, `next_action` |

Minimum status vocabulary: `proposed`, `accepted`, `in_progress`, `blocked`, `produced`, `verified`, `passed`, `failed`, `superseded`, `exception_recorded`.

## Defaults that permit an unattended start

- Use `chris-page-gov/okf-govuk-content` as the working repository name until explicitly changed.
- Use public, reproducible GOV.UK sources; treat internal access as optional corroboration, never a hidden release dependency.
- Interpret GOV.UK as `www.gov.uk` plus associated public assets/representations and external-link relationships.
- Keep page bodies out of the release-1 corpus; fetch authoritative pages on demand for retrieval-stage evaluation.
- Use 100 renderable instances per story plus an exactly 100-question curated suite per primary persona.
- Treat genuine human participant research as a scheduled external evidence gate; do not manufacture its result or block machine-only work that can proceed independently.
