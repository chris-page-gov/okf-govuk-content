import test from "node:test";
import assert from "node:assert/strict";

import { isGzipResourcePath } from "./browser-audit.mjs";

test("browser evidence recognises logical gzip shards and packaged gzip ranges", () => {
  assert.equal(isGzipResourcePath("https://example.test/data/records-0.json.gz"), true);
  assert.equal(isGzipResourcePath("https://example.test/data-packs/release-00000.pack.gz"), true);
  assert.equal(isGzipResourcePath("https://example.test/data/manifest.json"), false);
});
