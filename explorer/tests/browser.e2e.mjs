import test from "node:test";
import assert from "node:assert/strict";
import { copyFile, cp, mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { runFixtureBrowserAudit } from "./browser-audit.mjs";
import { launchChrome } from "./chrome-cdp.mjs";
import { startFixtureServer } from "./fixture-server.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(here, "..", "..");

function packageFixture(fixtureRoot, output) {
  const source = [
    "from pathlib import Path",
    "import sys",
    "root=Path(sys.argv[1]).resolve()",
    "fixture=Path(sys.argv[2]).resolve()",
    "sys.path.insert(0, str(root / 'src'))",
    "from govuk_okf.release_packaging import package_verified_release",
    "package_verified_release(repository_root=fixture, bundle=fixture / 'bundle', output=Path(sys.argv[3]), tag='v0.1.0', browser_evidence=fixture / 'release' / 'status.json')"
  ].join(";");
  const candidates = [process.env.PYTHON, join(repositoryRoot, ".venv", "bin", "python"), "python3"].filter(Boolean);
  let last = null;
  for (const executable of candidates) {
    const result = spawnSync(executable, ["-c", source, repositoryRoot, fixtureRoot, output], { cwd: repositoryRoot, encoding: "utf8" });
    if (result.error?.code === "ENOENT") continue;
    if (result.status === 0) return;
    last = result;
    break;
  }
  throw new Error(`could not package the exact browser fixture: ${last?.stderr || last?.error || "Python is unavailable"}`);
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

test("exact single-pack Pages fixture proves distinct virtual range requests", { timeout: 120000 }, async () => {
  const temporary = await mkdtemp(join(tmpdir(), "govuk-okf-packed-browser-"));
  const fixtureRoot = join(temporary, "repository");
  const sourceBundle = join(fixtureRoot, "bundle");
  const verified = join(temporary, "verified");
  let server = null;
  let browser = null;
  try {
    await cp(join(repositoryRoot, "bundle"), sourceBundle, { recursive: true });
    await cp(join(repositoryRoot, "release"), join(fixtureRoot, "release"), { recursive: true });
    for (const entry of await readdir(join(repositoryRoot, "explorer", "src"), { withFileTypes: true })) {
      if (entry.isFile()) await copyFile(join(repositoryRoot, "explorer", "src", entry.name), join(sourceBundle, entry.name));
    }
    packageFixture(fixtureRoot, verified);
    const site = join(verified, "site");
    const index = JSON.parse(await readFile(join(site, "release-data-plane.json"), "utf8"));
    assert.equal(index.packs.length, 1, "the regression fixture must exercise one physical pack");
    assert.ok(index.entries.length > 2, "the regression fixture must contain multiple virtual members");

    server = await startFixtureServer({ root: site, staticRoot: site });
    browser = await launchChrome();
    const evidence = await runFixtureBrowserAudit(browser, server, { iterations: 1, generatedAt: "single-pack-test" });
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
