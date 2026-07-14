import {
  buildCitation,
  buildContextExport,
  buildBoundedGraph,
  createInstrumentationEvent,
  filterRecords,
  normaliseRecord,
  paginate,
  parseExplorerState,
  safeExternalUrl,
  serialiseExplorerState,
  whyThisResult
} from "./core.js";
import {
  descriptorCandidates,
  fetchJson,
  LargeCorpusStore,
  referencePath,
  SearchClient
} from "./data.js";

const TRANSLATIONS = {
  en: {
    derivedLabel: "Derived, non-authoritative service",
    derivedDescription: "Use this catalogue to find and understand records, then follow the link to GOV.UK for authoritative guidance and transactions.",
    pageEyebrow: "Public GOV.UK metadata discovery",
    pageHeading: "Find GOV.UK content and connections",
    pageLede: "Search titles and metadata, browse official structures, and inspect lifecycle and source evidence. This is a discovery tool, not an answer chatbot.",
    searchLabel: "What are you trying to find?",
    searchSubmit: "Search",
    searchHint: "Try a title, organisation, topic, service, attachment or natural-language description.",
    examples: "Examples:",
    scopeHeading: "What this catalogue covers",
    presentationHeading: "Presentation",
    filterHeading: "Filter this context",
    filterHint: "The same filters apply to results, browse, lifecycle and relationships.",
    clear: "Clear",
    modeHint: "Modes change detail and layout only. They do not change the record set.",
    retry: "Try again",
    close: "Close",
    tabs: {
      results: "Results",
      sitemap: "Sitemap & routing",
      browse: "Browse paths",
      relationships: "Relationships",
      timeline: "Lifecycle",
      compare: "Pinned / compare"
    },
    modes: { simple: "Simple", explore: "Explore", evidence: "Evidence / Developer" },
    noResults: "No supported result was found in this static snapshot.",
    noResultsHelp: "Try fewer words, another spelling, a broader facet, or follow GOV.UK search. The catalogue does not invent an answer.",
    loading: "Loading the catalogue…",
    searching: "Searching the static index…",
    results: "results",
    sourceNative: "Source-native",
    inferred: "Inferred",
    authoritative: "Open authoritative GOV.UK page",
    boundary: "Open external destination",
    pin: "Pin for comparison",
    unpin: "Remove pin",
    relationships: "Show relationships",
    why: "Why this result",
    summary: "Summary",
    lifecycle: "Lifecycle",
    provenance: "Provenance",
    raw: "Raw metadata",
    copyCitation: "Copy stable citation",
    copied: "Copied to the clipboard.",
    graphCaveat: "The graph is a bounded visual summary. The complete equivalent relationship table follows it.",
    lifecycleCaveat: "Dates and states reflect the selected snapshot. Follow GOV.UK to confirm current guidance.",
    noRoute: "Select a record first to inspect its bounded relationship neighbourhood.",
    compareEmpty: "Pin records from results or a detail panel to build a replayable comparison.",
    previous: "Previous",
    next: "Next",
    page: "Page",
    of: "of"
  },
  cy: {
    derivedLabel: "Gwasanaeth deilliadol, anawdurdodol",
    derivedDescription: "Defnyddiwch y catalog hwn i ddod o hyd i gofnodion a’u deall, yna dilynwch y ddolen i GOV.UK ar gyfer canllawiau a thrafodion awdurdodol.",
    pageEyebrow: "Darganfod metadata cyhoeddus GOV.UK",
    pageHeading: "Dod o hyd i gynnwys a chysylltiadau GOV.UK",
    pageLede: "Chwiliwch deitlau a metadata, porwch strwythurau swyddogol, ac archwiliwch gylch oes a thystiolaeth ffynhonnell. Offeryn darganfod yw hwn, nid chatbot atebion.",
    searchLabel: "Beth ydych chi’n ceisio dod o hyd iddo?",
    searchSubmit: "Chwilio",
    searchHint: "Rhowch gynnig ar deitl, sefydliad, pwnc, gwasanaeth, atodiad neu ddisgrifiad iaith naturiol.",
    examples: "Enghreifftiau:",
    scopeHeading: "Beth mae’r catalog hwn yn ei gwmpasu",
    presentationHeading: "Cyflwyniad",
    filterHeading: "Hidlo’r cyd-destun hwn",
    filterHint: "Mae’r un hidlyddion yn berthnasol i ganlyniadau, pori, cylch oes a chysylltiadau.",
    clear: "Clirio",
    modeHint: "Dim ond manylion a chynllun y mae moddau’n eu newid. Nid ydynt yn newid y set gofnodion.",
    retry: "Rhoi cynnig arall arni",
    close: "Cau",
    tabs: {
      results: "Canlyniadau",
      sitemap: "Map safle a llwybro",
      browse: "Llwybrau pori",
      relationships: "Cysylltiadau",
      timeline: "Cylch oes",
      compare: "Wedi pinio / cymharu"
    },
    modes: { simple: "Syml", explore: "Archwilio", evidence: "Tystiolaeth / Datblygwr" },
    noResults: "Ni chanfuwyd canlyniad â chymorth yn y ciplun statig hwn.",
    noResultsHelp: "Rhowch gynnig ar lai o eiriau, sillafiad arall neu hidlydd ehangach. Nid yw’r catalog yn dyfeisio ateb.",
    loading: "Wrthi’n llwytho’r catalog…",
    searching: "Wrthi’n chwilio’r mynegai statig…",
    results: "canlyniad",
    sourceNative: "O’r ffynhonnell",
    inferred: "Wedi’i gasglu drwy resymeg",
    authoritative: "Agor y dudalen GOV.UK awdurdodol",
    boundary: "Agor y gyrchfan allanol",
    pin: "Pinio i gymharu",
    unpin: "Tynnu’r pin",
    relationships: "Dangos cysylltiadau",
    why: "Pam y canlyniad hwn",
    summary: "Crynodeb",
    lifecycle: "Cylch oes",
    provenance: "Tarddiad",
    raw: "Metadata crai",
    copyCitation: "Copïo dyfyniad sefydlog",
    copied: "Wedi copïo i’r clipfwrdd.",
    graphCaveat: "Crynodeb gweledol cyfyngedig yw’r graff. Mae’r tabl cysylltiadau cyfatebol cyflawn yn dilyn.",
    lifecycleCaveat: "Mae dyddiadau a chyflyrau’n adlewyrchu’r ciplun a ddewiswyd. Dilynwch GOV.UK i gadarnhau’r canllawiau cyfredol.",
    noRoute: "Dewiswch gofnod yn gyntaf i archwilio ei gymdogaeth gysylltiadau gyfyngedig.",
    compareEmpty: "Piniwch gofnodion o’r canlyniadau neu banel manylion i greu cymhariaeth y gellir ei hailchwarae.",
    previous: "Blaenorol",
    next: "Nesaf",
    page: "Tudalen",
    of: "o"
  }
};

let state = parseExplorerState(window.location.href);
let corpusStore = null;
let searchClient = null;
let records = [];
let searchBacked = false;
let selectedRecord = null;
let selectedRelationships = [];
let relationshipsLoading = false;
let siteTopologyLoading = false;
let siteTopologyError = "";
let requestSequence = 0;
let suggestionTimer = null;
let instrumentationConsent = false;
const instrumentationEvents = [];
let searchMetricSequence = 0;
let routeMetricSequence = 0;

