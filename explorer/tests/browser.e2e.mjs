import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { runFixtureBrowserAudit } from "./browser-audit.mjs";
import { launchChrome } from "./chrome-cdp.mjs";
import { startFixtureServer } from "./fixture-server.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(here, "..", "..");
const fixtureSource = join(here, "fixtures", "single-pack-source-records.jsonl");
const fixtureSnapshot = "fixture-browser-single-pack-2026-07-13";
const fixtureTag = "v0.1.0";

function pythonCandidates() {
  return [process.env.PYTHON, join(repositoryRoot, ".venv", "bin", "python"), "python3"].filter(Boolean);
}

function runPython(arguments_, label, timeout) {
  let last = null;
  for (const executable of pythonCandidates()) {
    const result = spawnSync(executable, arguments_, {
      cwd: repositoryRoot,
      encoding: "utf8",
      killSignal: "SIGKILL",
      maxBuffer: 8 * 1024 * 1024,
      timeout
    });
    if (result.error?.code === "ENOENT") continue;
    if (result.status === 0) return;
    last = result;
    break;
  }
  const reason = last?.error?.code === "ETIMEDOUT"
    ? `${label} subprocess exceeded ${timeout / 1000} seconds`
    : last?.stderr || last?.error || "Python is unavailable";
  throw new Error(`could not ${label}: ${reason}`);
}

async function buildSinglePackFixture(fixtureRoot) {
  const releaseRoot = join(fixtureRoot, "release");
  await mkdir(releaseRoot, { recursive: true });
  await writeFile(
    join(releaseRoot, "status.json"),
    `${JSON.stringify({ schema: "govuk-okf-browser-fixture-status.v1", publication_ready: false }, null, 2)}\n`,
    "utf8"
  );
  await writeFile(
    join(releaseRoot, "manifest.yaml"),
    `${JSON.stringify({
      schema: "govuk-okf-browser-fixture-release.v1",
      snapshot: fixtureSnapshot,
      version: fixtureTag.slice(1),
      tag: fixtureTag,
      release_kind: "fixture",
      artifacts: { status: "release/status.json" }
    }, null, 2)}\n`,
    "utf8"
  );
  runPython(
    [
      join(repositoryRoot, "scripts", "build_bundle.py"),
      "--source", fixtureSource,
      "--output", join(fixtureRoot, "bundle"),
      "--snapshot-id", fixtureSnapshot,
      "--generated-at", "2026-07-13T00:00:00Z",
      "--compiler", "memory"
    ],
    "build the dedicated browser fixture",
    120000
  );
  runPython(
    [
      join(repositoryRoot, "scripts", "build_checksums.py"),
      "--bundle", join(fixtureRoot, "bundle")
    ],
    "checksum the dedicated browser fixture",
    60000
  );
}

function packageFixture(fixtureRoot, output) {
  const source = [
    "from pathlib import Path",
    "import sys",
    "root=Path(sys.argv[1]).resolve()",
    "fixture=Path(sys.argv[2]).resolve()",
    "sys.path.insert(0, str(root / 'src'))",
    "from govuk_okf.release_packaging import package_verified_release",
    "package_verified_release(repository_root=fixture, bundle=fixture / 'bundle', output=Path(sys.argv[3]), tag=sys.argv[4], browser_evidence=fixture / 'release' / 'status.json')"
  ].join(";");
  runPython(
    ["-c", source, repositoryRoot, fixtureRoot, output, fixtureTag],
    "package the dedicated browser fixture",
    60000
  );
}

