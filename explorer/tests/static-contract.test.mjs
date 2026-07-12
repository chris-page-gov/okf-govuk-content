import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

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
});

test("application never uses executable source HTML sinks", async () => {
  const files = await Promise.all(["app.js", "data.js", "search.worker.js"].map((name) => readFile(join(source, name), "utf8")));
  const code = files.join("\n");
  assert.doesNotMatch(code, /\.innerHTML\s*=/);
  assert.doesNotMatch(code, /\beval\s*\(/);
  assert.doesNotMatch(code, /new\s+Function\s*\(/);
  assert.match(code, /textContent/);
  assert.match(code, /instrumentationConsent = false/);
  assert.match(code, /querySelectorAll\("button\[data-mode\]"\)/);
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
});
