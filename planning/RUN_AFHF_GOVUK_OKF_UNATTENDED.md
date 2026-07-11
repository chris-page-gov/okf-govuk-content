# Execute the AF/HF GOV.UK OKF programme unattended

Use this file as the top-level mission prompt for the programme controller or supervising coding agent. It is an execution instruction, not a request to produce another plan.

## Controlling inputs

Read, checksum and register these inputs before changing code or generating research:

1. `WHATS_ON_GOVUK_OKF.md` — the controlling goal and bundle brief;
2. `AFHF_GOVUK_OKF_REQUIREMENTS_REGISTER.md` — 95 atomic requirements and 11 acceptance gates;
3. `AFHF_GOVUK_OKF_BRIEF_TRACEABILITY.md` — populated brief/prompt/scope-decision to requirement crosswalk;
4. `AFHF_GOVUK_OKF_SOTA_RESEARCH_IMPLEMENTATION_PLAN.md` — research method, dependency graph, artefact contracts, model routing, evaluation and Definition of Done.

If they conflict, apply this precedence:

1. explicit legal, security, robots, licence or platform constraint;
2. the user's latest instruction that Release 1 includes the complete bounded public GOV.UK corpus;
3. the requirements register's operational definitions;
4. the implementation plan;
5. the original brief.

Record every conflict and resolution as an ADR. Never resolve a material scope conflict silently.

## Mission

Build, test and evaluate the Agent First, Human Friendly discovery layer described by the controlling inputs in the independent `chris-page-gov/okf-govuk-content` repository. Execute the dependency-aware programme from G0 through F2. Do not stop after scaffolding, a representative fixture, a design, a report or a sample crawl.

Release 1 must contain the complete, snapshot-bounded public metadata corpus for the declared `www.gov.uk` publishing-estate boundary, plus discoverable attachment metadata, public machine representations, redirects/lifecycle records and boundary links. A small representative fixture is permitted only for pre-release pipeline and Explorer testing. Full page bodies remain outside the release unless a later authorised decision changes that boundary.

## Start without clarification

Apply these defaults so that unattended execution can begin:

- public, reproducible GOV.UK interfaces only;
- no authenticated or internal source as a hidden release dependency;
- `www.gov.uk` plus associated public assets/representations, not every independently operated `*.gov.uk` domain;
- canonical metadata and on-demand authoritative source retrieval, not a body mirror;
- one globally coordinated rate limiter per host;
- every approved story has exactly 100 deterministic question instances;
- every approved primary persona has exactly 100 curated, independently verified questions covering all of its stories;
- machine work continues around human-only gates;
- genuine participant research is required before claiming Explorer is a “Human UI of choice” for any named population/task;
- a negative or partial conclusion is a valid completion result.

First run deterministic discovery and volume/cost projection. Before paid fan-out, require a signed launch manifest containing `max_model_cost`, `max_wall_time`, token/request/storage ceilings, provider and data-region allowlists, per-host rate ceilings, external-storage permission, authenticated-source permission, human-study authorisation and publication authorisation. Missing authority blocks only the dependent queue and produces a checkpoint; “unattended” never implies unlimited spend, internal access, participant research or public release.

## Bootstrap

Create the repository structure specified in section 16 of the implementation plan. Materialise, at minimum:

- `governance/requirements.yaml`, decisions, risks and exceptions;
- `orchestration/dag.yaml`, task contracts, schemas, budgets and model lock;
- append-only run events and immutable attempt directories;
- research, semantic, corpus, persona, story, question, Explorer, evaluation, provenance, report and release areas;
- CI for schema, unit, integration, accessibility, security, reproducibility, citation and full-corpus gates.

Import every requirement and gate before opening implementation work. Reject any task that cannot trace to at least one requirement, research question or evidence-remediation item.

## Controller rules

Implement a deterministic, durable scheduler with the state machine:

`queued -> leased -> running -> validating -> accepted`

and terminal/exception states `retryable`, `blocked`, `escalated`, `failed` and `superseded`.

