# Release evidence contract

`manifest.yaml` is canonical JSON, which is also valid YAML 1.2. Keeping this
release-control document in the shared JSON/YAML subset makes validation
deterministic without a permissive YAML loader.

The resumable execution order after T0 hydration, including resolution of the
content-addressed closing source manifest, is documented in
[`docs/post-hydration-runbook.md`](../docs/post-hydration-runbook.md).

The checked-in state is deliberately a non-publishable fixture checkpoint.
Run the structural checkpoint validation with:

```sh
python3 scripts/check_release.py
```

The requirement, traceability and task projections derive release identity,
kind, checkpoint/candidate/release state and readiness from `manifest.yaml`
and `status.json`; `governance/implementation-status-source.json` records only
evidence dispositions. A checkpoint may claim no passed requirement or
accepted task. A machine candidate/final has exactly 90 passed and five blocked
requirements: the human-gated `REQ-069`, `REQ-070`, `REQ-073`, `REQ-074` and
`REQ-077` stay blocked. Its 32 accepted tasks must collectively cite the exact
declared candidate/final terminal set; every cited row is schema-valid,
hash-chained, exactly completed, fully validated and hash-bound. Machine
releases keep the four-task human dependency closure blocked. A future full-
programme release also requires the separately declared, snapshot-bound
`ACT-E3-FULL-PROGRAMME-TERMINAL-001`.

`governance/implementation-status-source.json` uses release-coupled milestone
values: `full_corpus_checkpoint` after staging, `machine_release_candidate`
after promotion and `machine_release_finalized` after finalization. Projection
generation fails on a stale milestone.

GitHub Pages requires the stricter gate:

```sh
python3 scripts/check_release.py --publication-ready
```

Release control is a mandatory two-stage transaction. First stage the closing,
unsampled full-corpus snapshot; this remains non-publishable while clean-room
and the other snapshot-bound evidence are generated:

```sh
.venv/bin/python scripts/promote_release.py stage \
  --snapshot T1-YYYYMMDD-closed \
  --reconciliation corpus/reconciliation/T1-YYYYMMDD-closed.json \
  --source "$SOURCE_ROOT" \
  --generated-at YYYY-MM-DDTHH:MM:SSZ \
  --compiler disk
```

Staging records the exact frozen source, generation timestamp and compiler that
must reproduce the checked bundle. Set the implementation-status source
milestone to `full_corpus_checkpoint` before regenerating checkpoint
projections. After the question-v2, 28,800-by-10
evaluation, citation, semantic, rights, browser and completed security-scan
artefacts exist, promotion revalidates checksums and the SBOM, reruns provenance
and the full Python, Explorer and semantic test suites, then runs the clean-room
rebuild prospectively while the manifest and status are still the staged
checkpoint. It binds both staged hashes and the newly generated full-test
evidence, the frozen source contract, the current bundle tree, SBOM and every
immutable clean-workspace input. It appends the hash-chained clean-room terminal
and only then builds candidate provenance. Candidate controls,
clean-room evidence, manifest-bound rights evidence, provenance and the
regenerated aim assessment are installed inside one rollback transaction:

```sh
.venv/bin/python scripts/check_provenance.py \
  --snapshot "$RELEASE_ID" \
  --output release/provenance-validation.json
.venv/bin/python scripts/check_lockstep.py
.venv/bin/python scripts/promote_release.py promote
```

Promotion changes the authoritative release state to `candidate`. Update the
implementation-status source to terminal requirement dispositions and the
exact accepted/blocked task set, set `milestone` to
`machine_release_candidate`, then regenerate the three status projections
and aim assessment before the candidate commit and annotated `v0.1.0-rc.1`
tag:

```sh
.venv/bin/python scripts/build_status_projections.py
.venv/bin/python scripts/build_status_projections.py --check
.venv/bin/python scripts/build_aim_scorecard.py
.venv/bin/python scripts/build_aim_scorecard.py --check
.venv/bin/python scripts/check_lockstep.py
```

The full evaluation is retained as an immutable run below
`evaluation/agent-runs/`. Before promotion,
`scripts/project_evaluation_results.py` independently revalidates and
hash-binds the exact current release-v2 questions and bundle, verifies the
run's complete manifest, checksum ledger, all 288,000 raw traces and the
machine-only claim boundary, then atomically publishes summary evidence at
`evaluation/results`.

Provenance has two honest tiers around the external publication milestone.
Candidate promotion uses `check_provenance.py --require-candidate`: 10 of 11
terminal events must pass and only
`ACT-F2-PUBLICATION-REGISTRY-TERMINAL-001` may remain
`pending_post_publication`. After the candidate release, Pages live checks and
Explorer registry PR are evidenced, append that real terminal and finalize.
The nine post-closing terminals must name the exact release snapshot. In
particular, the final citation and security activities use distinct IDs that
supersede their fixture/pre-release terminals and carry SHA-256 bindings for
their required release reports; the earlier successful work cannot be reused
as final-snapshot provenance.
Finalization runs `check_provenance.py --require-release` semantics for strict
11-of-11 validation under the same ledger side lock; it does not synthesize the
external event. The old 10-of-11 candidate provenance is expected to become
stale when that terminal is appended, so strict provenance is generated before
the final controls are validated together. Prepared candidate and partial-final
crash states are replay-safe and completed finalization is idempotent:

```sh
.venv/bin/python scripts/promote_release.py finalize
.venv/bin/python scripts/build_status_projections.py
.venv/bin/python scripts/build_status_projections.py --check
.venv/bin/python scripts/build_aim_scorecard.py
.venv/bin/python scripts/build_aim_scorecard.py --check
.venv/bin/python scripts/check_lockstep.py
.venv/bin/python scripts/check_release.py --finalized
```

Those regenerated finalized controls are committed before the annotated final
`v0.1.0` tag. Set the source milestone to `machine_release_finalized` before
regeneration and add `ACT-F2-PUBLICATION-REGISTRY-TERMINAL-001` to the accepted
task group's terminal mapping. The accepted-task union must now equal the exact
11-event final contract; finalization cannot reuse candidate-labelled status
projection headers or their ten-event mapping.

The final manifest records the exact staged manifest/status hashes. Clean-room
evidence must bind both hashes and the generated full-test evidence. Any failed
generation or final validation restores the prior manifest, status, provenance,
full-test, clean-room, rights, activity-ledger and aim artefacts.

That gate rejects fixtures, samples and capacity runs. It requires a complete
T0/T1 closing reconciliation, opposing closed Search API partition proofs,
byte-stable sitemap evidence, a closed organisations census, zero unexplained
omissions, exact publication-count agreement, current bundle checksums and SBOM,
and passing snapshot-bound question/gold, 28,800-by-10 evaluation, semantic,
citation, rights/privacy, full-release browser, completed security scan,
provenance, full-repository tests, clean-room and aim evidence documents. Boolean
status flags alone cannot satisfy those gates.

`rights-privacy-audit.json` is produced by the disk-backed bounded scanner:

```sh
.venv/bin/python scripts/audit_rights_privacy.py
```

The fixture proves that the published data plane has no retained page or
attachment body fields and no credential material, while remaining a
non-release checkpoint. The final T1 run supplies the hydrated corpus manifest
and uses `--require-release`; unresolved conservative item triggers remain
visible and are non-blocking only where the frozen metadata-and-link policy
explicitly permits their publication.

The audit writes an immutable input contract for the publication manifest,
every hydrated corpus manifest, the review ledger and deterministic audit time.
Candidate promotion and finalization refresh the rights evidence only after the
new manifest is installed, then regenerate provenance and the aim scorecard.
Fresh CI and tag checkouts may use `--check --allow-archived-inputs` when the
hydrated corpus has deliberately remained outside Git; this is static
hash-bound validation, not a replacement scan, and present-but-changed inputs
remain fatal.

`sbom.cdx.json` is regenerated deterministically from `uv.lock` and
`semantic/package-lock.json`. `clean-room-reproduction.json` records an isolated
temporary-directory rebuild and binds its exact bundle and SBOM hashes. The
checked fixture may assert `fixture_reproduction_passed: true`, but must keep
`clean_room_reproduction_passed: false`. See `docs/reproducibility.md` for the
closing-snapshot command.

The machine release-candidate may retain
`human_evaluation_status: not_authorised` and must retain
`human_ui_of_choice_status: not_yet_testable`. A full-programme marker is
accepted only after genuine human evaluation is complete.

## Publication transport and recovery

Packaging preserves virtual shard paths in a dual-hash offset index and emits
gzip-framed `.pack.gz` files no larger than 64 MiB. The exact Pages site fails
closed at 950,000,000 bytes. Browser JavaScript fetches those ranges only from
the same Pages origin; GitHub Release copies are an offline immutable mirror,
not a CORS fallback.

The packed-site browser report binds both `data_plane_index_sha256` and
`site_checksums_sha256`. Release packaging accepts it only when those hashes,
the snapshot and all nested accessibility, routing, performance and no-error
gates match the exact package; a same-snapshot report from different bytes
cannot be replayed.

The tag workflow validates the packed site in a real browser, creates a draft
Release and verifies every API-reported asset digest. It then deploys and
live-smokes the exact Pages artifact. Only a dependent final job can reverify
that draft, publish it and require the versioned API to report
`draft:false, immutable:true`. If Pages fails, the Release remains an editable
draft. See
[`ADR-005`](../governance/decisions/ADR-005-github-pages-range-pack-data-plane.md).
The two GitHub services are not atomically committed: if Pages succeeds and the
final publish step fails, the honest state is `Pages live / Release draft` and
release finalization remains pending. Rerunning the final job revalidates the
attested exact-asset expectation; it never substitutes or clobbers assets.
