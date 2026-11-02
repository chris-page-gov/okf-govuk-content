# Dependency security maintenance

The repository locks MCP to 1.30.0, PyJWT to 2.15.0, cryptography to 50.0.0,
Certifi to 2026.10.19 and the semantic toolchain's transitive Undici to 6.29.1.
Certifi 2026.10.19 is outside the affected range for
`GHSA-6q48-6c7w-9gw2`. The dependency SBOM is generated from the locks using
`scripts/build_sbom.py`. Dependency changes must also regenerate the current
fixture's clean-room receipt and dependent aim scorecard using their existing
generators. The receipt records the interpreter's basename rather than
exposing a machine-specific account or checkout path.

The local MCP service remains a bounded, read-only demonstrator. These
dependency alerts do not establish exploitation of that service. The semantic
loader continues to refuse undeclared remote JSON-LD contexts.

`tests/test_dependency_security.py` uses synthetic tokens to check both decode
APIs. An unverified inspection must not mutate caller-owned options and weaken
a subsequent verified decode. Expired tokens, a wrong audience and a wrong
signature must still fail; an ordinary valid signed token must succeed. A
pre-update probe reproduced the options mutation on PyJWT 2.13.0; the same
probe rejects the expired token without mutation on 2.15.0. The advisory for
this particular behaviour has no declared first patched version, so the
behavioural check supplements version-range reconciliation.

This maintenance change did not include corpus acquisition, release, alert
dismissal or deployment. Historical release evidence retains its original
meaning. Run the existing deterministic CI gates and the dependency security
tests before merging future changes to these dependency boundaries.
