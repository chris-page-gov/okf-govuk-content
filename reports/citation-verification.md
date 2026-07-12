# Citation verification

Status: **PASS**
Snapshot: `fixture-2026-07-11`

- Released claims: 109
- Citation links: 171
- Unique sources: 101
- Passed: 171
- Non-dependent waivers: 0
- Per-citation failures: 0
- Joint claim reviews: 40/40 passed
- Blocking failures: 0

## Verification boundary

Transport, redirect, identity marker, locator, excerpt, hash, coverage, and binding checks are deterministic. Semantic support is accepted only from a separately recorded manual review bound to the exact claim and fetched document hashes.

A URL/title/token match never sets semantic support. The release verifier
requires a separate manual locator review bound to both the claim hash and
the fetched document hash. Any changed claim or source invalidates that review.
