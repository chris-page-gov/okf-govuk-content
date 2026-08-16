import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const budgets = JSON.parse(await readFile(join(here, "..", "requirements", "browser-budgets.json"), "utf8"));
const FIXTURE_SNAPSHOT = "fixture-2026-07-11";

export function isGzipResourcePath(url) {
  const pathname = new URL(url).pathname;
  return pathname.endsWith(".json.gz") || pathname.endsWith(".pack.gz");
}

function quantile(values, probability) {
  if (!values.length) return null;
  const ordered = [...values].sort((left, right) => left - right);
  return ordered[Math.max(0, Math.ceil(probability * ordered.length) - 1)];
}

function metricValue(metrics, name) {
  const match = metrics.find((metric) => metric.name === name);
  return match ? Number(match.value) : 0;
}

function sha256Bytes(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

async function resourceSha256(baseUrl, path) {
  try {
    const response = await fetch(new URL(path, baseUrl), { cache: "no-store" });
    if (!response.ok) return "";
    return sha256Bytes(new Uint8Array(await response.arrayBuffer()));
  } catch {
    return "";
  }
}

function recordDataRequests(items, directGzipPaths, packRequests) {
  for (const item of items) {
    if (!item.url) continue;
    const pathname = new URL(item.url).pathname;
    if (pathname.endsWith(".json.gz")) directGzipPaths.add(pathname);
    if (!pathname.endsWith(".pack.gz") || !/^bytes=\d+-\d+$/.test(String(item.range || ""))) continue;
    const key = `${pathname}\0${item.range}`;
    packRequests.set(key, {
      physical_path: pathname,
      range: item.range,
      status: Number(item.status || 0),
      content_range: String(item.content_range || "")
    });
  }
}

async function resolvePackRequests(baseUrl, packRequests) {
  let response;
  try {
    response = await fetch(new URL("release-data-plane.json", baseUrl), { cache: "no-store" });
  } catch {
    return { indexPresent: false, indexSha256: "", requests: [...packRequests.values()], virtualPaths: [] };
  }
  if (!response.ok) return { indexPresent: false, indexSha256: "", requests: [...packRequests.values()], virtualPaths: [] };
  const indexBytes = new Uint8Array(await response.arrayBuffer());
  const indexSha256 = sha256Bytes(indexBytes);
  const document = JSON.parse(new TextDecoder().decode(indexBytes));
  if (!document || !Array.isArray(document.packs) || !Array.isArray(document.entries)) {
    return { indexPresent: true, indexSha256, requests: [...packRequests.values()], virtualPaths: [] };
  }
  const packPaths = new Map(document.packs.map((pack) => [String(pack.id || ""), new URL(String(pack.path || ""), baseUrl).pathname]));
  const members = new Map();
  for (const entry of document.entries) {
    const physicalPath = packPaths.get(String(entry.pack || ""));
    const offset = Number(entry.offset);
    const bytes = Number(entry.packed_bytes);
    if (!physicalPath || !Number.isSafeInteger(offset) || !Number.isSafeInteger(bytes) || bytes < 1) continue;
    members.set(`${physicalPath}\0bytes=${offset}-${offset + bytes - 1}`, String(entry.path || ""));
  }
  const requests = [...packRequests].map(([key, request]) => ({
    ...request,
    virtual_path: members.get(key) || ""
  }));
  return {
    indexPresent: true,
    indexSha256,
    requests,
    virtualPaths: [...new Set(requests.map((request) => request.virtual_path).filter(Boolean))].sort()
  };
}

const DOM_AUDIT = String.raw`(() => {
  const visible = (element) => {
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && element.getClientRects().length > 0;
  };
  const label = (element) => {
    const labelledBy = (element.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean)
      .map((id) => document.getElementById(id)?.textContent || "").join(" ");
    return [element.getAttribute("aria-label"), labelledBy, element.labels && [...element.labels].map((item) => item.textContent).join(" "), element.getAttribute("alt"), element.getAttribute("title"), element.textContent]
      .map((value) => String(value || "").replace(/\s+/g, " ").trim()).find(Boolean) || "";
  };
  const interactive = [...document.querySelectorAll("a[href], button, input, select, textarea, summary")].filter(visible);
  const missingNames = interactive.filter((element) => !label(element)).map((element) => element.outerHTML.slice(0, 240));
  const ids = [...document.querySelectorAll("[id]")].map((element) => element.id);
  const duplicateIds = [...new Set(ids.filter((id, index) => ids.indexOf(id) !== index))];
  const headings = [...document.querySelectorAll("h1, h2, h3, h4, h5, h6")].filter(visible).map((heading) => ({ level: Number(heading.tagName.slice(1)), text: label(heading) }));
  const headingJumps = headings.slice(1).filter((heading, index) => heading.level > headings[index].level + 1);
  return { missingNames, duplicateIds, headings, headingJumps, h1Count: headings.filter((heading) => heading.level === 1).length, interactiveCount: interactive.length, liveRegionCount: document.querySelectorAll("[aria-live], [role=status], [role=alert]").length };
})()`;

const CONTRAST_AUDIT = String.raw`(() => {
  const parse = (value) => {
    const match = /rgba?\((\d+(?:\.\d+)?)[ ,]+(\d+(?:\.\d+)?)[ ,]+(\d+(?:\.\d+)?)(?:[ ,/]+(\d+(?:\.\d+)?))?\)/.exec(value || "");
    return match ? [Number(match[1]), Number(match[2]), Number(match[3]), match[4] === undefined ? 1 : Number(match[4])] : null;
  };
  const channel = (value) => {
    const normalized = value / 255;
    return normalized <= 0.04045 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
  };
  const luminance = (color) => 0.2126 * channel(color[0]) + 0.7152 * channel(color[1]) + 0.0722 * channel(color[2]);
  const ratio = (foreground, background) => {
    const high = Math.max(luminance(foreground), luminance(background));
    const low = Math.min(luminance(foreground), luminance(background));
    return (high + 0.05) / (low + 0.05);
  };
  const background = (element) => {
    let current = element;
    while (current) {
      const color = parse(getComputedStyle(current).backgroundColor);
      if (color && color[3] >= 0.99) return color;
      current = current.parentElement;
    }
    return [255, 255, 255, 1];
  };
  const candidates = [...document.body.querySelectorAll("*")].filter((element) => {
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || element.getClientRects().length === 0) return false;
    const ownText = [...element.childNodes].some((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
    return ownText || element.matches("input, button, summary");
  });
  const failures = [];
  for (const element of candidates) {
    const style = getComputedStyle(element);
    const foreground = parse(style.color);
    if (!foreground || foreground[3] < 0.99 || element.matches(":disabled")) continue;
    const size = Number.parseFloat(style.fontSize);
    const weight = Number.parseInt(style.fontWeight, 10) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const minimum = large ? 3 : 4.5;
    const observed = ratio(foreground, background(element));
    if (observed + 0.01 < minimum) failures.push({ selector: element.id ? "#" + element.id : element.tagName.toLowerCase() + "." + [...element.classList].join("."), ratio: Number(observed.toFixed(2)), minimum, text: element.textContent.trim().slice(0, 100) });
  }
  return failures.slice(0, 50);
})()`;

async function submitSearch(browser, query) {
  const sequence = Number(await browser.evaluate("document.documentElement.dataset.searchSequence || 0"));
  await browser.evaluate(`(() => { const input = document.getElementById("search-input"); input.value = ${JSON.stringify(query)}; document.getElementById("search-form").requestSubmit(); return true; })()`);
  await browser.waitFor(`Number(document.documentElement.dataset.searchSequence || 0) > ${sequence} && document.querySelectorAll(".result-card").length > 0`);
  return Number(await browser.evaluate("document.documentElement.dataset.lastSearchMs"));
}

async function pointerClick(browser, selector) {
  const point = await browser.evaluate(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) return null;
    element.scrollIntoView({ block: "center", inline: "center" });
    const rect = element.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  })()`);
  if (!point) throw new Error(`browser task could not find ${selector}`);
  await browser.client.command("Input.dispatchMouseEvent", { type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1 });
  await browser.client.command("Input.dispatchMouseEvent", { type: "mouseReleased", x: point.x, y: point.y, button: "left", clickCount: 1 });
}

async function completeFindRecordTask(browser, query) {
  let clicks = 0;
  const zeroClickState = await browser.evaluate(`(() => ({
    heading: document.getElementById("page-heading")?.textContent || "",
    lede: document.getElementById("page-lede")?.textContent || "",
    scope: document.getElementById("scope-description")?.textContent || "",
    examples: [...document.querySelectorAll("#search-examples [data-query]")].map((button) => button.dataset.query || ""),
    backend: document.documentElement.dataset.searchBackend || ""
  }))()`);
  const searchSequence = Number(await browser.evaluate("document.documentElement.dataset.searchSequence || 0"));
  await browser.evaluate(`(() => {
    const input = document.getElementById("search-input");
    input.value = ${JSON.stringify(query)};
    input.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  })()`);
  await pointerClick(browser, "#search-submit");
  clicks += 1;
  await browser.waitFor(`Number(document.documentElement.dataset.searchSequence || 0) > ${searchSequence} && document.querySelectorAll(".result-card").length > 0`);
  const resultState = await browser.evaluate(`({
    count: document.querySelectorAll(".result-card").length,
    status: document.getElementById("load-status")?.textContent || "",
    first_title: document.querySelector(".result-card h3 button")?.textContent || "",
    search_ms: Number(document.documentElement.dataset.lastSearchMs || 0)
  })`);
  const routeSequence = Number(await browser.evaluate("document.documentElement.dataset.routeSequence || 0"));
  await pointerClick(browser, ".result-card h3 button");
  clicks += 1;
  await browser.waitFor(`Number(document.documentElement.dataset.routeSequence || 0) > ${routeSequence} && document.activeElement && document.activeElement.id === "detail-heading"`);
  const resultFocus = await browser.evaluate(`({
    detail_heading_focused: document.activeElement.id === "detail-heading",
    canonical_hash_route: decodeURIComponent(location.hash.slice(1)),
    legacy_query_route_present: new URL(location.href).searchParams.has("route")
  })`);
  return {
    zero_click_state: zeroClickState,
    search_results_clicks: 1,
    record_detail_clicks: clicks,
    result_count: resultState.count,
    result_status: resultState.status,
    first_result_title: resultState.first_title,
    search_ms: resultState.search_ms,
    result_focus: resultFocus
  };
}

async function completeExampleSearchTasks(browser, examples) {
  const observations = [];
  for (let index = 0; index < examples.length; index += 1) {
    const sequence = Number(await browser.evaluate("document.documentElement.dataset.searchSequence || 0"));
    await pointerClick(browser, `#search-examples button:nth-of-type(${index + 1})`);
    await browser.waitFor(`Number(document.documentElement.dataset.searchSequence || 0) > ${sequence} && document.querySelectorAll(".result-card").length > 0`);
    observations.push(await browser.evaluate(`({
      query: document.getElementById("search-input")?.value || "",
      clicks: 1,
      result_count: document.querySelectorAll(".result-card").length,
      first_result_title: document.querySelector(".result-card h3 button")?.textContent || ""
    })`));
  }
  return observations;
}

