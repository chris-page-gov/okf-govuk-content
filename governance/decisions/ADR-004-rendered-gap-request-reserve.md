# ADR-004: Bound the T0 rendered-link detector at 75,000 pages

- Status: accepted
- Date: 2026-07-12
- Decision owner: programme controller
- Requirements: R-CORPUS-004, R-CORPUS-008, R-GOV-006

## Context

The authoritative T0 census contains 836,998 publication records: 836,543
`www.gov.uk` routes and 455 external-boundary records. Hydrating each GOV.UK
route requires at least one Content API attempt. The completed T0 census used
5,752 of the authorised 1,000,000 official-source attempts. A second complete
T1 census and closing work must remain inside that same programme ceiling.

The implementation's initial development default was a 150,000-page rendered
HTML sample. That number is not prescribed by the controlling brief, execution
contract, requirements register or implementation plan; those documents
require a bounded, disclosed rendered-link gap detector. Retaining the default
would require at least 992,296 attempts before retries, linked-only discoveries,
T1 or closing work and therefore could not preserve a credible completion
reserve.

## Decision

Freeze the T0 rendered-link detector at 75,000 deterministically selected
pages. Selection remains stratified: one minimum-hash record per
`document_type`/`schema_name`/locale stratum, followed by a global minimum-hash
sample. The released proof must publish the eligible population, selected and
unselected counts, strata count, selection digest, robots evidence, status
counts and exact request accounting.

The deterministic lower-bound allocation is:

| Work | Attempts reserved or observed |
|---|---:|
| Completed T0 census | 5,752 |
| Initial T0 Content API routes | 836,543 |
| T0 rendered pages | 75,000 |
| T0 robots check | 1 |
| Projected full T1 census at the T0 observed cost | 5,752 |
| Remaining for retries, structured/rendered discoveries and closing | 76,952 |

The shared counter and per-request limiters remain authoritative. If retries or
closure consume the reserve, acquisition checkpoints before the ceiling and no
release completeness claim is made.

## Consequences

- The detector is explicitly bounded and does not claim exhaustive HTML-link
  enumeration.
- The complete public metadata census remains in scope; only the optional
  rendered-link sampling density changes.
- Reproduction must invoke `scripts/hydrate_corpus.py T0-20260712
  --rendered-scan-limit 75000` and bind the frozen selection digest.
- Reports, provenance and release evidence must distinguish the full structured
  census from this bounded rendered detector.
