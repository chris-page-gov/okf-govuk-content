# Implementation status

- Status date: 13 July 2026
- Milestone: T0 census closed
- Publication ready: **no**

The repository has a working deterministic fixture pipeline and the main
release-oriented implementations, but it has not completed the full-corpus,
evaluation, citation or full-snapshot clean-room gates. The fixture bundle is useful for
development and review; it is not evidence that Release 1 covers GOV.UK.

## Current evidence

| Area | Current state | What remains before machine RC |
|---|---|---|
| Contract and controller | 95 requirements, 11 gates and 36 task contracts imported; controller checks pass | Accepted run evidence and terminal requirement dispositions |
| Official sources | Dated 32-source and 93-URL plan preflight retained; T0 closed from 137 opposing-pass Search partitions, 35 byte-stable sitemap shards and closed organisations/navigation | T0 hydration, T1 union closure and final rights/citation snapshot binding |
| Semantic profile | YAML-LD profile, JSON schemas, crosswalks, shapes and JSON-LD fixture projection produced | Release-snapshot equivalence, integrity and provenance evidence |
| Bundle and Explorer | 14-record fixture, static search, route index, adjacency, query/hash replay, Pages fallback, passing local/protected-CI real-Chromium gate and read-only CLI produced | Axe, accessibility-expert and screen-reader review, full hydrated corpus and measured full-snapshot accessibility/security/performance gates |
| Security | Completed repository-wide scan recorded 14 findings; its remediation diff scan confirmed those fixes and found three residual low issues, all fixed at `27890dc`; 32 focused post-fix tests pass | Run and hash-bind a new full-repository scan after the hydrated release snapshot and publication artefacts are frozen; `security_scan_passed` remains false |
| Census and hydration | Unsampled T0 closed at 848,977 candidates and 836,998 publication records; 848,971 represented plus six redirects, with zero exceptions/unexplained omissions; every shard digest passes; the resumable checkpoint now enforces the authorised 10 GiB retained-metadata ceiling, durably spools successful responses, admits all writes before mutation and rejects duplicate source identities/path escapes | Complete the prepared hydration queue, re-enumerate T1, close drift and retain zero unexplained omissions |
| Personas and stories | Machine-applicable saturation passes for 48 primary persona hypotheses, 17 overlays, 11 dimensions, 83 schema families, 136 overlay pairs, five high-risk t-way scenarios and two successive no-new challenge passes | Authorised human validation and final-snapshot regeneration of six release-v2 stories per persona; UI preference remains not yet testable |
| Questions | v2 corpus-anchored generator and separate verifier produced | Run against the closed snapshot and independently pass all gold/leakage/split checks |
| Evaluation and aims | Deterministic SQLite/FTS harness, matched baselines/ablations, raw-trace and analysis contracts produced and fixture-tested | Complete 28,800-question release run against the closed snapshot and aim scorecard |
| Citations and reproduction | 171/171 citations and 40/40 joint claims verified with zero waivers/blockers; lock-bound CycloneDX SBOM and byte-exact fixture rebuild produced | Regenerate the citation release evidence for the unsampled closing ID and append the distinct, output-hash-bound `ACT-F2-RELEASE-SNAPSHOT-CITATION-REVIEWS-TERMINAL-001`; the existing fixture terminal is deliberately insufficient; then run the full-snapshot clean-room replay |
| Usage and activity provenance | Four historical rows plus hash-chained v2 implementation, subagent, deterministic-run and pre-release citation/security terminal records; zero paid API use; source fallbacks, request-budget checkpoint and deterministic `release/provenance-validation.json` retained | Append the distinct final citation and security records so they supersede their pre-release terminals, append/supersede the other exact snapshot-bound terminal activities, close the shared request snapshot, then pass `check_provenance.py --require-release` against the unsampled T1 release ID |
| Rights and privacy | Bounded disk-backed fixture audit scans 745 publication data assets with zero body/credential findings and records 2 conservative hashed item-review triggers | Rerun against the final T1 hydration manifest; retain final trigger counts/reviews and snapshot-bound evidence |
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

Regenerate and verify them with:

```sh
python3 scripts/build_status_projections.py
python3 scripts/build_status_projections.py --check
python3 scripts/check_lockstep.py
```

At this checkpoint no requirement, clause, task or release gate is represented
as passed. `produced` means that a foundation artefact exists; it does not mean
the task was promoted or independently verified.

## Active blockers and constraints

- Hydration and closing remain long-running public-source operations. The
  836,543 initial GOV.UK routes have a theoretical minimum of about 29 hours at
  8 Content API requests/s, before retries, linked discoveries and T1. ADR-004
  limits the deterministic rendered-link detector to 75,000 pages and preserves
  a 76,952-attempt lower-bound reserve for retries, discoveries and closing.
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
