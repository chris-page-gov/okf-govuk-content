# Security review and remediation record

Status: completed pre-release security campaign; final release-snapshot scan
still required

Report date: 13 July 2026
Post-fix revision: `27890dc442f0bf1dcc955bd252468d5c1b68a981`

## Executive summary

The repository-wide Codex Security scan that was not visible in the product UI
did complete. Scan `9321b899-8ddf-479e-a7f6-a76ec3d34ba6` reviewed revision
`5f1eb4aaf6a81aec2e393e27589096dd17fd127e`, validated 14 findings and was
sealed on 12 July 2026. Its severity distribution was five medium and nine low;
it found no critical or high issue.

The remediation range through revision
`f0fa5b266a004111cd8bb3e63df795182edb3f39` fixed those 14 findings. A separate
full-file diff scan, `5427ec62-8388-4ebe-8458-4d382596edf0`, independently
confirmed the original remediations and reported three residual low-severity
issues. Commit `27890dc442f0bf1dcc955bd252468d5c1b68a981` fixes all three.

I reviewed both sealed scan manifests and finding sets against the repository
history, inspected the final fixes, and ran the 32 focused source-preflight,
sharded-JSONL and question-matrix regression tests at the post-fix revision;
all passed. I did not exercise private services, production CI network
topology, credentials or deliberate large resource exhaustion, and a third
Codex Security product scan has not yet been run over `27890dc`.

This is therefore the terminal record for the completed pre-release scan and
remediation campaign, not the release gate for the eventual hydrated corpus.
`release/security-scan.json` is intentionally absent and
`release/status.json.security_scan_passed` remains false. The final candidate
must be scanned again after the full corpus and release artefacts are frozen.

## Scope and evidence identity

The first scan covered the entire repository: the static Explorer, acquisition
and corpus pipelines, semantic/search/adjacency assets, local controller and
publication surfaces. It reviewed 149 selected full-file inventory units, 20
focused validation packages and 19 attack-path analyses. Its safe proofs were
local and bounded; no production network or large-allocation proof was used.

| Evidence | Revision or range | SHA-256 |
|---|---|---|
| Original scan manifest | `5f1eb4aaf6a81aec2e393e27589096dd17fd127e` | `faa387e2ce5d5e86765f5339033489d1e1310b2397dbdae61e94b7847f0ed517` |
| Original findings | 14 validated | `c42dcc3474cac401ebc05accf08b5e16c2a3ea2d0ccae7843c88cf07583bcb03` |
| Original coverage | repository-wide | `b1d34b656a50f2492214a8489b7f0e11c4c488ff40035367c0a4c7dff70a0e50` |
| Remediation diff-scan manifest | `5f1eb4a...f0fa5b2` | `ba59f3d0d93dfebe87286e57d19ef6bac2feb809711730c7506f8b4be7f7e5ac` |
| Remediation diff-scan findings | 3 validated low | `039d909793e4f192f5828fdc2b9dca93229f4c332bb74cebab1582b300e182a9` |
| Remediation diff-scan coverage | 430 exact diff files | `025f18e07a5138500efda6b052f167675c1145c8d51740b388e1df66a01a258e` |
| Remediation diff-scan report | generated scan projection | `dbac15702aa1d439a8e02ca6100783770ab0d669b86bfe9f441bf271b12cf2d6` |

The second scan reviewed 430 exact diff files with full-file receipts, three
focused validation packages, three attack-path analyses, 186 Python tests, 31
Explorer tests and exact-revision real-browser CI. Its clean clone did not have
the optional semantic `node_modules` installation; semantic checks passed in
the main checkout and CI.

## Threat model

The security objective is to protect bundle and citation provenance, release
completeness, browser and CI availability, build-account filesystem/network
authority, and append-only execution evidence. The material trust boundaries
are:

