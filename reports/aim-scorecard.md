# AF/HF GOV.UK OKF aim scorecard

- Assessment: `aim-assessment-fixture-2026-07-11`
- Snapshot: `fixture-2026-07-11` (`fixture`, sampled: `true`)
- Assessment tier: `fixture_checkpoint`
Acceptance Gate 11: `pending`

This is a non-compensatory assessment. A fixture or design artefact can support partial fulfilment, but it cannot substitute for closing full-corpus, semantic, evaluation, citation, rights or reproduction evidence. An unfavourable result is valid. Human preference remains untestable without genuine participant evidence.

## Summary

| Aim | Status | Confidence | Strongest current evidence |
|---|---|---|---|
| AIM-001 Independent OKF Bundle Wiki mapping GOV.UK | `partly_fulfilled` | `high` | `E-BUNDLE-DESCRIPTOR`, `E-INDEPENDENT-WIKI` |
| AIM-002 Whole bounded public GOV.UK metadata coverage | `partly_fulfilled` | `high` | `E-SOURCE-PREFLIGHT` |
| AIM-003 Help people navigate and understand GOV.UK | `partly_fulfilled` | `high` | `E-EXPLORER-FOUNDATION`, `E-EXPLORER-NONBROWSER-TESTS`, `E-PERSONA-FOUNDATION` |
| AIM-004 Become a Human UI of choice for defined populations and tasks | `not_yet_testable` | `high` | `E-HUMAN-NOT-AVAILABLE` |
| AIM-005 Help systems understand GOV.UK identities, hierarchy, lifecycle and relationships | `partly_fulfilled` | `high` | `E-PROFILE`, `E-SEMANTIC-FIXTURE`, `E-STATIC-SEARCH` |
| AIM-006 Help agents retrieve and cite authoritative GOV.UK content | `partly_fulfilled` | `high` | `E-EVALUATION-HARNESS` |
| AIM-007 Compare source metadata, public presentation and search or discovery behaviour | `partly_fulfilled` | `high` | `E-SOURCE-PREFLIGHT`, `E-COMPARATOR-REPORT` |
| AIM-008 Establish a reusable semantic and provenance layer | `partly_fulfilled` | `high` | `E-PROFILE`, `E-SEMANTIC-FIXTURE` |
| AIM-009 Transparent, unattended and reproducible execution | `partly_fulfilled` | `high` | `E-ACTIVITY-LEDGER`, `E-REQUIREMENT-COVERAGE`, `E-TRACEABILITY-COVERAGE`, `E-CLEAN-ROOM-FIXTURE` |

## Gate 11

Gate 11 remains fail-closed because the assessment is not yet bound to every required closing full-snapshot evidence result.

Unmet final-snapshot checks: `E-SNAPSHOT-FULL`, `E-SNAPSHOT-UNSAMPLED`, `E-FULL-CORPUS`, `E-ZERO-OMISSIONS`, `E-SEMANTIC-RELEASE`, `E-QUESTIONS-RELEASE`, `E-AGENT-EVALUATION`, `E-CITATIONS-RELEASE`, `E-CLEAN-ROOM-RELEASE`, `E-CHECKSUMS-RELEASE`, `E-RIGHTS-RELEASE`.

## AIM-001 — Independent OKF Bundle Wiki mapping GOV.UK

Status: `partly_fulfilled`. Confidence: `high`.

Publish a derived, non-authoritative, independently loadable federated bundle that maps the declared GOV.UK metadata boundary.

Boundary: A fixture demonstrates the pipeline but cannot fulfil an independent full release.

Evidence:

- `E-BUNDLE-DESCRIPTOR` — pass; `bundle/okf-explorer.json`; SHA-256 `27cba19217951c37dabe61f9f2aeb1485661d646d2a8088b6719e8b23abf918c`; observed `true`.
- `E-INDEPENDENT-WIKI` — pass; `bundle/okf-explorer.json/description`; SHA-256 `27cba19217951c37dabe61f9f2aeb1485661d646d2a8088b6719e8b23abf918c`; observed `"Derived, non-authoritative semantic catalogue of GOV.UK content, navigation, organisations, taxonomies and relationships."`.
- `E-SNAPSHOT-FULL` — not met; `release/manifest.yaml/snapshot/kind`; SHA-256 `a61235debedef4437ab9b5670effc00b0625f32f71708d3034ee28e2a838c9a2`; observed `"fixture"`.
- `E-SNAPSHOT-UNSAMPLED` — not met; `release/manifest.yaml/snapshot/sampled`; SHA-256 `a61235debedef4437ab9b5670effc00b0625f32f71708d3034ee28e2a838c9a2`; observed `true`.
- `E-CHECKSUMS-RELEASE` — not met; `release/status.json/checksum_validation_passed`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `false`.
- `E-CLEAN-ROOM-RELEASE` — not met; `release/status.json/clean_room_reproduction_passed`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `false`.

