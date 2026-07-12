import test from "node:test";
import assert from "node:assert/strict";

import { pagesBasePath, pagesFallbackUrl } from "../src/pages.js";

test("Pages fallback preserves the project base, query state and hash route", () => {
  const input = new URL("https://chris-page-gov.github.io/okf-govuk-content/missing/path?q=tax&view=timeline#dataset/record-one");
  const target = pagesFallbackUrl(input, "okf-govuk-content");
  assert.equal(target.toString(), "https://chris-page-gov.github.io/okf-govuk-content/?q=tax&view=timeline#dataset/record-one");
  assert.equal(pagesBasePath("/okf-govuk-content/missing/path", "okf-govuk-content"), "/okf-govuk-content/");
});

test("Pages fallback remains root-safe on a custom domain", () => {
  const target = pagesFallbackUrl("https://catalogue.example/missing?q=tax#publisher/one", "okf-govuk-content");
  assert.equal(target.toString(), "https://catalogue.example/?q=tax#publisher/one");
});
