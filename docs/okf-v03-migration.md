# OKF core 0.3 migration

Status: local candidate implementation, 24 November 2026.

The active generated bundle declares OKF core 0.3. Its source remains the
unchanged `NEW-CHILD-20260715` snapshot, not a newly acquired GOV.UK dataset.
The snapshot `source-records.jsonl` has SHA-256
`827d01d031e07fa165ab18af0f0c96f8c227b13b75ca08a82f422afd93a41cf1`; its
manifest has SHA-256
`172c119ab10e736d18ce4eceb6e56f66b92e27aaaaaac64f53311d3146c0a901`.
Source observation and retrieval dates remain those recorded in the snapshot;
this implementation date does not update them.

## Core and profile boundaries

The generated source tree has 86 typed Markdown concepts: 83 content-item and
organisation concepts plus three supporting reference documents. The 83
data concepts map to the existing 69 bounded records and 14 publishers; the
three documentation concepts remain independently source-addressable. The
generated semantic metadata graph retains its 392 source-evidence-backed
assertions. Dataset and publisher runtime rows and corresponding semantic
entities carry `core_role: concept`; supporting metadata entities carry
`core_role: extension`. This role is an additive Reader hint, not a new domain
fact or an identity change.

The custom GOV.UK semantic profile v1 and context remain byte-identical to the
historical files. A new GOV.UK profile v2 and context describe the 0.3 active
projection. Bundle Wiki v1 schemas and context files remain pinned structural
dependencies, with their vendor lock retained. The Bundle Wiki v2 repository
contract schema is separately pinned; using it does not claim the GOV.UK domain
profile is itself the central Bundle Wiki profile.

The generated `index.md` declares `okf_version: "0.3"`. The validator follows
the 0.3 reserved-file and untyped ancillary-Markdown rules. It does not convert
the old v0.2 conformance guide, the historic ADR, or any negative fixture into
new-version evidence.

## Walkthrough scope and limits

The required complex-question walkthrough is bound to this single logical
69-record Explorer bundle. It uses only the frozen metadata records and their
assertions. It must show source paths and SHA-256 values, the related leave
record and publisher/organisation connections, and a negative control that
appears in childcare and financial-help metadata but not in the pregnancy and
birth source membership. Its visible Explorer links carry the matching source
title, Reader view and record route; the per-bundle journey also exercises
search and graph traversal. The snapshot cannot establish current eligibility,
current payment amounts, current page wording or present organisational
responsibility.

The directory `demo/snapshots/NEW-CHILD-20260715` is a source fixture for the
same bundle, not a second published logical bundle. It remains separately
identified from generated bundle bytes. No full GOV.UK corpus, Release 1,
zero-omission closure, machine release, human preference or participant
acceptance is claimed. The existing release controls still say
`publication_ready: false`, and human evaluation remains unauthorised.

## Reproduction

Use the repository's locked CPython 3.12.11 environment:

```sh
uv sync --locked
uv run --locked python scripts/build_bundle.py
uv run --locked python scripts/check_okf_v03.py
uv run --locked python scripts/build_bundle.py --check
uv run --locked python scripts/validate_semantics.py --require-shard-metadata --check
```

These local source and graph checks do not establish public deployment,
real-browser, AI-client, full-corpus, specialist or release acceptance. Those
gates remain separate and must be run against the exact reviewed candidate.
