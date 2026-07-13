# Evaluation harness

The automated harness is a deterministic, local metadata-discovery evaluation.
It imports a built bundle into bounded SQLite/FTS5 indexes, then runs the same
frozen wording and gold contract through the proposal, three baselines, four
one-factor ablations and two serialization-invariance controls.

Release execution is fail-closed:

- `questions/release-v2` must contain exactly 28,800 questions, 288 stories and
  48 primary personas;
- the separate v2 verifier must have passed every gold target, path, split and
  checksum;
- release gold must use the matrix-declared bounded shard index, and every
  shard is streamed only after its count, canonical digest and file digest
  bindings pass;
- question and bundle snapshot IDs must match;
- every system must run every question, with no subset flag;
- JSON-LD and YAML-LD expanded controls must produce identical deterministic
  results; and
- model calls, network requests, tokens and paid cost remain zero.

Build and independently verify a bounded fixture matrix whose snapshot matches
the checked-in fixture bundle, then run a smoke test:

```sh
python3 scripts/build_question_matrix_v2.py \
  --corpus tests/fixtures/corpus/source-records.jsonl \
  --snapshot-id fixture-2026-07-11 \
  --snapshot-date 2026-07-11 \
  --output .tmp/evaluation-questions \
  --persona-limit 1
python3 scripts/verify_question_matrix_v2.py \
  --matrix .tmp/evaluation-questions \
  --corpus tests/fixtures/corpus/source-records.jsonl
python3 scripts/run_evaluation.py \
  --questions .tmp/evaluation-questions \
  --bundle bundle \
  --output .tmp/evaluation-run \
  --run-id fixture-20260712 \
  --mode fixture \
  --question-limit 100
```

Run the final complete comparison only after the release-v2 question and bundle
snapshots have closed:

```sh
python3 scripts/run_evaluation.py \
  --questions questions/release-v2 \
  --bundle bundle \
  --output evaluation/agent-runs/release-v0.1.0 \
  --run-id release-v0.1.0 \
  --mode release
python3 scripts/project_evaluation_results.py \
  --run evaluation/agent-runs/release-v0.1.0 \
  --questions questions/release-v2 \
  --bundle bundle \
  --output evaluation/results
```

An interrupted run leaves only bounded `.work` SQLite state and a checkpoint in
its output directory. Re-run the exact command with `--resume`; changed inputs,
systems or limits are rejected. A completed run removes working indexes and
retains content-addressed gzip JSONL traces, aggregate metrics, paired
cluster-level confidence intervals, slices, failure examples, usage, status,
checksums and a readable report. The second command verifies every immutable
run file and checksum, and independently revalidates the exact current
release-v2 questions and bundle before atomically publishing the small canonical
evidence projection consumed by the aim assessment and release promoter. Generated
development checkpoints live separately in `evaluation/development`, so
research-asset regeneration cannot overwrite release evidence.

Latency is an observed property of that run and can vary with the host. Ranking,
grading, slices and confidence-interval calculations are deterministic. Raw
page or attachment bodies are never read, fetched or copied.

Human research is a separate authority gate. Until an approved participant
study genuinely runs, outputs must keep `human_evaluation_status` as
`not_authorised`, `human_ui_of_choice_status` as `not_yet_testable`,
`full_evaluation_complete` as false and `programme_complete` as false.
