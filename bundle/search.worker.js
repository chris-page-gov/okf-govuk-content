import { rankOrdinals, searchShard, tokenize } from "./search-core.js";

const MAX_JSON_BYTES = 64 * 1024 * 1024;
const MAX_JSON_CACHE_ENTRIES = 64;
const MAX_SHARD_CACHE_ENTRIES = 32;
const RETRYABLE = new Set([408, 425, 429, 500, 502, 503, 504]);

let baseUrl = "";
let manifest = null;
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
  return String(reference.sha256 || "").toLowerCase();
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
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

async function readResponseText(response, url) {
  let readable = response;
  if (url.toLowerCase().endsWith(".gz") && !response.headers.get("content-encoding")?.toLowerCase().includes("gzip")) {
    if (!response.body || typeof DecompressionStream === "undefined") {
      throw new Error("This browser cannot decompress the advertised gzip search shard");
    }
    readable = new Response(response.body.pipeThrough(new DecompressionStream("gzip")));
  }
  const contentLength = Number(readable.headers.get("content-length") || 0);
  if (contentLength > MAX_JSON_BYTES) throw new Error("Search shard exceeds the 64 MiB response limit");
  if (!readable.body) {
    const text = await readable.text();
    if (new TextEncoder().encode(text).byteLength > MAX_JSON_BYTES) throw new Error("Search shard exceeds the 64 MiB response limit");
    return text;
  }
  const reader = readable.body.getReader();
  const decoder = new TextDecoder();
  let received = 0;
  let text = "";
  while (true) {
    const part = await reader.read();
    if (part.done) break;
    received += part.value.byteLength;
    if (received > MAX_JSON_BYTES) {
      await reader.cancel();
      throw new Error("Search shard exceeds the 64 MiB response limit");
    }
    text += decoder.decode(part.value, { stream: true });
  }
  return text + decoder.decode();
}

async function requestJson(reference, signal) {
  const url = resolvePath(reference);
  const expectedHash = referenceHash(reference);
  let lastError = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
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
      const text = await readResponseText(response, url);
      if (expectedHash && await sha256(text) !== expectedHash) throw new Error("Search shard integrity check failed");
      return JSON.parse(text);
    } catch (error) {
      if (error && error.name === "AbortError") throw error;
      lastError = error;
      if (attempt >= 2) break;
    }
  }
  throw lastError instanceof Error ? lastError : new Error("Search resource fetch failed");
}

async function cachedJson(reference, signal) {
  const key = resolvePath(reference) + "#" + referenceHash(reference);
  return cachedPromise(jsonCache, key, () => requestJson(reference, signal), MAX_JSON_CACHE_ENTRIES);
}

async function lexiconEntry(token, signal) {
  const shard = searchShard(token, Number(manifest.lexicon_shard_length || 2));
  const reference = manifest.entrypoints.lexicon[shard] || manifest.entrypoints.lexicon._;
  if (!reference) return null;
  const lexicon = await cachedPromise(
    lexiconCache,
    shard,
    () => cachedJson(reference, signal).then((rows) => new Map(rows.map((row) => [row.token, row]))),
    MAX_SHARD_CACHE_ENTRIES
  );
  return lexicon.get(token) || null;
}

async function suggestionsFor(prefix, signal) {
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
    () => cachedJson(reference, signal),
    MAX_SHARD_CACHE_ENTRIES
  );
  for (let length = Math.min(normalized.length, 8); length >= minimum; length -= 1) {
    const rows = payload[normalized.slice(0, length)] || [];
    if (!rows.length) continue;
    const exact = rows.filter((row) => String(row.token).startsWith(normalized));
    return (exact.length ? exact : rows).slice(0, 12);
  }
  return [];
}

async function entriesForToken(token, signal) {
  const exact = await lexiconEntry(token, signal);
  if (exact) return [exact];
  const suggestions = await suggestionsFor(token, signal);
  const rows = await Promise.all(suggestions.map((suggestion) => lexiconEntry(suggestion.token, signal)));
  return rows.filter(Boolean);
}

async function postingsFor(reference, signal) {
  const key = resolvePath(reference);
  return cachedPromise(
    postingsCache,
    key,
    () => cachedJson(reference, signal).then((payload) => payload.tokens || {}),
    MAX_SHARD_CACHE_ENTRIES
  );
}

async function resultsFor(reference, signal) {
  const key = resolvePath(reference);
  return cachedPromise(resultCache, key, () => cachedJson(reference, signal), MAX_SHARD_CACHE_ENTRIES);
}

async function queryIndex(query, signal) {
  const tokens = tokenize(query, Number(manifest.token_min_length || 2));
  if (!tokens.length) return [];
  const entryGroups = (await Promise.all(tokens.map((token) => entriesForToken(token, signal)))).filter((group) => group.length);
  if (!entryGroups.length) return [];
  const hydrated = [];
  for (const group of entryGroups) {
    const rows = [];
    for (const entry of group) {
      const postings = await postingsFor(entry.postings, signal);
      rows.push({ ...entry, rows: postings[entry.token] || [] });
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
  const documentByOrdinal = new Map();
  await Promise.all([...paths].map(async (reference) => {
    for (const document of await resultsFor(reference, signal)) documentByOrdinal.set(Number(document.ordinal), document);
  }));
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
      manifest = await requestJson(message.manifestUrl, undefined);
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
      const suggestions = await suggestionsFor(message.prefix, activeController.signal);
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
