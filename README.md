# What’s on GOV.UK — OKF Bundle Wiki

An independent, metadata-led catalogue of the public `www.gov.uk` publishing
estate, packaged as a federated Open Knowledge Format (OKF) Bundle Wiki for
people, software and retrieval agents.

> This is a derived, non-authoritative discovery layer. GOV.UK remains the
> authoritative destination for guidance, services and transactions.

## Current status

The checked-in publication is a **representative fixture and pre-release
foundation**, not the complete GOV.UK bundle and not a machine release
candidate. It contains 14 fixture records so that semantics, deterministic
sharding, static search, route adjacency, Explorer and read-only discovery can
be tested while the full corpus is hydrated and closed. The unsampled T0 union
census is now closed at 848,977 candidate keys and 836,998 publication records,
with six redirects and zero unexplained omissions; it is not yet the T1-closed
release corpus.

The Explorer source includes a no-skip real-Chromium fixture gate for
accessibility-relevant behaviour, durable query/hash links, Pages recovery,
gzip hydration and startup/search/route/heap budgets. The automated browser
subset now passes locally and in protected pull-request CI. Axe, accessibility-
expert, screen-reader and participant testing have not been completed, so this
does not claim WCAG conformance or human preference.

Publication remains fail-closed until T0 hydration and T1 enumeration/closing close,
`unexplained_omissions` is zero, the corpus-anchored v2 question matrix is
independently verified, machine evaluation and citations complete, checksums
agree and clean-room reproduction passes. See
[`docs/implementation-status.md`](docs/implementation-status.md) and the
machine-readable status projections under `governance/`.

The machine-applicable persona/use-taxonomy gate now passes with 48 primary
research hypotheses, 17 overlays, an 11-dimension matrix, complete overlay-pair
scenario coverage and two successive no-new held-out challenge passes. This is
not participant evidence: human validation is not authorised and Explorer UI
preference remains `not_yet_testable`. See [`personas/README.md`](personas/README.md).

## Release boundary

Release 1 inventories every canonical public item discovered by the frozen
union of declared official public enumerators during a dated T0–T1 acquisition
window. Each candidate is represented, linked to a represented alias, recorded
as redirect/tombstone, or carried as an evidenced exception. Complete page
bodies and independently operated `*.gov.uk` sites are outside the boundary.

The public release is permitted to reach the machine release-candidate state
while genuine participant research remains unauthorised. It must not claim
that Explorer is a human “UI of choice” without that study.

Publication uses an honest two-tag sequence: an annotated
`vMAJOR.MINOR.PATCH-rc.N` prerelease publishes the candidate bytes needed to
complete the external publication/Pages/Explorer provenance terminal; only a
later annotated `vMAJOR.MINOR.PATCH` tag may publish the final release, after
strict 11-of-11 provenance and finalized-promotion validation. Both channels
package and attest the already verified bytes without rebuilding. Repository
protection, the solo-owner review rationale and live-policy read-back are
documented in [`docs/repository-governance.md`](docs/repository-governance.md).

## Canonical outputs

- `bundle/okf-bundle.yamlld` — semantic source document
- `bundle/okf-bundle.jsonld` — equivalent JSON-LD projection
- `bundle/okf-explorer.json` — Explorer descriptor
- `bundle/data/manifest.json` — immutable record/search/adjacency manifest
- `bundle/data/semantic/manifest.json` — lazy JSON-LD entity, evidence,
  vocabulary and reified-assertion shards with per-shard integrity metadata
- `release/manifest.yaml` — snapshot, checksums and release status
- `release/sbom.cdx.json` — CycloneDX 1.6 inventory generated from both lock files
- `release/clean-room-reproduction.json` — isolated rebuild, environment,
  usage, rights and fallback evidence
- `release/rights-privacy-audit.json` — snapshot-bound, disk-backed proof of
  the metadata/body/credential boundary and conservative item-review triggers
