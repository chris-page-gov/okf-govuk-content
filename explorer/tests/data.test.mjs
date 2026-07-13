import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { gzipSync } from "node:zlib";

import { descriptorCandidates, fetchJson, integrityReference, LargeCorpusStore, referenceHash, referencePath, resolveReference, SearchClient } from "../src/data.js";
import { prepareReleaseDataPlane } from "../src/release-data-plane.js";

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });
}

function canonicalJson(value) {
  if (Array.isArray(value)) return "[" + value.map((item) => canonicalJson(item)).join(",") + "]";
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort().map((key) => JSON.stringify(key) + ":" + canonicalJson(value[key])).join(",") + "}";
  }
  return JSON.stringify(value);
}

function releasePackFixture() {
  const identitySource = Buffer.from(JSON.stringify({ kind: "identity" }) + "\n");
  const identityTransport = gzipSync(identitySource);
  const originalGzipSource = gzipSync(Buffer.from(JSON.stringify({ kind: "original-gzip" }) + "\n"));
  const packBytes = Buffer.concat([identityTransport, originalGzipSource]);
  const assetName = "okf-govuk-data-v1.2.3-00000.pack.gz";
  const packs = [{
    id: "pack-00000",
    asset_name: assetName,
    path: "data-packs/" + assetName,
    release_url: "https://github.com/chris-page-gov/okf-govuk-content/releases/download/v1.2.3/" + assetName,
    bytes: packBytes.byteLength,
    sha256: createHash("sha256").update(packBytes).digest("hex")
  }];
  const entries = [
    {
      path: "data/identity.json",
      pack: "pack-00000",
      offset: 0,
      bytes: identitySource.byteLength,
      sha256: createHash("sha256").update(identitySource).digest("hex"),
      compression: "identity",
      packed_bytes: identityTransport.byteLength,
      packed_sha256: createHash("sha256").update(identityTransport).digest("hex"),
      transport_compression: "gzip"
    },
    {
      path: "data/original.json.gz",
      pack: "pack-00000",
      offset: identityTransport.byteLength,
      bytes: originalGzipSource.byteLength,
      sha256: createHash("sha256").update(originalGzipSource).digest("hex"),
      compression: "gzip",
      packed_bytes: originalGzipSource.byteLength,
      packed_sha256: createHash("sha256").update(originalGzipSource).digest("hex"),
      transport_compression: "identity"
    }
  ];
  const document = {
    schema: "govuk-okf-github-release-pack-index.v1",
    schema_version: "1.0",
    algorithm: "concatenated-byte-ranges-v1",
    repository: "chris-page-gov/okf-govuk-content",
    tag: "v1.2.3",
    snapshot: "snap-1",
    max_pack_bytes: 67108864,
    packs,
    entries,
    counts: {
      packs: 1,
      virtual_shards: 2,
      packed_bytes: packBytes.byteLength,
      source_bytes: identitySource.byteLength + originalGzipSource.byteLength
    }
  };
  document.index_root_sha256 = createHash("sha256")
    .update(canonicalJson({ algorithm: document.algorithm, packs, entries }) + "\n")
    .digest("hex");
  return { document, packBytes };
}

test("descriptor and resource references remain portable across Pages paths", () => {
  const candidates = descriptorCandidates("https://example.test/project/explorer/src/", "", "../../okf-explorer.json");
  assert.equal(candidates[0], "https://example.test/project/explorer/src/okf-explorer.json");
  assert.ok(candidates.includes("https://example.test/project/okf-explorer.json"));
  assert.equal(referencePath({ path: "data/overview.json", sha256: "abc" }), "data/overview.json");
  assert.equal(referenceHash({ path: "data/overview.json", sha256: "a".repeat(64) }), "a".repeat(64));
  assert.throws(() => referenceHash({ path: "data/overview.json", sha256: "abc" }), /malformed/);
  assert.deepEqual(
    integrityReference("data/records.json.gz", [{ path: "data/records.json.gz", sha256: "c".repeat(64) }]),
    { path: "data/records.json.gz", sha256: "c".repeat(64) }
  );
  assert.throws(() => integrityReference("data/missing.json", [], "Record shard"), /no integrity metadata/);
  assert.equal(resolveReference("data/overview.json", "https://example.test/project/okf-explorer.json"), "https://example.test/project/data/overview.json");
});

test("gzip resource integrity covers published compressed bytes before bounded decoding", async (context) => {
  const originalFetch = globalThis.fetch;
  const encoded = gzipSync(Buffer.from(JSON.stringify({ snapshot: "snap-1" }) + "\n"));
  const sha256 = createHash("sha256").update(encoded).digest("hex");
  globalThis.fetch = async () => new Response(encoded, {
    status: 200,
    headers: { "content-length": String(encoded.byteLength), "content-type": "application/gzip" }
  });
  context.after(() => { globalThis.fetch = originalFetch; });
  const result = await fetchJson(
    { path: "data/shard.json.gz", sha256 },
    "https://example.test/",
    { currentOrigin: "https://example.test" }
  );
  assert.deepEqual(result.value, { snapshot: "snap-1" });
  await assert.rejects(
    fetchJson(
      { path: "data/shard.json.gz", sha256: "0".repeat(64) },
      "https://example.test/",
      { currentOrigin: "https://example.test" }
    ),
    /integrity check failed/
  );
});

