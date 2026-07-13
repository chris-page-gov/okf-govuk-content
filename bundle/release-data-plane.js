const INDEX_SCHEMA = "govuk-okf-github-release-pack-index.v1";
const PACK_ALGORITHM = "concatenated-byte-ranges-v1";
const RELEASE_ASSET_MAX_BYTES = 2 * 1024 * 1024 * 1024;
const PACK_MAX_BYTES = 64 * 1024 * 1024;
const MAX_PACKS = 900;
const SHA256 = /^[0-9a-f]{64}$/;
const SAFE_SEGMENT = /^[A-Za-z0-9_.-]+$/;

function canonicalJson(value) {
  if (Array.isArray(value)) return "[" + value.map((item) => canonicalJson(item)).join(",") + "]";
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort().map((key) => JSON.stringify(key) + ":" + canonicalJson(value[key])).join(",") + "}";
  }
  return JSON.stringify(value);
}

async function sha256Text(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function resourcePath(reference) {
  if (typeof reference === "string") return reference;
  if (!reference || typeof reference !== "object") return "";
  return String(reference.path || reference.url || reference.href || "");
}

function resourceHash(reference) {
  if (!reference || typeof reference !== "object") return "";
  return String(reference.sha256 || "").toLowerCase();
}

function safeVirtualPath(path) {
  if (typeof path !== "string" || !path || path.startsWith("/") || path.includes("\\") || path.split("/").includes("..")) {
    throw new Error("Release data-plane entry path is unsafe");
  }
  return path;
}

function expectedAssetUrl(repository, tag, assetName) {
  return `https://github.com/${repository}/releases/download/${encodeURIComponent(tag)}/${encodeURIComponent(assetName)}`;
}

export async function prepareReleaseDataPlane(document, baseUrl, expectedSnapshot = "") {
  if (!document || typeof document !== "object" || Array.isArray(document)) {
    throw new Error("Release data-plane index is not an object");
  }
  if (document.schema !== INDEX_SCHEMA || document.algorithm !== PACK_ALGORITHM) {
    throw new Error("Release data-plane index schema or algorithm is unsupported");
  }
  const repository = String(document.repository || "");
  const tag = String(document.tag || "");
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository) || !SAFE_SEGMENT.test(tag)) {
    throw new Error("Release data-plane repository or tag is malformed");
  }
  if (expectedSnapshot && String(document.snapshot || "") !== expectedSnapshot) {
    throw new Error("Release data-plane snapshot differs from the descriptor");
  }
  if (!Array.isArray(document.packs) || !Array.isArray(document.entries)) {
    throw new Error("Release data-plane packs or entries are malformed");
  }
  const maxPackBytes = Number(document.max_pack_bytes);
  if (!Number.isSafeInteger(maxPackBytes) || maxPackBytes < 1 || maxPackBytes > PACK_MAX_BYTES) {
    throw new Error("Release data-plane pack ceiling is invalid or exceeds 64 MiB");
  }
  if (document.packs.length > MAX_PACKS) throw new Error("Release data plane leaves fewer than 100 Release asset slots");

  const packs = new Map();
  const pagesBase = new URL(baseUrl);
  for (const row of document.packs) {
    if (!row || typeof row !== "object") throw new Error("Release data-plane pack row is malformed");
    const id = String(row.id || "");
    const assetName = String(row.asset_name || "");
    const bytes = Number(row.bytes);
    const hash = String(row.sha256 || "").toLowerCase();
    const path = safeVirtualPath(row.path);
    const releaseUrl = String(row.release_url || "");
    if (!/^pack-[0-9]{5}$/.test(id) || packs.has(id) || !SAFE_SEGMENT.test(assetName) || !assetName.endsWith(".pack.gz")) {
      throw new Error("Release data-plane pack identity is malformed or duplicated");
    }
    if (!Number.isSafeInteger(bytes) || bytes < 1 || bytes > maxPackBytes || bytes >= RELEASE_ASSET_MAX_BYTES || !SHA256.test(hash)) {
      throw new Error("Release data-plane pack size or hash is malformed");
    }
    if (path !== `data-packs/${assetName}` || releaseUrl !== expectedAssetUrl(repository, tag, assetName)) {
      throw new Error("Release data-plane pack paths are not bound to their repository, tag and asset");
    }
    const url = new URL(path, pagesBase).toString();
    if (new URL(url).origin !== pagesBase.origin) throw new Error("Browser pack delivery must stay on the Pages origin");
    packs.set(id, { ...row, id, asset_name: assetName, path, bytes, sha256: hash, release_url: releaseUrl, url });
  }

  const entries = new Map();
  const cursors = new Map([...packs].map(([id]) => [id, 0]));
  for (const row of document.entries) {
    if (!row || typeof row !== "object") throw new Error("Release data-plane entry is malformed");
    const path = safeVirtualPath(row.path);
    const pack = String(row.pack || "");
    const offset = Number(row.offset);
    const bytes = Number(row.bytes);
    const packedBytes = Number(row.packed_bytes);
    const hash = String(row.sha256 || "").toLowerCase();
    const packedHash = String(row.packed_sha256 || "").toLowerCase();
    const compression = String(row.compression || "identity");
    const transportCompression = String(row.transport_compression || "identity");
    if (entries.has(path) || !packs.has(pack)) throw new Error("Release data-plane entry is duplicated or names an unknown pack");
    if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(bytes) || !Number.isSafeInteger(packedBytes) || bytes < 1 || bytes > PACK_MAX_BYTES || packedBytes < 1 || packedBytes > maxPackBytes || offset !== cursors.get(pack)) {
      throw new Error("Release data-plane entry range is malformed or non-contiguous");
    }
    if (!SHA256.test(hash) || !SHA256.test(packedHash) || !new Set(["identity", "gzip"]).has(compression) || !new Set(["identity", "gzip"]).has(transportCompression)) {
      throw new Error("Release data-plane entry hash or compression is malformed");
    }
    if (transportCompression === "identity" && compression !== "gzip") {
      throw new Error("Only an original gzip shard may use identity transport");
    }
    if (offset + packedBytes > packs.get(pack).bytes) throw new Error("Release data-plane entry exceeds its pack");
    cursors.set(pack, offset + packedBytes);
    entries.set(path, {
      ...row,
      path,
      pack,
      offset,
      bytes,
      packed_bytes: packedBytes,
      sha256: hash,
      packed_sha256: packedHash,
      compression,
      transport_compression: transportCompression
    });
  }
  for (const [id, cursor] of cursors) {
    if (cursor !== packs.get(id).bytes) throw new Error("Release data-plane pack contains unindexed bytes");
  }
  const expectedCounts = {
    packs: document.packs.length,
    virtual_shards: document.entries.length,
    packed_bytes: document.packs.reduce((total, row) => total + Number(row.bytes), 0),
    source_bytes: document.entries.reduce((total, row) => total + Number(row.bytes), 0)
  };
  if (canonicalJson(document.counts) !== canonicalJson(expectedCounts)) throw new Error("Release data-plane counts differ");
  const rootMaterial = canonicalJson({ algorithm: PACK_ALGORITHM, packs: document.packs, entries: document.entries }) + "\n";
  if (!SHA256.test(String(document.index_root_sha256 || "")) || await sha256Text(rootMaterial) !== document.index_root_sha256) {
    throw new Error("Release data-plane index root differs");
  }
  return { document, packs, entries };
}

export function releaseDataRequest(reference, prepared) {
  if (!prepared) return null;
  const path = resourcePath(reference);
  const entry = prepared.entries.get(path);
  if (!entry) return null;
  const advertisedHash = resourceHash(reference);
  if (advertisedHash && (!SHA256.test(advertisedHash) || advertisedHash !== entry.sha256)) {
    throw new Error("Logical resource integrity differs from the release data-plane index");
  }
  const pack = prepared.packs.get(entry.pack);
  const end = entry.offset + entry.packed_bytes - 1;
  return {
    url: pack.url,
    headers: { Range: `bytes=${entry.offset}-${end}` },
    expectedHash: entry.sha256,
    expectedLength: entry.bytes,
    expectedPackedHash: entry.packed_sha256,
    expectedPackedLength: entry.packed_bytes,
    expectedContentRange: `bytes ${entry.offset}-${end}/${pack.bytes}`,
    logicalPath: path,
    compression: entry.compression,
    transportCompression: entry.transport_compression
  };
}

export function releaseDataPlaneDocument(prepared) {
  return prepared ? prepared.document : null;
}