- `corpus/reconciliation/T0-20260712.json` — authoritative opening-census
  counts, source-set differences and zero-omission proof
- `governance/requirements-status.json` — current implementation status for all
  95 requirements
- `governance/traceability-status.json` — clause status projected from mapped
  requirements
- `governance/task-status.json` — status of the 36 task contracts
- `explorer/src/evidence/fixture-browser.json` — honest fixture browser-evidence
  checkpoint, overwritten only by the measured evidence command

## Reproduce and validate

Python 3.12 or later is sufficient for the deterministic core.

```sh
python3 scripts/import_contract.py --check
python3 scripts/preflight_sources.py --check
python3 scripts/build_status_projections.py --check
python3 scripts/build_research_assets.py --check
python3 scripts/check_persona_saturation.py
python3 scripts/check_repository_policy.py
python3 scripts/check_provenance.py
python3 scripts/build_bundle.py --check
python3 -m unittest discover -s tests -v
python3 scripts/check_publication.py
python3 scripts/build_checksums.py --check
python3 scripts/build_sbom.py --check
python3 scripts/reproduce_release.py --check
python3 scripts/audit_rights_privacy.py --check
python3 scripts/check_release.py
(cd explorer && npm test)
(cd explorer && npm run test:browser)
```

The browser command requires installed Chrome/Chromium and an ephemeral
`127.0.0.1` listener. `CHROME_PATH` can identify a non-standard executable.
It never skips merely because the browser is absent.

The full-corpus hydration uses the globally shared request counter and the
ADR-004 75,000-page deterministic rendered-link detector:

```sh
python3 scripts/hydrate_corpus.py T0-20260712 --rendered-scan-limit 75000
```

The command is resumable. Its initial structured work has a theoretical
minimum of about 29 hours at the authorised 8 Content API requests/s; a
checkpoint is not a completion claim.

Full-corpus gzip snapshots and explicit `records-*`/`part-*` shard directories
select the bounded SQLite compiler automatically:

`SOURCE_ROOT` below is the parent directory of the content-addressed
`hydrated_records_path` from the closing reconciliation; see the post-hydration
runbook for exact resolution. The directory, not its detached `index.json`, is
the complete clean-room input.

```sh
python3 scripts/build_bundle.py \
  --source "$SOURCE_ROOT" \
  --output bundle \
  --snapshot-id T1-YYYYMMDD-closed \
  --generated-at YYYY-MM-DDTHH:MM:SSZ
```

The fixture and disk compilers are byte-equivalent; the publication validator
uses the same bounded-shard/SQLite approach. Measured capacity and temporary-
disk limits are recorded in `reports/publication-scale.md`.

The fixture currently reproduces byte for byte, while its full release gate
correctly remains false. The closing-snapshot command and evidence contract are
documented in [`docs/reproducibility.md`](docs/reproducibility.md); the exact
resumable continuation after T0 hydration is
[`docs/post-hydration-runbook.md`](docs/post-hydration-runbook.md).

Current operational decisions, source constraints, costs and human-only gates
are published under `governance/`, `research/`, `provenance/` and `reports/`.
Running the network preflight itself requires `python3
scripts/preflight_sources.py --live`; the normal CI check is offline and verifies
the frozen response metadata, counts, hashes and explicit failures.

The default `build_bundle.py` input is deliberately the representative fixture.
It must not be used as a release command. The complete acquisition, hydration,
release-question and publication sequence is documented in
[`docs/architecture.md`](docs/architecture.md).


## Licensing and attribution

Project-authored code and documentation are MIT licensed. Reused Crown
copyright metadata is attributed and made available under the Open Government
Licence v3.0 where that licence applies. Item-level third-party or restricted
material remains governed by its source terms and is not silently republished.
The rights/privacy evidence records structural item-review triggers with hashed
examples only; complete body or credential retention is always a hard failure.
See [LICENSE.md](LICENSE.md), the machine-readable constraint ledger and
`release/rights-privacy-audit.json`.
