import { isAllowedBundleUrl, normaliseRecord, relationshipBucket } from "./core.js";

const MAX_JSON_BYTES = 64 * 1024 * 1024;
const MAX_LARGE_SHARD_CACHE_ENTRIES = 32;
const RETRYABLE_STATUS = new Set([408, 425, 429, 500, 502, 503, 504]);

function cachedPromise(cache, key, factory, limit = MAX_LARGE_SHARD_CACHE_ENTRIES) {
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

export function referencePath(reference) {
  if (typeof reference === "string") return reference;
  if (!reference || typeof reference !== "object") return "";
  return String(reference.path || reference.url || reference.href || "");
}

export function referenceHash(reference) {
  if (!reference || typeof reference !== "object") return "";
  const value = String(reference.sha256 || "").toLowerCase();
  if (value && !/^[0-9a-f]{64}$/.test(value)) throw new Error("Resource SHA-256 is malformed");
  return value;
}

export function resolveReference(reference, baseUrl) {
  const path = referencePath(reference);
  if (!path) throw new Error("Resource path is missing");
  return new URL(path, baseUrl).toString();
}

export function integrityReference(reference, metadataRows, label = "resource") {
  if (reference && typeof reference === "object") {
    referenceHash(reference);
    return reference;
  }
  if (!Array.isArray(metadataRows)) return reference;
  const path = referencePath(reference);
  const metadata = metadataRows.find((row) => row && row.path === path);
  if (!metadata) throw new Error(label + " has no integrity metadata");
  return { path, sha256: referenceHash(metadata) };
}

async function digestText(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

async function readText(response, url) {
  let readable = response;
  if (url.toLowerCase().endsWith(".gz") && !response.headers.get("content-encoding")?.toLowerCase().includes("gzip")) {
    if (!response.body || typeof DecompressionStream === "undefined") throw new Error("This browser cannot decompress the advertised gzip resource");
    readable = new Response(response.body.pipeThrough(new DecompressionStream("gzip")));
  }
  const reported = Number(readable.headers.get("content-length") || 0);
  if (reported > MAX_JSON_BYTES) throw new Error("JSON resource exceeds the 64 MiB response limit");
  if (!readable.body) {
    const text = await readable.text();
    if (new TextEncoder().encode(text).byteLength > MAX_JSON_BYTES) throw new Error("JSON resource exceeds the 64 MiB response limit");
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
      throw new Error("JSON resource exceeds the 64 MiB response limit");
    }
    text += decoder.decode(part.value, { stream: true });
  }
  return text + decoder.decode();
}

export async function fetchJson(reference, baseUrl, options = {}) {
  const url = resolveReference(reference, baseUrl);
  if (!isAllowedBundleUrl(url, options.currentOrigin || baseUrl)) throw new Error("JSON resources must use HTTPS or the Explorer origin");
  let lastError = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const response = await fetch(url, { cache: options.cache || "default", signal: options.signal });
      if (!response.ok) {
        const error = new Error(url + ": " + response.status + " " + response.statusText);
        if (attempt < 2 && RETRYABLE_STATUS.has(response.status)) {
          lastError = error;
          await new Promise((resolve) => setTimeout(resolve, 200 * (2 ** attempt)));
          continue;
        }
        throw error;
      }
      const text = await readText(response, url);
      const expectedHash = referenceHash(reference);
      if (expectedHash && await digestText(text) !== expectedHash) throw new Error("Resource integrity check failed for " + url);
      return { url, value: JSON.parse(text) };
    } catch (error) {
      if (error && error.name === "AbortError") throw error;
      lastError = error;
      if (attempt >= 2) break;
    }
  }
  throw lastError instanceof Error ? lastError : new Error("JSON resource fetch failed");
}

export function descriptorCandidates(documentUrl, explicitUrl = "", configuredUrl = "") {
  const candidates = [];
  if (explicitUrl) candidates.push(new URL(explicitUrl, documentUrl).toString());
  candidates.push(new URL("./okf-explorer.json", documentUrl).toString());
  if (configuredUrl) candidates.push(new URL(configuredUrl, documentUrl).toString());
  candidates.push(new URL("../okf-explorer.json", documentUrl).toString());
  candidates.push(new URL("../../okf-explorer.json", documentUrl).toString());
  return [...new Set(candidates)];
}

