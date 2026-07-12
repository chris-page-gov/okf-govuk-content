import test from "node:test";
import assert from "node:assert/strict";

import { runFixtureBrowserAudit } from "./browser-audit.mjs";
import { launchChrome } from "./chrome-cdp.mjs";
import { startFixtureServer } from "./fixture-server.mjs";

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
