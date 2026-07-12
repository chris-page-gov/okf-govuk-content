# Repository working agreement

- Read `WHATS_ON_GOVUK_OKF.md` and every controlling file in `planning/` before
  changing implementation or publication artefacts. Apply the precedence and
  stop conditions in `planning/RUN_AFHF_GOVUK_OKF_UNATTENDED.md`.
- Treat the requirements register as acceptance criteria, the traceability
  crosswalk as mandatory coverage, and source-policy or rights uncertainty as
  a visible constraint. Never narrow scope, invent evidence, or erase an
  exception to make a gate pass.
- Use public, reproducible official GOV.UK sources by default. Respect robots,
  reuse terms, host-wide rate limits, and the metadata-led boundary; do not
  retain or publish complete page bodies.
- Preserve source-native distinctions between content identities, documents,
  editions, routes, rendered parts, navigation nodes, organisations,
  lifecycle events, and resources. Every generated assertion and relationship
  must retain stable IDs, evidence, retrieval time, derivation, and confidence.
- Human-maintained Markdown and YAML-LD plus frozen source-native metadata
  envelopes are the sources of truth. Regenerate JSON-LD, Explorer descriptors,
  manifests, search shards, route adjacency, reports, checksums, and release
  notes in the same change; never hand-edit a generated projection.
- `governance/requirements.yaml` records that a requirement is accepted into
  the contract; it does not mean the implementation passed. Maintain
  `governance/implementation-status-source.json` and regenerate the requirement,
  traceability and task status projections with
  `python3 scripts/build_status_projections.py` in the same change.
- Use deterministic code for enumeration, canonicalisation, counts, schemas,
  reconciliation, hashes, sharding, duplicate checks, and metrics. Record model
  identity, parameters, usage, cost, prompts, and validation for every model
  assisted artefact, and never use the same run as sole generator and judge.
- Keep runs resumable and auditable: immutable attempt directories,
  append-only events, content-addressed artefacts, bounded task contracts, and
  redacted logs. Never commit credentials, participant data, private reasoning,
  Finder artefacts, caches, build output, or temporary worktree files.
- Keep requirements status, traceability, decisions, risks, exceptions,
  bibliography, implementation documentation, changelog, and release evidence
  synchronized with code and generated data.
- Before committing publication changes, run the unit, schema, semantic,
  provenance, corpus-reconciliation, question-contract, citation, security,
  accessibility, performance-budget, checksum, and clean-reproduction checks
  applicable to the changed scope.
- Do not claim complete corpus representation unless the frozen source union is
  reconciled with `unexplained_omissions = 0`. Do not claim that Explorer is a
  human UI of choice without authorised, completed participant research.
- Treat the default `scripts/build_bundle.py` input and the checked-in v1
  question assets as development fixtures. A release must use the hydrated
  frozen corpus plus the independently verified `questions/release-v2`
  contract and pass `scripts/check_release.py --publication-ready`.
- Develop on focused `agent/*` branches, merge through reviewed pull requests,
  keep `main` protected, and publish Pages, registry projections, checksums,
  SBOM, tags, and GitHub Releases from verified commits only.
