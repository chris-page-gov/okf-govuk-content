# Evaluation question artefacts

There are two deliberately separate tiers.

## Design matrix v1

`bindings/`, `gold/` and `persona-suites/` are deterministic design fixtures.
They exercise the original 10 × 10 construction, but they have one story per
persona, synthetic wording and unassigned gold targets.  They are explicitly
`development_only`; they cannot satisfy Acceptance Gate 6 and must never set
`question_contract_passed`.

## Corpus-anchored matrix v2

`scripts/build_question_matrix_v2.py` consumes a frozen source-record file,
shard manifest or shard directory and produces six source-anchored stories per
persona, 100 concrete questions per story, exactly 100 curated questions per
persona, assigned gold, near misses, typed paths, entity-grouped splits and
immutable checksums.  The
generator records zero model calls for deterministic question construction and
does not claim that it verified its own assignments. The separate saturation
ledger records that exact usage/cost for model-assisted design judgement was
not available to the repository process rather than treating it as zero.

Release-v2 gold is emitted as a content-addressed
`govuk-okf-jsonl-shards.v1` catalogue, not as one unbounded JSONL file. The
matrix, contract and manifest bind the same index, canonical digest and shard
limits; the manifest also checksums every physical index and gzip shard. Both
the independent verifier and evaluator stream those shards with integrity and
size checks. The legacy `gold/catalogue.jsonl` path remains development-only.

The generator also requires the machine persona-saturation gate to pass. It
copies and hash-binds `personas/saturation.json` and the 11-dimension coverage
matrix into its contract and manifest, and carries the saturation hash and
dimension values on every story and question. This does not turn the persona
hypotheses into human evidence: final-snapshot generation and independent gold
verification remain separate, and UI preference remains `not_yet_testable`.

Run the independent deterministic verifier as a separate process:

`SOURCE_MANIFEST` is the content-addressed shard index recorded as
`hydrated_records_path` in the closing reconciliation. Resolve `SOURCE_ROOT` as
its parent directory and pass that complete directory to both commands; a
detached index is not a complete clean-room source.

```sh
python3 scripts/build_question_matrix_v2.py \
  --mode release \
  --corpus "$SOURCE_ROOT" \
  --snapshot-id T1-YYYYMMDD-closed \
  --snapshot-date YYYY-MM-DD \
  --snapshot-manifest corpus/records/T1-YYYYMMDD-closed/manifest.json \
  --reconciliation corpus/reconciliation/T1-YYYYMMDD-closed.json \
  --output questions/release-v2

python3 scripts/verify_question_matrix_v2.py \
  --matrix questions/release-v2 \
  --corpus "$SOURCE_ROOT" \
  --snapshot-manifest corpus/records/T1-YYYYMMDD-closed/manifest.json \
  --reconciliation corpus/reconciliation/T1-YYYYMMDD-closed.json \
  --require-release
```

The closing manifest embeds the exact reconciliation object used by the
independent verifier and also carries `reconciliation_path` for tools that need
the repository location. Its top-level T1 inventory and candidate-ledger
digests bind the release matrix to the independently closed census.

Release mode fails closed when the snapshot is sampled or fixture-labelled,
the reconciliation is absent or has non-zero unexplained omissions, fewer than
48 primary personas are present, a story anchor had to be reused, or any target,
path, resource, near miss, split, checksum, duplicate or leakage check fails.
The verifier validates metadata discovery gold only; authoritative body-content
answers and human preference require their separately authorised evaluation.
