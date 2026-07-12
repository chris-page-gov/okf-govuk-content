import { tokenize } from "./search-core.js";

export const VIEW_IDS = Object.freeze(["results", "browse", "relationships", "timeline", "compare"]);
export const MODE_IDS = Object.freeze(["simple", "explore", "evidence"]);
export const MAX_GRAPH_NODES = 250;
export const MAX_GRAPH_EDGES = 500;
export const MAX_QUERY_LENGTH = 240;

const VIEW_SET = new Set(VIEW_IDS);
const MODE_SET = new Set(MODE_IDS);
const STATE_PARAMS = ["q", "facet", "view", "mode", "lang", "route", "lifecycle", "jurisdiction", "page", "snapshot", "pin"];
const SAFE_FACET_KEY = /^[a-z][a-z0-9_.-]{0,63}$/i;
const SAFE_LANGUAGE = /^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$/i;
const NON_ROUTE_FRAGMENTS = new Set(["", "overview", "main-content"]);

export const DEFAULT_STATE = Object.freeze({
  query: "",
  facets: Object.freeze({}),
  view: "results",
  mode: "simple",
  language: "en",
  route: "",
  lifecycle: "all",
  jurisdiction: "all",
  page: 1,
  snapshot: "",
  pins: Object.freeze([])
});

function cleanText(value, maximum = 512) {
  return String(value || "").replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim().slice(0, maximum);
}

function uniqueStrings(values, maximum = 40) {
  return [...new Set((values || []).map((value) => cleanText(value, 256)).filter(Boolean))].slice(0, maximum);
}

function toArray(value) {
  if (Array.isArray(value)) return value;
  if (value === undefined || value === null || value === "") return [];
  return [value];
}

export function normaliseLanguage(value) {
  const language = cleanText(value || "en", 35).replace(/_/g, "-");
  if (!SAFE_LANGUAGE.test(language)) return "en";
  const parts = language.split("-");
  return parts
    .map((part, index) => {
      if (index === 0) return part.toLowerCase();
      if (part.length === 2) return part.toUpperCase();
      if (part.length === 4) return part[0].toUpperCase() + part.slice(1).toLowerCase();
      return part.toLowerCase();
    })
    .join("-");
}

function safeRoute(value) {
  const route = cleanText(value, 768);
  if (!route || route.startsWith("//") || /[<>]/.test(route)) return "";
  return route;
}

