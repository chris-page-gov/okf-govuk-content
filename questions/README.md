# Evaluation question artefacts

There are two deliberately separate tiers.

## Design matrix v1

`bindings/`, `gold/` and `persona-suites/` are deterministic design fixtures.
They exercise the original 10 × 10 construction, but they have one story per
persona, synthetic wording and unassigned gold targets.  They are explicitly
`development_only`; they cannot satisfy Acceptance Gate 6 and must never set
`question_contract_passed`.

## Corpus-anchored matrix v2

`scripts/build_question_matrix_v2.py` consumes a frozen source-record JSONL or
JSONL.GZ and produces six source-anchored stories per persona, 100 concrete
questions per story, exactly 100 curated questions per persona, assigned gold,
near misses, typed paths, entity-grouped splits and immutable checksums.  The
generator records zero model use and does not claim that it verified its own
assignments.

Run the independent deterministic verifier as a separate process:

```sh
python3 scripts/build_question_matrix_v2.py \
  --mode release \
  --corpus corpus/runs/T0/source-records.jsonl.gz \
  --snapshot-id T0-YYYYMMDD \
  --snapshot-date YYYY-MM-DD \
  --snapshot-manifest corpus/runs/T0/manifest.json \
  --reconciliation corpus/runs/T0/reconciliation.json \
  --output questions/release-v2

python3 scripts/verify_question_matrix_v2.py \
  --matrix questions/release-v2 \
  --corpus corpus/runs/T0/source-records.jsonl.gz \
  --require-release
```

Release mode fails closed when the snapshot is sampled or fixture-labelled,
the reconciliation is absent or has non-zero unexplained omissions, fewer than
48 primary personas are present, a story anchor had to be reused, or any target,
path, resource, near miss, split, checksum, duplicate or leakage check fails.
The verifier validates metadata discovery gold only; authoritative body-content
answers and human preference require their separately authorised evaluation.

