import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";

import {
  DOC_MAP_PARTITIONING_CONTRACT,
  POSTINGS_PARTITIONING_CONTRACT
} from "../src/search-contract.js";

function canonicalJson(value) {
  if (Array.isArray(value)) return "[" + value.map((item) => canonicalJson(item)).join(",") + "]";
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort().map((key) => JSON.stringify(key) + ":" + canonicalJson(value[key])).join(",") + "}";
  }
  return JSON.stringify(value);
}

function bytes(value) {
  return Buffer.from(JSON.stringify(value) + "\n");
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

test("search worker follows a lexicon token into the second physical postings partition", async (context) => {
  const originalFetch = globalThis.fetch;
  const originalSelf = globalThis.self;
  const messages = [];
  const calls = [];
  globalThis.self = {
    location: new URL("https://example.test/project/explorer/"),
    postMessage(message) { messages.push(message); }
  };
  context.after(() => {
    globalThis.fetch = originalFetch;
    if (originalSelf === undefined) delete globalThis.self;
    else globalThis.self = originalSelf;
  });

  const snapshot = "snap-partitioned";
  const paths = {
    lexicon: "data/search/lexicon/ca.json",
    firstPostings: "data/search/postings/ca-00000.json",
    secondPostings: "data/search/postings/ca-00001.json",
    results: "data/search/results-0.json",
    docMap: "data/search/doc-map-00000.json"
  };
  const resources = new Map([
    [paths.lexicon, bytes([
      { token: "ca000001", df: 1, postings: paths.firstPostings },
      { token: "ca000063", df: 1, postings: paths.secondPostings }
    ])],
    [paths.firstPostings, bytes({ tokens: { ca000001: [[0, 16, 1]] } })],
    [paths.secondPostings, bytes({ tokens: { ca000063: [[1, 16, 1]] } })],
    [paths.results, bytes([
      { ordinal: 0, open: "dataset/first", title: "First" },
      { ordinal: 1, open: "dataset/second", title: "Second" }
    ])],
    [paths.docMap, bytes({ 0: "dataset/first", 1: "dataset/second" })]
  ]);
  const metadata = (path) => ({ path, sha256: sha256(resources.get(path)), snapshot });
  const shards = {
    result_docs: [metadata(paths.results)],
    lexicon: [metadata(paths.lexicon)],
    postings: [metadata(paths.firstPostings), metadata(paths.secondPostings)],
    prefixes: [],
    doc_map: [metadata(paths.docMap)]
  };
  const shardDocument = { snapshot, shards };
  const manifest = {
    schema: "okf-static-search.v1",
    snapshot,
    token_min_length: 2,
    prefix_min_length: 3,
    lexicon_shard_length: 2,
    result_limit: 200,
    result_doc_chunk_size: 1000,
    counts: {
      max_postings_per_token: 2000,
      postings_shards: 2,
      doc_map_shards: 1
    },
    entrypoints: {
      doc_map: [paths.docMap],
      result_docs: [paths.results],
      lexicon: { ca: paths.lexicon },
      postings: [paths.firstPostings, paths.secondPostings],
      prefixes: {}
    },
    postings_partitioning: { ...POSTINGS_PARTITIONING_CONTRACT },
    doc_map_partitioning: { ...DOC_MAP_PARTITIONING_CONTRACT },
    shard_metadata: "data/search/shards.json",
    shard_manifest_sha256: sha256(Buffer.from(canonicalJson(shards) + "\n"))
  };
  resources.set("data/search/manifest.json", bytes(manifest));
  resources.set("data/search/shards.json", bytes(shardDocument));

  globalThis.fetch = async (url) => {
    const resolved = new URL(String(url));
    const relative = resolved.pathname.replace(/^\/project\//, "");
    calls.push(relative);
    const payload = resources.get(relative);
    if (!payload) return new Response("missing", { status: 404 });
    return new Response(payload, {
      status: 200,
      headers: { "content-length": String(payload.byteLength), "content-type": "application/json" }
    });
  };

  await import(`../src/search.worker.js?physical-partition=${Date.now()}`);
  await globalThis.self.onmessage({
    data: {
      type: "init",
      id: 1,
      baseUrl: "https://example.test/project/",
      manifestReference: "data/search/manifest.json",
      snapshotId: snapshot
    }
  });
  assert.equal(messages.at(-1).type, "ready");

  await globalThis.self.onmessage({
    data: { type: "query", id: 2, query: "ca000063" }
  });
  const response = messages.at(-1);
  assert.equal(response.type, "results", response.error);
  assert.equal(response.results[0].open, "dataset/second");
  assert.ok(calls.includes(paths.secondPostings));
  assert.equal(calls.includes(paths.firstPostings), false);
});
