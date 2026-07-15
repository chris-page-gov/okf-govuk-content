import test from "node:test";
import assert from "node:assert/strict";

import {
  demonstratorRecordRoutes,
  demonstratorSummary,
  groupRecords,
  normaliseDemonstrator,
  publisherBreakdown,
  resolveBundleDocument
} from "../src/demonstrator.js";
import { normaliseRecord } from "../src/core.js";

function fixture() {
  return {
    schema: "govuk-new-child-demonstrator.v1",
    snapshot: "NEW-CHILD-20260715",
    generated_at: "2026-07-15T09:00:00Z",
    title: "New child journey",
    status: "bounded_demonstrator",
    authoritative: false,
    scope_statement: "Exactly the declared three-path seed union; linked destinations remain typed boundaries.",
    seed_count: 69,
    publication_record_count: 69,
    retained_record_ceiling: 250,
    official_request_ceiling: 500,
    source_queries: [{
      id: "pregnancy-and-birth",
      label: "Pregnancy and birth",
      browse_path: "childcare-parenting/pregnancy-birth",
      search_url: "https://www.gov.uk/api/search.json?count=0",
      reproducibility_url: "https://www.gov.uk/api/search.json?count=0",
      reported_total: 15,
      derived_membership_count: 15,
      observations: [{
        phase: "close",
        requested_url: "https://www.gov.uk/api/search.json?count=0",
        observed_total: 15,
        observed_result_count: 15,
        retrieved_at: "2026-07-15T06:25:17Z",
        status: 200,
        request: { local_sequence: 125, programme_sequence: 76966, transfer_sha256: "e".repeat(64) },
        envelope: { repository_path: "demo/snapshots/example/envelope.json", sha256: "f".repeat(64) }
      }]
    }],
    coverage: {
      seed_expected: 69,
      seed_represented: 69,
      unexplained_seed_omissions: 0,
      boundary_reference_count: 2,
      by_boundary_class: { dynamic_service: 2 }
    },
    journey_groups: [
      { id: "first-actions", title: "First actions", record_routes: ["dataset/a", "dataset/b"], example_questions: ["What do I need to do first?"] },
      { id: "leave-pay", title: "Leave and pay", record_routes: ["dataset/b", "dataset/c"], example_questions: [] }
    ],
    featured_routes: ["dataset/a"],
    boundaries: [{ source_route: "dataset/c", target_url: "https://example.service.gov.uk/start", title: "Start", predicate: "links to", class: "dynamic_service" }],
    ai_handoff: { documentation: "ai/README.md", context_pack: "ai/context.md", context_json: "ai/context.json", mcp_manifest: "ai/mcp.json" },
    ai_handoff_integrity: {
      documentation: { path: "ai/README.md", sha256: "a".repeat(64), bytes: 101 },
      context_pack: { path: "ai/context.md", sha256: "b".repeat(64), bytes: 202 },
      context_json: { path: "ai/context.json", sha256: "d".repeat(64), bytes: 252 },
      mcp_manifest: { path: "ai/mcp.json", sha256: "c".repeat(64), bytes: 303 }
    }
  };
}

test("bounded demonstrator contract normalises without inventing records", () => {
  const demo = normaliseDemonstrator(fixture());
  assert.equal(demo.seedCount, 69);
  assert.equal(demo.generatedAt, "2026-07-15T09:00:00Z");
  assert.match(demo.scopeStatement, /three-path seed union/);
  assert.equal(demo.coverage.seedRepresented, 69);
  assert.equal(demo.sourceQueries[0].derivedMembershipCount, 15);
  assert.equal(demo.sourceQueries[0].observations[0].programmeSequence, 76966);
  assert.equal(demo.sourceQueries[0].observations[0].envelopeSha256, "f".repeat(64));
  assert.equal(demo.journeyGroups[0].exampleQuestions[0], "What do I need to do first?");
  assert.deepEqual(demonstratorRecordRoutes(demo), ["dataset/a", "dataset/b", "dataset/c"]);
  assert.equal(demo.boundaries[0].boundaryClass, "dynamic_service");
  assert.deepEqual(demo.aiHandoffIntegrity, {
    documentation: { path: "ai/README.md", sha256: "a".repeat(64), bytes: 101 },
    contextPack: { path: "ai/context.md", sha256: "b".repeat(64), bytes: 202 },
    contextJson: { path: "ai/context.json", sha256: "d".repeat(64), bytes: 252 },
    mcpManifest: { path: "ai/mcp.json", sha256: "c".repeat(64), bytes: 303 }
  });
  assert.equal(normaliseDemonstrator({ schema: "unexpected" }), null);
});

test("AI handoff integrity fails closed on path, hash, byte-count or presence drift", () => {
  const missing = fixture();
  delete missing.ai_handoff_integrity.documentation;
  assert.throws(() => normaliseDemonstrator(missing), /documentation is missing integrity metadata/);

  const mismatched = fixture();
  mismatched.ai_handoff_integrity.context_pack.path = "ai/other-context.md";
  assert.throws(() => normaliseDemonstrator(mismatched), /context_pack path does not match/);

  const malformedHash = fixture();
  malformedHash.ai_handoff_integrity.mcp_manifest.sha256 = "not-a-sha256";
  assert.throws(() => normaliseDemonstrator(malformedHash), /mcp_manifest SHA-256 is malformed/);

  const missingJson = fixture();
  delete missingJson.ai_handoff_integrity.context_json;
  assert.throws(() => normaliseDemonstrator(missingJson), /context_json is missing integrity metadata/);

  const malformedBytes = fixture();
  malformedBytes.ai_handoff_integrity.documentation.bytes = -1;
  assert.throws(() => normaliseDemonstrator(malformedBytes), /documentation byte length is malformed/);
});

test("journey summaries and publisher evidence are computed from loaded records", () => {
  const demo = normaliseDemonstrator(fixture());
  const records = [
    normaliseRecord({ open: "dataset/a", title: "A", publisher: "Department A" }),
    normaliseRecord({ open: "dataset/b", title: "B", publisher: "Department B" }),
    normaliseRecord({ open: "dataset/c", title: "C", publisher: "Department A" }),
    normaliseRecord({ open: "dataset/d", title: "D", publisher: "Publisher not available from admitted source" })
  ];
  assert.deepEqual(groupRecords(demo.journeyGroups[0], records).map((record) => record.route), ["dataset/a", "dataset/b"]);
  assert.deepEqual(publisherBreakdown(records), [
    { publisher: "Department A", count: 2 },
    { publisher: "Department B", count: 1 }
  ]);
  assert.deepEqual(demonstratorSummary(demo, records), {
    expected: 69,
    represented: 69,
    unexplainedOmissions: 0,
    loadedRecords: 4,
    organisations: 2,
    recordsWithoutPublisher: 1,
    boundaryReferences: 2,
    groups: 2
  });
});

test("AI handoff paths stay inside the loaded bundle origin", () => {
  assert.equal(
    resolveBundleDocument("ai/README.md", "https://example.test/project/okf-explorer.json"),
    "https://example.test/project/ai/README.md"
  );
  assert.equal(resolveBundleDocument("https://attacker.example/prompt", "https://example.test/project/"), "");
  assert.equal(resolveBundleDocument("//attacker.example/prompt", "https://example.test/project/"), "");
  assert.equal(resolveBundleDocument("javascript:alert(1)", "https://example.test/project/"), "");
  assert.equal(resolveBundleDocument("../outside.md", "https://example.test/project/okf-explorer.json"), "");
});