function routeUrl(baseUrl, snapshot, route, legacy = false) {
  const url = new URL(baseUrl);
  url.searchParams.set("snapshot", snapshot);
  url.searchParams.set("view", "relationships");
  if (legacy) url.searchParams.set("route", route);
  else url.hash = route;
  return url;
}

async function resolveBrowserTarget(baseUrl) {
  const [manifestResponse, resultsResponse] = await Promise.all([
    fetch(new URL("data/manifest.json", baseUrl), { cache: "no-store" }),
    fetch(new URL("data/search/results-0.json", baseUrl), { cache: "no-store" })
  ]);
  if (!manifestResponse.ok || !resultsResponse.ok) {
    throw new Error("browser audit could not load the advertised manifest and search bootstrap");
  }
  const manifest = await manifestResponse.json();
  const results = await resultsResponse.json();
  const row = Array.isArray(results)
    ? results.find((candidate) => candidate && typeof candidate.open === "string" && typeof candidate.title === "string")
    : null;
  if (!row) throw new Error("browser audit search bootstrap contains no routable titled record");
  return {
    snapshot: String(manifest.snapshot || manifest.snapshot_id || ""),
    route: row.publisher ? `publisher/${row.publisher}` : row.open,
    title: row.publisher_title || row.title,
    searchQuery: row.title
  };
}

