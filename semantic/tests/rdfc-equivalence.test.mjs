import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

test("RDFC-1.0 equivalence is independent of JSON key and array order", async () => {
  const root = await mkdtemp(join(tmpdir(), "govuk-okf-rdfc-"));
  const context = join(root, "context.jsonld");
  const left = join(root, "left.jsonld");
  const right = join(root, "right.jsonld");
  await writeFile(
    context,
    JSON.stringify({ "@context": { "name": "https://schema.org/name", "knows": { "@id": "https://schema.org/knows", "@type": "@id" } } }),
  );
  await writeFile(left, JSON.stringify({ "@context": "https://example.test/context/govuk-okf-v1.jsonld", "@id": "https://example.test/a", name: "A", knows: ["https://example.test/c", "https://example.test/b"] }));
  await writeFile(right, JSON.stringify({ knows: ["https://example.test/b", "https://example.test/c"], name: "A", "@id": "https://example.test/a", "@context": "https://example.test/context/govuk-okf-v1.jsonld" }));
  const result = spawnSync(process.execPath, ["rdfc-equivalence.mjs", left, right, context], {
    cwd: new URL("..", import.meta.url),
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).equivalent, true);
});

test("undeclared remote contexts fail closed", async () => {
  const root = await mkdtemp(join(tmpdir(), "govuk-okf-rdfc-"));
  const context = join(root, "context.jsonld");
  const left = join(root, "left.jsonld");
  await writeFile(context, JSON.stringify({ "@context": {} }));
  await writeFile(left, JSON.stringify({ "@context": "https://attacker.example/context", "@id": "https://example.test/a" }));
  const result = spawnSync(process.execPath, ["rdfc-equivalence.mjs", left, left, context], {
    cwd: new URL("..", import.meta.url),
    encoding: "utf8",
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /network and undeclared JSON-LD context are forbidden/);
});