export function routeFromHash(value) {
  const raw = String(value || "").replace(/^#/, "");
  let decoded = raw;
  try {
    decoded = decodeURIComponent(raw);
  } catch {
    // A malformed shared fragment must fail safe instead of breaking startup.
  }
  const route = safeRoute(decoded);
  return NON_ROUTE_FRAGMENTS.has(route.toLowerCase()) ? "" : route;
}

function parseFacet(value) {
  const token = cleanText(value, 330);
  const separator = token.indexOf(":");
  if (separator <= 0) return null;
  const key = token.slice(0, separator);
  const facetValue = token.slice(separator + 1);
  if (!SAFE_FACET_KEY.test(key) || !facetValue) return null;
  return { key, value: facetValue.slice(0, 256) };
}

export function parseExplorerState(input) {
  const url = input instanceof URL
    ? input
    : input instanceof URLSearchParams
      ? null
      : new URL(String(input), "https://explorer.invalid/");
  const params = input instanceof URLSearchParams ? input : url.searchParams;
  const facets = {};
  for (const value of params.getAll("facet")) {
    const facet = parseFacet(value);
    if (!facet) continue;
    facets[facet.key] = uniqueStrings([...(facets[facet.key] || []), facet.value]);
  }
  const parsedPage = Number.parseInt(params.get("page") || "1", 10);
  const view = params.get("view") || DEFAULT_STATE.view;
  const mode = params.get("mode") || DEFAULT_STATE.mode;
  return {
    query: cleanText(params.get("q"), MAX_QUERY_LENGTH),
    facets,
    view: VIEW_SET.has(view) ? view : DEFAULT_STATE.view,
    mode: MODE_SET.has(mode) ? mode : DEFAULT_STATE.mode,
    language: normaliseLanguage(params.get("lang") || DEFAULT_STATE.language),
    route: routeFromHash(url && url.hash) || safeRoute(params.get("route")),
    lifecycle: cleanText(params.get("lifecycle") || "all", 80) || "all",
    jurisdiction: cleanText(params.get("jurisdiction") || "all", 80) || "all",
    page: Number.isFinite(parsedPage) ? Math.min(100000, Math.max(1, parsedPage)) : 1,
    snapshot: cleanText(params.get("snapshot"), 160),
    pins: uniqueStrings(params.getAll("pin").map(safeRoute).filter(Boolean), 12)
  };
}

export function serialiseExplorerState(state, baseUrl) {
  const url = new URL(baseUrl, "https://explorer.invalid/");
  for (const parameter of STATE_PARAMS) url.searchParams.delete(parameter);
  const normalized = { ...DEFAULT_STATE, ...state };
  const query = cleanText(normalized.query, MAX_QUERY_LENGTH);
  if (query) url.searchParams.set("q", query);
  for (const key of Object.keys(normalized.facets || {}).sort()) {
    if (!SAFE_FACET_KEY.test(key)) continue;
    for (const value of uniqueStrings(normalized.facets[key]).sort()) {
      url.searchParams.append("facet", key + ":" + value);
    }
  }
  if (VIEW_SET.has(normalized.view) && normalized.view !== DEFAULT_STATE.view) url.searchParams.set("view", normalized.view);
  if (MODE_SET.has(normalized.mode) && normalized.mode !== DEFAULT_STATE.mode) url.searchParams.set("mode", normalized.mode);
  const language = normaliseLanguage(normalized.language);
  if (language !== DEFAULT_STATE.language) url.searchParams.set("lang", language);
  const route = safeRoute(normalized.route);
  url.hash = route || "";
  if (normalized.lifecycle && normalized.lifecycle !== "all") url.searchParams.set("lifecycle", cleanText(normalized.lifecycle, 80));
  if (normalized.jurisdiction && normalized.jurisdiction !== "all") url.searchParams.set("jurisdiction", cleanText(normalized.jurisdiction, 80));
  if (Number(normalized.page) > 1) url.searchParams.set("page", String(Math.floor(Number(normalized.page))));
  if (normalized.snapshot) url.searchParams.set("snapshot", cleanText(normalized.snapshot, 160));
  for (const pin of uniqueStrings(normalized.pins, 12)) {
    const safePin = safeRoute(pin);
    if (safePin) url.searchParams.append("pin", safePin);
  }
  return url;
}

export function safeExternalUrl(value, baseUrl) {
  try {
    const url = new URL(String(value || ""), baseUrl || "https://explorer.invalid/");
    return url.protocol === "https:" ? url.toString() : "";
  } catch {
    return "";
  }
}

export function isAllowedBundleUrl(value, currentOrigin) {
  try {
    const url = new URL(String(value || ""), currentOrigin || "https://explorer.invalid/");
    if (url.protocol === "https:") return true;
    return url.protocol === "http:" && Boolean(currentOrigin) && url.origin === new URL(currentOrigin).origin;
  } catch {
    return false;
  }
}

export function normaliseRecord(raw, fallbackRoute = "") {
  const source = raw && typeof raw === "object" ? raw : {};
  const name = cleanText(source.name || source.content_id || source.id || "record", 512);
  const route = safeRoute(source.open || source.route || fallbackRoute || "dataset/" + name);
  const language = normaliseLanguage(source.locale || source.language || "en");
  const inferred = source.inferred === true || source.assertion_status === "inferred" || source.source_status === "inferred";
  return {
    route,
    id: cleanText(source.id || source.content_id || route, 768),
    name,
    title: cleanText(source.title || source.label || name || route, 500),
    summary: cleanText(source.summary || source.description || source.notes || "", 1600),
    type: cleanText(source.record_type || source.content_type || source.document_type || source.type || "Content item", 160),
    publisher: cleanText(source.publisher_title || source.publisher || source.organisation || "Not recorded", 300),
    owner: cleanText(source.owner_title || source.owner || "", 300),
    status: cleanText(source.lifecycle_status || source.status || source.state || "not recorded", 100),
    firstPublishedAt: cleanText(source.first_published_at || source.publication_date || source.created_at || "", 80),
    updatedAt: cleanText(source.public_updated_at || source.updated_at || source.timestamp || "", 80),
    language,
    jurisdictions: uniqueStrings(toArray(source.jurisdiction || source.jurisdictions), 20),
    topics: uniqueStrings(toArray(source.topics || source.taxons || source.tags), 40),
    breadcrumb: uniqueStrings(toArray(source.breadcrumb || source.breadcrumbs).map((value) => typeof value === "object" ? value.title || value.label : value), 20),
    canonicalUrl: safeExternalUrl(source.canonical_url || source.web_url || source.url || ""),
    sourceStatus: inferred ? "inferred" : cleanText(source.source_status || source.assertion_status || "source-native", 80),
    confidence: inferred ? cleanText(source.confidence || "not recorded", 80) : "",
    evidenceUrl: safeExternalUrl(source.evidence_url || source.source_url || ""),
    retrievedAt: cleanText(source.retrieved_at || source.observed_at || "", 80),
    lifecycleEvents: toArray(source.lifecycle_events).filter((event) => event && typeof event === "object").slice(0, 100),
    facets: source.facets && typeof source.facets === "object" ? source.facets : {},
    score: Number(source.score || 0),
    raw: source
  };
}

export function facetValuesForRecord(record, key) {
  const explicit = record.facets && Object.hasOwn(record.facets, key) ? record.facets[key] : undefined;
  if (explicit !== undefined) return uniqueStrings(toArray(explicit));
  const fields = {
    type: [record.type],
    content_type: [record.type],
    publisher: [record.publisher],
    owner: [record.owner],
    lifecycle: [record.status],
    status: [record.status],
    language: [record.language],
    locale: [record.language],
    jurisdiction: record.jurisdictions,
    topic: record.topics,
    taxon: record.topics,
    source_status: [record.sourceStatus]
  };
  return uniqueStrings(fields[key] || toArray(record.raw && record.raw[key]));
}

export function whyThisResult(record, query) {
  const tokens = tokenize(query);
  if (!tokens.length) return [];
  const fields = [
    ["title", record.title],
    ["summary", record.summary],
    ["publisher", record.publisher],
    ["type", record.type],
    ["topic", record.topics.join(" ")],
    ["route", record.route]
  ];
  const reasons = [];
  for (const token of tokens) {
    const matched = fields.find(([, value]) => tokenize(value).some((candidate) => candidate === token || candidate.startsWith(token)));
    if (matched) reasons.push(matched[0] + " matches " + token);
  }
  return uniqueStrings(reasons, 4);
}

export function filterRecords(records, state) {
  const queryTokens = tokenize(state.query || "");
  return records.filter((record) => {
    const searchable = tokenize([
      record.title,
      record.summary,
      record.publisher,
      record.owner,
      record.type,
      record.route,
      ...record.topics,
      ...record.breadcrumb
    ].join(" "));
    if (queryTokens.some((token) => !searchable.some((candidate) => candidate === token || candidate.startsWith(token)))) return false;
    for (const [key, selected] of Object.entries(state.facets || {})) {
      const values = facetValuesForRecord(record, key);
      if (selected.length && !selected.some((value) => values.includes(value))) return false;
    }
    if (state.lifecycle && state.lifecycle !== "all" && record.status !== state.lifecycle) return false;
    if (state.jurisdiction && state.jurisdiction !== "all" && !record.jurisdictions.includes(state.jurisdiction)) return false;
    return true;
  });
}

export function paginate(records, page = 1, pageSize = 20) {
  const safeSize = Math.min(100, Math.max(1, Math.floor(pageSize)));
  const pageCount = Math.max(1, Math.ceil(records.length / safeSize));
  const safePage = Math.min(pageCount, Math.max(1, Math.floor(page)));
  const start = (safePage - 1) * safeSize;
  return { items: records.slice(start, start + safeSize), page: safePage, pageCount, total: records.length };
}

function normaliseRelationship(raw) {
  const source = safeRoute(raw && raw.source);
  const target = safeRoute(raw && raw.target);
  if (!source || !target) return null;
  return {
    source,
    target,
    kind: cleanText(raw.kind || raw.label || raw.type || "related to", 160),
    evidenceType: cleanText(raw.evidence_type || "not recorded", 120),
    evidenceUrl: safeExternalUrl(raw.evidence_url || ""),
    confidence: cleanText(raw.confidence || "", 80),
    raw
  };
}

export function buildBoundedGraph(records, relationships, options = {}) {
  const maximumNodes = Math.min(MAX_GRAPH_NODES, Math.max(1, Number(options.maxNodes || MAX_GRAPH_NODES)));
  const maximumEdges = Math.min(MAX_GRAPH_EDGES, Math.max(1, Number(options.maxEdges || MAX_GRAPH_EDGES)));
  const centerRoute = safeRoute(options.centerRoute || "");
  const recordByRoute = new Map(records.map((record) => [record.route, record]));
  const normalized = relationships.map(normaliseRelationship).filter(Boolean);
  normalized.sort((left, right) => {
    const leftCenter = left.source === centerRoute || left.target === centerRoute ? 0 : 1;
    const rightCenter = right.source === centerRoute || right.target === centerRoute ? 0 : 1;
    return leftCenter - rightCenter || left.kind.localeCompare(right.kind) || left.source.localeCompare(right.source);
  });
  const nodeIds = new Set(centerRoute ? [centerRoute] : []);
  const edges = [];
  let omittedEdges = 0;
  for (const relationship of normalized) {
    if (edges.length >= maximumEdges) {
      omittedEdges += 1;
      continue;
    }
    const additions = [relationship.source, relationship.target].filter((route) => !nodeIds.has(route));
    if (nodeIds.size + additions.length > maximumNodes) {
      omittedEdges += 1;
      continue;
    }
    additions.forEach((route) => nodeIds.add(route));
    edges.push(relationship);
  }
  const nodes = [...nodeIds].map((route) => {
    const record = recordByRoute.get(route);
    return { route, label: record ? record.title : route, type: record ? record.type : route.split("/")[0] || "record" };
  });
  const counts = new Map();
  for (const edge of normalized) counts.set(edge.kind, (counts.get(edge.kind) || 0) + 1);
  const summary = [...counts.entries()].map(([kind, count]) => ({ kind, count })).sort((left, right) => right.count - left.count || left.kind.localeCompare(right.kind));
  return {
    nodes,
    edges,
    list: edges.map((edge) => ({ ...edge })),
    summary,
    omittedNodes: Math.max(0, new Set(normalized.flatMap((edge) => [edge.source, edge.target])).size - nodes.length),
    omittedEdges
  };
}

export function relationshipBucket(route) {
  let hash = 0x811c9dc5;
  for (const byte of new TextEncoder().encode(String(route || ""))) {
    hash ^= byte;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return ((hash >>> 24) & 0xff).toString(16).padStart(2, "0");
}

const EVENT_FIELDS = Object.freeze({
  query: ["queryLength", "resultCount", "elapsedMs", "view", "mode", "locale", "snapshot"],
  result: ["position", "routeKind", "view", "mode", "snapshot"],
  facet: ["facetKey", "selectedCount", "view", "mode", "snapshot"],
  route: ["routeKind", "view", "mode", "snapshot"],
  completion: ["taskType", "success", "elapsedMs", "mode", "snapshot"],
  error: ["errorCode", "phase", "mode", "snapshot"]
});

export function createInstrumentationEvent(kind, detail = {}, now = new Date()) {
  if (!Object.hasOwn(EVENT_FIELDS, kind)) return null;
  const event = { schema: "govuk-okf-explorer-event.v1", kind, recorded_at: now.toISOString() };
  for (const field of EVENT_FIELDS[kind]) {
    const value = detail[field];
    if (value === undefined || value === null) continue;
    if (typeof value === "number") event[field] = Number.isFinite(value) ? value : 0;
    else if (typeof value === "boolean") event[field] = value;
    else event[field] = cleanText(value, 120);
  }
  return event;
}

export function buildCitation(record, snapshot) {
  const destination = record.canonicalUrl || record.route;
  return record.title + ". " + destination + ". What’s on GOV.UK snapshot " + (cleanText(snapshot, 160) || "not recorded") + ".";
}

function exportRecord(record) {
  return {
    id: record.id,
    route: record.route,
    title: record.title,
    type: record.type,
    canonical_url: record.canonicalUrl,
    lifecycle_status: record.status,
    language: record.language,
    jurisdiction: record.jurisdictions,
    source_status: record.sourceStatus
  };
}

function yamlScalar(value) {
  return JSON.stringify(value === undefined || value === null ? "" : String(value));
}

export function buildContextExport(format, records, state, descriptor = {}) {
  const selected = records.map(exportRecord);
  if (format === "markdown") {
    const lines = ["# What’s on GOV.UK selection", "", "Snapshot: " + (state.snapshot || descriptor.snapshot_id || "not recorded"), "", "Query: " + (state.query || "none"), ""];
    for (const record of selected) {
      lines.push("## " + record.title, "", "- Route: " + record.route, "- Type: " + record.type, "- Status: " + record.lifecycle_status, "- GOV.UK: " + (record.canonical_url || "not recorded"), "- Evidence state: " + record.source_status, "");
    }
    return { mediaType: "text/markdown", extension: "md", text: lines.join("\n") + "\n" };
  }
  const document = {
    "@context": descriptor["@context"] || "https://chris-page-gov.github.io/okf-govuk-content/context/okf-bundle-v1.jsonld",
    "@type": "govuk:ExplorerSelection",
    snapshot: state.snapshot || descriptor.snapshot_id || "",
    query: state.query || "",
    filters: state.facets || {},
    records: selected
  };
  if (format === "yamlld") {
    const lines = ["\"@context\": " + yamlScalar(document["@context"]), "\"@type\": " + yamlScalar(document["@type"]), "snapshot: " + yamlScalar(document.snapshot), "query: " + yamlScalar(document.query), "records:"];
    for (const record of selected) {
      lines.push("  - id: " + yamlScalar(record.id), "    route: " + yamlScalar(record.route), "    title: " + yamlScalar(record.title), "    type: " + yamlScalar(record.type), "    canonical_url: " + yamlScalar(record.canonical_url), "    lifecycle_status: " + yamlScalar(record.lifecycle_status), "    source_status: " + yamlScalar(record.source_status));
    }
    return { mediaType: "application/yaml", extension: "yamlld", text: lines.join("\n") + "\n" };
  }
  return { mediaType: "application/ld+json", extension: "jsonld", text: JSON.stringify(document, null, 2) + "\n" };
}
