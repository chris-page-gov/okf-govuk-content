# Citation verification contract

The released citation corpus is collected from Markdown links under the
controlling plan, research, governance, semantic, documentation, README and
reports, plus the explicit source contracts in `source-registry.yaml`.
Links may span Markdown lines; collection is based on the complete document,
not line-local regular-expression matches.
Namespace IRIs, schema identifiers, generated bibliography links and raw probe
payload URLs are not narrative citations; the latter remain covered by their
own response hashes.

The verifier keeps three judgements separate:

1. deterministic checks establish reachability, redirect identity, publisher
   and document identity markers, locator presence, short-excerpt fingerprints,
   retrieval time, version or commit and complete-document SHA-256;
2. a separate manual review decides whether the located evidence semantically
   supports the exact released claim, and is invalidated whenever the claim or
   document hash changes;
3. a claim citing multiple sources additionally needs a joint review proving
   that the exact cited set covers the whole claim. It is bound to the current
   per-citation review IDs, so replacing any constituent review invalidates it;
4. release policy accepts only an entailed review or an explicit, dated,
   owned, non-dependent waiver for a non-material reference. A material claim
   cannot be waived as non-dependent.

Token overlap and locator existence never set semantic support. This means a
fresh evidence fetch can make the release fail until a reviewer examines the
new document version. That is deliberate.

The live fetch uses Python's default strict certificate and cipher policy. It
does not disable certificate validation, lower OpenSSL security levels or retry
over plain HTTP; HTTPS-to-HTTP redirects fail closed. JSON evidence resolves
the declared JSON Pointer and XML evidence records and hashes the parsed root.
Only minimal metadata, short excerpts and hashes are retained;
complete third-party documents are processed transiently and discarded.

`fetch` creates `citation-review-packet.jsonl` and, for multi-source claims,
`claim-review-packet.jsonl`. Reviewers write decisions separately to
`citation-support-reviews.jsonl` and `claim-support-reviews.jsonl`; packet files
never contain a suggested semantic verdict. Individual reviews bind the claim,
document, locator and excerpt hashes. Joint reviews bind the exact citation IDs
and their current review IDs.

Commands:

```sh
python3 scripts/verify_citations.py collect
python3 scripts/verify_citations.py fetch
python3 scripts/verify_citations.py verify --snapshot-id SNAPSHOT-ID
python3 scripts/verify_citations.py verify --snapshot-id SNAPSHOT-ID --check
```

The final command is fully offline. It regenerates the current released claim
inventory in memory and fails on missing, stale or orphan evidence, review or
waiver records. The non-check form also writes the bibliography, claim/citation
ledgers, provenance ledger, human report and snapshot-bound release report.
