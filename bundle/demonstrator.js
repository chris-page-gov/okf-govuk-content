const DEMONSTRATOR_SCHEMA = "govuk-new-child-demonstrator.v1";
const MAX_DEMONSTRATOR_RECORDS = 250;
const MAX_BOUNDARY_REFERENCES = 1000;
const AI_HANDOFF_KEYS = Object.freeze({
  documentation: "documentation",
  context_pack: "contextPack",
  context_json: "contextJson",
  mcp_manifest: "mcpManifest"
});

function cleanText(value, maximum = 1024) {
  return String(value || "")
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, maximum);
}

function cleanRoute(value) {
  const route = cleanText(value, 768);
  if (!route || route.startsWith("//") || /[<>]/.test(route)) return "";
  return route;
}

function cleanUrl(value) {
  try {
    const url = new URL(String(value || ""));
    return url.protocol === "https:" ? url.toString() : "";
  } catch {
    return "";
  }
}

function boundedNumber(value, maximum = 1_000_000) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return 0;
  return Math.min(maximum, Math.floor(number));
}

function uniqueStrings(values, maximum = MAX_DEMONSTRATOR_RECORDS) {
  const output = [];
  const seen = new Set();
  for (const value of Array.isArray(values) ? values : []) {
    const item = cleanText(value, 768);
    if (!item || seen.has(item)) continue;
    seen.add(item);
    output.push(item);
    if (output.length >= maximum) break;
  }
  return output;
}

function normaliseGroup(group, ordinal) {
  const value = group && typeof group === "object" ? group : {};
  const id = cleanText(value.id || `stage-${ordinal + 1}`, 100);
  return {
    id,
    title: cleanText(value.title || id.replaceAll("-", " "), 240),
    description: cleanText(value.description, 1600),
    recordRoutes: uniqueStrings(value.record_routes?.map(cleanRoute).filter(Boolean)),
    exampleQuestions: uniqueStrings(value.example_questions, 12)
  };
}

function normaliseBoundary(boundary, ordinal) {
  const value = boundary && typeof boundary === "object" ? boundary : {};
  return {
    id: `boundary-${ordinal + 1}`,
    sourceRoute: cleanRoute(value.source_route),
    targetUrl: cleanUrl(value.target_url),
    title: cleanText(value.title || value.target_url || "Boundary destination", 320),
    predicate: cleanText(value.predicate || "links to", 160),
    boundaryClass: cleanText(value.class || value.boundary_class || "external destination", 160),
    evidenceUrl: cleanUrl(value.evidence_url),
    evidenceLocator: cleanText(value.evidence_locator, 512)
  };
}

function normaliseQueryObservation(observation) {
  const value = observation && typeof observation === "object" ? observation : {};
  const request = value.request && typeof value.request === "object" ? value.request : {};
  const envelope = value.envelope && typeof value.envelope === "object" ? value.envelope : {};
  return {
    phase: cleanText(value.phase, 40),
    requestedUrl: cleanUrl(value.requested_url),
    observedTotal: boundedNumber(value.observed_total),
    observedResultCount: boundedNumber(value.observed_result_count),
    retrievedAt: cleanText(value.retrieved_at, 80),
    status: boundedNumber(value.status, 999),
    localSequence: boundedNumber(request.local_sequence),
    programmeSequence: boundedNumber(request.programme_sequence),
    transferSha256: cleanText(request.transfer_sha256, 64).toLowerCase(),
    envelopePath: cleanText(envelope.repository_path, 768),
    envelopeSha256: cleanText(envelope.sha256, 64).toLowerCase()
  };
}

function normaliseAiHandoff(ai, rawIntegrity) {
  const handoff = {};
  const integrity = {};
  const sourceIntegrity = rawIntegrity && typeof rawIntegrity === "object" && !Array.isArray(rawIntegrity)
    ? rawIntegrity
    : {};
  for (const [sourceKey, normalizedKey] of Object.entries(AI_HANDOFF_KEYS)) {
    const path = cleanText(ai[sourceKey], 512);
    handoff[normalizedKey] = path;
    const rawEntry = sourceIntegrity[sourceKey];
    if (!path && rawEntry === undefined) continue;
    if (!rawEntry || typeof rawEntry !== "object" || Array.isArray(rawEntry)) {
      throw new Error(`AI handoff ${sourceKey} is missing integrity metadata`);
    }
    const integrityPath = cleanText(rawEntry.path, 512);
    const sha256 = cleanText(rawEntry.sha256, 64).toLowerCase();
    const bytes = rawEntry.bytes;
    if (!integrityPath) throw new Error(`AI handoff ${sourceKey} integrity path is missing`);
    if (path !== integrityPath) throw new Error(`AI handoff ${sourceKey} path does not match its integrity path`);
    if (!/^[0-9a-f]{64}$/.test(sha256)) throw new Error(`AI handoff ${sourceKey} SHA-256 is malformed`);
    if (!Number.isSafeInteger(bytes) || bytes < 0) throw new Error(`AI handoff ${sourceKey} byte length is malformed`);
    integrity[normalizedKey] = { path: integrityPath, sha256, bytes };
  }
  return { handoff, integrity };
}