async function addDemonstratorFixture(bundleRoot) {
  const descriptorPath = join(bundleRoot, "okf-explorer.json");
  const descriptor = JSON.parse(await readFile(descriptorPath, "utf8"));
  const aiFiles = {
    documentation: { path: "ai/README.md", content: "# Use this fixture bundle with an AI\n\nThe live GOV.UK page remains authoritative.\n" },
    context_pack: { path: "ai/context.md", content: "# Fixture context\n\nTwo bounded metadata records for browser verification.\n" },
    context_json: { path: "ai/context.json", content: `${JSON.stringify({ schema: "govuk-okf-portable-ai-context.v1", records: 2, authoritative: false }, null, 2)}\n` },
    mcp_manifest: { path: "ai/mcp.json", content: `${JSON.stringify({ schema: "govuk-okf-mcp-configuration.v1", transport: "stdio", read_only: true }, null, 2)}\n` }
  };
  await mkdir(join(bundleRoot, "ai"), { recursive: true });
  const aiIntegrity = {};
  for (const [key, file] of Object.entries(aiFiles)) {
    const bytes = Buffer.from(file.content, "utf8");
    await writeFile(join(bundleRoot, file.path), bytes);
    aiIntegrity[key] = {
      path: file.path,
      sha256: createHash("sha256").update(bytes).digest("hex"),
      bytes: bytes.byteLength
    };
  }
  const demonstrator = {
    schema: "govuk-new-child-demonstrator.v1",
    snapshot: fixtureSnapshot,
    title: "New child journey browser fixture",
    status: "bounded_demonstrator",
    authoritative: false,
    scope_statement: "Exactly the declared fixture seeds; outside destinations remain typed boundaries.",
    seed_count: 2,
    publication_record_count: 2,
    retained_record_ceiling: 250,
    official_request_ceiling: 500,
    source_queries: [{ label: "Fixture browse area", browse_path: "fixture/new-child", search_url: "https://www.gov.uk/api/search.json?count=0", reported_total: 2 }],
    coverage: { seed_expected: 2, seed_represented: 2, unexplained_seed_omissions: 0, boundary_reference_count: 1, by_boundary_class: { dynamic_service: 1 } },
    journey_groups: [{
      id: "first-actions",
      title: "First actions",
      description: "Fixture stage assembled from advertised record routes.",
      record_routes: [
        "dataset/00000000-0000-4000-8000-000000000001-en",
        "dataset/00000000-0000-4000-8000-000000000002-en"
      ],
      example_questions: ["Which official records help me understand the next steps?"]
    }],
    featured_routes: ["dataset/00000000-0000-4000-8000-000000000001-en"],
    boundaries: [{ source_route: "dataset/00000000-0000-4000-8000-000000000001-en", target_url: "https://www.gov.uk/", title: "GOV.UK", predicate: "hands off to", class: "dynamic_service" }],
    ai_handoff: { documentation: aiFiles.documentation.path, context_pack: aiFiles.context_pack.path, context_json: aiFiles.context_json.path, mcp_manifest: aiFiles.mcp_manifest.path },
    ai_handoff_integrity: aiIntegrity
  };
  const demonstratorBytes = Buffer.from(`${JSON.stringify(demonstrator, null, 2)}\n`, "utf8");
  descriptor.entrypoints.demonstrator = "data/demonstrator.json";
  descriptor.entrypoint_integrity ||= {};
  descriptor.entrypoint_integrity.demonstrator = {
    path: "data/demonstrator.json",
    sha256: createHash("sha256").update(demonstratorBytes).digest("hex")
  };
  await writeFile(descriptorPath, `${JSON.stringify(descriptor, null, 2)}\n`, "utf8");
  await writeFile(join(bundleRoot, "data", "demonstrator.json"), demonstratorBytes);
  return { aiIntegrity, demonstratorBytes };
}

test("real browser verifies fixture accessibility, routing, gzip and performance budgets", { timeout: 120000 }, async (context) => {
  const server = await startFixtureServer();
  const browser = await launchChrome();
  context.after(async () => {
    // Close the browser first so its keep-alive connections cannot make the
    // HTTP server's close callback wait indefinitely.
    await browser.close();
    await server.close();
  });

  const evidence = await runFixtureBrowserAudit(browser, server, { iterations: 3, generatedAt: "test-run" });
  assert.equal(evidence.accessibility.pass, true, JSON.stringify(evidence.accessibility, null, 2));
  assert.equal(evidence.routing_and_data.pass, true, JSON.stringify(evidence.routing_and_data, null, 2));
  assert.equal(evidence.performance.pass, true, JSON.stringify(evidence.performance, null, 2));
  assert.deepEqual(evidence.console_exceptions, []);
  assert.equal(evidence.overall_status, "automated_fixture_evidence_pass");
});

test("full-release mode binds evidence to a non-fixture snapshot", async () => {
  const stubBrowser = {};
  const stubServer = {};
  await assert.rejects(
    runFixtureBrowserAudit(stubBrowser, stubServer, {
      artifactTier: "full_release_snapshot",
      snapshot: "fixture-2026-07-11"
    }),
    /full-release browser evidence cannot use snapshot/
  );
});

