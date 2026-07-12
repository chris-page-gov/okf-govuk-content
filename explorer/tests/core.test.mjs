import test from "node:test";
import assert from "node:assert/strict";

import {
  buildBoundedGraph,
  buildCitation,
  buildContextExport,
  createInstrumentationEvent,
  filterRecords,
  normaliseLanguage,
  normaliseRecord,
  paginate,
  parseExplorerState,
  relationshipBucket,
  safeExternalUrl,
  serialiseExplorerState,
  whyThisResult
} from "../src/core.js";

test("replayable state round trips without losing an existing bundle URL", () => {
  const original = "https://example.test/explorer/?bundle=https%3A%2F%2Fdata.example%2Fokf-explorer.json&q=driving&facet=language%3Acy&facet=status%3Awithdrawn&view=timeline&mode=evidence&lang=cy&route=dataset%2Flicence&snapshot=snap-1&pin=dataset%2Fa&pin=dataset%2Fb&page=3";
  const state = parseExplorerState(original);
  assert.equal(state.query, "driving");
  assert.deepEqual(state.facets, { language: ["cy"], status: ["withdrawn"] });
  assert.equal(state.view, "timeline");
  assert.equal(state.mode, "evidence");
  assert.equal(state.language, "cy");
  assert.equal(state.route, "dataset/licence");
  assert.deepEqual(state.pins, ["dataset/a", "dataset/b"]);
  const replay = serialiseExplorerState(state, original);
  assert.equal(replay.searchParams.get("bundle"), "https://data.example/okf-explorer.json");
  assert.deepEqual(parseExplorerState(replay), state);
});

test("state parser constrains unsupported modes, unsafe routes and invalid facet keys", () => {
  const state = parseExplorerState("https://example.test/?view=admin&mode=raw&lang=not_a_language_123&route=%3Cscript%3E&facet=bad%20key%3Avalue&facet=type%3Aguidance&page=-4");
  assert.equal(state.view, "results");
  assert.equal(state.mode, "simple");
  assert.equal(state.language, "en");
  assert.equal(state.route, "");
  assert.deepEqual(state.facets, { type: ["guidance"] });
  assert.equal(state.page, 1);
});

test("language normalisation preserves a general BCP 47 path", () => {
  assert.equal(normaliseLanguage("cy"), "cy");
  assert.equal(normaliseLanguage("en-gb"), "en-GB");
  assert.equal(normaliseLanguage("zh-hant-tw"), "zh-Hant-TW");
  assert.equal(normaliseLanguage("javascript:alert(1)"), "en");
});

test("records retain source-native discovery, lifecycle and provenance fields", () => {
  const record = normaliseRecord({
    name: "passport",
    title: "Get a passport",
    description: "Official passport guidance",
    record_type: "Guidance",
    publisher_title: "HM Passport Office",
    lifecycle_status: "current",
    locale: "en-GB",
    jurisdiction: ["UK"],
    topics: ["Passports"],
    open: "dataset/passport",
    web_url: "https://www.gov.uk/browse/abroad/passports",
    source_status: "source-native",
    retrieved_at: "2026-07-11T00:00:00Z"
  });
  assert.equal(record.route, "dataset/passport");
  assert.equal(record.type, "Guidance");
  assert.equal(record.publisher, "HM Passport Office");
  assert.equal(record.language, "en-GB");
  assert.deepEqual(record.jurisdictions, ["UK"]);
  assert.equal(record.sourceStatus, "source-native");
  assert.equal(record.canonicalUrl, "https://www.gov.uk/browse/abroad/passports");
});

test("one reducer applies query, facets, lifecycle and jurisdiction together", () => {
  const records = [
    normaliseRecord({ name: "a", title: "Driving licence", publisher: "DVLA", status: "current", jurisdiction: ["England", "Wales"], facets: { topic: ["Driving"] } }),
    normaliseRecord({ name: "b", title: "Historic vehicle guidance", publisher: "DVLA", status: "withdrawn", jurisdiction: ["England"], facets: { topic: ["Driving"] } }),
    normaliseRecord({ name: "c", title: "Passport guidance", publisher: "HMPO", status: "current", jurisdiction: ["UK"], facets: { topic: ["Travel"] } })
  ];
  const selected = filterRecords(records, { query: "driv", facets: { topic: ["Driving"] }, lifecycle: "current", jurisdiction: "Wales" });
  assert.deepEqual(selected.map((record) => record.name), ["a"]);
  assert.deepEqual(whyThisResult(selected[0], "driv"), ["title matches driv"]);
});

test("bounded graph and equivalent list use exactly the same edge set", () => {
  const records = [normaliseRecord({ name: "center", title: "Centre" })];
  const relationships = Array.from({ length: 20 }, (_, index) => ({ source: "dataset/center", target: "dataset/target-" + index, kind: index % 2 ? "related to" : "classified under" }));
  const model = buildBoundedGraph(records, relationships, { centerRoute: "dataset/center", maxNodes: 6, maxEdges: 8 });
  assert.ok(model.nodes.length <= 6);
  assert.ok(model.edges.length <= 8);
  assert.deepEqual(model.list, model.edges);
  assert.ok(model.omittedEdges > 0);
  assert.deepEqual(model.summary, [{ kind: "classified under", count: 10 }, { kind: "related to", count: 10 }]);
});

test("adjacency hashing matches the audited portable UTF-8 vectors", () => {
  assert.equal(relationshipBucket("dataset/dataset-one"), "83");
  assert.equal(relationshipBucket("publisher/publisher-one"), "7f");
  assert.equal(relationshipBucket("é"), "1e");
});

test("instrumentation is allowlisted and never retains query text", () => {
  const event = createInstrumentationEvent("query", { query: "sensitive free text", queryLength: 19, resultCount: 2, view: "results", unexpected: "drop me" }, new Date("2026-07-12T00:00:00Z"));
  assert.equal(event.kind, "query");
  assert.equal(event.queryLength, 19);
  assert.equal(event.resultCount, 2);
  assert.equal(Object.hasOwn(event, "query"), false);
  assert.equal(Object.hasOwn(event, "unexpected"), false);
  assert.equal(createInstrumentationEvent("free_text", {}), null);
});

test("exports and citations retain canonical IDs and snapshot without raw source bodies", () => {
  const record = normaliseRecord({ name: "a", id: "https://example.test/id/a", title: "Record A", notes: "Metadata summary", web_url: "https://www.gov.uk/a", status: "current", body: "must not export" });
  const state = { query: "record", facets: {}, snapshot: "snapshot-1" };
  for (const format of ["markdown", "yamlld", "jsonld"]) {
    const output = buildContextExport(format, [record], state, { "@context": "https://example.test/context" });
    assert.match(output.text, /Record A/);
    assert.match(output.text, /snapshot-1/);
    assert.doesNotMatch(output.text, /must not export/);
  }
  assert.equal(buildCitation(record, "snapshot-1"), "Record A. https://www.gov.uk/a. What’s on GOV.UK snapshot snapshot-1.");
});

test("pagination and external URL handling fail safely", () => {
  const page = paginate([1, 2, 3, 4, 5], 10, 2);
  assert.deepEqual(page, { items: [5], page: 3, pageCount: 3, total: 5 });
  assert.equal(safeExternalUrl("javascript:alert(1)"), "");
  assert.equal(safeExternalUrl("http://www.gov.uk/example"), "");
  assert.equal(safeExternalUrl("https://www.gov.uk/example"), "https://www.gov.uk/example");
});