function recordBrowserMetric(name, value, sequenceName = "") {
  const root = document.documentElement;
  root.dataset[name] = String(Math.max(0, Math.round(Number(value) || 0)));
  if (sequenceName) {
    const next = sequenceName === "searchSequence" ? ++searchMetricSequence : ++routeMetricSequence;
    root.dataset[sequenceName] = String(next);
  }
}

function translation() {
  return state.language.toLowerCase().startsWith("cy") ? TRANSLATIONS.cy : TRANSLATIONS.en;
}

function createElement(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  if (options.className) element.className = options.className;
  if (options.text !== undefined) element.textContent = String(options.text);
  if (options.id) element.id = options.id;
  if (options.type) element.type = options.type;
  if (options.hidden) element.hidden = true;
  for (const [name, value] of Object.entries(options.attributes || {})) {
    if (value !== undefined && value !== null && value !== "") element.setAttribute(name, String(value));
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child instanceof Node) element.append(child);
    else if (child !== undefined && child !== null) element.append(document.createTextNode(String(child)));
  }
  return element;
}

function createButton(label, action, className = "") {
  const button = createElement("button", { type: "button", text: label, className });
  button.addEventListener("click", action);
  return button;
}

function createExternalLink(label, href) {
  const safe = safeExternalUrl(href);
  if (!safe) return createElement("span", { text: label + " (link unavailable)" });
  const link = createElement("a", { text: label, attributes: { href: safe, rel: "external noreferrer" } });
  return link;
}

function formatNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat(state.language).format(number) : "Not reported";
}

function formatDate(value) {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(state.language, { dateStyle: "medium", timeZone: "UTC" }).format(date);
}

function setText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value;
}