- public sources entering acquisition workers;
- repository or supplied artefacts entering generators and verifiers;
- federated HTTPS bundle data entering the fixed Explorer origin and worker;
- task contracts controlling local controller filesystem paths.

We assume an adversary may contribute repository-controlled plan, corpus,
persona or evaluation data, control a declared source host and its DNS answers,
or supply internally consistent but adversarial bundle metadata. The restored
invariants are one immutable snapshot per resource graph, fail-closed path and
network effects, contained derived paths, bounded aggregate work, and explicit
outbound destinations.

## Original scan findings and disposition

The original report's 14 validated findings are listed below. The remediation
range through `f0fa5b2`, centred on the trust-boundary changes in `fd17c51`,
addressed every item; the independent diff scan explicitly verified that set
before looking for regressions.

| ID | Severity | Finding | Disposition |
|---|---|---|---|
| `GOVUK-BEXP-MIXED-SNAPSHOT-001` | medium | Explorer accepted mixed-revision resources while displaying one descriptor snapshot | Fixed: bootstrap now requires one consistent snapshot identity |
| `GOVUK-BEXP-SEARCH-MANIFEST-INTEGRITY-001` | medium | Search-manifest SHA-256 was discarded before the worker fetch | Fixed: integrity-bearing references survive the worker boundary and are verified |
| `GOVUK-BEXP-SEARCH-RESOURCE-EXHAUSTION-001` | medium | Manifest-controlled search fan-out bypassed cache-entry ceilings | Fixed: search contracts clamp fan-out, sizes, postings and concurrent work |
| `GOVUK-BPIPE-ELIGIBILITY-RECONCILIATION-001` | medium | Release prerequisite accepted unbound or structurally incomplete reconciliation | Fixed: release eligibility is snapshot-, structure- and count-bound |
| `GOVUK-BPIPE-VERIFIER-TRUST-001` | medium | Independent verifier trusted rehashable eligibility bits without a fixed release scale | Fixed: verifier derives eligibility from trusted, count-bound release evidence |
| `GOVUK-BCONTROLLER-TASK-ID-PATH-001` | low | DAG task ID could escape task-contract materialisation root | Fixed: task identifiers and resolved output paths are contained |
| `GOVUK-BPIPE-GZIP-GENERATOR-001` | low | Question generator lacked decoded-record and line ceilings for gzip input | Fixed: shared bounded canonical JSONL reader is used |
| `GOVUK-BPIPE-GZIP-PROBE-001` | low | Probe byte ceiling was applied before unbounded gzip decompression | Fixed: streaming decompression enforces decoded-byte ceilings |
| `GOVUK-BPIPE-GZIP-VERIFIER-001` | low | Question verifier repeated unbounded gzip decompression | Fixed: verifier uses the same bounded reader |
| `GOVUK-BPIPE-PATH-BINDING-001` | low | Persona ID traversal escaped through story-binding filenames | Fixed: identifier-derived output paths are validated and contained |
| `GOVUK-BPIPE-PATH-SUITE-001` | low | Persona ID traversal escaped through suite filenames | Fixed: suite paths use the same fail-closed containment contract |
| `GOVUK-BPIPE-SSRF-PLAN-001` | low | Repository plan URL could select private or non-HTTPS probe destinations | Fixed: HTTPS, public-address and approved-host policy is mandatory |
| `GOVUK-BPIPE-SSRF-REDIRECT-001` | low | Default redirects could pivot an approved probe to an internal destination | Fixed: every redirect hop is revalidated against destination policy |
| `GOVUK-BRESEARCH-PATH-TRAVERSAL-001` | low | Persona seed slug escaped research-asset output roots | Fixed: slugs and resolved asset paths are contained |

No finding was waived. The remediation did not downgrade a finding or erase an
exception to pass the gate.

## Residual findings from the remediation diff scan

The second scan found three low-severity primitives that survived the first
remediation. We can follow each untrusted value to a local/CI effect, but the
existing controls limited the impact and the affected interfaces were not
anonymous public request handlers.

