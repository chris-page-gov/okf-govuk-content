import { rankOrdinals, searchShard, tokenize } from "./search-core.js";
import { mapWithConcurrency, QueryBudget, SEARCH_LIMITS, validateSearchManifest } from "./search-contract.js";

const MAX_JSON_BYTES = 8 * 1024 * 1024;
const MAX_JSON_CACHE_ENTRIES = 64;
const MAX_SHARD_CACHE_ENTRIES = 32;
const RETRYABLE = new Set([408, 425, 429, 500, 502, 503, 504]);

let baseUrl = "";
let manifest = null;
let shardIntegrity = new Map();
let activeController = null;
const jsonCache = new Map();
const lexiconCache = new Map();
const postingsCache = new Map();
const resultCache = new Map();
const prefixCache = new Map();

function cachedPromise(cache, key, factory, limit) {
  if (cache.has(key)) {
    const existing = cache.get(key);
    cache.delete(key);
    cache.set(key, existing);
    return existing;
  }
  const pending = factory();
  cache.set(key, pending);
  pending.catch(() => {
    if (cache.get(key) === pending) cache.delete(key);
  });
  while (cache.size > limit) cache.delete(cache.keys().next().value);
  return pending;
}

function referencePath(reference) {
  if (typeof reference === "string") return reference;
  if (!reference || typeof reference !== "object") return "";
  return String(reference.path || reference.url || reference.href || "");
}

function referenceHash(reference) {
  if (!reference || typeof reference !== "object") return "";
  const value = String(reference.sha256 || "").toLowerCase();
  if (value && !/^[0-9a-f]{64}$/.test(value)) throw new Error("Search resource SHA-256 is malformed");
  return value;
}

function resolvePath(reference) {
  const path = referencePath(reference);
  if (!path) throw new Error("Search resource path is missing");
  const url = new URL(path, baseUrl);
  if (url.protocol !== "https:" && url.origin !== self.location.origin) {
    throw new Error("Search resources must use HTTPS or the Explorer origin");
  }
  return url.toString();
}

async function sha256(text) {
  return sha256Bytes(new TextEncoder().encode(text));
}

async function sha256Bytes(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function canonicalJson(value) {
  if (Array.isArray(value)) return "[" + value.map((item) => canonicalJson(item)).join(",") + "]";
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort().map((key) => JSON.stringify(key) + ":" + canonicalJson(value[key])).join(",") + "}";
  }
  return JSON.stringify(value);
}

function bindShardIntegrity(reference) {
  if (reference && typeof reference === "object") return reference;
  const path = referencePath(reference);
  const expectedHash = shardIntegrity.get(path);
  if (!expectedHash) throw new Error("Search shard has no integrity metadata");
  return { path, sha256: expectedHash };
}

async function loadShardIntegrity(signal, expectedSnapshot) {
  const document = await requestJson(manifest.shard_metadata, signal);
  if (!document || typeof document !== "object" || !document.shards || typeof document.shards !== "object") {
    throw new Error("Search shard metadata is malformed");
  }
  const snapshot = String(document.snapshot_id || document.snapshot || "");
  if (expectedSnapshot && snapshot !== expectedSnapshot) throw new Error("Search shard metadata snapshot differs");
  const observed = await sha256(canonicalJson(document.shards) + "\n");
  if (observed !== manifest.shard_manifest_sha256) throw new Error("Search shard metadata integrity check failed");
  const entries = new Map();
  for (const rows of Object.values(document.shards)) {
    if (!Array.isArray(rows)) throw new Error("Search shard metadata group is malformed");
    for (const row of rows) {
      const path = referencePath(row);
      const hash = referenceHash(row);
      if (!path || !hash) throw new Error("Search shard metadata row is incomplete");
      if (expectedSnapshot && String(row.snapshot || "") !== expectedSnapshot) throw new Error("Search shard snapshot differs");
      if (entries.has(path)) throw new Error("Duplicate search shard integrity path");
      entries.set(path, hash);
    }
  }
  return entries;
}

async function readResponseBytes(response) {
  const contentLength = Number(response.headers.get("content-length") || 0);
  if (contentLength > MAX_JSON_BYTES) throw new Error("Search shard exceeds the 8 MiB response limit");
  if (!response.body) {
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength > MAX_JSON_BYTES) throw new Error("Search shard exceeds the 8 MiB response limit");
    return bytes;
  }
  const reader = response.body.getReader();
  const chunks = [];
  let received = 0;
  while (true) {
    const part = await reader.read();
    if (part.done) break;
    received += part.value.byteLength;
    if (received > MAX_JSON_BYTES) {
      await reader.cancel();
      throw new Error("Search shard exceeds the 8 MiB response limit");
    }
    chunks.push(part.value);
  }
  const bytes = new Uint8Array(received);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