export function normaliseDemonstrator(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  if (raw.schema !== DEMONSTRATOR_SCHEMA) return null;
  const groups = (Array.isArray(raw.journey_groups) ? raw.journey_groups : [])
    .slice(0, 24)
    .map(normaliseGroup);
  const coverage = raw.coverage && typeof raw.coverage === "object" ? raw.coverage : {};
  const ai = raw.ai_handoff && typeof raw.ai_handoff === "object" ? raw.ai_handoff : {};
  const aiContract = normaliseAiHandoff(ai, raw.ai_handoff_integrity);
  return {
    schema: DEMONSTRATOR_SCHEMA,
    snapshot: cleanText(raw.snapshot, 160),
    generatedAt: cleanText(raw.generated_at, 80),
    title: cleanText(raw.title || "New child GOV.UK journey demonstrator", 320),
    status: cleanText(raw.status || "bounded_demonstrator", 120),
    authoritative: raw.authoritative === true,
    scopeStatement: cleanText(raw.scope_statement, 2000),
    seedCount: boundedNumber(raw.seed_count),
    publicationRecordCount: boundedNumber(raw.publication_record_count),
    retainedRecordCeiling: boundedNumber(raw.retained_record_ceiling),
    officialRequestCeiling: boundedNumber(raw.official_request_ceiling),
    sourceQueries: (Array.isArray(raw.source_queries) ? raw.source_queries : []).slice(0, 20).map((query) => ({
      id: cleanText(query && query.id, 120),
      label: cleanText(query && query.label, 240),
      browsePath: cleanText(query && query.browse_path, 512),
      searchUrl: cleanUrl(query && query.search_url),
      reproducibilityUrl: cleanUrl(query && query.reproducibility_url),
      reportedTotal: boundedNumber(query && query.reported_total),
      derivedMembershipCount: boundedNumber(query && query.derived_membership_count),
      observations: (Array.isArray(query && query.observations) ? query.observations : [])
        .slice(0, 8)
        .map(normaliseQueryObservation)
    })),
    coverage: {
      seedExpected: boundedNumber(coverage.seed_expected),
      seedRepresented: boundedNumber(coverage.seed_represented),
      unexplainedSeedOmissions: boundedNumber(coverage.unexplained_seed_omissions),
      boundaryReferenceCount: boundedNumber(coverage.boundary_reference_count),
      byBoundaryClass: coverage.by_boundary_class && typeof coverage.by_boundary_class === "object"
        ? Object.fromEntries(Object.entries(coverage.by_boundary_class).slice(0, 40).map(([key, value]) => [cleanText(key, 160), boundedNumber(value)]).filter(([key]) => key))
        : {}
    },
    journeyGroups: groups,
    featuredRoutes: uniqueStrings((raw.featured_routes || []).map(cleanRoute).filter(Boolean), 40),
    boundaries: (Array.isArray(raw.boundaries) ? raw.boundaries : []).slice(0, MAX_BOUNDARY_REFERENCES).map(normaliseBoundary),
    aiHandoff: aiContract.handoff,
    aiHandoffIntegrity: aiContract.integrity,
    raw
  };
}

export function demonstratorRecordRoutes(demonstrator) {
  if (!demonstrator) return [];
  return uniqueStrings([
    ...demonstrator.featuredRoutes,
    ...demonstrator.journeyGroups.flatMap((group) => group.recordRoutes)
  ]);
}

export function groupRecords(group, records) {
  const byRoute = new Map((records || []).map((record) => [record.route, record]));
  return (group && group.recordRoutes || []).map((route) => byRoute.get(route)).filter(Boolean);
}

export function publisherBreakdown(records) {
  const counts = new Map();
  for (const record of records || []) {
    const publisher = cleanText(record.publisher || "Not recorded", 300) || "Not recorded";
    if (publisher === "Not recorded" || publisher === "Publisher not available from admitted source") continue;
    counts.set(publisher, (counts.get(publisher) || 0) + 1);
  }
  return [...counts.entries()]
    .map(([publisher, count]) => ({ publisher, count }))
    .sort((left, right) => right.count - left.count || left.publisher.localeCompare(right.publisher));
}

export function demonstratorSummary(demonstrator, records = []) {
  if (!demonstrator) return null;
  const represented = demonstrator.coverage.seedRepresented || demonstrator.publicationRecordCount || records.length;
  const expected = demonstrator.coverage.seedExpected || demonstrator.seedCount;
  const publishers = publisherBreakdown(records);
  const recordsWithPublisher = publishers.reduce((total, row) => total + row.count, 0);
  return {
    expected,
    represented,
    unexplainedOmissions: demonstrator.coverage.unexplainedSeedOmissions,
    loadedRecords: records.length,
    organisations: publishers.length,
    recordsWithoutPublisher: Math.max(0, records.length - recordsWithPublisher),
    boundaryReferences: demonstrator.coverage.boundaryReferenceCount || demonstrator.boundaries.length,
    groups: demonstrator.journeyGroups.length
  };
}

export function resolveBundleDocument(reference, baseUrl) {
  const path = cleanText(reference, 512);
  if (!path || path.startsWith("//") || /^[a-z][a-z0-9+.-]*:/i.test(path) || path.includes("\\")) return "";
  try {
    const base = new URL(baseUrl);
    const directory = new URL(".", base);
    const url = new URL(path, directory);
    return url.origin === directory.origin && url.pathname.startsWith(directory.pathname) ? url.toString() : "";
  } catch {
    return "";
  }
}

export { DEMONSTRATOR_SCHEMA, MAX_BOUNDARY_REFERENCES, MAX_DEMONSTRATOR_RECORDS };
