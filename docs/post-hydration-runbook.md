# Post-hydration closing and release runbook

This is the fail-closed continuation after the unrestricted T0 hydration command
exits zero and exports `corpus/reconciliation/<T0>-hydrated.json`. Do not start
T1 while that process is still running. A retry must reuse the same labels and
paths; never create a second label to hide an open or failed checkpoint.

The deterministic acquisition, closing, compilation, question and evaluation
commands make no model calls. T1 acquisition and closing consume only the
shared official-source request authority. Citation verification below is
offline against the frozen evidence; if evidence must be fetched again, book
those attempts in the separate citation aggregate before finalising the shared
request snapshot.

## 1. Freeze labels and close T1

Set these values once. `T1_LABEL` is the UTC date on which the authoritative T1
enumeration starts; keep it unchanged across resumes.

```sh
T0_LABEL=T0-20260712
T1_LABEL=T1-YYYYMMDD
RELEASE_ID=${T1_LABEL}-closed
T1_DATE=YYYY-MM-DD

.venv/bin/python scripts/acquire_corpus.py "$T1_LABEL"
.venv/bin/python scripts/close_corpus.py \
  "$T0_LABEL" "$T1_LABEL" \
  --label "$RELEASE_ID" \
  --rate 8 \
  --www-rate 2 \
  --official-request-ceiling 1000000
```

Both commands are resumable when invoked again with the same arguments. Do not
use `--work-limit`, sampling limits, single Search pass, unstable-sitemap
options or `--no-export` for a release run.

Resolve the immutable content-addressed source manifest from the closing
reconciliation. The physical directory includes a digest suffix, so a literal
`corpus/records/<release>/source-records` path is not a valid substitute.

```sh
CLOSING_RECON=corpus/reconciliation/${RELEASE_ID}.json
CLOSING_MANIFEST=corpus/records/${RELEASE_ID}/manifest.json
SOURCE_MANIFEST="$(.venv/bin/python -c \
  'import json,pathlib,sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["hydrated_records_path"])' \
  "$CLOSING_RECON")"
SOURCE_ROOT="$(dirname "$SOURCE_MANIFEST")"
GENERATED_AT="$(.venv/bin/python -c \
  'import json,pathlib,sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["closing_watermark"])' \
  "$CLOSING_RECON")"

test -f "$CLOSING_RECON"
test -f "$CLOSING_MANIFEST"
test -f "$SOURCE_MANIFEST"
test -d "$SOURCE_ROOT"
test -n "$GENERATED_AT"
```

`SOURCE_MANIFEST` is the shard index used for the existence check;
`SOURCE_ROOT` is the complete frozen input. Always pass the directory to build,
question, staging and clean-room commands so every hash-declared sibling shard
is copied and bound. Staging and reproduction fail closed if given a detached
standard shard index.

The reconciliation must be unsampled, hydrated and closed, with `pending = 0`,
`unexplained_omissions = 0`, closed opposing Search partitions, byte-stable
sitemaps, closed organisation/navigation proofs and exact entity-class
accounting. Its `inventory_canonical_sha256` is the independently verified T1
inventory digest. The closing manifest embeds that exact reconciliation object
and separately retains `reconciliation_path`.

Append the real `ACT-D1-T0-HYDRATION-TERMINAL-001` and
`ACT-E1-T1-RECONCILIATION-TERMINAL-001` rows only after their outputs, request
intervals and hashes are known. The T0 hydration row remains T0-bound; the T1
row must include `$RELEASE_ID` in `source_snapshots`. Construct every manual
terminal with the declaration-driven commands in
[`terminal-activity-closure.md`](terminal-activity-closure.md); never append a
pre-authored terminal JSON object.

## 2. Build and stage the frozen bundle

```sh
.venv/bin/python scripts/build_bundle.py \
  --source "$SOURCE_ROOT" \
  --output bundle \
  --snapshot-id "$RELEASE_ID" \
  --generated-at "$GENERATED_AT" \
  --compiler disk
.venv/bin/python scripts/check_publication.py
.venv/bin/python scripts/build_checksums.py
.venv/bin/python scripts/build_checksums.py --check
.venv/bin/python scripts/build_sbom.py
.venv/bin/python scripts/build_sbom.py --check

.venv/bin/python scripts/promote_release.py stage \
  --snapshot "$RELEASE_ID" \
  --reconciliation "$CLOSING_RECON" \
  --source "$SOURCE_ROOT" \
  --generated-at "$GENERATED_AT" \
  --compiler disk
.venv/bin/python scripts/check_release.py
```