test("real browser renders the bounded journey from its advertised contract", { timeout: 120000 }, async (context) => {
  const temporary = await mkdtemp(join(tmpdir(), "govuk-okf-demonstrator-browser-"));
  const fixtureRoot = join(temporary, "repository");
  let server = null;
  let browser = null;
  try {
    await buildSinglePackFixture(fixtureRoot);
    const fixture = await addDemonstratorFixture(join(fixtureRoot, "bundle"));
    server = await startFixtureServer({ root: join(fixtureRoot, "bundle") });
    browser = await launchChrome();
    const url = new URL(server.baseUrl);
    url.searchParams.set("view", "journey");
    url.searchParams.set("mode", "explore");
    url.searchParams.set("snapshot", fixtureSnapshot);
    await browser.navigate(url.toString());
    await browser.waitFor("document.documentElement.dataset.explorerReady === 'true' && document.documentElement.dataset.demonstratorRecordsReady === 'true'");
    const rendered = await browser.evaluate(`(() => {
      const visible = (node) => {
        const style = getComputedStyle(node);
        return !node.hidden && style.display !== "none" && style.visibility !== "hidden" && node.getClientRects().length > 0;
      };
      const headings = [...document.querySelectorAll("h1, h2, h3, h4, h5, h6")]
        .filter(visible)
        .map((node) => Number(node.tagName.slice(1)));
      return {
        view: new URL(location.href).searchParams.get("view"),
        callout: !document.querySelector("#demonstrator-callout").hidden,
        stages: document.querySelectorAll(".journey-stage").length,
        records: document.querySelectorAll(".journey-record").length,
        coverage: document.querySelector(".journey-metrics")?.textContent || "",
        scope: document.querySelector(".journey-scope-statement")?.textContent || "",
        ai: document.querySelector(".ai-handoff")?.textContent || "",
        visibleH1s: [...document.querySelectorAll("h1")].filter(visible).length,
        headingJumps: headings.slice(1).filter((level, index) => level > headings[index] + 1).length,
        duplicateIds: [...document.querySelectorAll("[id]")]
          .map((node) => node.id)
          .filter((id, index, values) => values.indexOf(id) !== index),
        unnamedActions: [...document.querySelectorAll("a, button")]
          .filter(visible)
          .filter((node) => !(node.textContent || node.getAttribute("aria-label") || "").trim())
          .length
      };
    })()`);
    assert.equal(rendered.view, "journey");
    assert.equal(rendered.callout, true);
    assert.equal(rendered.stages, 1);
    assert.equal(rendered.records, 2);
    assert.match(rendered.coverage, /2declared seed records/);
    assert.match(rendered.coverage, /0unexplained seed omissions/);
    assert.match(rendered.scope, /declared fixture seeds/);
    assert.match(rendered.ai, /MCP/);
    assert.equal(rendered.visibleH1s, 1);
    assert.equal(rendered.headingJumps, 0);
    assert.deepEqual(rendered.duplicateIds, []);
    assert.equal(rendered.unnamedActions, 0);
    const handoffs = await browser.evaluate(`(async () => {
      const hex = (buffer) => [...new Uint8Array(buffer)].map((value) => value.toString(16).padStart(2, "0")).join("");
      return Promise.all([...document.querySelectorAll(".ai-handoff a")].map(async (link) => {
        const response = await fetch(link.href, { cache: "no-store" });
        const bytes = await response.arrayBuffer();
        return {
          label: link.textContent.trim(),
          path: new URL(link.href).pathname.split("/okf-govuk-content/")[1],
          status: response.status,
          bytes: bytes.byteLength,
          sha256: hex(await crypto.subtle.digest("SHA-256", bytes))
        };
      }));
    })()`);
    assert.equal(handoffs.length, 4);
    const observedByPath = Object.fromEntries(handoffs.map((entry) => [entry.path, entry]));
    for (const expected of Object.values(fixture.aiIntegrity)) {
      const observed = observedByPath[expected.path];
      assert.ok(observed, `browser did not expose AI handoff ${expected.path}`);
      assert.equal(observed.status, 200);
      assert.equal(observed.bytes, expected.bytes);
      assert.equal(observed.sha256, expected.sha256);
    }
  } finally {
    if (browser) await browser.close();
    if (server) await server.close();
    await rm(temporary, { recursive: true, force: true });
  }
});

test("exact single-pack Pages fixture proves distinct virtual range requests", { timeout: 180000 }, async () => {
  const temporary = await mkdtemp(join(tmpdir(), "govuk-okf-packed-browser-"));
  const fixtureRoot = join(temporary, "repository");
  const verified = join(temporary, "verified");
  let server = null;
  let browser = null;
  try {
    await buildSinglePackFixture(fixtureRoot);
    packageFixture(fixtureRoot, verified);
    const site = join(verified, "site");
    const index = JSON.parse(await readFile(join(site, "release-data-plane.json"), "utf8"));
    assert.equal(index.packs.length, 1, "the regression fixture must exercise one physical pack");
    assert.ok(index.entries.length > 2, "the regression fixture must contain multiple virtual members");

    server = await startFixtureServer({ root: site, staticRoot: site });
    browser = await launchChrome();
    const evidence = await runFixtureBrowserAudit(browser, server, {
      iterations: 1,
      generatedAt: "single-pack-test",
      snapshot: fixtureSnapshot,
      route: "publisher/government-digital-service",
      routeTitle: "Government Digital Service",
      searchQuery: "welcome"
    });
    assert.equal(evidence.routing_and_data.pass, true, JSON.stringify(evidence.routing_and_data, null, 2));
    assert.deepEqual(evidence.routing_and_data.physical_pack_resources.length, 1);
    assert.ok(evidence.routing_and_data.range_requests.filter((request) => request.virtual_path).length >= 2);
    assert.ok(evidence.routing_and_data.virtual_resources_loaded.length >= 2);
    assert.equal(evidence.overall_status, "automated_fixture_evidence_pass");
  } finally {
    if (browser) await browser.close();
    if (server) await server.close();
    await rm(temporary, { recursive: true, force: true });
  }
});
