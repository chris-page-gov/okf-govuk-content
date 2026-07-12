# Release evidence contract

`manifest.yaml` is canonical JSON, which is also valid YAML 1.2. Keeping this
release-control document in the shared JSON/YAML subset makes validation
deterministic without a permissive YAML loader.

The checked-in state is deliberately a non-publishable fixture checkpoint.
Run the structural checkpoint validation with:

```sh
python3 scripts/check_release.py
```

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
  --reconciliation corpus/reconciliation/T1-YYYYMMDD-closed.json
```

After the question-v2, 28,800-by-10 evaluation, citation, semantic, rights,
browser, completed security-scan and clean-room artefacts exist, promotion
revalidates checksums and the SBOM, reruns provenance and the full Python,
Explorer and semantic test suites, regenerates the aim assessment, and invokes
the publication-ready validator inside one rollback transaction:

```sh
.venv/bin/python scripts/promote_release.py promote
```

Provenance has two honest tiers around the external publication milestone.
Candidate promotion uses `check_provenance.py --require-candidate`: 10 of 11
terminal events must pass and only
`ACT-F2-PUBLICATION-REGISTRY-TERMINAL-001` may remain
`pending_post_publication`. After the candidate release, Pages live checks and
Explorer registry PR are evidenced, append that real terminal and finalize.
Finalization runs `check_provenance.py --require-release` semantics for strict
11-of-11 validation; it does not synthesize the external event:

```sh
.venv/bin/python scripts/promote_release.py finalize
.venv/bin/python scripts/check_release.py --finalized
```

The final manifest records the exact staged manifest/status hashes. Clean-room
evidence must bind the staged manifest hash. Any failed final validation restores
the prior manifest, status, provenance/test evidence and aim projections.

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