The search manifest produced here must declare
`okf-search-postings-partitioning.v1` and
`okf-search-doc-map-partitioning.v1`. Do not work around a shard-size failure by
raising the 5 MiB budget, narrowing the corpus or changing logical lexicon
width. The compiler keeps a legacy filename for one-partition groups and emits
five-digit physical suffixes only for skewed groups; publication validation
checks every physical path, byte bound, token range and document-map ordinal.
The earlier T0 capacity attempt failed honestly on a 6,712,946-byte `ca`
postings file. ADR-006 fixes that physical layout, but this full frozen build is
the required proof that no lexicon, prefix or other singleton distribution is
still oversized.

Staging is intentionally non-publishable. It binds the exact frozen source,
generated time, compiler, reconciliation and already-built bundle while the
remaining snapshot evidence is generated.

## 3. Generate and independently verify release-v2

```sh
.venv/bin/python scripts/build_question_matrix_v2.py \
  --mode release \
  --corpus "$SOURCE_ROOT" \
  --snapshot-id "$RELEASE_ID" \
  --snapshot-date "$T1_DATE" \
  --snapshot-manifest "$CLOSING_MANIFEST" \
  --reconciliation "$CLOSING_RECON" \
  --output questions/release-v2

.venv/bin/python scripts/verify_question_matrix_v2.py \
  --matrix questions/release-v2 \
  --corpus "$SOURCE_ROOT" \
  --snapshot-manifest "$CLOSING_MANIFEST" \
  --reconciliation "$CLOSING_RECON" \
  --require-release
```

Do not append `ACT-C1-RELEASE-V2-TERMINAL-001` until the separate verifier
passes all 28,800 questions, 4,800 persona-suite entries, gold, near-miss,
split, leakage and checksum checks. Its `source_snapshots` must contain the
exact `$RELEASE_ID`.

## 4. Run and project the complete evaluation

```sh
EVALUATION_RUN=evaluation/agent-runs/${RELEASE_ID}-release-v2

.venv/bin/python scripts/run_evaluation.py \
  --mode release \
  --questions questions/release-v2 \
  --bundle bundle \
  --output "$EVALUATION_RUN" \
  --run-id "${RELEASE_ID}-release-v2" \
  --resume

.venv/bin/python scripts/project_evaluation_results.py \
  --run "$EVALUATION_RUN" \
  --questions questions/release-v2 \
  --bundle bundle \
  --output evaluation/results
```

`--resume` is safe for a new or incomplete matching run; a completed run is
immutable. Projection independently revalidates and hash-binds the current
release-v2 question contract and bundle, then verifies every declared file,
checksum, all 288,000 trace records and the machine-only claim boundary before
atomically writing canonical release evidence. Then append the exact-snapshot
`ACT-E2-AUTOMATED-EVALUATION-TERMINAL-001` row.

## 5. Produce the remaining snapshot evidence

```sh
.venv/bin/python scripts/verify_citations.py collect --check
.venv/bin/python scripts/verify_citations.py verify --snapshot-id "$RELEASE_ID"
.venv/bin/python scripts/verify_citations.py verify \
  --snapshot-id "$RELEASE_ID" --check

.venv/bin/python scripts/validate_semantics.py --require-shard-metadata
.venv/bin/python scripts/validate_semantics.py \
  --require-shard-metadata --check

.venv/bin/python scripts/audit_rights_privacy.py \
  --corpus-manifest "$CLOSING_MANIFEST" \
  --generated-at "$GENERATED_AT" \
  --require-release
.venv/bin/python scripts/audit_rights_privacy.py \
  --corpus-manifest "$CLOSING_MANIFEST" \
  --generated-at "$GENERATED_AT" \
  --check --require-release

(cd explorer && npm run evidence:release -- \
  --snapshot "$RELEASE_ID" --generated-at "$GENERATED_AT")
```

Run the complete repository tests and publication-shard audit before freezing
the final security scan. Any implementation change after these checks requires
the affected checks and scan to be repeated.