export async function runFixtureBrowserAudit(browser, server, options = {}) {
  const iterations = Math.max(1, Number(options.iterations || 3));
  const exampleSearchesRequired = options.exampleSearchesRequired !== false;
  const artifactTier = String(options.artifactTier || "representative_fixture");
  const fullRelease = artifactTier === "full_release_snapshot";
  if (fullRelease && options.snapshot && /fixture|sample|capacity|development|test/i.test(String(options.snapshot))) {
    throw new Error(`full-release browser evidence cannot use snapshot ${options.snapshot}`);
  }
  const hasExplicitTarget = options.snapshot && options.route && options.routeTitle && options.searchQuery;
  const advertised = hasExplicitTarget ? {} : await resolveBrowserTarget(server.baseUrl);
  const snapshot = String(options.snapshot || advertised.snapshot || FIXTURE_SNAPSHOT);
  const route = String(options.route || advertised.route);
  const routeTitle = String(options.routeTitle || advertised.title);
  const searchQuery = String(options.searchQuery || advertised.searchQuery);
  if (fullRelease && /fixture|sample|capacity|development|test/i.test(snapshot)) {
    throw new Error(`full-release browser evidence cannot use snapshot ${snapshot}`);
  }
  const startupSamples = [];
  const coldSearchSamples = [];
  const warmSearchSamples = [];
  const routeSamples = [];
  const bootstrapBytesSamples = [];
  const heapSamples = [];
  const directGzipPaths = new Set();
  const packRequests = new Map();
  let dom = null;
  let contrastFailures = [];
  let axRoles = [];
  let keyboard = null;
  let resultFocus = null;
  let reducedMotion = null;
  let forcedColors = false;
  let reflow = null;
  let sitemapRouting = null;
  let taskCompletion = null;

  for (let iteration = 0; iteration < iterations; iteration += 1) {
    await browser.client.command("Network.clearBrowserCache");
    await browser.client.command("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
    await browser.client.command("Emulation.setEmulatedMedia", { media: "screen", features: [] });
    await browser.navigate(server.baseUrl, "document.documentElement.dataset.explorerReady === 'true'");
    startupSamples.push(Number(await browser.evaluate("document.documentElement.dataset.firstUsefulRenderMs")));
    bootstrapBytesSamples.push(browser.network.reduce((total, item) => total + item.encoded_bytes, 0));

    if (iteration === 0) {
      dom = await browser.evaluate(DOM_AUDIT);
      contrastFailures = await browser.evaluate(CONTRAST_AUDIT);
      const tree = await browser.client.command("Accessibility.getFullAXTree");
      axRoles = [...new Set((tree.nodes || []).filter((node) => !node.ignored).map((node) => String(node.role?.value || "").toLowerCase()).filter(Boolean))].sort();

      const originalHash = await browser.evaluate("location.hash");
      await browser.key("Tab", "Tab", 9);
      const skipFocused = await browser.evaluate("document.activeElement && document.activeElement.id === 'skip-link'");
      await browser.key("Enter", "Enter", 13);
      await browser.waitFor("document.activeElement && document.activeElement.id === 'main-content'");
      keyboard = {
        skip_link_first: Boolean(skipFocused),
        target_focused: Boolean(await browser.evaluate("document.activeElement && document.activeElement.id === 'main-content'")),
        route_hash_preserved: (await browser.evaluate("location.hash")) === originalHash
      };

      await browser.client.command("Emulation.setEmulatedMedia", { media: "screen", features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
      reducedMotion = await browser.evaluate(`(() => {
        const style = getComputedStyle(document.getElementById("search-submit"));
        const milliseconds = (value) => Math.max(...String(value).split(",").map((part) => part.trim().endsWith("ms") ? Number.parseFloat(part) : Number.parseFloat(part) * 1000));
        return { matches: matchMedia("(prefers-reduced-motion: reduce)").matches, animation_ms: milliseconds(style.animationDuration), transition_ms: milliseconds(style.transitionDuration) };
      })()`);
      await browser.client.command("Emulation.setEmulatedMedia", { media: "screen", features: [{ name: "forced-colors", value: "active" }] });
      forcedColors = Boolean(await browser.evaluate("matchMedia('(forced-colors: active)').matches"));
      await browser.client.command("Emulation.setEmulatedMedia", { media: "screen", features: [] });
      await browser.client.command("Emulation.setDeviceMetricsOverride", { width: budgets.accessibility.reflow_viewport_css_px, height: 720, deviceScaleFactor: 1, mobile: false });
      reflow = await browser.evaluate(`({ client_width: document.documentElement.clientWidth, scroll_width: document.documentElement.scrollWidth, passes: document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1 })`);
      await browser.client.command("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
    }

    if (iteration === 0) {
      taskCompletion = await completeFindRecordTask(browser, searchQuery);
      coldSearchSamples.push(taskCompletion.search_ms);
      resultFocus = taskCompletion.result_focus;
    } else {
      coldSearchSamples.push(await submitSearch(browser, searchQuery));
    }
    warmSearchSamples.push(await submitSearch(browser, searchQuery));
    if (iteration === 0 && exampleSearchesRequired) {
      taskCompletion.example_searches = await completeExampleSearchTasks(browser, taskCompletion.zero_click_state.examples);
    } else if (iteration === 0) {
      taskCompletion.example_searches = [];
    }
    recordDataRequests(browser.network, directGzipPaths, packRequests);

    if (iteration === 0 && !resultFocus) {
      const sequence = Number(await browser.evaluate("document.documentElement.dataset.routeSequence || 0"));
      await browser.evaluate("document.querySelector('.result-card h3 button').click(); true");
      await browser.waitFor(`Number(document.documentElement.dataset.routeSequence || 0) > ${sequence} && document.activeElement && document.activeElement.id === 'detail-heading'`);
      resultFocus = await browser.evaluate(`({ detail_heading_focused: document.activeElement.id === "detail-heading", canonical_hash_route: decodeURIComponent(location.hash.slice(1)), legacy_query_route_present: new URL(location.href).searchParams.has("route") })`);
    }

    await browser.navigate(
      routeUrl(server.baseUrl, snapshot, route).toString(),
      `document.documentElement.dataset.explorerReady === 'true' && document.getElementById('detail-heading').textContent.includes(${JSON.stringify(routeTitle)})`
    );
    routeSamples.push(Number(await browser.evaluate("document.documentElement.dataset.lastRouteMs")));
    recordDataRequests(browser.network, directGzipPaths, packRequests);
    const performance = await browser.client.command("Performance.getMetrics");
    heapSamples.push(metricValue(performance.metrics || [], "JSHeapUsedSize"));
  }

  const sitemapUrl = new URL(server.baseUrl);
  sitemapUrl.searchParams.set("snapshot", snapshot);
  sitemapUrl.searchParams.set("view", "sitemap");
  await browser.navigate(
    sitemapUrl.toString(),
    "document.documentElement.dataset.explorerReady === 'true' && document.querySelectorAll('.topology-table tbody tr').length > 0"
  );
  sitemapRouting = await browser.evaluate(`(() => {
    const tables = [...document.querySelectorAll(".topology-table")];
    const topologyLink = [...document.querySelectorAll("#view-content a")].find((link) => link.textContent.includes("machine-readable site topology"));
    return {
      view: new URL(location.href).searchParams.get("view"),
      heading: document.querySelector("#view-heading h2")?.textContent || "",
      mechanism_cards: document.querySelectorAll(".topology-mechanisms .summary-card").length,
      host_rows: tables[0]?.querySelectorAll("tbody tr").length || 0,
      redirect_rows: tables[1]?.querySelectorAll("tbody tr").length || 0,
      machine_path: topologyLink ? new URL(topologyLink.href).pathname : "",
      unavailable: document.getElementById("view-content").textContent.includes("data is unavailable")
    };
  })()`);
  recordDataRequests(browser.network, directGzipPaths, packRequests);

  await browser.navigate(routeUrl(server.baseUrl, snapshot, route, true).toString(), "document.documentElement.dataset.explorerReady === 'true'");
  const legacyAlias = await browser.evaluate(`({ has_query_route: new URL(location.href).searchParams.has("route"), hash: decodeURIComponent(location.hash.slice(1)), heading: document.getElementById("detail-heading").textContent })`);

  const fallback = new URL("missing/nested", server.baseUrl);
  fallback.searchParams.set("snapshot", snapshot);
  fallback.searchParams.set("view", "relationships");
  fallback.hash = route;
  await browser.navigate(fallback.toString(), "location.pathname.endsWith('/okf-govuk-content/') && document.documentElement.dataset.explorerReady === 'true'");
  const pagesFallback = await browser.evaluate(`({ pathname: location.pathname, hash: decodeURIComponent(location.hash.slice(1)), view: new URL(location.href).searchParams.get("view"), heading: document.getElementById("detail-heading").textContent })`);
  recordDataRequests(browser.network, directGzipPaths, packRequests);

  const packCoverage = await resolvePackRequests(server.baseUrl, packRequests);
  const siteChecksumsSha256 = await resourceSha256(server.baseUrl, "checksums.json");
  const successfulPackRequests = packCoverage.requests.filter((request) =>
    request.status === 206 && request.content_range.startsWith(request.range.replace("=", " ") + "/") && request.virtual_path
  );
  const physicalPackPaths = [...new Set(successfulPackRequests.map((request) => request.physical_path))].sort();
  const packedVirtualPaths = [...new Set(successfulPackRequests.map((request) => request.virtual_path))].sort();
  const dataCoveragePass = packCoverage.indexPresent
    ? successfulPackRequests.length >= 2 && packedVirtualPaths.length >= 2 && physicalPackPaths.length >= 1
    : directGzipPaths.size >= 2;

  const performanceThresholds = budgets.performance;
  const accessibilityThresholds = budgets.accessibility;
  const usabilityThresholds = budgets.usability;
  const observed = {
    bootstrap_encoded_bytes_max: Math.max(...bootstrapBytesSamples),
    first_useful_render_p75_ms: quantile(startupSamples, 0.75),
    cold_search_p95_ms: quantile(coldSearchSamples, 0.95),
    warm_search_p95_ms: quantile(warmSearchSamples, 0.95),
    route_hydration_p95_ms: quantile(routeSamples, 0.95),
    steady_js_heap_bytes_max: Math.max(...heapSamples)
  };
  const performancePass =
    observed.bootstrap_encoded_bytes_max <= performanceThresholds.bootstrap_encoded_bytes_max &&
    observed.first_useful_render_p75_ms <= performanceThresholds.first_useful_render_p75_ms_max &&
    observed.cold_search_p95_ms <= performanceThresholds.cold_search_p95_ms_max &&
    observed.warm_search_p95_ms <= performanceThresholds.warm_search_p95_ms_max &&
    observed.route_hydration_p95_ms <= performanceThresholds.route_hydration_p95_ms_max &&
    observed.steady_js_heap_bytes_max <= performanceThresholds.steady_js_heap_bytes_max;
  const missingRoles = accessibilityThresholds.required_landmarks.filter((role) => !axRoles.includes(role));
  const accessibilityPass =
    dom.missingNames.length <= accessibilityThresholds.missing_accessible_names_max &&
    dom.duplicateIds.length <= accessibilityThresholds.duplicate_ids_max &&
    contrastFailures.length <= accessibilityThresholds.computed_contrast_failures_max &&
    missingRoles.length === 0 &&
    dom.h1Count === 1 && dom.headingJumps.length === 0 && dom.liveRegionCount > 0 &&
    keyboard.skip_link_first && keyboard.target_focused && keyboard.route_hash_preserved &&
    resultFocus.detail_heading_focused && resultFocus.canonical_hash_route.startsWith("dataset/") && !resultFocus.legacy_query_route_present &&
    reflow.passes && reducedMotion.matches &&
    reducedMotion.animation_ms <= accessibilityThresholds.reduced_motion_max_duration_ms &&
    reducedMotion.transition_ms <= accessibilityThresholds.reduced_motion_max_duration_ms &&
    forcedColors;
  const routePass =
    dataCoveragePass &&
    sitemapRouting.view === "sitemap" && sitemapRouting.heading.includes("Sitemap") &&
    sitemapRouting.mechanism_cards >= 5 && sitemapRouting.host_rows >= 1 &&
    sitemapRouting.machine_path.endsWith("/data/site-topology.json") && !sitemapRouting.unavailable &&
    !legacyAlias.has_query_route && legacyAlias.hash === route && legacyAlias.heading.includes(routeTitle) &&
    pagesFallback.pathname === server.basePath && pagesFallback.hash === route && pagesFallback.view === "relationships" && pagesFallback.heading.includes(routeTitle);
  const scopeText = `${taskCompletion?.zero_click_state?.heading || ""} ${taskCompletion?.zero_click_state?.lede || ""}`;
  const usabilityPass = Boolean(
    taskCompletion &&
    (!usabilityThresholds.scope_visible_without_clicks || (/69 GOV\.UK records/i.test(scopeText) && /does not search all GOV\.UK/i.test(scopeText))) &&
    taskCompletion.zero_click_state.backend === (usabilityThresholds.worker_search_required ? "worker" : taskCompletion.zero_click_state.backend) &&
    taskCompletion.search_results_clicks <= usabilityThresholds.search_results_max_clicks &&
    (!exampleSearchesRequired || (
      taskCompletion.example_searches.length > 0 &&
      taskCompletion.example_searches.every((example) => example.clicks <= usabilityThresholds.example_search_results_max_clicks && example.result_count > 0)
    )) &&
    taskCompletion.record_detail_clicks <= usabilityThresholds.record_detail_max_clicks &&
    taskCompletion.result_count > 0 &&
    taskCompletion.result_focus.detail_heading_focused
  );

  return {
    schema: "govuk-okf-explorer-browser-evidence.v1",
    generated_at: options.generatedAt || new Date().toISOString(),
    snapshot,
    artifact_tier: artifactTier,
    data_plane_index_sha256: packCoverage.indexSha256,
    site_checksums_sha256: siteChecksumsSha256,
    publication_ready: fullRelease && accessibilityPass && routePass && performancePass && usabilityPass && browser.consoleErrors.length === 0,
    browser: {
      name_version: browser.version,
      engine: "Chromium",
      automation: "Chrome DevTools Protocol using Node built-ins",
      platform: process.platform,
      architecture: process.arch,
      node: process.version
    },
    accessibility: {
      status: accessibilityPass ? (fullRelease ? "automated_full_release_subset_pass" : "automated_fixture_subset_pass") : "failed",
      pass: accessibilityPass,
      scope: "real-browser landmarks, names, focus, reflow, reduced motion, forced colours and computed contrast subset",
      ax_roles: axRoles,
      missing_required_roles: missingRoles,
      missing_accessible_names: dom.missingNames,
      duplicate_ids: dom.duplicateIds,
      computed_contrast_failures: contrastFailures,
      keyboard,
      result_selection_focus: resultFocus,
      reduced_motion: reducedMotion,
      forced_colors_media_active: forcedColors,
      reflow,
      heading_outline: dom.headings,
      heading_jumps: dom.headingJumps,
      h1_count: dom.h1Count,
      live_region_count: dom.liveRegionCount,
      qualifications: {
        wcag_conformance_claimed: false,
        axe_status: "not_run_dependency_install_blocked",
        expert_review: "not_run",
        screen_reader_review: "not_run",
        representative_user_review: "not_authorised"
      }
    },
    routing_and_data: {
      status: routePass ? "pass" : "failed",
      pass: routePass,
      canonical_route_fragment: route,
      sitemap_routing: sitemapRouting,
      legacy_query_alias: legacyAlias,
      pages_404_fallback: pagesFallback,
      direct_gzip_resources_loaded: [...directGzipPaths].sort(),
      release_data_plane_index_present: packCoverage.indexPresent,
      physical_pack_resources: physicalPackPaths,
      range_requests: packCoverage.requests,
      virtual_resources_loaded: packedVirtualPaths
    },
    usability: {
      status: usabilityPass ? "automated_task_budget_pass" : "failed",
      pass: usabilityPass,
      scope: "automated pointer path from a clear zero-click scope statement to search results and an opened record",
      thresholds: usabilityThresholds,
      example_searches_required: exampleSearchesRequired,
      observed: taskCompletion,
      qualifications: {
        representative_user_research_claimed: false,
        expert_usability_review_claimed: false
      }
    },
    performance: {
      status: performancePass ? (fullRelease ? "full_release_budget_pass" : "fixture_budget_pass") : "failed",
      pass: performancePass,
      iterations,
      thresholds: performanceThresholds,
      samples: {
        bootstrap_encoded_bytes: bootstrapBytesSamples,
        first_useful_render_ms: startupSamples,
        cold_search_ms: coldSearchSamples,
        warm_search_ms: warmSearchSamples,
        route_hydration_ms: routeSamples,
        steady_js_heap_bytes: heapSamples
      },
      observed
    },
    console_exceptions: browser.consoleErrors,
    full_release_gates: {
      full_corpus_browser_measurement: fullRelease && accessibilityPass && routePass && performancePass ? "passed" : "not_run",
      axe: "not_run_dependency_install_blocked",
      accessibility_expert_review: "not_run",
      participant_research: "not_authorised"
    },
    overall_status: accessibilityPass && routePass && performancePass && usabilityPass && browser.consoleErrors.length === 0
      ? (fullRelease ? "automated_full_release_evidence_pass" : "automated_fixture_evidence_pass")
      : "failed"
  };
}
