import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";

import { runFixtureBrowserAudit } from "./browser-audit.mjs";
import { launchChrome } from "./chrome-cdp.mjs";
import { startFixtureServer } from "./fixture-server.mjs";

function option(name, fallback = "") {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

const output = resolve(option("--output", new URL("../src/evidence/fixture-browser.json", import.meta.url).pathname));
const bundle = resolve(option("--bundle", new URL("../../bundle", import.meta.url).pathname));
const mode = option("--mode", "fixture");
const iterations = Number(option("--iterations", "5"));
const generatedAt = option("--generated-at", new Date().toISOString());
if (!["fixture", "release"].includes(mode)) throw new Error("--mode must be fixture or release");
const dataManifest = JSON.parse(await readFile(join(bundle, "data", "manifest.json"), "utf8"));
const snapshot = String(option("--snapshot", dataManifest.snapshot || ""));
if (!snapshot) throw new Error("bundle data manifest has no snapshot");
const artifactTier = mode === "release" ? "full_release_snapshot" : "representative_fixture";
const expectedStatus = mode === "release" ? "automated_full_release_evidence_pass" : "automated_fixture_evidence_pass";
// Release evidence must exercise the exact packaged shell and data bytes. The
// fixture path deliberately keeps the editable Explorer source as its shell.
const server = await startFixtureServer({
  root: bundle,
  staticRoot: mode === "release" ? bundle : undefined
});
const browser = await launchChrome();
try {
  const evidence = await runFixtureBrowserAudit(browser, server, { iterations, generatedAt, snapshot, artifactTier });
  await mkdir(dirname(output), { recursive: true });
  await writeFile(output, JSON.stringify(evidence, null, 2) + "\n", "utf8");
  process.stdout.write(`${evidence.overall_status}: ${output}\n`);
  if (evidence.overall_status !== expectedStatus) process.exitCode = 1;
} finally {
  await browser.close();
  await server.close();
}