```sh
.venv/bin/python -m unittest discover -s tests -v
(cd explorer && npm test)
(cd semantic && npm test)
.venv/bin/python scripts/check_publication.py
.venv/bin/python scripts/build_checksums.py --check
.venv/bin/python scripts/build_sbom.py --check
```

Before freezing that scan, synchronize the actual release state across
`governance/implementation-status-source.json`, the generated requirement,
traceability and task projections, `docs/implementation-status.md`, the root
and release READMEs, the post-hydration/reproducibility/governance runbooks and
`CHANGELOG.md`. Record only evidence that exists for the exact snapshot; retain
human research as `not_authorised` and UI-of-choice as `not_yet_testable`.
The staged manifest is a `full_corpus_checkpoint`, so set the source
`milestone` to that exact value; leaving `t0_census_closed` is a lockstep
failure.

Any terminal rows appended so far change the activity-ledger hash. Regenerate
the snapshot-bound checkpoint provenance before asking lockstep to compare the
checked evidence with the live ledger:

```sh
.venv/bin/python scripts/check_provenance.py \
  --snapshot "$RELEASE_ID" \
  --output release/provenance-validation.json
.venv/bin/python scripts/build_status_projections.py
.venv/bin/python scripts/build_status_projections.py --check
.venv/bin/python scripts/build_aim_scorecard.py
.venv/bin/python scripts/build_aim_scorecard.py --check
.venv/bin/python scripts/check_lockstep.py
```

Commit those code, workflow, test and control-surface changes before the final
Codex Security scan. After the scan starts, do not change any path in
`scripts/check_release.py::SECURITY_SCAN_INPUT_PATHS`; a change requires a new
scan against the new frozen commit.

The final citation activity is
`ACT-F2-RELEASE-SNAPSHOT-CITATION-REVIEWS-TERMINAL-001`. It must supersede
`ACT-F2-CITATION-REVIEWS-TERMINAL-001`, include `$RELEASE_ID`, record an exact
request disposition, and hash-bind these outputs:

- `release/citation-verification.json`;
- `reports/citation-verification.md`;
- `provenance/citation-request-aggregate.json`.

Run Codex Security only after the release code and artefacts above are frozen.
Retain the scan ID, scanned commit, finding dispositions and a machine document
at `release/security-scan.json` satisfying the release schema. `scanned_commit`
must be the full lowercase 40-hex revision inspected by the scan, and
`code_tree` must record the exact
`scripts/check_release.py::SECURITY_SCAN_INPUT_PATHS`
array plus `_tree_sha256` value observed at that revision. Release validation
recomputes that tree from the candidate checkout, so rerunning tests after a
code change cannot make an older scan reusable. Append
`ACT-D2-RELEASE-SNAPSHOT-SECURITY-SCAN-TERMINAL-001`, superseding
`ACT-D2-SECURITY-SCAN-TERMINAL-001`, and hash-bind both that machine document
and `reports/security.md`. The fixture citation terminal and pre-release
security terminal cannot satisfy either gate.

Also append the exact-snapshot shard-audit terminal and replace the open source
request checkpoint with a final snapshot equal to the live shared counter.
Append `ACT-F2-SOURCE-REQUEST-BUDGET-TERMINAL-001` only after its count equals
that final snapshot. Never fold citation attempts into model cost.

After every pre-promotion terminal above is present, regenerate checkpoint-mode
provenance one final time and rerun the generated status, aim and lockstep
checks. This is the evidence state that promotion consumes; using the earlier
pre-scan ledger hash is forbidden.

```sh
.venv/bin/python scripts/check_provenance.py \
  --snapshot "$RELEASE_ID" \
  --output release/provenance-validation.json
.venv/bin/python scripts/build_status_projections.py
.venv/bin/python scripts/build_status_projections.py --check
.venv/bin/python scripts/build_aim_scorecard.py
.venv/bin/python scripts/build_aim_scorecard.py --check
.venv/bin/python scripts/check_lockstep.py
```

## 6. Promote, publish and finalize

Before promotion, the ledger must satisfy every candidate terminal except the
clean-room terminal, which promotion appends transactionally, and the external
publication terminal. Promotion independently regenerates full-test and
clean-room evidence. After installing candidate manifest/status controls, it
replays the rights audit's hash-bound publication, corpus and review input
contract, then regenerates provenance and the aim assessment. Any failure rolls
back rights evidence and every other control artefact.

