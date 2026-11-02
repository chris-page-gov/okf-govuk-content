# Dependency security maintenance

This candidate updates MCP to 1.30.0, PyJWT to 2.15.0, cryptography to 50.0.0
and the semantic toolchain's transitive Undici to 6.29.1. The dependency SBOM
is regenerated from the locks using `scripts/build_sbom.py`. Regenerate the
current fixture's clean-room receipt and dependent aim scorecard using their
existing generators. The receipt records the interpreter's basename rather
than exposing a machine-specific account or checkout path.

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

No corpus acquisition, release, alert dismissal or deployment is included.
Historical release evidence retains its original meaning. Run the existing
deterministic CI gates and the new tests before considering this candidate
for merge.
