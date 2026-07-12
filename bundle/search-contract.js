export const SEARCH_LIMITS = Object.freeze({
  maxDecodedBytesPerQuery: 32 * 1024 * 1024,
  maxDocumentsPerQuery: 2000,
  maxInFlightRequests: 4,
  maxManifestShardReferences: 4096,
  maxPostingRowsPerQuery: 250000,
  maxQueryResources: 256,
  maxQueryTokens: 24,
  maxResultChunksPerQuery: 16,
  maxResultLimit: 500,
  maxSuggestionsPerToken: 3,
  maxPostingsPerToken: 50000
});

function boundedInteger(value, fallback, minimum, maximum, label) {
  const candidate = value === undefined || value === null || value === "" ? fallback : Number(value);
  if (!Number.isInteger(candidate) || candidate < minimum || candidate > maximum) {
    throw new Error(`Search manifest ${label} is outside the supported range`);
  }
  return candidate;
}

function referenceCount(value, label) {
  if (Array.isArray(value)) return value.length;
  if (value && typeof value === "object") return Object.keys(value).length;
  throw new Error(`Search manifest ${label} entrypoints are malformed`);
}

export function validateSearchManifest(value, expectedSnapshot = "") {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Search manifest must be an object");
  if (value.schema !== "okf-static-search.v1") throw new Error("Unsupported static-search manifest");
  const entrypoints = value.entrypoints;
  if (!entrypoints || typeof entrypoints !== "object" || Array.isArray(entrypoints)) {
    throw new Error("Search manifest entrypoints are malformed");
  }
  for (const [label, references] of [
    ["result_docs", entrypoints.result_docs],
    ["lexicon", entrypoints.lexicon],
    ["postings", entrypoints.postings],
    ["prefixes", entrypoints.prefixes]
  ]) {
    if (referenceCount(references, label) > SEARCH_LIMITS.maxManifestShardReferences) {
      throw new Error(`Search manifest ${label} entrypoints exceed the supported limit`);
    }
  }
  const snapshot = String(value.snapshot_id || value.snapshot || "");
  if (expectedSnapshot && snapshot !== expectedSnapshot) {
    throw new Error("Search manifest snapshot differs from the loaded bundle snapshot");
  }
  const counts = value.counts && typeof value.counts === "object" ? value.counts : {};
  return {
    ...value,
    token_min_length: boundedInteger(value.token_min_length, 2, 2, 16, "token_min_length"),
    prefix_min_length: boundedInteger(value.prefix_min_length, 3, 2, 16, "prefix_min_length"),
    lexicon_shard_length: boundedInteger(value.lexicon_shard_length, 2, 1, 4, "lexicon_shard_length"),
    result_limit: boundedInteger(value.result_limit, 200, 1, SEARCH_LIMITS.maxResultLimit, "result_limit"),
    result_doc_chunk_size: boundedInteger(value.result_doc_chunk_size, 1000, 1, 100000, "result_doc_chunk_size"),
    counts: {
      ...counts,
      max_postings_per_token: boundedInteger(
        counts.max_postings_per_token,
        SEARCH_LIMITS.maxPostingsPerToken,
        1,
        SEARCH_LIMITS.maxPostingsPerToken,
        "counts.max_postings_per_token"
      )
    }
  };
}

export class QueryBudget {
  constructor(limits = SEARCH_LIMITS) {
    this.limits = limits;
    this.decodedBytes = 0;
    this.documents = 0;
    this.postingRows = 0;
    this.resources = 0;
  }

  consumeDecodedBytes(count) {
    this.decodedBytes += Number(count) || 0;
    if (this.decodedBytes > this.limits.maxDecodedBytesPerQuery) {
      throw new Error("Search query exceeds the aggregate decoded-byte budget");
    }
  }

  consumeDocuments(count) {
    this.documents += Number(count) || 0;
    if (this.documents > this.limits.maxDocumentsPerQuery) {
      throw new Error("Search query exceeds the document materialisation budget");
    }
  }

  consumePostingRows(count) {
    this.postingRows += Number(count) || 0;
    if (this.postingRows > this.limits.maxPostingRowsPerQuery) {
      throw new Error("Search query exceeds the posting-row budget");
    }
  }

  consumeResource() {
    this.resources += 1;
    if (this.resources > this.limits.maxQueryResources) {
      throw new Error("Search query exceeds the resource request budget");
    }
  }
}

export async function mapWithConcurrency(values, concurrency, mapper, signal) {
  const items = [...values];
  if (!items.length) return [];
  const results = new Array(items.length);
  let next = 0;
  async function run() {
    while (true) {
      if (signal && signal.aborted) throw new DOMException("Search request was superseded", "AbortError");
      const index = next;
      next += 1;
      if (index >= items.length) return;
      results[index] = await mapper(items[index], index);
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, () => run()));
  return results;
}
