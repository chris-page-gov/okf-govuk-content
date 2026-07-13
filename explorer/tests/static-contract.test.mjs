import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { releaseEvidenceUrl } from "../src/accessibility-evidence.js";

const here = dirname(fileURLToPath(import.meta.url));
const source = join(here, "..", "src");

test("HTML is CSP-safe and contains task-first accessible landmarks", async () => {
  const html = await readFile(join(source, "index.html"), "utf8");
  assert.match(html, /Content-Security-Policy/);
  assert.doesNotMatch(html, /unsafe-inline|unsafe-eval/);
  assert.doesNotMatch(html, /\son[a-z]+\s*=/i);
  assert.doesNotMatch(html, /<style[\s>]/i);
  assert.equal((html.match(/<script\b/g) || []).length, 1);
  assert.match(html, /<script type="module" src="app\.js"><\/script>/);
  assert.match(html, /role="search"/);
  assert.match(html, /href="#main-content"/);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /data-language="en"/);
  assert.match(html, /data-language="cy"/);
  assert.match(html, /Derived, non-authoritative service/);
  assert.match(html, /id="skip-link"/);
  assert.match(html, /href="accessibility\.html"/);
});

test("Pages fallback preserves state without inline executable content", async () => {
  const html = await readFile(join(source, "404.html"), "utf8");
  const pages = await readFile(join(source, "pages.js"), "utf8");
  assert.match(html, /data-pages-project="okf-govuk-content"/);
  assert.match(html, /src="\/okf-govuk-content\/pages\.js"/);
  assert.doesNotMatch(html, /unsafe-inline|\son[a-z]+\s*=/i);
  assert.match(pages, /target\.search = current\.search/);
  assert.match(pages, /target\.hash = current\.hash/);
  assert.doesNotMatch(pages, /document\.write|innerHTML/);
});

test("accessibility statement distinguishes historical fixture and current release evidence from conformance", async () => {
  const html = await readFile(join(source, "accessibility.html"), "utf8");
  assert.match(html, /not a claim of WCAG 2\.2 AA conformance/i);
  assert.match(html, /accessibility-expert review/i);
  assert.match(html, /screen-reader testing/i);
  assert.match(html, /representative-user research/i);
  assert.match(html, /evidence\/fixture-browser\.json/);
  assert.match(html, /historical fixture machine evidence/i);
  assert.match(html, /current snapshot-bound release browser evidence/i);
  assert.match(html, /src="accessibility-evidence\.js"/);
  assert.doesNotMatch(html, /current evidence checkpoint is blocked/i);
  assert.equal(
    releaseEvidenceUrl({
      schema: "govuk-okf-github-release-pack-index.v1",
      repository: "chris-page-gov/okf-govuk-content",
      tag: "v0.1.0-rc.1"
    }),
    "https://github.com/chris-page-gov/okf-govuk-content/releases/download/v0.1.0-rc.1/evidence-browser-workflow.json"
  );
  assert.equal(releaseEvidenceUrl({ schema: "govuk-okf-github-release-pack-index.v1", repository: "other/repo", tag: "v0.1.0" }), null);
  assert.equal(releaseEvidenceUrl({ schema: "govuk-okf-github-release-pack-index.v1", repository: "chris-page-gov/okf-govuk-content", tag: "../../latest" }), null);
});

test("application never uses executable source HTML sinks", async () => {
  const files = await Promise.all(["app.js", "data.js", "search.worker.js", "accessibility-evidence.js"].map((name) => readFile(join(source, name), "utf8")));
  const code = files.join("\n");
  assert.doesNotMatch(code, /\.innerHTML\s*=/);
  assert.doesNotMatch(code, /\beval\s*\(/);
  assert.doesNotMatch(code, /new\s+Function\s*\(/);
  assert.match(code, /textContent/);
  assert.match(code, /instrumentationConsent = false/);
  assert.match(code, /querySelectorAll\("button\[data-mode\]"\)/);
  assert.match(code, /dataset\.explorerReady/);
  assert.match(code, /getElementById\("skip-link"\)/);
});

test("CSS includes reflow, reduced-motion, focus and forced-colour support", async () => {
  const css = await readFile(join(source, "styles.css"), "utf8");
  assert.match(css, /:focus-visible/);
  assert.match(css, /prefers-reduced-motion/);
  assert.match(css, /forced-colors/);
  assert.match(css, /max-width:\s*52rem/);
  assert.match(css, /min-height:\s*2\.75rem/);
});

test("all UX epics are explicit without claiming completed empirical acceptance", async () => {
  const epics = await readFile(join(here, "..", "requirements", "ux-epics.yaml"), "utf8");
  for (let index = 1; index <= 15; index += 1) {
    assert.match(epics, new RegExp("id: UX-" + String(index).padStart(2, "0")));
  }
  assert.match(epics, /pending_human_evidence/);
  assert.match(epics, /pending_full_corpus/);
  assert.doesNotMatch(epics, /acceptance_status:\s*(passed|complete)/);
  JSON.parse(await readFile(join(here, "..", "requirements", "explorer-state.schema.json"), "utf8"));
  const budgets = JSON.parse(await readFile(join(here, "..", "requirements", "browser-budgets.json"), "utf8"));
  assert.equal(budgets.qualifications.automated_fixture_pass_is_wcag_conformance, false);
  assert.equal(budgets.performance.first_useful_render_p75_ms_max, 2500);
});