test("same-origin range packs recover transport-gzip identity and original-gzip shards", async (context) => {
  const originalFetch = globalThis.fetch;
  const { document, packBytes } = releasePackFixture();
  const prepared = await prepareReleaseDataPlane(document, "https://example.test/project/", "snap-1");
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), range: options.headers && options.headers.Range });
    const match = /^bytes=(\d+)-(\d+)$/.exec(options.headers.Range);
    const start = Number(match[1]);
    const end = Number(match[2]);
    return new Response(packBytes.subarray(start, end + 1), {
      status: 206,
      headers: {
        "content-length": String(end - start + 1),
        "content-range": `bytes ${start}-${end}/${packBytes.byteLength}`
      }
    });
  };
  context.after(() => { globalThis.fetch = originalFetch; });

  const identity = await fetchJson(
    { path: document.entries[0].path, sha256: document.entries[0].sha256 },
    "https://example.test/project/",
    { currentOrigin: "https://example.test", releaseDataPlane: prepared }
  );
  const originalGzip = await fetchJson(
    { path: document.entries[1].path, sha256: document.entries[1].sha256 },
    "https://example.test/project/",
    { currentOrigin: "https://example.test", releaseDataPlane: prepared }
  );
  assert.deepEqual(identity.value, { kind: "identity" });
  assert.deepEqual(originalGzip.value, { kind: "original-gzip" });
  assert.equal(calls[0].url, "https://example.test/project/data-packs/okf-govuk-data-v1.2.3-00000.pack.gz");
  assert.equal(calls[0].range, `bytes=0-${document.entries[0].packed_bytes - 1}`);
  assert.equal(calls[1].range, `bytes=${document.entries[1].offset}-${packBytes.byteLength - 1}`);
});

test("range-pack transport tampering and wrong Content-Range fail closed", async (context) => {
  const originalFetch = globalThis.fetch;
  const { document, packBytes } = releasePackFixture();
  const prepared = await prepareReleaseDataPlane(document, "https://example.test/base/path/", "snap-1");
  const entry = document.entries[0];
  let wrongRange = false;
  globalThis.fetch = async (_url, options = {}) => {
    const match = /^bytes=(\d+)-(\d+)$/.exec(options.headers.Range);
    const start = Number(match[1]);
    const end = Number(match[2]);
    const payload = Buffer.from(packBytes.subarray(start, end + 1));
    if (!wrongRange) payload[0] ^= 0xff;
    return new Response(payload, {
      status: 206,
      headers: {
        "content-length": String(payload.byteLength),
        "content-range": wrongRange ? `bytes ${start}-${end}/${packBytes.byteLength + 1}` : `bytes ${start}-${end}/${packBytes.byteLength}`
      }
    });
  };
  context.after(() => { globalThis.fetch = originalFetch; });
  await assert.rejects(
    fetchJson(
      { path: entry.path, sha256: entry.sha256 },
      "https://example.test/base/path/",
      { currentOrigin: "https://example.test", releaseDataPlane: prepared }
    ),
    /(integrity check failed|not gzip-framed)/
  );
  wrongRange = true;
  await assert.rejects(
    fetchJson(
      { path: entry.path, sha256: entry.sha256 },
      "https://example.test/base/path/",
      { currentOrigin: "https://example.test", releaseDataPlane: prepared }
    ),
    /Content-Range differs/
  );
});

test("range-pack index cannot weaken the 64 MiB and 900-pack contract", async () => {
  const { document } = releasePackFixture();
  document.max_pack_bytes = 64 * 1024 * 1024 + 1;
  await assert.rejects(
    prepareReleaseDataPlane(document, "https://example.test/project/", "snap-1"),
    /pack ceiling.*64 MiB/
  );
  const fixture = releasePackFixture();
  fixture.document.packs = Array.from({ length: 901 }, (_, ordinal) => ({
    ...fixture.document.packs[0],
    id: `pack-${String(ordinal).padStart(5, "0")}`
  }));
  await assert.rejects(
    prepareReleaseDataPlane(fixture.document, "https://example.test/project/", "snap-1"),
    /fewer than 100 Release asset slots/
  );
});

test("large-corpus bootstrap rejects mixed snapshot declarations", async (context) => {
  const originalFetch = globalThis.fetch;
  const payloads = new Map([
    ["https://example.test/project/data/manifest.json", { snapshot: "snap-manifest", indexes: { overview: "data/overview.json", analysis: "data/analysis.json" }, chunks: {} }],
    ["https://example.test/project/data/overview.json", { snapshot: "snap-overview", sample_records: [] }],
    ["https://example.test/project/data/analysis.json", { snapshot: "snap-analysis" }]
  ]);
  globalThis.fetch = async (url) => jsonResponse(payloads.get(String(url)));
  context.after(() => { globalThis.fetch = originalFetch; });
  const descriptor = { kind: "okf-large-corpus", snapshot: "snap-descriptor", entrypoints: { data_manifest: "data/manifest.json" } };
  const store = new LargeCorpusStore("https://example.test/project/okf-explorer.json", descriptor, "https://example.test");
  await assert.rejects(store.bootstrap(), /different snapshot identifiers/);
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
  const manifestReference = { path: "data/search/manifest.json", sha256: "b".repeat(64) };
  const ready = client.init("https://example.test/", manifestReference, "snap-1");
  assert.deepEqual(worker.messages[0].manifestReference, manifestReference);
  assert.equal(worker.messages[0].snapshotId, "snap-1");
  worker.respond({ type: "ready", id: 1, manifest: { schema: "okf-static-search.v1" } });
  await assert.doesNotReject(ready);
  const pending = client.query("passport");
  worker.respond({ type: "cancelled", id: 2 });
  await assert.rejects(pending, { name: "AbortError" });
  client.destroy();
  assert.equal(worker.terminated, true);
});