For every task:

1. resolve and verify input hashes;
2. reserve request, token, money and wall-time budgets;
3. use an isolated attempt directory and, for code, an isolated branch/worktree;
4. supply only a bounded evidence packet and allowed-source policy;
5. require typed output and claim/evidence IDs;
6. run deterministic validation before model review;
7. accept and promote only passing artefacts;
8. write events, usage, tool versions, model identity, prompts, evidence and validation results;
9. retry only under the declared policy;
10. checkpoint so an interrupted run resumes idempotently.

Do not expose secrets, private reasoning or unnecessary participant data. Record concise decision rationales and evidence instead.

## Execution order and safe parallelism

Execute the plan DAG exactly unless a recorded ADR proves a change is necessary:

1. **G0:** freeze charter, boundaries, budgets, DAG and success contract.
2. **A1/A2/A3 in parallel:** audit official sources; conduct SOTA/comparator research; preregister evaluation.
3. **B1/B2 in parallel, then B3 with their evidence:** freeze the corpus census; define the semantic profile; saturate the use ontology and persona set.
4. **C1/C2:** compile stories and both 100-question contracts; build the representative fixture and precise Explorer/agent requirements.
5. **D1/D2/D3 in parallel:** hydrate and compile the complete corpus; implement Explorer and agent surfaces; implement baseline/evaluation harnesses.
6. **E1/E2/E3:** close the T1 delta and reconcile; run paired agent evaluation; run only authorised human research.
7. **F1/F2:** independent red-team and aim-by-aim assessment; verify every artefact, claim, citation and release gate.

Partition independent research by claim/comparator family, personas by actor/journey family, questions by story hash and corpus work by canonical identifier hash. Share source rate limits, schemas, source manifests and immutable evidence; do not let workers overwrite common canonical files.

## Model routing

Calibrate current available models on a representative task set and pin exact identifiers, versions and parameters in `models.lock.yaml`. Route by capability rather than brand name:

- deterministic code: enumeration, canonicalisation, counts, hashes, schemas, set reconciliation, metric calculation and URL mechanics;
- economical structured generation: templated question bindings and low-risk normalisation;
- balanced research/coding: bounded source audit, persona/story drafting, implementation and ordinary review;
- frontier/deep research: ontology disputes, cross-source synthesis, architecture and material adjudication;
- independent reviewer configuration or provider: red-team, semantic citation support and final conclusions;
- human/domain expert: unresolved high-risk facts, real-user research and release acceptance.

Never let the same model/run be the sole generator and judge. Escalate only failed or disputed units, not entire accepted batches.

## Corpus gate

At T0, freeze and hash the union of verified official public enumerators. At T1, re-enumerate and apply a closing delta. Each admitted candidate must end in exactly one public disposition:

- represented canonical record;
- redirect/replacement relation;
- gone/withdrawn/tombstone record;
- explicit, evidenced constraint or exception record.

The release gate, evaluated separately for every entity class, is:

`expected_candidate_keys = represented + alias_of_represented + redirect_only + tombstone_only + exceptioned`

with `unexplained_omissions = 0`. Constraints are annotations except where a candidate is genuinely `exceptioned`; publish exception rates/reasons so an accounted-for failure cannot be described as represented coverage.

Counts, set differences, source watermarks, source versions, checksums and drift must be published. Do not label the corpus complete without that evidence.

## Research and citation gate

Use primary official or peer-reviewed sources for material claims. Record searches, inclusion/exclusion decisions, retrieval dates and contrary evidence. Give each claim–source link the strongest stable locator available: commit/path/lines, dated specification section, PDF page/section, HTML heading plus paragraph/text fingerprint or API JSON Pointer.

Before release, independently verify every citation for:

- reachable requested and final URL, redirects and MIME type;
- correct publisher/title/version or commit;
- locator existence and excerpt fingerprint;
- semantic support for the exact claim;
- numbers, dates and named entities;
- agreement between inline citation and generated bibliography.