async function decodeResponseBytes(bytes, response, url, budget) {
  if (!url.toLowerCase().endsWith(".gz")) {
    if (budget) budget.consumeDecodedBytes(bytes.byteLength);
    return new TextDecoder().decode(bytes);
  }
  if (response.headers.get("content-encoding")?.toLowerCase().includes("gzip")) {
    throw new Error("Pre-compressed search shards must be served without Content-Encoding so their published bytes can be verified");
  }
  if (typeof DecompressionStream === "undefined") {
    throw new Error("This browser cannot decompress the advertised gzip search shard");
  }
  const body = new Response(bytes).body;
  if (!body) throw new Error("This browser cannot stream the advertised gzip search shard");
  const reader = body.pipeThrough(new DecompressionStream("gzip")).getReader();
  const decoder = new TextDecoder();
  let received = 0;
  let text = "";
  while (true) {
    const part = await reader.read();
    if (part.done) break;
    received += part.value.byteLength;
    if (received > MAX_JSON_BYTES) {
      await reader.cancel();
      throw new Error("Search shard exceeds the 8 MiB decoded response limit");
    }
    if (budget) budget.consumeDecodedBytes(part.value.byteLength);
    text += decoder.decode(part.value, { stream: true });
  }
  return text + decoder.decode();
}

async function requestJson(reference, signal, budget) {
  const url = resolvePath(reference);
  const expectedHash = referenceHash(reference);
  let lastError = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      if (budget) budget.consumeResource();
      const response = await fetch(url, { cache: "default", signal });
      if (!response.ok) {
        const error = new Error(url + ": " + response.status + " " + response.statusText);
        if (attempt < 2 && RETRYABLE.has(response.status)) {
          lastError = error;
          await new Promise((resolve) => setTimeout(resolve, 200 * (2 ** attempt)));
          continue;
        }
        throw error;
      }
      const bytes = await readResponseBytes(response);
      if (expectedHash && await sha256Bytes(bytes) !== expectedHash) throw new Error("Search shard integrity check failed");
      const text = await decodeResponseBytes(bytes, response, url, budget);
      return JSON.parse(text);
    } catch (error) {
      if (error && error.name === "AbortError") throw error;
      lastError = error;
      if (attempt >= 2) break;
    }
  }
  throw lastError instanceof Error ? lastError : new Error("Search resource fetch failed");
}

async function cachedJson(reference, signal, budget) {
  reference = bindShardIntegrity(reference);
  const key = resolvePath(reference) + "#" + referenceHash(reference);
  return cachedPromise(jsonCache, key, () => requestJson(reference, signal, budget), MAX_JSON_CACHE_ENTRIES);
}

async function lexiconEntry(token, signal, budget) {
  const shard = searchShard(token, Number(manifest.lexicon_shard_length || 2));
  const reference = manifest.entrypoints.lexicon[shard] || manifest.entrypoints.lexicon._;
  if (!reference) return null;
  const lexicon = await cachedPromise(
    lexiconCache,
    shard,
    () => cachedJson(reference, signal, budget).then((rows) => new Map(rows.map((row) => [row.token, row]))),
    MAX_SHARD_CACHE_ENTRIES
  );
  return lexicon.get(token) || null;
}

