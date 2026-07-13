# Reproducibility and supply-chain evidence

The frozen-release rebuild is intentionally separate from reacquiring the live
site. Reacquisition creates a new T0/T1 snapshot and is expected to change.
Reproduction consumes an already frozen metadata source and must not contact
GOV.UK or a model provider.

## Locked dependency inventory

Python dependencies are locked in `uv.lock`. The semantic JSON-LD/RDFC test
runtime is locked in `semantic/package-lock.json`. Generate the deterministic
CycloneDX 1.6 inventory with:

```sh
.venv/bin/python scripts/build_sbom.py
.venv/bin/python scripts/build_sbom.py --check
```

The SBOM records every direct and transitive locked package, its package URL,
available distribution hash, dependency graph, and the SHA-256 of both lock
files. The project component, version and MIT identifier are bound to the
SHA-256 of `pyproject.toml`. The result is evidence of dependency resolution,
not a vulnerability scan or a claim that package licences have received legal
approval.

The published semantic profile contains only the declared `context`,
`crosswalks`, `profile`, `schemas` and `shapes` assets plus its README. Local
`node_modules`, package-manager state, semantic validator scripts and tests are
never copied into the bundle.

## Fixture clean rebuild

The checked-in checkpoint can be regenerated, or verified without changing the
checkout:

```sh
.venv/bin/python scripts/reproduce_release.py
.venv/bin/python scripts/reproduce_release.py --check
```

The verifier creates a new system temporary directory, copies only its declared
source/code/profile/lock inputs, builds the bundle, runs the bounded publication
validator, generates and checks bundle checksums, regenerates the SBOM, and
compares both output trees byte for byte. `--check` writes nothing in the
checkout and hashes all declared checkout inputs and expected outputs before
and after execution.

`release/clean-room-reproduction.json` truthfully records two separate results:

- `fixture_reproduction_passed` is true when the representative fixture
  rebuild is exact;
- `clean_room_reproduction_passed` remains false for fixtures, samples,
  capacity runs, incomplete test evidence or a mismatched release manifest.

The evidence also records the exact commands, tool and platform versions, input
and output hashes, validator return codes, checkout mutation check, zero-network
declaration, activity-ledger usage/cost summary, source-access restrictions,
rights/fair-use triggers and attempted fallbacks. Unavailable Codex product-
session tokens, backend version and marginal cost remain `unavailable`; they are
not converted to a false zero. The deterministic rebuild itself makes exactly
zero model calls and has zero marginal model cost.

## Activity, usage and fallback provenance

`provenance/activity-ledger.jsonl` preserves its four original rows and starts
a SHA-256 previous-row chain at v2. New entries are appended under an exclusive
lock and validated against `provenance/activity-ledger.schema.json`:

```sh
.venv/bin/python scripts/append_activity.py path/to/entry.json
.venv/bin/python scripts/check_provenance.py
```

The default validation resolves the current `release/status.json` release ID
and deterministically writes `release/provenance-validation.json`. Promotion is
honestly split around the external publication milestone. The candidate gate
requires 10 terminal events and allows only the publication/Pages/registry
event to remain `pending_post_publication`:

```sh
.venv/bin/python scripts/check_provenance.py \
  --snapshot T1-YYYYMMDD-closed \
  --output release/provenance-validation.json \
  --require-candidate
```

After the candidate is published, Pages and release assets are verified and
the Explorer registry PR exists, append
`ACT-F2-PUBLICATION-REGISTRY-TERMINAL-001`. The final gate then requires all 11
events:

```sh
.venv/bin/python scripts/check_provenance.py \
  --snapshot T1-YYYYMMDD-closed \
  --output release/provenance-validation.json \
  --require-release
```

Failure still writes a machine-readable report and exits non-zero. Release mode
rejects fixture/sample/capacity/development/test labels, an open or over-ceiling
shared request budget, any unresolved `pending_final` or in-progress activity,
and any missing or incomplete exact terminal activity declared in
`provenance/reproduction-declarations.json`. Terminal rows use
`supersedes_activity_ids`; the validator requires prior references, one
unambiguous superseder, a precise completion time, complete validation, no
pending output and exact source-request counts for request-bearing events. All
11 declarations carry an explicit snapshot-binding disposition. The nine
post-closing events must name the exact requested release snapshot; T0 census
and hydration remain correctly bound to their opening T0 snapshot. The final
citation and security terminals additionally bind declared release artefacts
by repository-relative path and SHA-256, and validation recomputes those hashes
from the current checkout.