A material failed citation blocks its dependent conclusion unless a dated, owned waiver is published.

## Evaluation gate

Freeze question manifests and gold evidence before implementation tuning. Run proposal and baselines against the same snapshot, model, prompts and resource budgets. Retain raw traces and paired results.

Report separately:

- discovery versus authoritative-source retrieval/answering;
- human versus agent effectiveness;
- effectiveness versus efficiency;
- source-native versus inferred semantics;
- overall versus accessibility, language, jurisdiction, lifecycle, risk and long-tail strata.

Use confidence intervals and failure analysis. Do not infer human preference from synthetic users or agent results. Mark the human-dependent claim `not_yet_testable` if the authorised participant study has not completed.

## Failure, pause and escalation

Fail closed only on the affected branch for rights, robots, access, security, corpus, citation or evidence uncertainty. Continue all safe independent work. Never bypass a policy boundary, invent a source, silently narrow scope or claim a human result.

Escalate only the decision that genuinely needs human authority:

- authenticated/internal data access or ambiguous reuse rights;
- human-study ethics, recruitment, consent or participant retention;
- external immutable shard hosting if repository/Pages capacity is insufficient;
- final publication and acceptance of residual risk.

An escalation packet must contain the decision, evidence, options, consequences, recommendation and work that continued meanwhile.

## Required final outputs

Produce all canonical artefacts in the repository structure, plus:

1. a public-source registry and constraint ledger;
2. T0/T1 corpus inventories, reconciliation, drift and full-corpus manifests;
3. the versioned GOV.UK OKF profile, YAML-LD, semantically equivalent JSON-LD, shapes and crosswalks;
4. human Markdown, Explorer descriptor, search/adjacency shards and registry entry;
5. an evidenced use ontology, personas, stories and complete question/gold corpus;
6. implemented Explorer and read-only agent discovery surfaces;
7. baseline adapters and reproducible agent/human evaluation artefacts;
8. aim-by-aim scorecard with `fulfilled`, `partly_fulfilled`, `not_fulfilled` or `not_yet_testable`, confidence, contrary evidence and limitations;
9. complete verified bibliography and citation-verification report;
10. requirements trace, checksums, SBOM, risk/exception/waiver ledgers and reproducibility report.

## Stop condition

Do not declare completion because tasks ran or a report exists. If the machine work, agent evaluation, citations and clean-room reproduction pass while the authorised human study is absent, emit only the machine marker below. `human_evaluation_status` must be one of `not_authorised`, `blocked`, `not_yet_testable` or `completed`; this example uses one concrete value:

```json
{
  "completion_statement": "AFHF_GOVUK_OKF_MACHINE_RELEASE_CANDIDATE_V1",
  "machine_rc_complete": true,
  "full_evaluation_complete": false,
  "agent_evaluation_status": "completed",
  "human_evaluation_status": "not_yet_testable",
  "human_ui_of_choice_status": "not_yet_testable",
  "full_corpus_reconciled": true,
  "unexplained_omissions": 0,
  "semantic_validation_passed": true,
  "question_contract_passed": true,
  "citation_verification_passed": true,
  "clean_room_reproduction_passed": true,
  "programme_complete": false
}
```

After E3, F1 and clean-room F2 have completed, and only then, the verifier may emit:

```json
{
  "completion_statement": "AFHF_GOVUK_OKF_RESEARCH_IMPLEMENTATION_COMPLETE_V1",
  "machine_rc_complete": true,
  "full_evaluation_complete": true,
  "agent_evaluation_status": "completed",
  "human_evaluation_status": "completed",
  "aims_assessed": true,
  "citation_verification_passed": true,
  "clean_room_reproduction_passed": true,
  "programme_complete": true
}
```

A human outcome may be unfavourable; completion requires that it was genuinely evaluated, not that Explorer won. If a required field is false, emit a checkpoint and machine-readable blocker/exception report. Do not substitute a favourable narrative for a failed gate.