export class SearchClient {
  constructor(workerFactory = () => new Worker(new URL("./search.worker.js", import.meta.url), { type: "module" })) {
    this.worker = workerFactory();
    this.pending = new Map();
    this.nextId = 1;
    this.manifest = null;
    this.worker.onmessage = (event) => {
      const message = event.data || {};
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.type === "error") pending.reject(new Error(message.error || "Search worker failed"));
      else if (message.type === "cancelled") {
        const error = new Error("Search request was superseded");
        error.name = "AbortError";
        pending.reject(error);
      } else if (message.type === "ready") {
        this.manifest = message.manifest || null;
        pending.resolve(this.manifest);
      } else if (message.type === "results") pending.resolve(message.results || []);
      else if (message.type === "suggestions") pending.resolve(message.suggestions || []);
      else pending.reject(new Error("Unknown search worker response"));
    };
  }

  request(message) {
    const id = this.nextId;
    this.nextId += 1;
    this.worker.postMessage({ ...message, id });
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }

  init(baseUrl, manifestReference, snapshotId = "") {
    return this.request({ type: "init", baseUrl, manifestReference, snapshotId });
  }

  query(query) {
    return this.request({ type: "query", query });
  }

  suggest(prefix) {
    return this.request({ type: "suggest", prefix });
  }

  destroy() {
    this.worker.terminate();
    for (const pending of this.pending.values()) pending.reject(new Error("Search worker stopped"));
    this.pending.clear();
  }
}

export class LargeCorpusStore {
  constructor(descriptorUrl, descriptor, currentOrigin) {
    if (!descriptor || descriptor.kind !== "okf-large-corpus") throw new Error("Not an OKF large-corpus descriptor");
    this.descriptorUrl = descriptorUrl;
    this.baseUrl = new URL(".", descriptorUrl).toString();
    this.descriptor = descriptor;
    this.currentOrigin = currentOrigin;
    this.manifest = null;
    this.overview = null;
    this.analysis = null;
    this.adjacencyManifest = null;
    this.adjacencyBuckets = new Map();
    this.routeIndex = null;
    this.routeBuckets = new Map();
    this.recordChunks = new Map();
  }

  async bootstrap(signal) {
    const manifestResult = await fetchJson(this.descriptor.entrypoints.data_manifest, this.baseUrl, { signal, currentOrigin: this.currentOrigin });
    this.manifest = manifestResult.value;
    const advertisedRoot = String(this.descriptor.data_plane_manifest_root_sha256 || "");
    const manifestRoot = String(this.manifest.integrity && this.manifest.integrity.manifest_root_sha256 || "");
    if (advertisedRoot && advertisedRoot !== manifestRoot) throw new Error("Descriptor and data manifest integrity roots differ");
    const overviewReference = this.descriptor.entrypoints.overview_index || this.manifest.indexes.overview;
    this.overview = (await fetchJson(overviewReference, this.baseUrl, { signal, currentOrigin: this.currentOrigin })).value;
    const analysisReference = this.descriptor.entrypoints.analysis_overview || this.manifest.indexes.analysis;
    if (analysisReference) {
      try {
        this.analysis = (await fetchJson(analysisReference, this.baseUrl, { signal, currentOrigin: this.currentOrigin })).value;
      } catch {
        this.analysis = null;
      }
    }
    this.snapshotId();
    return this;
  }

  searchManifestReference() {
    return this.descriptor.entrypoints.search_manifest || this.manifest.indexes.search || "";
  }

  snapshotId() {
    const declarations = [];
    for (const [label, document] of [
      ["descriptor", this.descriptor],
      ["data manifest", this.manifest],
      ["overview", this.overview],
      ["analysis", this.analysis]
    ]) {
      if (!document || typeof document !== "object") continue;
      const values = [document.snapshot_id, document.snapshot]
        .filter((value) => value !== undefined && value !== null && value !== "")
        .map((value) => {
          if (typeof value !== "string" || !value.trim()) throw new Error(label + " has an invalid snapshot identifier");
          return value.trim();
        });
      if (new Set(values).size > 1) throw new Error(label + " advertises conflicting snapshot identifiers");
      if (values.length) declarations.push([label, values[0]]);
    }
    if (!declarations.length) return "";
    const unique = new Set(declarations.map(([, value]) => value));
    if (unique.size !== 1) {
      throw new Error("Bundle resources advertise different snapshot identifiers: " + declarations.map(([label, value]) => label + "=" + value).join(", "));
    }
    return declarations[0][1];
  }