The complete order is: stage the closing checkpoint; transactionally generate
full-test and clean-room evidence and promote the candidate; publish the
candidate GitHub Release and Pages site and open the Explorer registry PR;
append the publication terminal; run strict provenance and finalize; then make
the final release and live verification. Candidate mode never sets the final
provenance conclusion: its
document reports `validation_tier: candidate`,
`release_requirements_satisfied: false` and
`publication_workflow_status: pending_post_publication` until the external
milestone is actually evidenced.

V2 separates external paid-model API use, Codex product-session use and
official-source request attempts. External paid-model calls, tokens and cost are
exactly zero. Product-session backend version, parameters, tokens and marginal
cost are unavailable to the repository and remain explicit unavailable values.
The user's product usage-limit reset permits continuation while the product
allows; it is not a numeric token measurement or paid-API authority.
In-progress rows are immutable observations: completion is represented by a new
hash-chained terminal row that names the checkpoint it supersedes.

The shared official-source counter is checkpointed independently in
`provenance/source-request-budget.json`. It remains open while T0/T1 work runs;
release requires exact terminal T0, hydration, T1 and closing-delta activities
plus a final counter snapshot. Citation-verification requests retain per-source
attempt evidence and must receive a separate final aggregate rather than being
folded into model cost.

The completed remediation campaign is also only a pre-release checkpoint. The
final frozen repository scan must append the distinct
`ACT-D2-RELEASE-SNAPSHOT-SECURITY-SCAN-TERMINAL-001` activity, superseding
`ACT-D2-SECURITY-SCAN-TERMINAL-001`; reusing the earlier terminal cannot satisfy
candidate provenance. Likewise, the completed fixture citation review remains
`ACT-F2-CITATION-REVIEWS-TERMINAL-001`. The closing snapshot must produce
`ACT-F2-RELEASE-SNAPSHOT-CITATION-REVIEWS-TERMINAL-001`, supersede that fixture
terminal, name the exact T1 release ID and hash-bind the final citation report,
review report and request aggregate. A fixture-labelled citation terminal can
never satisfy candidate or release provenance.

`provenance/reproduction-declarations.json` records every used access fallback:
ACM to the Waterloo RRF paper; CMU legacy TLS through blocked ResearchGate to
Crossref bibliographic identity; the National Archives certificate-failure
chain to the GOV.UK Knowledge Asset guide; and the OpenAI article 403 to the
official CDN BrowseComp paper. Failed originals, timestamps and evidence IDs
remain visible and no TLS or access-control bypass is used.

## Closing full snapshot

The exact resumable T1, content-addressed source resolution, release-v2,
evaluation, evidence, promotion and publication order is in
[`post-hydration-runbook.md`](post-hydration-runbook.md).

After T1 closure, build the SBOM and stage the same unsampled snapshot with its
exact frozen build inputs. Do not mutate the manifest into a candidate before
clean-room verification:

```sh
.venv/bin/python scripts/build_sbom.py
.venv/bin/python scripts/promote_release.py stage \
  --snapshot T1-YYYYMMDD-closed \
  --reconciliation corpus/reconciliation/T1-YYYYMMDD-closed.json \
  --source "$SOURCE_ROOT" \
  --generated-at YYYY-MM-DDTHH:MM:SSZ \
  --compiler disk
.venv/bin/python scripts/check_provenance.py \
  --snapshot T1-YYYYMMDD-closed \
  --output release/provenance-validation.json
.venv/bin/python scripts/check_lockstep.py
.venv/bin/python scripts/promote_release.py promote
```

After staging, set the implementation-status source milestone to
`full_corpus_checkpoint`; the projection builder rejects the earlier
`t0_census_closed` milestone for a staged closing snapshot.

After promotion, synchronize terminal requirement dispositions and the exact
accepted/blocked task set in
`governance/implementation-status-source.json`. Set `milestone` to
`machine_release_candidate`; exactly the five requirements owned by `E3-01`
(`REQ-069`, `REQ-070`, `REQ-073`, `REQ-074` and `REQ-077`) remain blocked while
the other 90 pass. The 32 accepted tasks collectively cite the exact ten-event
candidate terminal set through schema-valid, hash-chained, fully completed
rows. Regenerate the requirement,
traceability, task and aim projections and pass lockstep before committing the
candidate or creating the annotated `v0.1.0-rc.1` tag:

```sh
.venv/bin/python scripts/build_status_projections.py
.venv/bin/python scripts/build_status_projections.py --check
.venv/bin/python scripts/build_aim_scorecard.py
.venv/bin/python scripts/build_aim_scorecard.py --check
.venv/bin/python scripts/check_lockstep.py
```

After the publication terminal is appended and release finalization succeeds,
set `milestone` to `machine_release_finalized` and run that same projection
sequence again. Add `ACT-F2-PUBLICATION-REGISTRY-TERMINAL-001` to the accepted
task group's terminal mapping first: its union must expand from the exact ten-
event candidate contract to the exact eleven-event final contract. The
manifest/status state has changed
from candidate to release, so candidate-labelled generated headers are stale.
Commit the finalized controls before creating the annotated `v0.1.0` tag.

The future `full_programme_complete` milestone additionally requires the
declared `ACT-E3-FULL-PROGRAMME-TERMINAL-001`; machine terminal evidence cannot
substitute for authorised participant/expert evidence.

Promotion generates independent full-repository test evidence for the same
snapshot, temporarily installs only that evidence, and performs the clean-room
rebuild against the still-staged manifest/status. The resulting evidence binds
the frozen source content/tree, generation timestamp, compiler, raw staged
hashes, full-test evidence, SBOM, current bundle tree and every immutable input
copied into the clean workspace. Promotion then appends the hash-chained
`ACT-F2-CLEAN-ROOM-RC-TERMINAL-001` row and builds candidate provenance from
that updated ledger; only then are candidate controls and regenerated
rights, provenance and assessment artefacts installed. Rights evidence is
rebuilt from its exact input contract after the candidate manifest is in place,
so its release-manifest hash cannot be made stale by promotion. Finalization
performs the same ordering after installing the final manifest/status. The side
lock and prepared terminal make
post-clean and partial-candidate crash retries idempotent. Any failure restores
the ledger, rights evidence and every other release-control artefact. Boolean flags alone cannot
promote a fixture or sampled corpus.

Generate the independent rights/privacy evidence after the final T1 bundle and
hydrated record manifest exist:

```sh
.venv/bin/python scripts/audit_rights_privacy.py \
  --corpus-manifest corpus/records/T1-YYYYMMDD-closed/manifest.json \
  --generated-at YYYY-MM-DDTHH:MM:SSZ \
  --require-release
.venv/bin/python scripts/audit_rights_privacy.py \
  --corpus-manifest corpus/records/T1-YYYYMMDD-closed/manifest.json \
  --generated-at YYYY-MM-DDTHH:MM:SSZ \
  --check --require-release
```

The scanner reads records incrementally with explicit file, record,
decompression, nesting and string ceilings and uses temporary SQLite for the
full classification set. Its examples are record fingerprints, never source
values. Structural rights triggers remain distinct from hard failures: a
trigger may be controlled by the declared metadata-and-link policy, but any
retained body/credential material, integrity error or snapshot mismatch blocks
the release. The resulting `audit_input_contract` binds the publication
manifest, every corpus manifest, the review ledger and deterministic
`generated_at` value by repository-relative path, bytes and SHA-256. Promotion
replays those exact inputs when present. A fresh candidate/final checkout may
omit the archived hydrated corpus; in that case only the explicit
`--allow-archived-inputs` check path is permitted, and it validates the retained
input bindings, current publication/release controls and all release gates
without claiming to have rescanned unavailable bytes. A present but changed
input always fails.

CI installs dependencies from both locks and dispatches on the checked release
contract. The exact development fixture still runs byte-for-byte
`build_bundle.py --check` and `reproduce_release.py --check`. A promoted,
unsampled candidate/final instead validates its completed clean-room evidence,
current bundle/release bindings and archived-input contracts; checkpoints or
ambiguous states fail closed. The small single-pack browser regression builds a
dedicated two-record fixture at test time and never copies the checked release
bundle. Full-corpus packed-browser evidence remains a release-workflow gate. A
release run should retain the CI log and exact runner/container identity
alongside the machine evidence.

## Rights and access boundary

The clean rebuild retains metadata and authoritative links, not complete page
or attachment bodies. OGL v3 applies only where it applies at source. Personal
data, third-party credits, logos/insignia, protected rights, identity documents,
complete media bytes and source-specific notices trigger item-level review.
The deterministic evidence is `release/rights-privacy-audit.json`; the fixture
currently proves the mechanical boundary while correctly failing the full T1
release gate.
Authenticated GOV.UK surfaces remain comparator-only. The strict-TLS fallback
attempt for the Pirolli citation remains unsuccessful and visible; TLS or access
controls were not weakened.
