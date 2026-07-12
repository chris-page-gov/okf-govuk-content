# Persona and use-taxonomy saturation

The checked-in persona set now passes the **machine-applicable** part of
Acceptance Gate 5. It does not claim that the hypotheses have been validated
with people.

The baseline audit covered 48 primary archetypes and 16 reusable overlays. A
contract-first challenge found one genuine omission: the controlling plan
explicitly required privacy-sensitive context, but no such overlay existed. It
was added as `privacy-sensitive-context`, giving 17 current overlays. The
assignment to relevant personal-facts, delegated and hand-off contexts remains
`research_hypothesis_not_human_validated`; it is not a prevalence claim.

## Machine evidence

- `coverage-matrix.jsonl` gives every primary persona non-empty values for
  actor, goal, journey stage, content/service type, relationship need,
  jurisdiction, language, accessibility need, device/context, urgency/risk and
  agent involvement.
- `coverage-matrix.json` proves marginal coverage for all 11 dimensions and all
  83 pinned GOV.UK content-schema families, with no unexplained machine
  dimension gap.
- `overlay-covering-array.json` enumerates all 136 pairs across 17 overlays and
  five explicit high-risk three-way scenarios. These are test scenarios, not
  claims that combinations are common or compatible.
- `challenges/` contains three hash-bound passes. The first records and resolves
  the privacy and matrix gaps; two subsequent held-out method/input passes add
  zero new machine-evidenced use classes, satisfying the below-1% stopping
  rule.
- `saturation.json` binds the matrix, covering array and challenge pass hashes,
  retains residual gaps and records the human/preference boundary.
- `scripts/check_persona_saturation.py` independently verifies counts,
  checksums, evidence IDs, matrix coverage, pairwise/t-way scenarios, input
  hashes, stopping rule and claim boundaries.

The three passes are independent by method and input partition only. They were
produced in one repository implementation workflow and are not independent
human or model adjudication. Direct-observation saturation remains constrained
because participant research and query/support/contact data were not
authorised.

## Question-release boundary

The v2 question generator copies and hash-binds `saturation.json` and the
coverage matrix into every generated matrix, story and question. Its six
stories per persona cover six journey roles, and each story spans ten goal
operations by ten challenge modes. The final 28,800-question release must still
be regenerated against the reconciled closing snapshot and pass the separate
verifier. Checked-in development questions are not release evidence.

No human “UI of choice”, preference, prevalence or observed-behaviour claim is
made. Human validation remains `not_authorised_not_run`; UI preference remains
`not_yet_testable`.
