#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import process from "node:process";
import readline from "node:readline";
import jsonld from "jsonld";

const [contextPath] = process.argv.slice(2);
if (!contextPath) throw new Error("usage: rdfc-stream.mjs <offline-context.jsonld>");
const contextDocument = JSON.parse(await readFile(resolve(contextPath), "utf8"));
const loader = async (url) => {
  if (String(url).endsWith("/context/govuk-okf-v1.jsonld")) {
    return { contextUrl: null, document: contextDocument, documentUrl: url };
  }
  throw new Error(`network and undeclared JSON-LD context are forbidden: ${url}`);
};
const sha256 = (value) => createHash("sha256").update(value, "utf8").digest("hex");
const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

for await (const line of lines) {
  if (!line.trim()) continue;
  const task = JSON.parse(line);
  try {
    const expanded = await jsonld.expand(task.document, { documentLoader: loader, safe: true });
    const nquads = await jsonld.canonize(task.document, {
      algorithm: "RDFC-1.0",
      format: "application/n-quads",
      documentLoader: loader,
      safe: true,
      maxWorkFactor: 1,
      signal: AbortSignal.timeout(30_000),
    });
    process.stdout.write(`${JSON.stringify({
      id: task.id,
      ok: true,
      expandedSha256: sha256(JSON.stringify(expanded)),
      canonicalNQuadsSha256: sha256(nquads),
      canonicalNQuadsStatements: nquads ? nquads.trimEnd().split("\n").length : 0,
    })}\n`);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ id: task.id, ok: false, error: String(error) })}\n`);
    process.exitCode = 1;
  }
}