Negative findings and limitations:

- The bundle is independently published but remains derived and non-authoritative; GOV.UK is the authoritative destination.
- The active release artefacts are bound to a fixture rather than a full-corpus snapshot.
- Full release checksum validation has not passed.
- Only fixture reproduction, not full-snapshot clean-room reproduction, has passed.

Next actions:

- Build and bind the publication artefacts to the closing unsampled full-corpus snapshot.
- Reproduce the full release from pinned frozen inputs before publication.

## AIM-002 — Whole bounded public GOV.UK metadata coverage

Status: `partly_fulfilled`. Confidence: `high`.

Close the declared T0/T1 union with one disposition per candidate and zero unexplained omissions; this is not a claim about every *.gov.uk site or complete page bodies.

Boundary: A source audit or sampled capacity run is supporting evidence only, not corpus closure.

Evidence:

- `E-SOURCE-PREFLIGHT` — pass; `research/source-preflight.json/summary/official_failed`; SHA-256 `ffd1ba217dab2a247032f328ebe0a7124f1764e2d38e9c309f6271fccd055ca6`; observed `0`.
- `E-SNAPSHOT-FULL` — not met; `release/manifest.yaml/snapshot/kind`; SHA-256 `a61235debedef4437ab9b5670effc00b0625f32f71708d3034ee28e2a838c9a2`; observed `"fixture"`.
- `E-SNAPSHOT-UNSAMPLED` — not met; `release/manifest.yaml/snapshot/sampled`; SHA-256 `a61235debedef4437ab9b5670effc00b0625f32f71708d3034ee28e2a838c9a2`; observed `true`.
- `E-FULL-CORPUS` — not met; `release/status.json/full_corpus_reconciled`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `false`.
- `E-ZERO-OMISSIONS` — not met; `release/status.json/unexplained_omissions`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `null`.
- `E-RECONCILIATION-ARTEFACT` — not met; `release/manifest.yaml/artifacts/reconciliation`; SHA-256 `a61235debedef4437ab9b5670effc00b0625f32f71708d3034ee28e2a838c9a2`; observed `null`.
- `E-RIGHTS-RELEASE` — not met; `release/rights-privacy-audit.json/rights_privacy_audit_passed`; SHA-256 `3e12c456c5a59884446bb868df70f80ff46852cfb54a824c73d56b0e50562134`; observed `false`.

Negative findings and limitations:

- Completeness is bounded to the declared public-source union and cannot prove that an unknown, non-enumerated item does not exist.
- The active release artefacts are bound to a fixture rather than a full-corpus snapshot.
- The frozen T0/T1 source union has not been reconciled as a full corpus.
- Zero unexplained omissions has not been established for the full source union.
- The rights/privacy audit is a sampled checkpoint and has not passed for the final snapshot.

Next actions:

- Complete T0 hydration, T1 closure and per-class reconciliation.
- Run and resolve the rights/privacy review against the final unsampled snapshot.

## AIM-003 — Help people navigate and understand GOV.UK

Status: `partly_fulfilled`. Confidence: `high`.

Provide an accessible discovery surface and establish human task benefit with genuine evidence, while retaining GOV.UK as the authoritative destination.

Boundary: Fixture UI and automated checks support only partial fulfilment without completed participant evidence.

Evidence:

- `E-EXPLORER-FOUNDATION` — pass; `explorer/src/index.html`; SHA-256 `32def87a1256763faa51bc71ceefd455e8c4fcabb56a79c12f8c432c783d5a83`; observed `true`.
- `E-EXPLORER-NONBROWSER-TESTS` — pass; `explorer/src/evidence/fixture-browser.json/completed_non_browser_checks/failed`; SHA-256 `c495d18b049c05c9885a8c59e8cec5285f664695d2bacd3c85a80e50f81fc798`; observed `0`.
- `E-PERSONA-FOUNDATION` — pass; `personas/manifest.json`; SHA-256 `8c2ae8a35a708e2f1cf59423cee78befddec239e0227396d71f8d5b7220111ab`; observed `true`.
- `E-HUMAN-COMPLETE` — not met; `release/status.json/human_evaluation_status`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `"not_authorised"`.
- `E-HUMAN-AIM-FULFILLED` — not met; `release/status.json/human_ui_of_choice_status`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `"not_yet_testable"`.
- `E-HUMAN-AIM-PARTLY` — not met; `release/status.json/human_ui_of_choice_status`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `"not_yet_testable"`.
- `E-HUMAN-AIM-FAILED` — not met; `release/status.json/human_ui_of_choice_status`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `"not_yet_testable"`.

