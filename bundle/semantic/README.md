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

The profile is an extension of OKF v0.1. YAML-LD, JSON-LD, typed assertions,
PROV/SKOS/ORG/CPSV mappings and large-corpus indexes are profile features, not
claims about guarantees made by base OKF.