  assertResourceSnapshot(document, label) {
    if (!document || typeof document !== "object" || Array.isArray(document)) return;
    const advertised = document.snapshot_id || document.snapshot || "";
    if (!advertised) return;
    const expected = this.snapshotId();
    if (!expected || advertised !== expected) throw new Error(label + " snapshot differs from the loaded bundle snapshot");
  }

  overviewRecords() {
    const values = this.overview.recent_records || this.overview.recent_datasets || this.overview.sample_records || this.overview.samples || [];
    return values.map((record) => normaliseRecord(record));
  }

  async loadRelationships(route, signal) {
    const reference = this.descriptor.entrypoints.relationship_adjacency || this.manifest.indexes.relationship_adjacency;
    if (!reference) return [];
    if (!this.adjacencyManifest) {
      this.adjacencyManifest = (await fetchJson(reference, this.baseUrl, { signal, currentOrigin: this.currentOrigin })).value;
      this.assertResourceSnapshot(this.adjacencyManifest, "Relationship adjacency manifest");
      if (this.adjacencyManifest.algorithm !== "fnv1a32-prefix-2") throw new Error("Unsupported relationship adjacency algorithm");
    }
    const bucket = relationshipBucket(route);
    const bucketReference = integrityReference(
      this.adjacencyManifest.buckets[bucket],
      this.adjacencyManifest.shards,
      "Relationship adjacency shard"
    );
    if (!bucketReference) return [];
    const payload = await cachedPromise(
      this.adjacencyBuckets,
      bucket,
      () => fetchJson(bucketReference, this.baseUrl, { signal, currentOrigin: this.currentOrigin }).then((result) => result.value)
    );
    return Array.isArray(payload[route]) ? payload[route] : [];
  }

  async loadRecord(route, signal) {
    const indexReference = this.descriptor.entrypoints.route_index || this.manifest.indexes.route_index || this.manifest.indexes.routes;
    if (!indexReference) return null;
    if (!this.routeIndex) {
      this.routeIndex = (await fetchJson(indexReference, this.baseUrl, { signal, currentOrigin: this.currentOrigin })).value;
      this.assertResourceSnapshot(this.routeIndex, "Route-index manifest");
    }
    if (this.routeIndex.schema !== "okf-route-index.v1" || this.routeIndex.entry_shape !== "identifier-to-typed-matches") {
      throw new Error("Unsupported route-index contract");
    }
    if (this.routeIndex.algorithm !== "fnv1a32-prefix-2") throw new Error("Unsupported route-index algorithm");
    const bucket = relationshipBucket(route);
    const bucketReference = integrityReference(
      this.routeIndex.buckets && this.routeIndex.buckets[bucket],
      this.routeIndex.shards,
      "Route-index shard"
    );
    if (!bucketReference) return null;
    const routePayload = await cachedPromise(
      this.routeBuckets,
      bucket,
      () => fetchJson(bucketReference, this.baseUrl, { signal, currentOrigin: this.currentOrigin }).then((result) => result.value)
    );
    const expectedKind = { dataset: "datasets", publisher: "publishers", resource: "resources" }[String(route).split("/", 1)[0]];
    if (!expectedKind) return null;
    const matches = Array.isArray(routePayload[route])
      ? routePayload[route].filter((entry) => entry && entry.kind === expectedKind && entry.open === route)
      : [];
    if (!matches.length) return null;
    if (matches.length !== 1) throw new Error("Ambiguous exact route in route index");
    const locator = matches[0];
    const ordinal = Number(locator.ordinal);
    const chunkSize = Number(this.routeIndex.chunk_size || 1000);
    if (!Number.isInteger(ordinal) || ordinal < 0 || !Number.isInteger(chunkSize) || chunkSize < 1) {
      throw new Error("Invalid route-index locator");
    }
    const chunkReference = integrityReference(
      this.manifest.chunks[expectedKind] && this.manifest.chunks[expectedKind][Math.floor(ordinal / chunkSize)],
      this.manifest.shards && this.manifest.shards[expectedKind],
      "Record shard"
    );
    if (!chunkReference) return null;
    const chunkKey = resolveReference(chunkReference, this.baseUrl);
    const chunk = await cachedPromise(
      this.recordChunks,
      chunkKey,
      () => fetchJson(chunkReference, this.baseUrl, { signal, currentOrigin: this.currentOrigin }).then((result) => result.value)
    );
    const raw = Array.isArray(chunk) ? chunk[ordinal % chunkSize] : null;
    if (raw && raw.open !== route) throw new Error("Route-index target mismatch");
    return raw ? normaliseRecord(raw, route) : null;
  }
}