Negative findings and limitations:

- Metadata discovery cannot replace reading authoritative guidance or completing a government transaction.
- No authorised participant study has completed; synthetic or agent evidence cannot substitute.
- No completed participant evidence establishes the preferred-human-UI outcome.

Applicable exceptions:

- `EXC-HUMAN-001` — `governance/exceptions.yaml`; SHA-256 `753a13e949ba536b2d824993d76c7c96dfbab91c137c00d33b9ba96397ef51be`.

Next actions:

- After authority and ethics approval, run the preregistered accessible participant study.
- Retain task failures and report a partial or negative human outcome if that is what the evidence shows.

## AIM-004 — Become a Human UI of choice for defined populations and tasks

Status: `not_yet_testable`. Confidence: `high`.

Make a preference/effectiveness conclusion only for preregistered populations and tasks after genuine accessible participant research.

Boundary: Synthetic, automated, expert-only or agent evidence cannot satisfy this aim.

Evidence:

- `E-HUMAN-NOT-AVAILABLE` — pass; `release/status.json/human_evaluation_status`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `"not_authorised"`.
- `E-HUMAN-COMPLETE` — not met; `release/status.json/human_evaluation_status`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `"not_authorised"`.
- `E-HUMAN-AIM-FULFILLED` — not met; `release/status.json/human_ui_of_choice_status`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `"not_yet_testable"`.
- `E-HUMAN-AIM-PARTLY` — not met; `release/status.json/human_ui_of_choice_status`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `"not_yet_testable"`.
- `E-HUMAN-AIM-FAILED` — not met; `release/status.json/human_ui_of_choice_status`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `"not_yet_testable"`.

Negative findings and limitations:

- Any preferred-UI result is population- and task-specific, not a universal preference claim.
- No authorised participant study has completed; synthetic or agent evidence cannot substitute.
- No completed participant evidence establishes the preferred-human-UI outcome.

Applicable exceptions:

- `EXC-HUMAN-001` — `governance/exceptions.yaml`; SHA-256 `753a13e949ba536b2d824993d76c7c96dfbab91c137c00d33b9ba96397ef51be`.

Next actions:

- Obtain human-study authority and complete the preregistered participant comparison.

## AIM-005 — Help systems understand GOV.UK identities, hierarchy, lifecycle and relationships

Status: `partly_fulfilled`. Confidence: `high`.

Expose source-native entity distinctions and evidence-bearing typed relationships through validated machine-readable artefacts.

Boundary: Passing fixture semantics demonstrates the model, not complete full-corpus system coverage.

Evidence:

- `E-PROFILE` — pass; `semantic/profile/govuk-okf-profile-v1.yamlld`; SHA-256 `897430cb44849f3b6e96d8f7ad9b7f9f5242b4f3fcd0df4ec445f55d5221daf8`; observed `true`.
- `E-SEMANTIC-FIXTURE` — pass; `release/semantic-validation.json/passed`; SHA-256 `46c36354ca25306d700c0339e73f78545f302e8219b7a0b5ef05d7f07f1d10a3`; observed `true`.
- `E-STATIC-SEARCH` — pass; `bundle/data/search/manifest.json`; SHA-256 `5324e944efc26b9c775de0b8a65f9ac10e00efc75081b1dc3c719c063ac37574`; observed `true`.
- `E-SNAPSHOT-FULL` — not met; `release/manifest.yaml/snapshot/kind`; SHA-256 `a61235debedef4437ab9b5670effc00b0625f32f71708d3034ee28e2a838c9a2`; observed `"fixture"`.
- `E-FULL-CORPUS` — not met; `release/status.json/full_corpus_reconciled`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `false`.
- `E-SEMANTIC-RELEASE` — not met; `release/status.json/semantic_validation_passed`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `false`.

Negative findings and limitations:

