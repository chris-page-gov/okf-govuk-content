#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import process from "node:process";
import jsonld from "jsonld";

function sha256(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function usage() {
  throw new Error("usage: rdfc-equivalence.mjs <left.jsonld> <right.jsonld> <offline-context.jsonld>");
}

const [leftPath, rightPath, contextPath] = process.argv.slice(2);
if (!leftPath || !rightPath || !contextPath) usage();

const contextDocument = JSON.parse(await readFile(resolve(contextPath), "utf8"));
const contextNames = new Set([
  "https://chris-page-gov.github.io/okf-govuk-content/context/govuk-okf-v1.jsonld",
  "https://chris-page-gov.github.io/okf-govuk-content/semantic/context/govuk-okf-v1.jsonld",
]);
const documentLoader = async (url) => {
  if (contextNames.has(url) || String(url).endsWith("/context/govuk-okf-v1.jsonld")) {
    return { contextUrl: null, document: contextDocument, documentUrl: url };
  }
  throw new Error(`network and undeclared JSON-LD context are forbidden: ${url}`);
};

async function canonical(path) {
  const document = JSON.parse(await readFile(resolve(path), "utf8"));
  const expanded = await jsonld.expand(document, { documentLoader, safe: true });
  const nquads = await jsonld.canonize(document, {
    algorithm: "RDFC-1.0",
    format: "application/n-quads",
    documentLoader,
    safe: true,
    maxWorkFactor: 1,
    signal: AbortSignal.timeout(30_000),
  });
  return {
    expandedSha256: sha256(JSON.stringify(expanded)),
    canonicalNQuadsSha256: sha256(nquads),
    canonicalNQuadsStatements: nquads ? nquads.trimEnd().split("\n").length : 0,
    canonicalNQuads: nquads,
  };
}

const left = await canonical(leftPath);
const right = await canonical(rightPath);
const equivalent = left.canonicalNQuads === right.canonicalNQuads;
const output = {
  schema: "govuk-okf-rdfc-equivalence.v1",
  algorithm: "RDFC-1.0",
  implementation: { jsonld: "9.0.0", rdfCanonize: "5.0.0", node: process.version },
  offlineContext: resolve(contextPath),
  networkAccess: false,
  complexityControls: { maxWorkFactor: 1, timeoutMs: 30_000 },
  equivalent,
  left: { ...left, canonicalNQuads: undefined },
  right: { ...right, canonicalNQuads: undefined },
};
process.stdout.write(`${JSON.stringify(output)}\n`);
if (!equivalent) process.exitCode = 1;
