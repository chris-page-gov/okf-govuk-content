# What’s on GOV.UK — OKF Bundle Wiki

An independent, metadata-led catalogue of the public `www.gov.uk` publishing
estate, packaged as a federated Open Knowledge Format (OKF) Bundle Wiki for
people, software and retrieval agents.

> This is a derived, non-authoritative discovery layer. GOV.UK remains the
> authoritative destination for guidance, services and transactions.

## Release boundary

Release 1 inventories every canonical public item discovered by the frozen
union of declared official public enumerators during a dated T0–T1 acquisition
window. Each candidate is represented, linked to a represented alias, recorded
as redirect/tombstone, or carried as an evidenced exception. Complete page
bodies and independently operated `*.gov.uk` sites are outside the boundary.

The public release is permitted to reach the machine release-candidate state
while genuine participant research remains unauthorised. It must not claim
that Explorer is a human “UI of choice” without that study.

## Canonical outputs

- `bundle/okf-bundle.yamlld` — semantic source document
- `bundle/okf-bundle.jsonld` — equivalent JSON-LD projection
- `bundle/okf-explorer.json` — Explorer descriptor
- `bundle/data/manifest.json` — immutable record/search/adjacency manifest
- `release/manifest.yaml` — snapshot, checksums and release status
- `release/requirements-status.json` — acceptance status for all 95 requirements

## Reproduce and validate

Python 3.12 or later is sufficient for the deterministic core.

```sh
python3 scripts/import_contract.py --check
python3 scripts/preflight_sources.py --check
python3 scripts/run_pipeline.py build --fixture
python3 -m unittest discover -s tests -v
python3 scripts/check_publication.py
python3 scripts/build_checksums.py --check
```

Current operational decisions, source constraints, costs and human-only gates
are published under `governance/`, `research/`, `provenance/` and `reports/`.
Running the network preflight itself requires `python3
scripts/preflight_sources.py --live`; the normal CI check is offline and verifies
the frozen response metadata, counts, hashes and explicit failures.


## Licensing and attribution

Project-authored code and documentation are MIT licensed. Reused Crown
copyright metadata is attributed and made available under the Open Government
Licence v3.0 where that licence applies. Item-level third-party or restricted
material remains governed by its source terms and is not silently republished.
See [LICENSE.md](LICENSE.md) and the machine-readable constraint ledger.
