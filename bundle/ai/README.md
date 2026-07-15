# Use this bundle with an AI

This is a derived, non-authoritative 69-record metadata demonstrator for snapshot
`NEW-CHILD-20260715`. GOV.UK remains authoritative.

## Recommended portable input: question-specific context

Generate a bounded context for the actual question, then upload or paste that
result into a file-capable AI. Typical demonstrator queries produce about
22–35 KB (roughly 5,500–9,000 tokens using a simple four-characters-per-token
estimate), although the exact size depends on the matches and relationships:

```sh
uv run --project <REPOSITORY_CHECKOUT> govuk-okf-demo-query \
  --bundle <BUNDLE_DIRECTORY> context \
  "What financial help should I investigate?" --format markdown
```

Use the safety and citation instructions included in that output. If the AI
cannot accept files, paste the question-specific result into its prompt.

## Bulk/archive input

`new-child-context.md` and `new-child-context.json` contain the full handoff and
are about 830 KB (roughly 207,000 tokens by the same simple estimate). They are
useful for archival review or models with a sufficiently large context window,
but are not the universal default and may exceed some products' limits.

## Deterministic command line

From the repository checkout:

```sh
uv run govuk-okf-demo-query --bundle bundle search "help after having a baby"
uv run govuk-okf-demo-query --bundle bundle context "What financial help should I investigate?" --format markdown
```

## MCP (best for repeated, selective access)

Use the local `stdio` recipe in `mcp.json`. The server exposes five bounded,
read-only tools and never fetches arbitrary URLs. Start it with:

```sh
uv run --project <REPOSITORY_CHECKOUT> govuk-okf-demo-mcp --bundle <BUNDLE_DIRECTORY>
```

Point an MCP-capable AI client at that command. Prefer question-specific context
for a single review; prefer MCP when the assistant needs repeated search, record
retrieval, citation and relationship traversal without loading every record on
every turn.

Full instructions and client examples are in the repository's
`docs/ai-input.md`.