async function suggestionsFor(prefix, signal, budget) {
  const tokens = tokenize(prefix, Number(manifest.token_min_length || 2));
  const normalized = tokens[tokens.length - 1] || String(prefix || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  const minimum = Number(manifest.prefix_min_length || 3);
  if (normalized.length < minimum) return [];
  const shard = searchShard(normalized, Number(manifest.lexicon_shard_length || 2));
  const reference = manifest.entrypoints.prefixes[shard] || manifest.entrypoints.prefixes._;
  if (!reference) return [];
  const payload = await cachedPromise(
    prefixCache,
    shard,
    () => cachedJson(reference, signal, budget),
    MAX_SHARD_CACHE_ENTRIES
  );
  for (let length = Math.min(normalized.length, 8); length >= minimum; length -= 1) {
    const rows = payload[normalized.slice(0, length)] || [];
    if (!rows.length) continue;
    const exact = rows.filter((row) => String(row.token).startsWith(normalized));
    return (exact.length ? exact : rows).slice(0, SEARCH_LIMITS.maxSuggestionsPerToken);
  }
  return [];
}

async function entriesForToken(token, signal, budget) {
  const exact = await lexiconEntry(token, signal, budget);
  if (exact) return [exact];
  const suggestions = await suggestionsFor(token, signal, budget);
  const rows = await mapWithConcurrency(
    suggestions,
    SEARCH_LIMITS.maxInFlightRequests,
    (suggestion) => lexiconEntry(suggestion.token, signal, budget),
    signal
  );
  return rows.filter(Boolean);
}

async function postingsFor(reference, signal, budget) {
  reference = bindShardIntegrity(reference);
  const key = resolvePath(reference);
  return cachedPromise(
    postingsCache,
    key,
    () => cachedJson(reference, signal, budget).then((payload) => payload.tokens || {}),
    MAX_SHARD_CACHE_ENTRIES
  );
}

async function resultsFor(reference, signal, budget) {
  reference = bindShardIntegrity(reference);
  const key = resolvePath(reference);
  return cachedPromise(resultCache, key, () => cachedJson(reference, signal, budget), MAX_SHARD_CACHE_ENTRIES);
}

async function queryIndex(query, signal) {
  const tokens = tokenize(query, Number(manifest.token_min_length || 2));
  if (!tokens.length) return [];
  if (tokens.length > SEARCH_LIMITS.maxQueryTokens) throw new Error("Search query exceeds the supported token limit");
  const budget = new QueryBudget();
  const entryGroups = (await mapWithConcurrency(
    tokens,
    SEARCH_LIMITS.maxInFlightRequests,
    (token) => entriesForToken(token, signal, budget),
    signal
  )).filter((group) => group.length);
  if (!entryGroups.length) return [];
  const hydrated = [];
  for (const group of entryGroups) {
    const rows = [];
    for (const entry of group) {
      const postings = await postingsFor(entry.postings, signal, budget);
      const postingRows = postings[entry.token] || [];
      budget.consumePostingRows(postingRows.length);
      rows.push({ ...entry, rows: postingRows });
    }
    hydrated.push(rows);
  }
  hydrated.sort((left, right) => Math.min(...left.map((entry) => Number(entry.df || 0))) - Math.min(...right.map((entry) => Number(entry.df || 0))));
  const ranked = rankOrdinals(
    hydrated,
    Number(manifest.result_limit || 200),
    Number(manifest.counts && manifest.counts.max_postings_per_token || Number.MAX_SAFE_INTEGER)
  );
  const chunkSize = Number(manifest.result_doc_chunk_size || 1000);
  const paths = new Set();
  for (const match of ranked) {
    const reference = manifest.entrypoints.result_docs[Math.floor(match.ordinal / chunkSize)];
    if (reference) paths.add(reference);
  }
  if (paths.size > SEARCH_LIMITS.maxResultChunksPerQuery) throw new Error("Search query exceeds the result-chunk budget");
  const documentByOrdinal = new Map();
  await mapWithConcurrency([...paths], SEARCH_LIMITS.maxInFlightRequests, async (reference) => {
    const documents = await resultsFor(reference, signal, budget);
    budget.consumeDocuments(documents.length);
    for (const document of documents) documentByOrdinal.set(Number(document.ordinal), document);
  }, signal);
  return ranked.flatMap((match) => {
    const document = documentByOrdinal.get(match.ordinal);
    return document ? [{ ...document, score: match.score }] : [];
  });
}

self.onmessage = async (event) => {
  const message = event.data || {};
  try {
    if (message.type === "init") {
      baseUrl = new URL(message.baseUrl).toString();
      manifest = validateSearchManifest(
        await requestJson(message.manifestReference ?? message.manifestUrl, undefined),
        String(message.snapshotId || "")
      );
      shardIntegrity = await loadShardIntegrity(undefined, String(message.snapshotId || ""));
      self.postMessage({ type: "ready", id: message.id, manifest });
      return;
    }
    if (!manifest) throw new Error("Search worker is not initialised");
    if (activeController) activeController.abort();
    activeController = new AbortController();
    if (message.type === "query") {
      const results = await queryIndex(message.query, activeController.signal);
      self.postMessage({ type: "results", id: message.id, results });
      return;
    }
    if (message.type === "suggest") {
      const suggestions = await suggestionsFor(message.prefix, activeController.signal, new QueryBudget());
      self.postMessage({ type: "suggestions", id: message.id, suggestions });
      return;
    }
    throw new Error("Unknown search worker request");
  } catch (error) {
    if (error && error.name === "AbortError") {
      self.postMessage({ type: "cancelled", id: message.id });
      return;
    }
    self.postMessage({ type: "error", id: message.id, error: error instanceof Error ? error.message : String(error) });
  }
};
