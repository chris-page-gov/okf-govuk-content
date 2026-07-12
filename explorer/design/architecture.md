# Static Explorer architecture

Status: source implementation plus a zero-dependency real-Chromium fixture gate. The current environment did not authorise the localhost/browser execution, and axe installation was separately refused by the usage reviewer; full-corpus, axe, accessibility-expert, privacy and participant evidence remain pending.

## Product boundary

The Explorer is a derived, non-authoritative metadata discovery surface. It never presents itself as GOV.UK, answers questions generatively, mirrors complete page bodies, simulates a transaction or hides an unsupported result. Every card keeps a canonical GOV.UK hand-off visible.

The source has no runtime dependency or build step. `index.html` loads only local JavaScript, CSS, worker, icon and manifest assets. Source strings enter the DOM through `textContent`; no source HTML or scripts execute.

## Bootstrap and data contract

The client consumes the audited federated profile:

1. `okf-explorer.json` with `kind: okf-large-corpus`;
2. `data/manifest.json`;
3. overview and optional analysis payloads;
4. the static search manifest in a dedicated worker;
5. route-scoped `okf-relationship-adjacency.v1` buckets using `fnv1a32-prefix-2`;
6. `okf-route-index.v1` buckets for one-record progressive hydration.

Descriptor discovery first honours an explicit HTTPS `bundle` parameter. Otherwise it tries configured and nearby relative descriptor paths so the same source can be copied to a Pages project root or served from `explorer/src/` during fixture development. A CSP-safe `404.html` and external `pages.js` preserve supported query and hash state when GitHub Pages serves an unknown path.

Bootstrap never requests record or whole-relationship chunks. Search loads only required lexicon, prefix, postings and result-document shards. Selecting a route loads one adjacency bucket and, when advertised, one record shard. Each JSON response is capped at 64 MiB, transient responses retry with bounded backoff, gzip is explicit, failed integrity references fail closed, and a requested snapshot mismatch blocks record display.

The route index uses the same `fnv1a32-prefix-2` buckets as adjacency. Each
identifier maps to a sorted list of typed matches with `kind`, kind-local
`ordinal` and exact `open` route. Cross-type aliases are valid: for example an
organisation URL can identify both its content-item dataset and its publisher
node. Exact `dataset/`, `publisher/` and `resource/` routes select their type;
an untyped native identifier with more than one match is ambiguous and must
not be resolved by choosing the first row. The kind-local ordinal selects a
record from the matching `data/manifest.json` chunk list.

The search worker follows `okf-static-search.v1`: NFKD tokenisation, two-character lexicon shards, prefix suggestions, posting triples and ordinal-addressed result-document chunks. Query cancellation prevents superseded results from replacing current state.

## Canonical state and deep links

`core.js` is the only state codec. Query parameters serialise query, repeated facet expressions, view, presentation mode, BCP 47 chrome language, lifecycle, jurisdiction, page, immutable snapshot and up to 12 pins. The selected record uses the federation's decoded hash-route convention. Legacy `route=` query links remain accepted aliases and are canonicalised to the hash. Browser back/forward replays this state. All views derive from the same reduced record set; mode changes presentation only.

English and Welsh chrome are first-class. Other valid BCP 47 record languages remain data facets and can be added to chrome without changing state shape. Language variants are records/relationships, not text substitutions inside another record.

## Human views

- Results are the default task-first path, with title, source type, publisher/owner, lifecycle, date, language/jurisdiction, breadcrumb, match explanation and canonical hand-off.
- Browse uses generated hierarchies or facets, retaining taxonomy, mainstream browse, organisation, service/journey and collection labels.
- Relationships load route adjacency, aggregate kinds first and cap one visual expansion at 250 nodes and 500 edges. The accessible table is built from exactly the same bounded edges; the SVG is never the sole representation.
- Lifecycle derives publication, update, withdrawal, redirect and replacement events and always states the snapshot caveat.
- Compare stores route pins in the URL and exports the same IDs/state as Markdown, YAML-LD or JSON-LD.
- Detail progressively discloses summary, relationships, lifecycle, provenance and raw source metadata. Confidence is displayed only for inferred assertions.

## Security and privacy

The HTML policy excludes inline scripts/styles, plugins, non-HTTPS remote data and arbitrary form targets. The JavaScript uses no `innerHTML`, `eval` or dynamic module URL from bundle data. External anchors accept HTTPS only. JSON and gzip reads are bounded. Search and public metadata are treated as untrusted data, never instructions.

Instrumentation is disabled on every load. Consent enables only an in-memory 200-event ring. Six fixed event kinds have field allowlists; query events retain length, result count and timing but never query text. Nothing is transmitted or persisted. A lawful basis, retention policy and participant-data review remain prerequisites before any human study.

## Accessibility and performance verification

The source provides landmarks, native controls, live regions, visible focus, 44-pixel-scale targets, keyboard operation, narrow reflow, reduced-motion and forced-colour handling. `tests/browser.e2e.mjs` drives installed Chrome/Chromium through the DevTools protocol over an ephemeral real HTTP server and checks the accessibility tree, names, duplicate IDs, focus, reflow, media preferences and a computed-contrast subset. It also exercises query/hash replay, Pages 404 recovery, gzip route/index/adjacency loading, heap and timing budgets. The gate deliberately has no skip path when Chrome is absent.

The exact browser thresholds are versioned in `requirements/browser-budgets.json`; the evidence generator writes `src/evidence/fixture-browser.json` only after all automated fixture assertions pass. A fixture pass is not WCAG conformance and cannot promote a release: axe installation/execution, manual keyboard and screen-reader review, expert inspection, authorised representative-user testing and a fresh run over the complete frozen corpus remain explicit gates. In this execution environment, dependency installation and localhost browser execution were refused because the Codex usage limit was still reported as exhausted, so no passing evidence file is claimed yet.
