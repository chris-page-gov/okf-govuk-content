# GOV.UK OKF semantic profile

This directory is the source-controlled semantic contract for the derived,
non-authoritative GOV.UK metadata catalogue. It deliberately keeps GOV.UK's
source-native identity layers separate and uses evidence-bearing assertion
nodes for every relationship.

- `context/` contains the pinned, offline JSON-LD context.
- `profile/` contains the readable YAML-LD profile and normative narrative.
- `schemas/` contains JSON Schema 2020-12 contracts.
- `shapes/` contains the portable SHACL-like graph constraints.
- `crosswalks/` records reversible source and standards mappings.

The canonical Markdown tree conforms to OKF v0.2. Structured `generated`,
`sources`, lifecycle and trust semantics use the v0.2 core contract. YAML-LD,
JSON-LD, typed assertions, PROV/SKOS/ORG/CPSV mappings, snapshot/live state and
large-corpus indexes remain profile features, not claims about guarantees made
by base OKF.
