import test from "node:test";
import assert from "node:assert/strict";

import {
  DOC_MAP_PARTITIONING_CONTRACT,
  mapWithConcurrency,
  POSTINGS_PARTITIONING_CONTRACT,
  QueryBudget,
  SEARCH_LIMITS,
  validateSearchManifest
} from "../src/search-contract.js";

function manifest(overrides = {}) {
  return {
    schema: "okf-static-search.v1",
    snapshot: "snap-1",
    token_min_length: 2,
    prefix_min_length: 3,
    lexicon_shard_length: 2,
    result_limit: 200,
    result_doc_chunk_size: 1000,
    counts: { max_postings_per_token: 2000 },
    entrypoints: {
      doc_map: "data/search/doc-map.json",
      result_docs: [],
      lexicon: {},
      postings: [],
      prefixes: {}
    },
    ...overrides
  };
}

test("search manifest limits and snapshot are fixed by the client", () => {
  assert.equal(validateSearchManifest(manifest(), "snap-1").result_limit, 200);
  assert.throws(() => validateSearchManifest(manifest({ result_limit: 1000000 }), "snap-1"), /result_limit/);
  assert.throws(() => validateSearchManifest(manifest(), "snap-2"), /snapshot differs/);
  assert.throws(
    () => validateSearchManifest(manifest({ counts: { max_postings_per_token: SEARCH_LIMITS.maxPostingsPerToken + 1 } }), "snap-1"),
    /max_postings_per_token/
  );
});

test("search manifest accepts legacy singleton and versioned partition entrypoints", () => {
  assert.equal(
    validateSearchManifest(manifest(), "snap-1").entrypoints.doc_map,
    "data/search/doc-map.json"
  );
  const partitioned = manifest({
    postings_partitioning: { ...POSTINGS_PARTITIONING_CONTRACT },
    doc_map_partitioning: { ...DOC_MAP_PARTITIONING_CONTRACT },
    counts: {
      max_postings_per_token: 2000,
      postings_shards: 2,
      doc_map_shards: 2
    },
    entrypoints: {
      doc_map: ["data/search/doc-map-00000.json", "data/search/doc-map-00001.json"],
      result_docs: [],
      lexicon: {},
      postings: ["data/search/postings/ca-00000.json", "data/search/postings/ca-00001.json"],
      prefixes: {}
    }
  });
  assert.equal(validateSearchManifest(partitioned, "snap-1").entrypoints.postings.length, 2);
});

test("search manifest rejects contract drift, wrong doc-map shape, and shard-count drift", () => {
  assert.throws(
    () => validateSearchManifest(manifest({
      postings_partitioning: { ...POSTINGS_PARTITIONING_CONTRACT, token_atomic: false }
    })),
    /unsupported or has drifted/
  );
  assert.throws(
    () => validateSearchManifest(manifest({ postings_partitioning: null })),
    /contract is malformed/
  );
  assert.throws(
    () => validateSearchManifest(manifest({ doc_map_partitioning: null })),
    /contract is malformed/
  );
  assert.throws(
    () => validateSearchManifest(manifest({
      postings_partitioning: { ...POSTINGS_PARTITIONING_CONTRACT, token_atomic: 1 }
    })),
    /unsupported or has drifted/
  );
  assert.throws(
    () => validateSearchManifest(manifest({
      postings_partitioning: { ...POSTINGS_PARTITIONING_CONTRACT },
      lexicon_shard_length: 3
    })),
    /logical lexicon width/
  );
  assert.throws(
    () => validateSearchManifest(manifest({
      doc_map_partitioning: { ...DOC_MAP_PARTITIONING_CONTRACT }
    })),
    /path list/
  );
  assert.throws(
    () => validateSearchManifest(manifest({ counts: {
      max_postings_per_token: 2000,
      postings_shards: 1
    } })),
    /postings_shards/
  );
});

test("query budget fails closed at aggregate resource boundaries", () => {
  const budget = new QueryBudget({
    ...SEARCH_LIMITS,
    maxDecodedBytesPerQuery: 8,
    maxDocumentsPerQuery: 2,
    maxPostingRowsPerQuery: 3,
    maxQueryResources: 1
  });
  budget.consumeDecodedBytes(8);
  assert.throws(() => budget.consumeDecodedBytes(1), /decoded-byte/);
  budget.consumeDocuments(2);
  assert.throws(() => budget.consumeDocuments(1), /document materialisation/);
  budget.consumePostingRows(3);
  assert.throws(() => budget.consumePostingRows(1), /posting-row/);
  budget.consumeResource();
  assert.throws(() => budget.consumeResource(), /resource request/);
});

test("bounded mapper never exceeds its concurrency ceiling", async () => {
  let active = 0;
  let observed = 0;
  const values = await mapWithConcurrency([1, 2, 3, 4, 5], 2, async (value) => {
    active += 1;
    observed = Math.max(observed, active);
    await new Promise((resolve) => setTimeout(resolve, 1));
    active -= 1;
    return value * 2;
  });
  assert.deepEqual(values, [2, 4, 6, 8, 10]);
  assert.equal(observed, 2);
});
