# Implementation status

- Status date: 15 July 2026
- Milestone: T0 census closed; bounded new-child demonstrator produced
- Publication ready: **no**

The repository has a working deterministic 69-record new-child demonstrator
and the main release-oriented implementations, but it has not completed the
full-corpus, evaluation, citation or full-snapshot clean-room gates. The
demonstrator is useful for GOV.UK team review; it is not evidence that Release 1
covers GOV.UK or that these 69 records contain every route relevant to a new
child.

## Current evidence

| Area | Current state | What remains before machine RC |
|---|---|---|
| Contract and controller | 95 requirements, 11 gates and 36 task contracts imported; controller checks pass | Accepted run evidence and terminal requirement dispositions |
| Official sources | Dated 32-source and 93-URL plan preflight retained; T0 closed from 137 opposing-pass Search partitions, 35 byte-stable sitemap shards and closed organisations/navigation | T0 hydration, T1 union closure and final rights/citation snapshot binding |
| Semantic profile | YAML-LD profile, JSON schemas, crosswalks, shapes and JSON-LD fixture projection produced | Release-snapshot equivalence, integrity and provenance evidence |
| Bundle and Explorer | Accepted body-free snapshot closes 69/69 seeds with zero unexplained omissions, 118 bounded metadata observations, 753 direct typed boundaries and 127 individually receipted HTTP 200 attempts; static search, route index, adjacency, dedicated journey view, source-query evidence, query/hash replay, Pages fallback and read-only discovery are produced; exact-byte postings partitions and 1,000-record document maps preserve logical lookup and legacy manifests while bounding physical files | Finish screenshot evidence and focused security review for this bounded PR; separately rerun full-corpus capacity/closing and its accessibility/security/performance gates; axe, accessibility-expert and screen-reader review remain open |
| AI handoff | Question-specific Markdown/JSON export is the universal default; the full portable pack is labelled bulk/archive. The deterministic Python/CLI adapter and official-SDK MCP server expose five read-only, idempotent, closed-world bounded tools, validate the 69-record identity and finite data-plane hashes, and make no model calls or arbitrary URL fetches | Retain the rule that metadata supports discovery, not eligibility or substantive guidance. Local stdio and SDK round trips pass; a remote MCP service would additionally require TLS, authentication, authorisation, Origin validation, rate limits and audit logging |
| Security | Completed repository-wide scan recorded 14 findings; its remediation diff scan confirmed those fixes and found three residual low issues, all fixed at `27890dc`; 32 focused post-fix tests pass | Run and hash-bind a new full-repository scan after the hydrated release snapshot and publication artefacts are frozen; `security_scan_passed` remains false |
| Census and hydration | Unsampled T0 closed at 848,977 candidates and 836,998 publication records; 848,971 represented plus six redirects, with zero exceptions/unexplained omissions; every shard digest passes; the resumable checkpoint enforces the authorised 10 GiB retained-metadata ceiling, durably spools successful responses, admits all writes before mutation and rejects duplicate source identities/path escapes | The long full hydration was stopped and its checkpoint preserved because it could not complete within the demonstration window. Resume only as a separate full-release operation, then re-enumerate T1, close drift and retain zero unexplained omissions |
| Personas and stories | Machine-applicable saturation passes for 48 primary persona hypotheses, 17 overlays, 11 dimensions, 83 schema families, 136 overlay pairs, five high-risk t-way scenarios and two successive no-new challenge passes | Authorised human validation and final-snapshot regeneration of six release-v2 stories per persona; UI preference remains not yet testable |
| Questions | v2 corpus-anchored generator and separate verifier produced | Run against the closed snapshot and independently pass all gold/leakage/split checks |
| Evaluation and aims | Deterministic SQLite/FTS harness, matched baselines/ablations, raw-trace and analysis contracts produced and fixture-tested | Complete 28,800-question release run against the closed snapshot and aim scorecard |
| Citations and reproduction | The current 137-claim/205-citation inventory has 158 citation passes, 47 citation failures and 40/44 joint-claim review passes; all 51 failures remain visible blockers with zero waivers. The lock-bound CycloneDX SBOM and bounded-snapshot clean-room replay are generated separately | Acquire claim-specific evidence and independent support review for the blockers, then regenerate against the unsampled closing ID and append the distinct, output-hash-bound `ACT-F2-RELEASE-SNAPSHOT-CITATION-REVIEWS-TERMINAL-001`; the existing pre-release terminal is deliberately insufficient |
| Usage and activity provenance | Four historical rows plus hash-chained v2 implementation, subagent, deterministic-run and pre-release citation/security terminal records; zero paid API use; source fallbacks, request-budget checkpoint and deterministic `release/provenance-validation.json` retained | Append the distinct final citation and security records so they supersede their pre-release terminals, append/supersede the other exact snapshot-bound terminal activities, close the shared request snapshot, then pass `check_provenance.py --require-release` against the unsampled T1 release ID |
| Rights and privacy | Bounded disk-backed audit scans 954 publication assets for `NEW-CHILD-20260715`: 475 classified items, zero body/credential findings, zero retention/secret violations and zero item-review triggers. Mechanical controls pass, but the sampled checkpoint is not a release rights determination | Rerun against the final T1 hydration manifest; retain final trigger counts/reviews and snapshot-bound evidence |
| Human evidence | Not authorised | Governed participant research; until then UI of choice is `not_yet_testable` |

## Machine-readable status

`governance/requirements.yaml` and `governance/traceability.json` are contract
projections. Their `accepted` value means “accepted as an obligation”, not
“implementation passed”. Current implementation state is generated from
`governance/implementation-status-source.json`:

- `governance/requirements-status.json` covers all 95 requirements;
- `governance/traceability-status.json` projects the least-advanced mapped
  requirement onto all 21 controlling clauses;