- Public metadata cannot expose source-native edition or internal publishing detail that GOV.UK does not publish.
- The active release artefacts are bound to a fixture rather than a full-corpus snapshot.
- The frozen T0/T1 source union has not been reconciled as a full corpus.
- Semantic validation has passed only for the fixture, not the final full snapshot.

Next actions:

- Run JSON Schema, SHACL, RDFC equivalence and referential checks across every final release shard.

## AIM-006 — Help agents retrieve and cite authoritative GOV.UK content

Status: `partly_fulfilled`. Confidence: `high`.

Demonstrate metadata discovery, typed traversal, authoritative hand-off, citation correctness and abstention under matched frozen conditions.

Boundary: This is not a claim that the metadata layer itself answers substantive body-content questions.

Evidence:

- `E-EVALUATION-HARNESS` — pass; `scripts/run_evaluation.py`; SHA-256 `7d263dd599fd9ca36df60e7268edf77ed335ecb7ca50c97c0fb5338c04cf67c6`; observed `true`.
- `E-QUESTIONS-RELEASE` — not met; `release/status.json/question_contract_passed`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `false`.
- `E-AGENT-EVALUATION` — not met; `release/status.json/agent_evaluation_status`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `"not_started"`.
- `E-MACHINE-RESULTS` — not met; `evaluation/results/status.json/agent_evaluation_status`; SHA-256 `0d4ac6a8567ad9d7455bcf9d31bf631972a8fdae75f8125238ca4beaace6dc55`; observed `null`.
- `E-CITATIONS-RELEASE` — not met; `release/status.json/citation_verification_passed`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `false`.
- `E-PAIRED-COMPARISONS` — not met; `evaluation/results/paired-comparisons.json`; SHA-256 `missing`; observed `false`.
- `E-AGENT-AIM-FULFILLED` — not met; `evaluation/results/aim-findings.json/aim_statuses/AIM-006/status`; SHA-256 `missing`; observed `null`.
- `E-AGENT-AIM-FAILED` — not met; `evaluation/results/aim-findings.json/aim_statuses/AIM-006/status`; SHA-256 `missing`; observed `null`.

Negative findings and limitations:

- The layer supports discovery and evidence hand-off; substantive answers still require retrieval from authoritative GOV.UK content.
- The independently verified release-v2 question contract has not passed.
- The complete release question suite has not produced a completed machine evaluation.
- No release-bound paired comparison artefact is present.
- No independent final finding establishes that matched agent effectiveness and efficiency fulfilled the aim.

Applicable exceptions:

- `EXC-MODEL-001` — `governance/exceptions.yaml`; SHA-256 `753a13e949ba536b2d824993d76c7c96dfbab91c137c00d33b9ba96397ef51be`.

Next actions:

- Run every frozen release-v2 question against every matched machine system.
- Issue an independent H-03 disposition from the paired effectiveness, efficiency and failure evidence; retain an unfavourable result.

## AIM-007 — Compare source metadata, public presentation and search or discovery behaviour

Status: `partly_fulfilled`. Confidence: `high`.

Publish reproducible comparator evidence and matched behaviour comparisons without fabricating unavailable external-system results.

Boundary: Architecture and source comparison without complete matched runs is partial fulfilment.

Evidence:

- `E-SOURCE-PREFLIGHT` — pass; `research/source-preflight.json/summary/official_failed`; SHA-256 `ffd1ba217dab2a247032f328ebe0a7124f1764e2d38e9c309f6271fccd055ca6`; observed `0`.
- `E-COMPARATOR-REPORT` — pass; `reports/comparators.md`; SHA-256 `06d09e323647631bfa4e2e639e700d8bc85aec9699ec7ee43d063a541e78bb1b`; observed `true`.
- `E-PAIRED-COMPARISONS` — not met; `evaluation/results/paired-comparisons.json`; SHA-256 `missing`; observed `false`.
- `E-CITATIONS-RELEASE` — not met; `release/status.json/citation_verification_passed`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `false`.
- `E-AGENT-EVALUATION` — not met; `release/status.json/agent_evaluation_status`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `"not_started"`.

Negative findings and limitations:

- Internal or unavailable comparators cannot be represented as matched empirical results without authorised reproducible access.
- No release-bound paired comparison artefact is present.
- The complete release question suite has not produced a completed machine evaluation.
- The release-bound citation gate has not passed.

Next actions:

- Publish the frozen paired comparisons and preserve explicit exclusions for unavailable external systems.

## AIM-008 — Establish a reusable semantic and provenance layer