function applyChrome() {
  const text = translation();
  document.documentElement.lang = state.language.toLowerCase().startsWith("cy") ? "cy" : "en";
  document.body.dataset.mode = state.mode;
  document.title = text.pageHeading + " – What’s on GOV.UK";
  setText("derived-label", text.derivedLabel);
  setText("derived-description", text.derivedDescription);
  setText("page-eyebrow", text.pageEyebrow);
  setText("page-heading", text.pageHeading);
  setText("page-lede", text.pageLede);
  setText("search-label", text.searchLabel);
  setText("search-submit", text.searchSubmit);
  setText("search-hint", text.searchHint);
  setText("examples-label", text.examples);
  setText("scope-heading", text.scopeHeading);
  setText("presentation-heading", text.presentationHeading);
  setText("filter-heading", text.filterHeading);
  setText("filter-hint", text.filterHint);
  setText("clear-filters", text.clear);
  setText("mode-hint", text.modeHint);
  setText("retry-load", text.retry);
  setText("close-detail", text.close);
  document.querySelectorAll("[data-language]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.language === document.documentElement.lang)));
  document.querySelectorAll("button[data-mode]").forEach((button) => {
    button.textContent = text.modes[button.dataset.mode] || button.dataset.mode;
    button.setAttribute("aria-pressed", String(button.dataset.mode === state.mode));
  });
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.textContent = text.tabs[button.dataset.view] || button.dataset.view;
    if (button.dataset.view === state.view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
}

function recordInstrumentation(kind, detail) {
  if (!instrumentationConsent) return;
  const event = createInstrumentationEvent(kind, detail);
  if (!event) return;
  instrumentationEvents.push(event);
  if (instrumentationEvents.length > 200) instrumentationEvents.shift();
}

function syncState(nextState, push = true) {
  state = parseExplorerState(serialiseExplorerState({ ...state, ...nextState }, window.location.href));
  const url = serialiseExplorerState(state, window.location.href);
  if (push) window.history.pushState({}, "", url);
  else window.history.replaceState({}, "", url);
  applyChrome();
  renderAll();
}

function visibleRecords() {
  const filterState = searchBacked ? { ...state, query: "" } : state;
  return filterRecords(records, filterState);
}

function setStatus(message) {
  const status = document.getElementById("load-status");
  status.textContent = message;
}

function setBusy(isBusy) {
  document.getElementById("view-content").setAttribute("aria-busy", String(isBusy));
  document.getElementById("scope-panel").setAttribute("aria-busy", String(isBusy && !corpusStore));
}

function showFatal(error) {
  const panel = document.getElementById("fatal-error");
  panel.hidden = false;
  setText("fatal-error-message", error instanceof Error ? error.message : String(error));
  setStatus("");
  setBusy(false);
  document.documentElement.dataset.explorerReady = "false";
  document.documentElement.dataset.fatalError = "true";
}

async function loadCatalogue() {
  document.documentElement.dataset.bootstrapReady = "false";
  document.documentElement.dataset.explorerReady = "false";
  document.documentElement.dataset.fatalError = "false";
  document.getElementById("fatal-error").hidden = true;
  setStatus(translation().loading);
  setBusy(true);
  if (searchClient) searchClient.destroy();
  searchClient = null;
  corpusStore = null;
  records = [];
  selectedRecord = null;
  selectedRelationships = [];
  siteTopologyLoading = false;
  siteTopologyError = "";
  const query = new URLSearchParams(window.location.search);
  const explicitBundle = query.get("bundle") || "";
  const configured = document.documentElement.dataset.defaultBundle || "";
  const candidates = explicitBundle
    ? descriptorCandidates(document.baseURI, explicitBundle).slice(0, 1)
    : descriptorCandidates(document.baseURI, "", configured);
  let descriptorResult = null;
  let lastError = null;
  for (const candidate of candidates) {
    try {
      const result = await fetchJson(candidate, document.baseURI, { currentOrigin: window.location.origin });
      if (!result.value || result.value.kind !== "okf-large-corpus") throw new Error("The descriptor is not an OKF large corpus");
      descriptorResult = result;
      break;
    } catch (error) {
      lastError = error;
    }
  }
  if (!descriptorResult) throw lastError || new Error("No Explorer descriptor was found");
  corpusStore = new LargeCorpusStore(descriptorResult.url, descriptorResult.value, window.location.origin);
  await corpusStore.bootstrap();
  const snapshot = corpusStore.snapshotId();
  if (state.snapshot && snapshot && state.snapshot !== snapshot) {
    throw new Error("This link requests snapshot " + state.snapshot + ", but the descriptor advertises " + snapshot + ". The record has not been opened against a different snapshot.");
  }
  state = parseExplorerState(serialiseExplorerState({ ...state, snapshot: state.snapshot || snapshot }, window.location.href));
  window.history.replaceState({}, "", serialiseExplorerState(state, window.location.href));
  records = corpusStore.overviewRecords();
  searchBacked = false;
  const searchReference = corpusStore.searchManifestReference();
  if (searchReference && typeof Worker !== "undefined") {
    try {
      searchClient = new SearchClient();
      await searchClient.init(corpusStore.baseUrl, searchReference, snapshot, corpusStore.releaseDataPlaneDocument());
    } catch (error) {
      console.warn("Static search index unavailable", error);
      if (searchClient) searchClient.destroy();
      searchClient = null;
    }
  }
  setBusy(false);
  renderAll();
  document.documentElement.dataset.bootstrapReady = "true";
  recordBrowserMetric("firstUsefulRenderMs", performance.now());
  if (state.query) await runSearch(state.query, false);
  if (state.route) await openRoute(state.route, false);
  document.documentElement.dataset.explorerReady = "true";
}

async function runSearch(queryValue, push = true) {
  const query = String(queryValue || "").trim().slice(0, 240);
  document.getElementById("search-input").value = query;
  if (push) syncState({ query, page: 1 }, true);
  else state = { ...state, query, page: 1 };
  document.getElementById("search-suggestions").replaceChildren();
  const requestId = ++requestSequence;
  if (!query) {
    records = corpusStore ? corpusStore.overviewRecords() : [];
    searchBacked = false;
    renderAll();
    return;
  }
  setStatus(translation().searching);
  setBusy(true);
  const started = performance.now();
  try {
    if (searchClient) {
      const result = await searchClient.query(query);
      if (requestId !== requestSequence) return;
      records = result.map((record) => normaliseRecord(record));
      searchBacked = true;
    } else {
      searchBacked = false;
    }
    const elapsedMs = Math.round(performance.now() - started);
    recordInstrumentation("query", {
      queryLength: query.length,
      resultCount: visibleRecords().length,
      elapsedMs,
      view: state.view,
      mode: state.mode,
      locale: state.language,
      snapshot: state.snapshot
    });
  } catch (error) {
    if (!error || error.name !== "AbortError") setStatus("Search unavailable: " + (error instanceof Error ? error.message : String(error)));
  } finally {
    if (requestId === requestSequence) {
      setBusy(false);
      renderAll();
      recordBrowserMetric("lastSearchMs", performance.now() - started, "searchSequence");
    }
  }
}

function renderScope() {
  if (!corpusStore) return;
  const overview = corpusStore.overview || {};
  const counts = overview.counts || corpusStore.descriptor.counts || {};
  const coverage = overview.coverage || {};
  setText("scope-description", overview.description || overview.summary || corpusStore.descriptor.description || "Snapshot-bounded public metadata with visible coverage and source constraints.");
  const metrics = [
    ["Snapshot", state.snapshot || "Not reported"],
    ["Content records", formatNumber(counts.content_items ?? counts.records ?? counts.datasets)],
    ["Routes", formatNumber(counts.routes)],
    ["Attachments and representations", formatNumber(counts.resources ?? counts.attachments)],
    ["Relationships", formatNumber(counts.relationships)],
    ["Unexplained omissions", formatNumber(coverage.unexplained_omissions ?? counts.unexplained_omissions)]
  ];
  const metricList = document.getElementById("scope-metrics");
  metricList.replaceChildren(...metrics.map(([label, value]) => {
    const wrapper = createElement("div");
    wrapper.append(createElement("dt", { text: label }), createElement("dd", { text: value }));
    return wrapper;
  }));
  const notices = [
    ...(Array.isArray(overview.notices) ? overview.notices : []),
    ...(Array.isArray(overview.warnings) ? overview.warnings : []),
    ...(Array.isArray(coverage.notices) ? coverage.notices : [])
  ].slice(0, 12);
  if (!notices.length) notices.push("Coverage claims are limited to the declared snapshot and public-source boundary. GOV.UK remains authoritative.");
  const noticeList = document.getElementById("scope-notices");
  noticeList.replaceChildren(...notices.map((notice) => createElement("p", { text: notice })));
  document.getElementById("scope-panel").setAttribute("aria-busy", "false");
}

function facetDefinitions() {
  if (!corpusStore) return [];
  const analysis = corpusStore.analysis || {};
  const previews = corpusStore.overview.facet_previews || corpusStore.overview.facets || {};
  if (Array.isArray(analysis.facet_analysis) && analysis.facet_analysis.length) {
    return analysis.facet_analysis
      .filter((facet) => facet.recommendation !== "suppressed")
      .map((facet) => ({ ...facet, values: facet.values || previews[facet.key] || [] }));
  }
  return Object.entries(previews)
    .filter(([, values]) => Array.isArray(values))
    .map(([key, values]) => ({ key, label: key.replaceAll("_", " "), values }));
}

function renderFacets() {
  const list = document.getElementById("facet-list");
  if (!corpusStore) {
    list.replaceChildren();
    return;
  }
  const groups = facetDefinitions().slice(0, 20).map((facet) => {
    const selected = new Set(state.facets[facet.key] || []);
    const details = createElement("details", { className: "facet-group" });
    if (selected.size) details.open = true;
    const label = facet.label || facet.key.replaceAll("_", " ");
    const summary = createElement("summary", { text: label + (selected.size ? " (" + selected.size + " selected)" : "") });
    details.append(summary);
    const values = (facet.values || []).map((row) => typeof row === "object" ? row : { value: row, count: 0 }).slice(0, 200);
    const options = createElement("div", { className: "facet-options" });
    const renderOptions = (needle = "") => {
      const normalized = needle.trim().toLowerCase();
      const rows = values.filter((row) => !normalized || String(row.value).toLowerCase().includes(normalized)).slice(0, 60);
      options.replaceChildren(...rows.map((row) => {
        const value = String(row.value);
        const input = createElement("input", { type: "checkbox", attributes: { value, "aria-label": label + ": " + value } });
        input.checked = selected.has(value);
        input.addEventListener("change", () => toggleFacet(facet.key, value, input.checked));
        const text = createElement("span", {}, [value, createElement("span", { className: "facet-count", text: " (" + formatNumber(row.count) + ")" })]);
        return createElement("label", { className: "facet-option" }, [input, text]);
      }));
    };
    if (values.length > 8) {
      const search = createElement("input", { className: "facet-search", attributes: { type: "search", placeholder: "Find " + label.toLowerCase(), "aria-label": "Find a value in " + label } });
      search.addEventListener("input", () => renderOptions(search.value));
      details.append(search);
    }
    renderOptions();
    details.append(options);
    return details;
  });
  list.replaceChildren(...groups);
}

function toggleFacet(key, value, checked) {
  const facets = Object.fromEntries(Object.entries(state.facets).map(([facetKey, values]) => [facetKey, [...values]]));
  const selected = new Set(facets[key] || []);
  if (checked) selected.add(value);
  else selected.delete(value);
  if (selected.size) facets[key] = [...selected];
  else delete facets[key];
  recordInstrumentation("facet", { facetKey: key, selectedCount: selected.size, view: state.view, mode: state.mode, snapshot: state.snapshot });
  syncState({ facets, page: 1 }, true);
}

function renderActiveContext() {
  const container = document.getElementById("active-context");
  const chips = [];
  if (state.query) chips.push(createElement("span", { className: "context-chip", text: "Search: " + state.query }));
  for (const [key, values] of Object.entries(state.facets)) {
    for (const value of values) chips.push(createElement("span", { className: "context-chip", text: key.replaceAll("_", " ") + ": " + value }));
  }
  if (state.snapshot) chips.push(createElement("span", { className: "context-chip mode-evidence", text: "Snapshot: " + state.snapshot }));
  container.replaceChildren(...chips);
}

function sourceBadge(record) {
  const inferred = record.sourceStatus === "inferred";
  return createElement("span", { className: "badge " + (inferred ? "inferred" : "source-native"), text: inferred ? translation().inferred : translation().sourceNative });
}

function metadataList(record, compact = false) {
  const values = [
    ["Type", record.type],
    ["Publisher", record.publisher],
    ...(!compact && record.owner ? [["Owner", record.owner]] : []),
    ["Lifecycle", record.status],
    ["Updated", formatDate(record.updatedAt)],
    ["Language", record.language],
    ["Jurisdiction", record.jurisdictions.length ? record.jurisdictions.join(", ") : "Not recorded"]
  ];
  const list = createElement("dl", { className: compact ? "result-meta" : "detail-meta" });
  for (const [label, value] of values) list.append(createElement("dt", { text: label }), createElement("dd", { text: value || "Not recorded" }));
  return list;
}

function renderResultCard(record, position) {
  const heading = createElement("h3");
  heading.append(createButton(record.title, () => selectRecord(record, position)));
  const card = createElement("article", { className: "result-card" }, [sourceBadge(record), heading]);
  if (record.summary) card.append(createElement("p", { text: record.summary }));
  card.append(metadataList(record, true));
  if (record.breadcrumb.length) card.append(createElement("p", { className: "mode-explore", text: "Path: " + record.breadcrumb.join(" > ") }));
  const reasons = whyThisResult(record, state.query);
  if (reasons.length) card.append(createElement("p", { className: "why-result", text: translation().why + ": " + reasons.join("; ") }));
  if (record.canonicalUrl) {
    const host = new URL(record.canonicalUrl).hostname;
    card.append(createElement("p", {}, [createExternalLink(host === "www.gov.uk" ? translation().authoritative : translation().boundary, record.canonicalUrl)]));
  }
  const pinned = state.pins.includes(record.route);
  const actions = createElement("div", { className: "card-actions" }, [
    createButton(pinned ? translation().unpin : translation().pin, () => togglePin(record.route)),
    createButton(translation().relationships, async () => {
      await selectRecord(record, position);
      syncState({ view: "relationships" }, true);
    })
  ]);
  card.append(actions);
  return card;
}

function renderResults() {
  const content = document.getElementById("view-content");
  const page = paginate(visibleRecords(), state.page, 20);
  if (!page.total) {
    const empty = createElement("div", { className: "empty-state" }, [
      createElement("h3", { text: translation().noResults }),
      createElement("p", { text: translation().noResultsHelp }),
      createElement("p", {}, [createExternalLink("Search on GOV.UK", "https://www.gov.uk/search/all?keywords=" + encodeURIComponent(state.query))])
    ]);
    content.replaceChildren(empty);
    renderPagination(page);
    return;
  }
  const list = createElement("div", { className: "result-list" });
  page.items.forEach((record, index) => list.append(renderResultCard(record, (page.page - 1) * 20 + index + 1)));
  content.replaceChildren(list);
  renderPagination(page);
}

function renderPagination(page) {
  const navigation = document.getElementById("pagination");
  if (state.view !== "results" || page.pageCount <= 1) {
    navigation.replaceChildren();
    return;
  }
  const previous = createButton(translation().previous, () => syncState({ page: page.page - 1 }, true));
  previous.disabled = page.page <= 1;
  const label = createElement("span", { text: translation().page + " " + page.page + " " + translation().of + " " + page.pageCount });
  const next = createButton(translation().next, () => syncState({ page: page.page + 1 }, true));
  next.disabled = page.page >= page.pageCount;
  navigation.replaceChildren(previous, label, next);
}

function setSingleFacet(key, value) {
  const facets = { ...state.facets, [key]: [value] };
  syncState({ facets, page: 1, view: "results" }, true);
}

function browseCard(label, description, count, action) {
  const heading = createElement("h3");
  heading.append(createButton(label, action));
  return createElement("article", { className: "browse-card" }, [
    heading,
    description ? createElement("p", { text: description }) : null,
    Number.isFinite(Number(count)) ? createElement("p", { className: "facet-count", text: formatNumber(count) + " records" }) : null
  ].filter(Boolean));
}

function routeFacet(route, fallbackKey, fallbackValue) {
  const match = /^facet\/([^/]+)\/(.+)$/.exec(String(route || ""));
  if (match) return { key: match[1], value: match[2] };
  return { key: fallbackKey, value: fallbackValue };
}

function renderBrowse() {
  const content = document.getElementById("view-content");
  const analysis = corpusStore && corpusStore.analysis || {};
  const grid = createElement("div", { className: "browse-grid" });
  if (Array.isArray(analysis.hierarchies) && analysis.hierarchies.length) {
    for (const hierarchy of analysis.hierarchies) {
      const heading = createElement("h3", { text: hierarchy.label || hierarchy.id || "Browse path" });
      grid.append(heading);
      for (const value of (hierarchy.values || []).slice(0, 60)) {
        const facet = routeFacet(value.route, hierarchy.facet, value.id || value.value || value.label);
        grid.append(browseCard(
          value.label || value.id,
          (hierarchy.levels || []).join(" → "),
          value.count,
          () => setSingleFacet(facet.key, facet.value)
        ));
      }
    }
  } else {
    for (const facet of facetDefinitions().slice(0, 8)) {
      grid.append(createElement("h3", { text: facet.label || facet.key }));
      for (const row of (facet.values || []).slice(0, 12)) {
        const value = typeof row === "object" ? row.value : row;
        const count = typeof row === "object" ? row.count : undefined;
        grid.append(browseCard(String(value), "Browse by " + (facet.label || facet.key), count, () => setSingleFacet(facet.key, String(value))));
      }
    }
  }
  if (!grid.childNodes.length) grid.append(createElement("div", { className: "empty-state", text: "No browse hierarchy is advertised in this snapshot." }));
  content.replaceChildren(grid);
  document.getElementById("pagination").replaceChildren();
}

function labelForRoute(route) {
  if (selectedRecord && selectedRecord.route === route) return selectedRecord.title;
  const match = records.find((record) => record.route === route);
  return match ? match.title : route;
}

function routeButton(route) {
  return createButton(labelForRoute(route), () => openRoute(route, true), "route-button");
}

function topologyTable(headings, rows) {
  const table = createElement("table", { className: "topology-table" });
  const head = createElement("thead");
  head.append(createElement("tr", {}, headings.map((label) => createElement("th", { text: label, attributes: { scope: "col" } }))));
  const body = createElement("tbody");
  for (const cells of rows) {
    body.append(createElement("tr", {}, cells.map((cell, index) => createElement(
      "td",
      { attributes: { "data-label": headings[index] } },
      Array.isArray(cell) ? cell : [cell]
    ))));
  }
  table.append(head, body);
  return table;
}

function topologyCount(label, value) {
  return createElement("article", { className: "topology-count" }, [
    createElement("strong", { text: formatNumber(value) }),
    createElement("span", { text: label })
  ]);
}

async function searchHost(hostname) {
  syncState({ view: "results", query: hostname, page: 1 }, true);
  await runSearch(hostname, false);
}

async function ensureSiteTopology() {
  if (!corpusStore || corpusStore.siteTopology || siteTopologyLoading) return;
  siteTopologyLoading = true;
  siteTopologyError = "";
  if (state.view === "sitemap") setBusy(true);
  try {
    const topology = await corpusStore.loadSiteTopology();
    if (!topology) siteTopologyError = "This snapshot does not advertise a sitemap and routing projection.";
  } catch (error) {
    siteTopologyError = error instanceof Error ? error.message : String(error);
  } finally {
    siteTopologyLoading = false;
    if (state.view === "sitemap") renderAll();
  }
}

function renderSitemap() {
  const content = document.getElementById("view-content");
  document.getElementById("pagination").replaceChildren();
  const topology = corpusStore && corpusStore.siteTopology;
  if (!topology) {
    if (!siteTopologyError) ensureSiteTopology();
    content.replaceChildren(createElement("div", { className: siteTopologyError ? "empty-state" : "loading-state" }, [
      createElement("p", { text: siteTopologyError ? "Sitemap and routing data is unavailable: " + siteTopologyError : "Loading the sitemap and routing projection…" }),
      siteTopologyError ? createButton("Retry", () => {
        siteTopologyError = "";
        ensureSiteTopology();
      }) : null
    ].filter(Boolean)));
    return;
  }

  const counts = topology.counts || {};
  const notice = createElement("section", { className: "topology-notice" }, [
    createElement("h3", { text: "Snapshot-bounded topology, not a release completeness claim" }),
    createElement("p", { text: "This projection combines the admitted official source union. The GOV.UK XML sitemap is one Search-derived enumerator; independently operated boundary sites are recorded as destinations and relationships rather than mirrored as complete sites." }),
    createElement("p", { className: "hint", text: "Status: " + String(topology.status || "not recorded") + ". Page bodies are not retained or published." })
  ]);
  const metricGrid = createElement("div", { className: "topology-counts" }, [
    topologyCount("published route records", counts.published_records),
    topologyCount("observed hosts", counts.hosts),
    topologyCount("GOV.UK-domain boundary hosts", counts.gov_uk_domain_boundary_hosts),
    topologyCount("other external boundary hosts", counts.other_external_boundary_hosts),
    topologyCount("source-declared redirect rules", counts.redirect_rules),
    topologyCount("typed relationship assertions", counts.relationship_assertions)
  ]);
  const mechanisms = createElement("div", { className: "topology-mechanisms" });
  for (const mechanism of topology.routing_mechanisms || []) {
    mechanisms.append(createElement("article", { className: "summary-card" }, [
      createElement("h4", { text: mechanism.label || mechanism.id }),
      createElement("p", { text: formatNumber(mechanism.count) }),
      createElement("p", { className: "hint", text: "Full detail: " + String(mechanism.full_detail || "not recorded") })
    ]));
  }

  const hostRows = (topology.hosts || []).slice(0, 500).map((host) => [
    [createButton(host.hostname, () => searchHost(host.hostname), "route-button")],
    String(host.host_kind || "not recorded").replaceAll("_", " "),
    formatNumber(host.record_count),
    (host.routing_kinds || []).map((row) => row.value + " (" + formatNumber(row.count) + ")").join(", ") || "Not recorded"
  ]);
  const hostSection = createElement("section", {}, [
    createElement("h3", { text: "Observed sites and hosts" }),
    createElement("p", { className: "hint", text: hostRows.length < (topology.hosts || []).length ? "Showing the first 500 deterministically ordered hosts. The machine-readable projection contains all hosts." : "All hosts in this snapshot are shown." }),
    hostRows.length ? topologyTable(["Host", "Boundary class", "Records", "Routing kinds"], hostRows) : createElement("p", { text: "No host rows were advertised." })
  ]);

  const redirectRows = (topology.redirect_samples || []).map((redirect) => [
    [createButton(redirect.source_url || redirect.source_route, () => openRoute(redirect.source_route, true), "route-button")],
    redirect.path || "/",
    redirect.type + " / " + redirect.segments_mode,
    redirect.destination_url
      ? [createExternalLink(redirect.destination || redirect.destination_url, redirect.destination_url)]
      : redirect.destination || "Not recorded"
  ]);
  const redirectSection = createElement("section", {}, [
    createElement("h3", { text: "Source-declared redirects" }),
    createElement("p", { className: "hint", text: topology.redirect_samples_complete ? "All redirect rules in this snapshot are shown. Each record also retains its complete source-native redirect fields." : "Showing a bounded sample. Complete redirect detail remains on every record and in the route adjacency shards." }),
    redirectRows.length ? topologyTable(["Source", "Matched path", "Rule", "Destination"], redirectRows) : createElement("p", { text: "No source-declared redirect rules were present in this snapshot." })
  ]);

  const reference = corpusStore.descriptor.entrypoints.site_topology || corpusStore.manifest.indexes.site_topology;
  const machineLink = reference ? createElement("a", {
    text: "Open the machine-readable site topology",
    attributes: { href: new URL(referencePath(reference), corpusStore.baseUrl).toString() }
  }) : null;
  content.replaceChildren(notice, metricGrid, createElement("h3", { text: "Routing mechanisms" }), mechanisms, hostSection, redirectSection, machineLink || createElement("span"));
}

function relationshipTable(relationships) {
  const table = createElement("table", { className: "relationship-table" });
  const head = createElement("thead");
  head.append(createElement("tr", {}, [
    createElement("th", { text: "From", attributes: { scope: "col" } }),
    createElement("th", { text: "Relationship", attributes: { scope: "col" } }),
    createElement("th", { text: "To", attributes: { scope: "col" } }),
    createElement("th", { text: "Evidence", attributes: { scope: "col" } })
  ]));
  const body = createElement("tbody");
  for (const relationship of relationships) {
    const evidence = relationship.evidenceUrl
      ? createExternalLink(relationship.evidenceType || "Source", relationship.evidenceUrl)
      : createElement("span", { text: relationship.evidenceType || "Not recorded" });
    body.append(createElement("tr", {}, [
      createElement("td", { attributes: { "data-label": "From" } }, [routeButton(relationship.source)]),
      createElement("td", { text: relationship.kind || relationship.label || relationship.type || "related to", attributes: { "data-label": "Relationship" } }),
      createElement("td", { attributes: { "data-label": "To" } }, [routeButton(relationship.target)]),
      createElement("td", { attributes: { "data-label": "Evidence" } }, [evidence])
    ]));
  }
  table.append(head, body);
  return table;
}

function graphSvg(model) {
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  svg.setAttribute("viewBox", "0 0 800 480");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const nodes = model.nodes.slice(0, 40);
  const positions = new Map();
  nodes.forEach((node, index) => {
    const angle = -Math.PI / 2 + (index / Math.max(1, nodes.length)) * Math.PI * 2;
    const radius = nodes.length === 1 ? 0 : 175;
    positions.set(node.route, { x: 400 + Math.cos(angle) * radius, y: 240 + Math.sin(angle) * radius });
  });
  for (const edge of model.edges) {
    const source = positions.get(edge.source);
    const target = positions.get(edge.target);
    if (!source || !target) continue;
    const line = document.createElementNS(namespace, "line");
    line.setAttribute("x1", String(source.x));
    line.setAttribute("y1", String(source.y));
    line.setAttribute("x2", String(target.x));
    line.setAttribute("y2", String(target.y));
    line.setAttribute("stroke", "#505a5f");
    line.setAttribute("stroke-width", "2");
    svg.append(line);
  }
  for (const node of nodes) {
    const point = positions.get(node.route);
    const group = document.createElementNS(namespace, "g");
    const circle = document.createElementNS(namespace, "circle");
    circle.setAttribute("cx", String(point.x));
    circle.setAttribute("cy", String(point.y));
    circle.setAttribute("r", node.route === state.route ? "15" : "10");
    circle.setAttribute("fill", node.route === state.route ? "#d4351c" : "#1d70b8");
    const title = document.createElementNS(namespace, "title");
    title.textContent = node.label;
    group.append(title, circle);
    svg.append(group);
  }
  return svg;
}

function renderRelationshipSummary() {
  const content = document.getElementById("view-content");
  const types = corpusStore && corpusStore.analysis && corpusStore.analysis.relationship_overview && corpusStore.analysis.relationship_overview.types || [];
  if (!types.length) {
    content.replaceChildren(createElement("div", { className: "empty-state", text: translation().noRoute }));
    return;
  }
  const grid = createElement("div", { className: "relationship-summary" });
  for (const row of types.slice(0, 40)) {
    grid.append(createElement("article", { className: "summary-card" }, [
      createElement("h3", { text: row.kind || row.label || "Relationship" }),
      createElement("p", { text: formatNumber(row.count) + " relationships in the snapshot" })
    ]));
  }
  content.replaceChildren(grid, createElement("p", { className: "hint", text: translation().noRoute }));
}

function renderRelationships() {
  const content = document.getElementById("view-content");
  document.getElementById("pagination").replaceChildren();
  if (!state.route) {
    renderRelationshipSummary();
    return;
  }
  if (relationshipsLoading) {
    content.replaceChildren(createElement("p", { text: "Loading the selected route’s relationship shard…" }));
    return;
  }
  const graphRecords = selectedRecord ? [...visibleRecords(), selectedRecord] : visibleRecords();
  const model = buildBoundedGraph(graphRecords, selectedRelationships, { centerRoute: state.route });
  const summary = createElement("div", { className: "relationship-summary" });
  for (const row of model.summary.slice(0, 12)) summary.append(createElement("div", { className: "summary-card" }, [createElement("strong", { text: row.kind }), createElement("span", { text: " " + formatNumber(row.count) })]));
  const visual = createElement("figure", { className: "graph-figure mode-explore" }, [graphSvg(model), createElement("figcaption", { text: translation().graphCaveat })]);
  const limits = createElement("p", { className: "hint", text: "Showing " + model.nodes.length + " nodes and " + model.edges.length + " edges. Omitted by bounds: " + model.omittedNodes + " nodes, " + model.omittedEdges + " edges." });
  const listRegion = createElement("div", {}, [createElement("h3", { text: "Equivalent relationship list" }), model.list.length ? relationshipTable(model.list) : createElement("p", { text: "No relationship rows were advertised for this route." })]);
  content.replaceChildren(summary, visual, limits, listRegion);
}

function lifecycleEvents(recordsToUse) {
  const events = [];
  for (const record of recordsToUse) {
    if (record.lifecycleEvents.length) {
      for (const event of record.lifecycleEvents) {
        events.push({
          route: record.route,
          title: record.title,
          kind: String(event.kind || event.type || event.status || "Lifecycle event"),
          date: String(event.at || event.date || event.timestamp || ""),
          note: String(event.note || event.description || "")
        });
      }
    } else {
      if (record.updatedAt) events.push({ route: record.route, title: record.title, kind: "Updated", date: record.updatedAt, note: "Snapshot metadata" });
      if (record.firstPublishedAt) events.push({ route: record.route, title: record.title, kind: "First published", date: record.firstPublishedAt, note: "Snapshot metadata" });
      if (["withdrawn", "redirect", "redirected", "gone", "replaced"].includes(record.status.toLowerCase())) events.push({ route: record.route, title: record.title, kind: record.status, date: record.updatedAt, note: "Confirm the current destination on GOV.UK" });
    }
  }
  return events.sort((left, right) => String(right.date).localeCompare(String(left.date)) || left.title.localeCompare(right.title));
}

function renderTimeline() {
  const content = document.getElementById("view-content");
  const events = lifecycleEvents(visibleRecords());
  const list = createElement("div", { className: "timeline-list" });
  list.append(createElement("p", { className: "hint", text: translation().lifecycleCaveat }));
  for (const event of events.slice(0, 200)) {
    const heading = createElement("h3");
    heading.append(createButton(event.title, () => openRoute(event.route, true)));
    list.append(createElement("article", { className: "timeline-event" }, [
      createElement("time", { text: formatDate(event.date), attributes: event.date ? { datetime: event.date } : {} }),
      heading,
      createElement("p", { text: event.kind }),
      event.note ? createElement("p", { className: "hint", text: event.note }) : null
    ].filter(Boolean)));
  }
  if (!events.length) list.append(createElement("div", { className: "empty-state", text: "No lifecycle events are available in the active context." }));
  content.replaceChildren(list);
  document.getElementById("pagination").replaceChildren();
}

function pinnedRecords() {
  return state.pins.map((route) => records.find((record) => record.route === route) || (selectedRecord && selectedRecord.route === route ? selectedRecord : normaliseRecord({ route, title: route, source_status: "not recorded" }, route)));
}

function exportActions(recordsToExport) {
  const container = createElement("div", { className: "export-actions" });
  for (const [format, label] of [["markdown", "Export Markdown"], ["yamlld", "Export YAML-LD"], ["jsonld", "Export JSON-LD"]]) {
    container.append(createButton(label, () => downloadExport(format, recordsToExport)));
  }
  container.append(createButton("Copy replayable link", () => copyText(serialiseExplorerState(state, window.location.href).toString())));
  return container;
}

function renderCompare() {
  const content = document.getElementById("view-content");
  const pinned = pinnedRecords();
  if (!pinned.length) {
    content.replaceChildren(createElement("div", { className: "empty-state", text: translation().compareEmpty }));
    document.getElementById("pagination").replaceChildren();
    return;
  }
  const table = createElement("table", { className: "compare-table" });
  const head = createElement("thead");
  head.append(createElement("tr", {}, ["Record", "Type", "Publisher", "Lifecycle", "Language / jurisdiction", "Actions"].map((label) => createElement("th", { text: label, attributes: { scope: "col" } }))));
  const body = createElement("tbody");
  for (const record of pinned) {
    body.append(createElement("tr", {}, [
      createElement("td", { attributes: { "data-label": "Record" } }, [routeButton(record.route)]),
      createElement("td", { text: record.type, attributes: { "data-label": "Type" } }),
      createElement("td", { text: record.publisher, attributes: { "data-label": "Publisher" } }),
      createElement("td", { text: record.status, attributes: { "data-label": "Lifecycle" } }),
      createElement("td", { text: record.language + (record.jurisdictions.length ? " / " + record.jurisdictions.join(", ") : ""), attributes: { "data-label": "Language / jurisdiction" } }),
      createElement("td", { attributes: { "data-label": "Actions" } }, [createButton(translation().unpin, () => togglePin(record.route), "link-button")])
    ]));
  }
  table.append(head, body);
  content.replaceChildren(table, exportActions(pinned));
  document.getElementById("pagination").replaceChildren();
}

function viewDescription(view, count) {
  const descriptions = {
    results: count + " " + translation().results + " in the active context.",
    sitemap: "Hosts, routes, redirects, boundary destinations and routing mechanisms in the loaded snapshot.",
    browse: "Official browse, taxonomy, organisation and journey structures remain distinct.",
    relationships: "Typed, evidence-bearing paths for the selected record or aggregate snapshot.",
    timeline: "Publication, update, withdrawal, redirect and replacement events in the active context.",
    compare: state.pins.length + " pinned records in a replayable working set."
  };
  return descriptions[view];
}

function renderView() {
  const heading = document.getElementById("view-heading");
  heading.replaceChildren(createElement("div", {}, [createElement("h2", { text: translation().tabs[state.view] }), createElement("p", { text: viewDescription(state.view, visibleRecords().length) })]));
  if (state.view === "sitemap") renderSitemap();
  else if (state.view === "browse") renderBrowse();
  else if (state.view === "relationships") renderRelationships();
  else if (state.view === "timeline") renderTimeline();
  else if (state.view === "compare") renderCompare();
  else renderResults();
}

function detailSection(title, body, className = "") {
  const details = createElement("details", { className });
  details.append(createElement("summary", { text: title }), body);
  return details;
}

function renderDetail() {
  const panel = document.getElementById("detail-panel");
  if (!selectedRecord || !state.route) {
    panel.hidden = true;
    document.getElementById("detail-content").replaceChildren();
    return;
  }
  panel.hidden = false;
  setText("detail-kind", selectedRecord.type);
  setText("detail-heading", selectedRecord.title);
  const content = document.getElementById("detail-content");
  const sections = [];
  if (selectedRecord.breadcrumb.length) {
    const list = createElement("ol");
    selectedRecord.breadcrumb.forEach((label) => list.append(createElement("li", { text: label })));
    sections.push(createElement("nav", { className: "breadcrumbs", attributes: { "aria-label": "Breadcrumb" } }, [list]));
  }
  sections.push(createElement("p", {}, [sourceBadge(selectedRecord), " ", createElement("span", { text: "This catalogue record is derived metadata. Confirm guidance and complete transactions on GOV.UK." })]));
  if (selectedRecord.canonicalUrl) {
    const canonicalHost = new URL(selectedRecord.canonicalUrl).hostname;
    sections.push(createElement("p", {}, [createExternalLink(canonicalHost === "www.gov.uk" ? translation().authoritative : translation().boundary, selectedRecord.canonicalUrl)]));
  }
  const actions = createElement("div", { className: "card-actions" }, [
    createButton(state.pins.includes(selectedRecord.route) ? translation().unpin : translation().pin, () => togglePin(selectedRecord.route)),
    createButton(translation().copyCitation, () => copyText(buildCitation(selectedRecord, state.snapshot))),
    createButton("Export JSON-LD", () => downloadExport("jsonld", [selectedRecord]))
  ]);
  sections.push(actions);
  const summaryBody = createElement("div", {}, [selectedRecord.summary ? createElement("p", { text: selectedRecord.summary }) : createElement("p", { text: "No summary was supplied in source metadata." }), metadataList(selectedRecord)]);
  sections.push(detailSection(translation().summary, summaryBody));
  const routingBody = createElement("div");
  const routingMetadata = createElement("dl", { className: "detail-meta" });
  for (const [label, value] of [
    ["Routing kind", selectedRecord.routingKind],
    ["Entity class", selectedRecord.entityClass],
    ["Coverage disposition", selectedRecord.coverageDisposition]
  ]) routingMetadata.append(createElement("dt", { text: label }), createElement("dd", { text: value || "Not recorded" }));
  routingBody.append(routingMetadata);
  if (selectedRecord.redirects.length) {
    routingBody.append(createElement("h4", { text: "Source-native redirect rules" }));
    routingBody.append(topologyTable(["Matched path", "Rule", "Segments", "Destination"], selectedRecord.redirects.map((redirect) => [
      redirect.path || "/",
      redirect.type,
      redirect.segmentsMode,
      redirect.destinationUrl ? [createExternalLink(redirect.destination || redirect.destinationUrl, redirect.destinationUrl)] : redirect.destination
    ])));
  } else routingBody.append(createElement("p", { text: "No source-declared redirect rule was supplied for this record." }));
  sections.push(detailSection("Routing", routingBody));
  const relationshipBody = relationshipsLoading
    ? createElement("p", { text: "Loading the route-scoped adjacency shard…" })
    : selectedRelationships.length
      ? relationshipTable(selectedRelationships)
      : createElement("p", { text: "No relationship rows were advertised for this route." });
  sections.push(detailSection(translation().relationships, relationshipBody, "mode-explore"));
  const events = lifecycleEvents([selectedRecord]);
  const lifecycleBody = createElement("div", {}, [createElement("p", { className: "hint", text: translation().lifecycleCaveat })]);
  for (const event of events) lifecycleBody.append(createElement("p", { text: formatDate(event.date) + " — " + event.kind + (event.note ? ": " + event.note : "") }));
  if (!events.length) lifecycleBody.append(createElement("p", { text: "No lifecycle events were supplied." }));
  sections.push(detailSection(translation().lifecycle, lifecycleBody));
  const provenance = createElement("dl", { className: "detail-meta" });
  const provenanceRows = [
    ["Canonical record ID", selectedRecord.id],
    ["Explorer route", selectedRecord.route],
    ["Snapshot", state.snapshot || "Not recorded"],
    ["Assertion state", selectedRecord.sourceStatus],
    ["Retrieved", formatDate(selectedRecord.retrievedAt)]
  ];
  if (selectedRecord.sourceStatus === "inferred") provenanceRows.push(["Confidence", selectedRecord.confidence || "Not recorded"]);
  for (const [label, value] of provenanceRows) provenance.append(createElement("dt", { text: label }), createElement("dd", { text: value || "Not recorded" }));
  if (selectedRecord.evidenceUrl) provenance.append(createElement("dt", { text: "Evidence" }), createElement("dd", {}, [createExternalLink("Open source evidence", selectedRecord.evidenceUrl)]));
  sections.push(detailSection(translation().provenance, provenance, "mode-explore"));
  const raw = createElement("pre", { className: "code-block" }, [createElement("code", { text: JSON.stringify(selectedRecord.raw, null, 2) })]);
  sections.push(detailSection(translation().raw, raw, "mode-evidence"));
  if (instrumentationConsent) {
    const eventBlock = createElement("pre", { className: "code-block" }, [createElement("code", { text: JSON.stringify(instrumentationEvents, null, 2) })]);
    sections.push(detailSection("Allowlisted session events (" + instrumentationEvents.length + ")", eventBlock, "mode-evidence"));
  }
  content.replaceChildren(...sections);
}

async function loadSelectedRelationships(route) {
  if (!corpusStore || !route) return;
  relationshipsLoading = true;
  selectedRelationships = [];
  renderDetail();
  if (state.view === "relationships") renderRelationships();
  const selection = route;
  try {
    const rows = await corpusStore.loadRelationships(route);
    if (!selectedRecord || selectedRecord.route !== selection) return;
    selectedRelationships = rows;
  } catch (error) {
    setStatus("Relationship shard unavailable: " + (error instanceof Error ? error.message : String(error)));
  } finally {
    if (selectedRecord && selectedRecord.route === selection) {
      relationshipsLoading = false;
      renderDetail();
      if (state.view === "relationships") renderRelationships();
    }
  }
}

async function selectRecord(record, position = 0) {
  const started = performance.now();
  selectedRecord = record;
  selectedRelationships = [];
  recordInstrumentation("result", { position, routeKind: record.route.split("/")[0], view: state.view, mode: state.mode, snapshot: state.snapshot });
  syncState({ route: record.route }, true);
  await loadSelectedRelationships(record.route);
  document.getElementById("detail-panel").scrollIntoView({ block: "start" });
  document.getElementById("detail-heading").focus?.();
  recordBrowserMetric("lastRouteMs", performance.now() - started, "routeSequence");
}

async function openRoute(route, push = true) {
  const started = performance.now();
  const safeRoute = String(route || "").replace(/[<>]/g, "").slice(0, 768);
  if (!safeRoute) return;
  let record = records.find((item) => item.route === safeRoute) || (selectedRecord && selectedRecord.route === safeRoute ? selectedRecord : null);
  if (!record && corpusStore) {
    try {
      record = await corpusStore.loadRecord(safeRoute);
    } catch (error) {
      setStatus("Record shard unavailable: " + (error instanceof Error ? error.message : String(error)));
    }
  }
  if (!record && searchClient) {
    try {
      const matches = await searchClient.query(safeRoute.split("/").slice(1).join(" "));
      const exact = matches.find((item) => item.open === safeRoute || item.route === safeRoute || "dataset/" + item.name === safeRoute);
      if (exact) record = normaliseRecord(exact, safeRoute);
    } catch {
      record = null;
    }
  }
  if (!record) record = normaliseRecord({ route: safeRoute, title: safeRoute, source_status: "not recorded" }, safeRoute);
  selectedRecord = record;
  selectedRelationships = [];
  if (push) syncState({ route: safeRoute }, true);
  else {
    state = { ...state, route: safeRoute };
    applyChrome();
    renderAll();
  }
  recordInstrumentation("route", { routeKind: safeRoute.split("/")[0], view: state.view, mode: state.mode, snapshot: state.snapshot });
  await loadSelectedRelationships(safeRoute);
  recordBrowserMetric("lastRouteMs", performance.now() - started, "routeSequence");
}

function togglePin(route) {
  const pins = state.pins.includes(route)
    ? state.pins.filter((item) => item !== route)
    : [route, ...state.pins.filter((item) => item !== route)].slice(0, 12);
  syncState({ pins }, true);
}

async function copyText(value) {
  try {
    await navigator.clipboard.writeText(String(value));
    setStatus(translation().copied);
  } catch {
    setStatus("Clipboard access is unavailable. Use the export action instead.");
  }
}

function downloadExport(format, recordsToExport) {
  const output = buildContextExport(format, recordsToExport, state, corpusStore ? corpusStore.descriptor : {});
  const blob = new Blob([output.text], { type: output.mediaType + ";charset=utf-8" });
  const href = URL.createObjectURL(blob);
  const link = createElement("a", { attributes: { href, download: "govuk-okf-selection." + output.extension } });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(href), 0);
}

async function suggest(value) {
  const container = document.getElementById("search-suggestions");
  const query = String(value || "").trim();
  if (!searchClient || query.length < 3) {
    container.replaceChildren();
    return;
  }
  try {
    const suggestions = await searchClient.suggest(query);
    container.replaceChildren(...suggestions.slice(0, 8).map((suggestion) => createButton(suggestion.token, () => runSearch(suggestion.token, true))));
  } catch (error) {
    if (!error || error.name !== "AbortError") container.replaceChildren();
  }
}

function renderAll() {
  applyChrome();
  if (!corpusStore) return;
  if (document.activeElement !== document.getElementById("search-input")) document.getElementById("search-input").value = state.query;
  renderScope();
  renderActiveContext();
  renderFacets();
  renderView();
  renderDetail();
  const topologyBusy = state.view === "sitemap" && siteTopologyLoading;
  if (!topologyBusy && (!document.getElementById("view-content").getAttribute("aria-busy") || document.getElementById("view-content").getAttribute("aria-busy") === "false")) {
    setStatus(visibleRecords().length + " " + translation().results + " in the active context.");
  }
  document.getElementById("view-content").setAttribute("aria-busy", String(topologyBusy));
}

function bindEvents() {
  document.getElementById("skip-link").addEventListener("click", (event) => {
    event.preventDefault();
    const main = document.getElementById("main-content");
    main.scrollIntoView({ block: "start" });
    main.focus();
  });
  document.getElementById("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    runSearch(document.getElementById("search-input").value, true);
  });
  document.getElementById("search-input").addEventListener("input", (event) => {
    if (suggestionTimer !== null) window.clearTimeout(suggestionTimer);
    suggestionTimer = window.setTimeout(() => suggest(event.target.value), 250);
  });
  document.querySelectorAll("[data-query]").forEach((button) => button.addEventListener("click", () => runSearch(button.dataset.query, true)));
  document.querySelectorAll("[data-language]").forEach((button) => button.addEventListener("click", () => syncState({ language: button.dataset.language }, true)));
  document.querySelectorAll("button[data-mode]").forEach((button) => button.addEventListener("click", () => syncState({ mode: button.dataset.mode }, true)));
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => syncState({ view: button.dataset.view, page: 1 }, true)));
  document.getElementById("clear-filters").addEventListener("click", () => syncState({ facets: {}, lifecycle: "all", jurisdiction: "all", page: 1 }, true));
  document.getElementById("close-detail").addEventListener("click", () => {
    selectedRecord = null;
    selectedRelationships = [];
    relationshipsLoading = false;
    syncState({ route: "" }, true);
  });
  document.getElementById("retry-load").addEventListener("click", () => loadCatalogue().catch(showFatal));
  document.getElementById("scope-toggle").addEventListener("click", (event) => {
    const content = document.getElementById("scope-content");
    const expanded = event.currentTarget.getAttribute("aria-expanded") === "true";
    event.currentTarget.setAttribute("aria-expanded", String(!expanded));
    event.currentTarget.textContent = expanded ? "Show details" : "Hide details";
    content.hidden = expanded;
  });
  document.getElementById("instrumentation-consent").addEventListener("change", (event) => {
    instrumentationConsent = event.currentTarget.checked;
    if (!instrumentationConsent) instrumentationEvents.splice(0);
    renderDetail();
  });
  window.addEventListener("popstate", async () => {
    const previousQuery = state.query;
    const previousRoute = state.route;
    state = parseExplorerState(window.location.href);
    applyChrome();
    if (state.query !== previousQuery) await runSearch(state.query, false);
    else renderAll();
    if (state.route && state.route !== previousRoute) await openRoute(state.route, false);
    if (!state.route) {
      selectedRecord = null;
      selectedRelationships = [];
      renderDetail();
    }
  });
}

bindEvents();
applyChrome();
loadCatalogue().catch(showFatal);
