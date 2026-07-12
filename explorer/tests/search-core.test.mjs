import test from "node:test";
import assert from "node:assert/strict";

import { intersectSets, rankOrdinals, searchShard, tokenize } from "../src/search-core.js";

test("static search tokenisation is deterministic, accent-folded and stop-word aware", () => {
  assert.deepEqual(tokenize("The Café café driving-licence API.v2"), ["cafe", "driving-licence", "api.v2"]);
  assert.deepEqual(tokenize("a an and"), []);
  assert.equal(searchShard("Driving", 2), "dr");
  assert.equal(searchShard("é", 2), "_");
});

test("set intersection retains only shared ordinals", () => {
  assert.deepEqual([...intersectSets(new Set([1, 2, 3]), new Set([2, 3, 4]))], [2, 3]);
});

test("ranking intersects complete postings and applies title boosts", () => {
  const groups = [
    [{ df: 2, rows: [[1, 10, 1], [2, 8, 0]] }],
    [{ df: 2, rows: [[1, 4, 0], [3, 20, 1]] }]
  ];
  assert.deepEqual(rankOrdinals(groups, 20, 10), [{ ordinal: 1, score: 18 }]);
});

test("capped frequent postings score but do not incorrectly exclude a document", () => {
  const groups = [
    [{ df: 1000, rows: [[1, 1, 0]] }],
    [{ df: 1, rows: [[2, 12, 1]] }]
  ];
  assert.deepEqual(rankOrdinals(groups, 20, 100), [{ ordinal: 2, score: 16 }]);
});