Status: `partly_fulfilled`. Confidence: `high`.

Provide portable source-native semantics, equivalent YAML-LD and JSON-LD, assertion provenance and stable static distribution for later consumers.

Boundary: Internal validation proves conformance; full reuse requires independent-consumer evidence.

Evidence:

- `E-PROFILE` — pass; `semantic/profile/govuk-okf-profile-v1.yamlld`; SHA-256 `897430cb44849f3b6e96d8f7ad9b7f9f5242b4f3fcd0df4ec445f55d5221daf8`; observed `true`.
- `E-SEMANTIC-FIXTURE` — pass; `release/semantic-validation.json/passed`; SHA-256 `46c36354ca25306d700c0339e73f78545f302e8219b7a0b5ef05d7f07f1d10a3`; observed `true`.
- `E-SEMANTIC-RELEASE` — not met; `release/status.json/semantic_validation_passed`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `false`.
- `E-CLEAN-ROOM-RELEASE` — not met; `release/status.json/clean_room_reproduction_passed`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `false`.
- `E-PORTABILITY-AIM-FULFILLED` — not met; `release/portability-validation.json/aim_status`; SHA-256 `missing`; observed `null`.
- `E-PORTABILITY-AIM-FAILED` — not met; `release/portability-validation.json/aim_status`; SHA-256 `missing`; observed `null`.

Negative findings and limitations:

- Profile extensions exceed the minimal OKF base and require consumers to support the declared versioned profile.
- Semantic validation has passed only for the fixture, not the final full snapshot.
- Only fixture reproduction, not full-snapshot clean-room reproduction, has passed.
- No independent-consumer portability result establishes full reuse beyond Explorer.

Next actions:

- Validate the released profile with an independent consumer that does not depend on Explorer-specific code.

## AIM-009 — Transparent, unattended and reproducible execution

Status: `partly_fulfilled`. Confidence: `high`.

Make deterministic work, evidence, exceptions, usage, checks and final dispositions inspectable and reproducible from pinned inputs.

Boundary: A reproducible fixture and implemented controller are partial until the final full release reproduces and every requirement is dispositioned.

Evidence:

- `E-ACTIVITY-LEDGER` — pass; `provenance/activity-ledger.jsonl`; SHA-256 `cb20de37494a2ffe18ecec765a79160706b544d9f27b28c9627542cee21666d8`; observed `true`.
- `E-REQUIREMENT-COVERAGE` — pass; `governance/requirements-status.json/counts/requirements`; SHA-256 `1334f700720e9d08149c9b8d8837c22e828a3c1f72c1922cd684a58fe8462927`; observed `95`.
- `E-TRACEABILITY-COVERAGE` — pass; `governance/traceability-status.json/counts/clauses`; SHA-256 `8a1ad43f937fc0713ec64d0629dfd8bdb4ae9a84cbe91c86e3d504a7baf17d18`; observed `21`.
- `E-CLEAN-ROOM-FIXTURE` — pass; `release/clean-room-reproduction.json/fixture_reproduction_passed`; SHA-256 `eddbaa12089bf74eba68905083926fc4058f9d61561a8ab285d8dcee168310f6`; observed `true`.
- `E-SNAPSHOT-FULL` — not met; `release/manifest.yaml/snapshot/kind`; SHA-256 `a61235debedef4437ab9b5670effc00b0625f32f71708d3034ee28e2a838c9a2`; observed `"fixture"`.
- `E-CLEAN-ROOM-RELEASE` — not met; `release/status.json/clean_room_reproduction_passed`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `false`.
- `E-CHECKSUMS-RELEASE` — not met; `release/status.json/checksum_validation_passed`; SHA-256 `3543473af0d4896c7ad3b187d69834722a791f059dc18219c31dfcd161bdb2a9`; observed `false`.

Negative findings and limitations:

- A frozen release can reproduce its artefacts but cannot make a changing live GOV.UK snapshot permanently current.
- The active release artefacts are bound to a fixture rather than a full-corpus snapshot.
- Only fixture reproduction, not full-snapshot clean-room reproduction, has passed.
- Full release checksum validation has not passed.

Next actions:

- Run the full-snapshot clean-room replay and bind the result to release checksums and the SBOM.

## Machine-readable evidence

The canonical machine-readable projection is `release/aim-assessment.json`. Every evidence row records the repository-relative path, exact SHA-256, locator, observed value, expected value and deterministic match result. This Markdown file is generated from that same object.
