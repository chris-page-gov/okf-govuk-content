# Terminal activity closure

`scripts/finalize_terminal_activity.py` is the only manual entry point for the
post-hydration terminal activities that are not owned by release promotion. It
does not accept a pre-authored ledger row. Each operation reads its contract
from `provenance/reproduction-declarations.json`, validates the canonical
artifacts and counters, constructs a schema-v2 row, and appends it through the
repository ledger lock.

The tool is offline. Publication input JSON files must be immutable captures of
the named GitHub API responses and the Pages live-smoke result; the command does
not fetch or infer those external facts.

## Invariants

- Supply exact UTC `--started-at`, `--ended-at`, and `--recorded-at` values.
  Do not replace an unavailable timestamp with the current time.
- Record `--request-start` immediately before and `--request-end` immediately
  after every request-bearing stage. Hydration and T1/closing use the shared
  `.tmp/request-budget/official-sources.count` counter. Citation review uses its
  separate collector/reviewer counter.
- Do not edit `provenance/activity-ledger.jsonl` or
  `provenance/source-request-budget.json` by hand. Existing T0 bytes are
  immutable.
- An exact rerun is a no-op. Reusing an activity ID with different timestamps,
  artifact hashes, counters, snapshots, URLs, or other evidence fails.
- Any file named by an output binding is re-hashed while the ledger lock is
  held. Symlinks, repository escapes, missing files, duplicate output paths,
  discontinuous counters, and declaration conflicts fail closed.
- Run the final release-snapshot security scan after the candidate code,
  automation, and tests stop changing. The scan evidence must use the exact
  `scripts/check_release.py::SECURITY_SCAN_INPUT_PATHS` list and current tree
  hash.

## Candidate sequence

Set the immutable identifiers and use each stage's recorded timestamps and
counters, not the illustrative values below:

```sh
PY=.venv/bin/python
T0=T0-20260712
RELEASE_ID=T1-20260713-closed

$PY scripts/finalize_terminal_activity.py hydration \
  --snapshot "$T0" \
  --started-at 2026-07-12T09:00:00Z \
  --ended-at 2026-07-13T01:00:00Z \
  --recorded-at 2026-07-13T01:00:01Z \
  --request-start 5753 --request-end 900000

$PY scripts/finalize_terminal_activity.py reconciliation \
  --snapshot "$RELEASE_ID" \
  --started-at 2026-07-13T01:01:00Z \
  --ended-at 2026-07-13T03:00:00Z \
  --recorded-at 2026-07-13T03:00:01Z \
  --request-start 900000 --request-end 910000

$PY scripts/finalize_terminal_activity.py questions \
  --snapshot "$RELEASE_ID" \
  --started-at 2026-07-13T03:01:00Z \
  --ended-at 2026-07-13T03:30:00Z \
  --recorded-at 2026-07-13T03:30:01Z

$PY scripts/finalize_terminal_activity.py evaluation \
  --snapshot "$RELEASE_ID" \
  --evaluation-run evaluation/runs/RUN_ID \
  --started-at 2026-07-13T03:31:00Z \
  --ended-at 2026-07-13T04:30:00Z \
  --recorded-at 2026-07-13T04:30:01Z

$PY scripts/finalize_terminal_activity.py citations \
  --snapshot "$RELEASE_ID" \
  --started-at 2026-07-13T04:31:00Z \
  --ended-at 2026-07-13T05:00:00Z \
  --recorded-at 2026-07-13T05:00:01Z \
  --request-start 0 --request-end 737

$PY scripts/finalize_terminal_activity.py shards \
  --snapshot "$RELEASE_ID" \
  --started-at 2026-07-13T05:01:00Z \
  --ended-at 2026-07-13T05:10:00Z \
  --recorded-at 2026-07-13T05:10:01Z

$PY scripts/finalize_terminal_activity.py security \
  --snapshot "$RELEASE_ID" \
  --scan-id SCAN_ID \
  --scanned-commit FULL_40_HEX_COMMIT \
  --started-at 2026-07-13T05:11:00Z \
  --ended-at 2026-07-13T05:40:00Z \
  --recorded-at 2026-07-13T05:40:01Z

$PY scripts/finalize_terminal_activity.py source-budget \
  --snapshot "$RELEASE_ID" \
  --started-at 2026-07-12T08:00:00Z \
  --ended-at 2026-07-13T05:41:00Z \
  --recorded-at 2026-07-13T05:41:01Z \
  --request-start 0 --request-end 910000
```

The numbers above are examples only. The hydration manifest, closing
reconciliation and live counter must prove the supplied values. The final
budget operation additionally proves that the immutable T0 census interval,
the recorded `5752..5753` interrupted pre-hardening hydration request, the
completed hydration interval and T1/closing interval are ordered and
contiguous. It holds
both the activity-ledger lock and the shared request-counter lock, writes a
recovery journal, and either commits the final budget plus terminal row or
restores both original files.

`scripts/promote_release.py promote` owns the clean-room reproduction terminal
and appends it inside the release-promotion transaction. Do not construct a
second clean-room row with this tool.

## External publication terminal

After the candidate commit, CI, tag, GitHub Release, Pages deployment and
Explorer registry PR exist, save raw API responses under a non-secret release
evidence directory. The publication-settings document is a small JSON object
with the raw `immutable_releases` and `pages` responses under keys of those
names. The branch-protection document may be either the raw response or an
object with that response under `branch_protection`.

Then provide every external identifier explicitly:

```sh
$PY scripts/finalize_terminal_activity.py publication \
  --snapshot "$RELEASE_ID" \
  --commit FULL_40_HEX_PUBLISHED_COMMIT \
  --tag v1.0.0-rc.1 \
  --repository-pr-url https://github.com/chris-page-gov/okf-govuk-content/pull/NUMBER \
  --ci-url https://github.com/chris-page-gov/okf-govuk-content/actions/runs/RUN_ID \
  --release-url https://github.com/chris-page-gov/okf-govuk-content/releases/tag/v1.0.0-rc.1 \
  --pages-url https://chris-page-gov.github.io/okf-govuk-content/ \
  --registry-pr-url https://github.com/chris-page-gov/okf-explorer/pull/NUMBER \
  --repository-pr-json release/external/repository-pr.json \
  --ci-json release/external/ci-run.json \
  --release-json release/external/github-release.json \
  --pages-smoke release/pages-live-smoke.json \
  --registry-pr-json release/external/explorer-registry-pr.json \
  --branch-protection release/external/branch-protection.json \
  --publication-settings release/external/publication-settings.json \
  --started-at 2026-07-13T06:00:00Z \
  --ended-at 2026-07-13T07:00:00Z \
  --recorded-at 2026-07-13T07:00:01Z
```

The command validates the repository/URL identities, merge commit, successful
CI head, published tag, Pages release snapshot and route/range checks, registry
PR state, protected-main read-back, immutable-release setting and Pages
settings. It writes `release/publication-verification.json`, binds its hash in
`ACT-F2-PUBLICATION-REGISTRY-TERMINAL-001`, and rejects evidence dated after the
declared end time.

After candidate terminals, regenerate provenance through the documented
promotion flow. After publication, run the finalization flow and strict
`scripts/check_release.py --finalized` validation. Never hand-edit the generated
provenance, status, aim-assessment or release projections.
