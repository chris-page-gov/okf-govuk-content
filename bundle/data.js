import { isAllowedBundleUrl, normaliseRecord, relationshipBucket } from "./core.js";
import { prepareReleaseDataPlane, releaseDataPlaneDocument, releaseDataRequest } from "./release-data-plane.js";

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

export function descriptorEntrypoint(descriptor, name) {
  const entrypoint = descriptor && descriptor.entrypoints && descriptor.entrypoints[name];
  const integrity = descriptor && descriptor.entrypoint_integrity && descriptor.entrypoint_integrity[name];
  if (!integrity) return entrypoint;
  if (referencePath(integrity) !== referencePath(entrypoint)) {
    throw new Error("Descriptor entrypoint and integrity path differ for " + name);
  }
  referenceHash(integrity);
  return integrity;
}

async function digestBytes(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

async function readBoundedBytes(response) {
  const reported = Number(response.headers.get("content-length") || 0);
  if (reported > MAX_JSON_BYTES) throw new Error("JSON resource exceeds the 64 MiB response limit");
  if (!response.body) {
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength > MAX_JSON_BYTES) throw new Error("JSON resource exceeds the 64 MiB response limit");
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
      throw new Error("JSON resource exceeds the 64 MiB response limit");
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

async function gunzipBoundedBytes(bytes, label) {
  if (typeof DecompressionStream === "undefined") throw new Error("This browser cannot decompress " + label);
  const body = new Response(bytes).body;
  if (!body) throw new Error("This browser cannot stream " + label);
  const reader = body.pipeThrough(new DecompressionStream("gzip")).getReader();
  const chunks = [];
  let received = 0;
  while (true) {
    const part = await reader.read();
    if (part.done) break;
    received += part.value.byteLength;
    if (received > MAX_JSON_BYTES) {
      await reader.cancel();
      throw new Error(label + " exceeds the 64 MiB decoded response limit");
    }
    chunks.push(part.value);
  }
  const decoded = new Uint8Array(received);
  let offset = 0;
  for (const chunk of chunks) {
    decoded.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return decoded;
}

async function decodeJsonBytes(bytes, response, logicalPath, compression = "") {
  const isGzip = compression ? compression === "gzip" : logicalPath.toLowerCase().endsWith(".gz");
  if (!isGzip) return new TextDecoder().decode(bytes);
  if (response.headers.get("content-encoding")?.toLowerCase().includes("gzip")) {
    throw new Error("Pre-compressed JSON resources must be served without Content-Encoding so their published bytes can be verified");
  }
  return new TextDecoder().decode(await gunzipBoundedBytes(bytes, "the advertised gzip resource"));
}

export async function fetchJson(reference, baseUrl, options = {}) {
  const distributed = releaseDataRequest(reference, options.releaseDataPlane);
  const url = distributed ? distributed.url : resolveReference(reference, baseUrl);
  if (!isAllowedBundleUrl(url, options.currentOrigin || baseUrl)) throw new Error("JSON resources must use HTTPS or the Explorer origin");
  let lastError = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const response = await fetch(url, {
        cache: options.cache || "default",
        signal: options.signal,
        headers: distributed ? distributed.headers : undefined
      });
      if (!response.ok) {
        const error = new Error(url + ": " + response.status + " " + response.statusText);
        if (attempt < 2 && RETRYABLE_STATUS.has(response.status)) {
          lastError = error;
          await new Promise((resolve) => setTimeout(resolve, 200 * (2 ** attempt)));
          continue;
        }
        throw error;
      }
      if (distributed && response.status !== 206) throw new Error("Release pack server did not honour the bounded byte-range request");
      const bytes = await readBoundedBytes(response);
      if (distributed && (bytes[0] !== 0x1f || bytes[1] !== 0x8b)) {
        throw new Error("Release-pack transport member is not gzip-framed");
      }
      if (distributed && response.headers.get("content-encoding")) {
        throw new Error("Pages packs must be served as published bytes without Content-Encoding");
      }
      if (distributed && bytes.byteLength !== distributed.expectedPackedLength) {
        throw new Error("Release pack byte-range length differs from the index");
      }
      const contentRange = response.headers.get("content-range");
      if (distributed && contentRange !== distributed.expectedContentRange) {
        throw new Error("Release pack Content-Range differs from the index");
      }
      const packedHash = distributed ? distributed.expectedPackedHash : referenceHash(reference);
      if (packedHash && await digestBytes(bytes) !== packedHash) throw new Error("Resource integrity check failed for " + url);
      const sourceBytes = distributed && distributed.transportCompression === "gzip"
        ? await gunzipBoundedBytes(bytes, "the release-pack transport member")
        : bytes;
      if (distributed && sourceBytes.byteLength !== distributed.expectedLength) {
        throw new Error("Release-pack decoded member length differs from the index");
      }
      if (distributed && await digestBytes(sourceBytes) !== distributed.expectedHash) {
        throw new Error("Logical resource integrity check failed for " + distributed.logicalPath);
      }
      const logicalPath = distributed ? distributed.logicalPath : referencePath(reference);
      const text = await decodeJsonBytes(sourceBytes, response, logicalPath, distributed ? distributed.compression : "");
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

  init(baseUrl, manifestReference, snapshotId = "", releaseDataPlane = null) {
    return this.request({ type: "init", baseUrl, manifestReference, snapshotId, releaseDataPlane });
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
    this.siteTopology = null;
    this.adjacencyManifest = null;
    this.adjacencyBuckets = new Map();
    this.routeIndex = null;
    this.routeBuckets = new Map();
    this.recordChunks = new Map();
    this.releaseDataPlane = null;
  }

  async bootstrap(signal) {
    const releaseReference = descriptorEntrypoint(this.descriptor, "release_data_plane");
    if (releaseReference) {
      const result = await fetchJson(releaseReference, this.baseUrl, { signal, currentOrigin: this.currentOrigin });
      this.releaseDataPlane = await prepareReleaseDataPlane(
        result.value,
        this.baseUrl,
        String(this.descriptor.snapshot_id || this.descriptor.snapshot || "")
      );
    }
    const manifestResult = await this.fetch(descriptorEntrypoint(this.descriptor, "data_manifest"), signal);
    this.manifest = manifestResult.value;
    const advertisedRoot = String(this.descriptor.data_plane_manifest_root_sha256 || "");
    const manifestRoot = String(this.manifest.integrity && this.manifest.integrity.manifest_root_sha256 || "");
    if (advertisedRoot && advertisedRoot !== manifestRoot) throw new Error("Descriptor and data manifest integrity roots differ");
    const overviewReference = descriptorEntrypoint(this.descriptor, "overview_index") || this.manifest.indexes.overview;
    this.overview = (await this.fetch(overviewReference, signal)).value;
    const analysisReference = descriptorEntrypoint(this.descriptor, "analysis_overview") || this.manifest.indexes.analysis;
    if (analysisReference) {
      try {
        this.analysis = (await this.fetch(analysisReference, signal)).value;
      } catch {
        this.analysis = null;
      }
    }
    this.snapshotId();
    return this;
  }

  fetch(reference, signal) {
    return fetchJson(reference, this.baseUrl, {
      signal,
      currentOrigin: this.currentOrigin,
      releaseDataPlane: this.releaseDataPlane
    });
  }

  releaseDataPlaneDocument() {
    return releaseDataPlaneDocument(this.releaseDataPlane);
  }

  searchManifestReference() {
    return descriptorEntrypoint(this.descriptor, "search_manifest") || this.manifest.indexes.search || "";
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

  async loadSiteTopology(signal) {
    if (this.siteTopology) return this.siteTopology;
    const reference = descriptorEntrypoint(this.descriptor, "site_topology") || this.manifest.indexes.site_topology;
    if (!reference) return null;
    const topology = (await this.fetch(reference, signal)).value;
    this.assertResourceSnapshot(topology, "Site-topology index");
    if (!topology || topology.schema !== "govuk-site-topology.v1") {
      throw new Error("Unsupported site-topology contract");
    }
    this.siteTopology = topology;
    return topology;
  }

  async loadRelationships(route, signal) {
    const reference = descriptorEntrypoint(this.descriptor, "relationship_adjacency") || this.manifest.indexes.relationship_adjacency;
    if (!reference) return [];
    if (!this.adjacencyManifest) {
      this.adjacencyManifest = (await this.fetch(reference, signal)).value;
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
      () => this.fetch(bucketReference, signal).then((result) => result.value)
    );
    return Array.isArray(payload[route]) ? payload[route] : [];
  }

  async loadRecord(route, signal) {
    const indexReference = descriptorEntrypoint(this.descriptor, "route_index") || this.manifest.indexes.route_index || this.manifest.indexes.routes;
    if (!indexReference) return null;
    if (!this.routeIndex) {
      this.routeIndex = (await this.fetch(indexReference, signal)).value;
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
      () => this.fetch(bucketReference, signal).then((result) => result.value)
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
      () => this.fetch(chunkReference, signal).then((result) => result.value)
    );
    const raw = Array.isArray(chunk) ? chunk[ordinal % chunkSize] : null;
    if (raw && raw.open !== route) throw new Error("Route-index target mismatch");
    return raw ? normaliseRecord(raw, route) : null;
  }
}
