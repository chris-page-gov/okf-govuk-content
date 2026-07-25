# OKF v0.2 conformance

The checked-in GOV.UK demonstrator targets Open Knowledge Format v0.2 while
retaining the richer GOV.UK/Explorer profile as additive extensions.

## Canonical and projected layers

The canonical OKF layer is Markdown:

- `bundle/index.md` declares only `okf_version: "0.2"`;
- `bundle/concepts/` contains one typed concept for every published GOV.UK
  content item, organisation and attachment;
- ordinary concept files carry `type`, `generated`, `sources` where evidence is
  available, and `status`;
- nested `index.md` files provide progressive disclosure and have no
  frontmatter;
- `bundle/log.md` is a reserved, date-grouped build history.

The following remain profile projections rather than additions to the OKF core:

- `okf-bundle.yamlld` and equivalent JSON-LD;
- evidence-bearing assertion and entity shards;
- the Explorer large-corpus descriptor, facets, search and graph indexes;
- route-scoped relationship adjacency and data-plane integrity manifests;
- AI handoff and read-only MCP discovery surfaces.

Consumers must tolerate those extensions and unknown concept types. They must
also tolerate missing optional v0.2 families and broken links, as required by
the base specification.

## Provenance, trust and lifecycle

The generator writes its own publication time to `generated.at`. It does not
reuse a GOV.UK update or retrieval time for that field.

When an official source observation includes `public_updated_at`, the concept
records only its date as `sources[].last_modified`. The full retrieval time is
kept separately under the `govuk` extension. These three times answer different
questions:

| Field | Meaning |
|---|---|
| `generated.at` | When this Markdown projection was generated |
| `sources[].last_modified` | When the official source says its content last changed |
| `govuk.retrieved_at` | When the frozen official metadata observation was retrieved |

No concept currently carries `verified`, because this migration did not perform
a v0.2 verification event. The concepts are therefore explicitly `draft` and
derive the `unverified` trust tier. `stale_after` is absent because the project
does not yet have an approved concept-review expiry policy.

## Snapshot and live authority

The Explorer descriptor's `snapshot_state` names:

- the immutable snapshot identifier;
- the publication compilation time;
- the latest source-observation time available in the snapshot;
- `https://www.gov.uk/` as the live authoritative destination;
- that snapshot drift is expected.

This makes a governed historical snapshot useful without implying that its
metadata is live. The bundle index and every canonical concept repeat the
non-authoritative/live-check boundary.

## Attested computations

The conformance validator recognises the v0.2 `Attested Computation` type and
requires its `runtime` field when present. This GOV.UK discovery bundle does not
publish an attested computation, executor or attester. Loading the bundle never
executes code; runtime receipts and attestation verdicts would remain separate
from document-level `verified` events.

## Validation

The deterministic publication validator:

1. checks every Markdown file against the reserved-file rules;
2. validates required `type`, actors, ISO dates, source resources, lifecycle
   values and Attested Computation minimum fields;
3. confirms descriptor, semantic projection and canonical Markdown entrypoints
   agree on OKF v0.2;
4. continues the existing JSON-LD/YAML-LD equivalence, provenance, integrity,
   shard, checksum and clean-room gates.

Run the focused contract gate with:

```sh
python3 scripts/check_okf_v02.py
```

It also runs as an explicit named CI step before the wider publication
validator.

The migration decision and its boundary are recorded in
[`ADR-010`](../governance/decisions/ADR-010-okf-v0.2-core-with-govuk-extensions.md).