### Unsafe question-matrix artifact references

The matrix verifier recorded a failed lexical path check but continued to join,
read and hash the unsafe path. Parent traversal, absolute paths and symlink
escapes could therefore cause local/CI file reads before the overall verifier
failed.

Commit `27890dc` now resolves references with a canonical containment helper,
returns no path after a safety failure and applies the bounded control-JSON
reader only after containment succeeds. The end-to-end regression test verifies
that a parent reference is rejected and that the outside sentinel path is never
passed to the loader.

### DNS rebinding between policy validation and TLS connection

The source probe previously validated a public DNS response, discarded it and
allowed the default HTTPS connection to resolve the host again. A controlled
host could return a public address for validation and a private address for the
connection. The initial source URL could also expand an otherwise empty host
policy.

Commit `27890dc` requires an explicit host allowlist, disables environment
proxies, resolves and rejects non-public addresses at connection time, connects
to the validated address, and preserves the original hostname for TLS/SNI and
certificate verification. Redirects reuse the same resolver and allowlist.

### Aggregate shard-index resource amplification

The shared shard reader bounded each shard but did not bound the index or the
aggregate corpus work. An internally consistent index could repeat one valid
shard and multiply hashing, decompression, parsing and downstream accumulation.

Commit `27890dc` preflights index bytes, shard count, declared aggregate record
count, aggregate compressed and decoded bytes, bounded integer fields and
unique resolved shard paths before expensive processing. Per-shard integrity
and canonical JSONL checks remain in force.

## Post-fix validation at `27890dc`

The following focused command was run at the post-fix commit:

```sh
.venv/bin/python -m unittest \
  tests.test_source_preflight \
  tests.test_sharded_jsonl \
  tests.test_question_matrix_v2 -v
```

All 32 tests passed. The security-specific cases demonstrate:

- an explicit probe host policy and no inherited proxy use;
- one validated public DNS answer reused for the TLS connection, with private
  connection-time answers rejected and the original hostname retained for TLS;
- redirect-hop revalidation and bounded gzip probing;
- rejection of duplicate shard paths, excess shards, aggregate record,
  compressed and decoded ceilings before hashing or decompression;
- bounded per-shard compressed bytes, decoded bytes and single-line records;
- parent, absolute and symlink matrix-reference rejection before I/O;
- end-to-end verifier refusal to load an outside matrix artifact.

The broader original-remediation evidence remains the sealed diff scan: it
found no new issue in the changed Explorer browser/search/route runtime,
remaining Python/JavaScript runtime, generated bundle/evidence artefacts or
supporting contracts after the 14-finding remediation.

## Residual risk and release boundary

The remaining risk is bounded but not zero:

- tests use controlled DNS/socket substitutes and temporary artefacts rather
  than a production CI network or hostile public service;
- deliberate browser or build-host resource exhaustion was not performed;
- installed dependency source was represented by lockfiles, workflows and the
  SBOM rather than recursively reviewed as application source;
- later corpus hydration, generated release shards, workflow changes or
  dependency updates can change the attack surface;
- `27890dc` has deterministic post-fix validation but not a third sealed Codex
  Security product scan.

For those reasons this report closes `ACT-D2-SECURITY-SCAN-001` only as the
pre-release scan/remediation activity. Publication must still fail closed until
the final, frozen release repository receives a completed full-release scan
with zero open critical/high findings, a hash-bound report, and any remaining
medium finding either fixed or covered by a dated owned exception. That scan
must append `ACT-D2-RELEASE-SNAPSHOT-SECURITY-SCAN-TERMINAL-001`, bind the
release snapshot and scanned commit, and supersede
`ACT-D2-SECURITY-SCAN-TERMINAL-001`; the earlier terminal is not reusable. Its
activity outputs must include SHA-256 bindings for both
`release/security-scan.json` and this report, and provenance validation
recomputes those hashes from the frozen checkout.
