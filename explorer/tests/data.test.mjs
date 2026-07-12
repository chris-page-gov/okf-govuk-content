import test from "node:test";
import assert from "node:assert/strict";

import { descriptorCandidates, LargeCorpusStore, referencePath, resolveReference, SearchClient } from "../src/data.js";

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });
}

test("descriptor and resource references remain portable across Pages paths", () => {
  const candidates = descriptorCandidates("https://example.test/project/explorer/src/", "", "../../okf-explorer.json");
  assert.equal(candidates[0], "https://example.test/project/explorer/src/okf-explorer.json");
  assert.ok(candidates.includes("https://example.test/project/okf-explorer.json"));
  assert.equal(referencePath({ path: "data/overview.json", sha256: "abc" }), "data/overview.json");
  assert.equal(resolveReference("data/overview.json", "https://example.test/project/okf-explorer.json"), "https://example.test/project/data/overview.json");
});

test("large-corpus bootstrap stays overview-first and route adjacency loads one bucket", async (context) => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  const payloads = new Map([
    ["https://example.test/project/data/manifest.json", { snapshot: "snap-1", indexes: { overview: "data/overview.json", analysis: "data/analysis.json", relationship_adjacency: "data/adjacency/manifest.json", route_index: "data/routes/manifest.json" }, chunks: { datasets: ["data/records-0.json"], publishers: [], resources: [] } }],
    ["https://example.test/project/data/overview.json", { title: "Overview", counts: { records: 1 }, sample_records: [{ name: "one", title: "One", open: "dataset/one" }] }],
    ["https://example.test/project/data/analysis.json", { schema: "okf-explorer-analysis.v1", facet_analysis: [] }],
    ["https://example.test/project/data/adjacency/manifest.json", { schema: "okf-relationship-adjacency.v1", algorithm: "fnv1a32-prefix-2", buckets: { 83: "data/adjacency/83.json" } }],
    ["https://example.test/project/data/adjacency/83.json", { "dataset/dataset-one": [{ source: "dataset/dataset-one", target: "publisher/publisher-one", kind: "published by" }] }],
    ["https://example.test/project/data/routes/manifest.json", { schema: "okf-route-index.v1", algorithm: "fnv1a32-prefix-2", entry_shape: "identifier-to-typed-matches", chunk_size: 1000, buckets: { "b8": "data/routes/b8.json" } }],
    ["https://example.test/project/data/routes/b8.json", { "dataset/one": [{ kind: "datasets", ordinal: 0, open: "dataset/one" }] }],
    ["https://example.test/project/data/records-0.json", [{ name: "one", title: "One full", open: "dataset/one" }]]
  ]);
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return payloads.has(String(url)) ? jsonResponse(payloads.get(String(url))) : jsonResponse({ error: "missing" }, 404);
  };
  context.after(() => { globalThis.fetch = originalFetch; });
  const descriptor = { kind: "okf-large-corpus", entrypoints: { data_manifest: "data/manifest.json" }, counts: {} };
  const store = new LargeCorpusStore("https://example.test/project/okf-explorer.json", descriptor, "https://example.test");
  await store.bootstrap();
  assert.equal(store.snapshotId(), "snap-1");
  assert.equal(store.overviewRecords()[0].route, "dataset/one");
  assert.equal(calls.includes("https://example.test/project/data/records-0.json"), false);
  const relationships = await store.loadRelationships("dataset/dataset-one");
  assert.equal(relationships[0].kind, "published by");
  assert.equal(calls.includes("https://example.test/project/data/relationships-0.json"), false);
  const record = await store.loadRecord("dataset/one");
  assert.equal(record.title, "One full");
});

test("search client preserves request identity and cancellation semantics", async () => {
  class MockWorker {
    constructor() {
      this.messages = [];
      this.onmessage = null;
      this.terminated = false;
    }
    postMessage(message) { this.messages.push(message); }
    terminate() { this.terminated = true; }
    respond(message) { this.onmessage({ data: message }); }
  }
  const worker = new MockWorker();
  const client = new SearchClient(() => worker);
  const ready = client.init("https://example.test/", "https://example.test/data/search/manifest.json");
  worker.respond({ type: "ready", id: 1, manifest: { schema: "okf-static-search.v1" } });
  await assert.doesNotReject(ready);
  const pending = client.query("passport");
  worker.respond({ type: "cancelled", id: 2 });
  await assert.rejects(pending, { name: "AbortError" });
  client.destroy();
  assert.equal(worker.terminated, true);
});