- `governance/task-status.json` covers all 36 task contracts.

Release identity, kind, checkpoint/candidate/release state and publication
readiness are derived exclusively from the jointly validated
`release/manifest.yaml` and `release/status.json` controls. They are not
declared a second time in the implementation source. The generator fails
closed on mixed IDs, schemas, snapshot kinds, sampled flags, readiness or
promotion-finalization state.

Regenerate and verify them with:

```sh
python3 scripts/build_status_projections.py
python3 scripts/build_status_projections.py --check
python3 scripts/check_lockstep.py
```

At this checkpoint no requirement, clause, task or release gate is represented
as passed. `produced` means that a foundation artefact exists; it does not mean
the task was promoted or independently verified.

At a machine candidate or finalized machine release, every requirement must
have a terminal `passed` or `blocked` disposition. The five requirements owned
by the directly human-gated `E3-01` contract (`REQ-069`, `REQ-070`, `REQ-073`,
`REQ-074` and `REQ-077`) remain blocked, so the exact machine disposition is 90
passed and five blocked. Every task outside the human-gated dependency closure
must be `accepted`; `E3-01` and its dependent full-programme tasks `F1-01`,
`F2-02` and `F2-03` remain `blocked` while participant research is not
authorised. Accepted tasks must collectively cite the exact declared candidate
or release terminal set. Those rows must pass the activity schema and hash
chain, have exact completion times, complete validation, final request usage,
non-pending hash-bound outputs and the declaration-required snapshot binding.
A full-programme release additionally requires the declared
`ACT-E3-FULL-PROGRAMME-TERMINAL-001`, all 95 requirements passed, all 36 tasks
accepted, finalized promotion and completed human evaluation. Traceability
clauses continue to show the least-advanced status of their mapped
requirements.

The source milestone is coupled to the manifest/status transition:
`t0_census_closed`, `full_corpus_checkpoint`, `machine_release_candidate`,
`machine_release_finalized` or `full_programme_complete`. A stale milestone
fails projection generation.

## Active blockers and constraints

- Full-corpus hydration and closing remain long-running public-source operations. The
  836,543 initial GOV.UK routes have a theoretical minimum of about 29 hours at
  8 Content API requests/s, before retries, linked discoveries and T1. ADR-004
  limits the deterministic rendered-link detector to 75,000 pages and preserves
  a 76,952-attempt lower-bound reserve for retries, discoveries and closing.
  The prior hydration job has been stopped; the bounded demonstrator does not
  imply that this full-release blocker has passed.
- The first exact T0 capacity build ran 44,342.28 seconds and failed closed on
  a 6,712,946-byte `ca` postings file. ADR-006 implements deterministic bounded
  physical partitions and its failure-shaped regression passes, but the exact
  T0 rerun must still prove all postings, lexicon, prefix, document-map and
  other shard distributions before capacity can be accepted.
- Search API v1 is unsupported and has no immutable cursor; sitemap coverage is
  “majority” rather than complete and its shards changed during audit. Opposing
  partition passes and byte-stable retries are mandatory.
- No public complete historical redirect/gone inventory was found. Instances
  discovered through admitted sources are represented; the residual public
  enumerability limit remains explicit.
- OGL does not automatically cover all third-party rights, logos, personal data
  or attachment contents. The release remains metadata-and-link first. The
  fixture audit has zero hard retention/credential findings; its two unresolved
  structural triggers are policy-controlled review work, not evidence that
  attachment bytes or third-party material were copied.
- The original Pirolli host still fails current safe TLS defaults. TLS was not
  weakened; the `DH_KEY_TOO_SMALL` result remains in the frozen source preflight
  and citation access-history ledger.
- The first secure fallback, ResearchGate, and its exact author PDF both returned
  HTTP 403 on 12 July 2026. No access-control bypass was attempted.
- The release now uses the strict-TLS [Crossref DOI record](https://api.crossref.org/works/10.1037%2F0033-295X.106.4.643)
  only for title, authors, journal, date, pages, publisher and DOI identity. It
  makes no section-level claim from inaccessible full text.
- External shard hosting, paid model API calls and participant research are not
  authorised. Absence of those authorities does not permit a hidden workaround.
- The Codex product does not expose the exact backend build, parameters, token
  counts or marginal product-session cost. Those fields remain unavailable;
  only external paid-model API calls/tokens/cost are exact zero. The open
  official-source counter is reported separately from model usage.
- The real-Chromium Explorer fixture gate now passes locally and in protected
  pull-request CI. It covers an automated accessibility subset, routes, range-
  packed data and performance budgets; it is not axe, expert, screen-reader or
  participant evidence. The full-snapshot release measurement remains pending.
- The pre-release security campaign is complete and its 17 findings are fixed,
  but the final hydrated release repository has not yet been scanned. The
  existing terminal is deliberately insufficient: the final scan must append
  `ACT-D2-RELEASE-SNAPSHOT-SECURITY-SCAN-TERMINAL-001` and supersede it. The
  checked-in release status therefore continues to record
  `security_scan_passed: false`; no fixture or earlier-revision result is
  promoted as final-snapshot evidence.
- The 171 citation and 40 joint-support review results are valid fixture-stage
  evidence, but their terminal names `fixture-2026-07-11`. Candidate and final
  provenance require a new
  `ACT-F2-RELEASE-SNAPSHOT-CITATION-REVIEWS-TERMINAL-001` row that supersedes
  the fixture terminal, names the exact closing ID and hash-binds the current
  citation verification JSON, Markdown report and request aggregate.

The authoritative checkpoint is `release/status.json`: machine RC is false,
full-corpus reconciliation is false, question/citation/clean-room gates are
false, the final security gate is false, human evaluation is `not_authorised`,
and programme completion is false.