```sh
.venv/bin/python scripts/promote_release.py promote
.venv/bin/python scripts/check_release.py --publication-ready
```

Promotion changes the authoritative manifest/status state from checkpoint to
candidate. Before committing or tagging the candidate, update
`governance/implementation-status-source.json` to terminal requirement
dispositions, set `milestone` to `machine_release_candidate`, and record the
exact accepted/blocked task set. Exactly `REQ-069`, `REQ-070`, `REQ-073`,
`REQ-074` and `REQ-077` remain blocked; the other 90 requirements pass. Every
accepted task must cite a nonempty, deduplicated list, and the accepted tasks
collectively cite all ten declared candidate terminal activities. The ledger
must be schema-valid and hash-chained; each cited terminal has an exact
completion time, complete validation, final request disposition, non-pending
hash-bound outputs and its declared snapshot binding.
`E3-01`, `F1-01`, `F2-02` and `F2-03` remain blocked while human research is
not authorised. Regenerate and validate every dependent projection:

```sh
.venv/bin/python scripts/build_status_projections.py
.venv/bin/python scripts/build_status_projections.py --check
.venv/bin/python scripts/build_aim_scorecard.py
.venv/bin/python scripts/build_aim_scorecard.py --check
.venv/bin/python scripts/check_lockstep.py
```

The initial candidate tag is the annotated `v0.1.0-rc.1` tag. Do not use the
future `v1.0.0` series for this first release.

Publish the exact verified candidate commit and bytes through the release and
Pages workflows, verify the live Pages snapshot, and open the normal OKF
Explorer registry pull request. Only then append
`ACT-F2-PUBLICATION-REGISTRY-TERMINAL-001` with the commit, PR, CI, protected-
main read-back, candidate tag/release, Pages and registry-PR evidence.

```sh
.venv/bin/python scripts/promote_release.py finalize
.venv/bin/python scripts/check_provenance.py \
  --snapshot "$RELEASE_ID" \
  --output release/provenance-validation.json \
  --require-release
.venv/bin/python scripts/check_release.py --finalized
```

Finalization changes the authoritative state from candidate to release and
therefore makes all three status projections stale even when their substantive
machine dispositions are unchanged. Regenerate the status and aim projections,
set the implementation-status source milestone to `machine_release_finalized`,
add `ACT-F2-PUBLICATION-REGISTRY-TERMINAL-001` to the terminal-activity mapping
for the accepted task group so its union is the exact 11-event final contract,
then run lockstep, commit those finalized controls, and only then create the
annotated final `v0.1.0` tag:

```sh
.venv/bin/python scripts/build_status_projections.py
.venv/bin/python scripts/build_status_projections.py --check
.venv/bin/python scripts/build_aim_scorecard.py
.venv/bin/python scripts/build_aim_scorecard.py --check
.venv/bin/python scripts/check_lockstep.py
.venv/bin/python scripts/check_release.py --finalized
```

Finalization repeats the manifest -> rights -> provenance -> aim order inside
the same rollback transaction. If hydrated corpus manifests have been archived
outside Git, fresh CI/release checkouts use
`audit_rights_privacy.py --check --require-release --allow-archived-inputs` and
`check_release.py --finalized --allow-archived-inputs`; those modes accept
missing corpus inputs only when the original path/byte/hash contract remains
complete. They never accept a changed input or represent static validation as a
new scan.

A later full-programme release is a separate transition. It cannot reuse the
machine terminal set as human evidence: it requires explicit study authority,
the snapshot-bound `ACT-E3-FULL-PROGRAMME-TERMINAL-001` declared in
`provenance/reproduction-declarations.json`, all 95 requirements passed, all 36
tasks accepted and source milestone `full_programme_complete`.

The final tag and GitHub Release must use the already verified bytes; no
publication workflow may rebuild the bundle. Recheck the final release assets,
checksums, Pages routes/search/adjacency and Explorer registry entry live before
claiming completion. Human evaluation remains `not_authorised` and UI-of-choice
remains `not_yet_testable` unless separately authorised participant evidence
actually exists.
