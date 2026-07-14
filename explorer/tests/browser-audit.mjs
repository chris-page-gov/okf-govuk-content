import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const budgets = JSON.parse(await readFile(join(here, "..", "requirements", "browser-budgets.json"), "utf8"));
const ROUTE = "publisher/government-digital-service";
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

function routeUrl(baseUrl, snapshot, legacy = false) {
  const url = new URL(baseUrl);
  url.searchParams.set("snapshot", snapshot);
  url.searchParams.set("view", "relationships");
  if (legacy) url.searchParams.set("route", ROUTE);
  else url.hash = ROUTE;
  return url;
}

export async function runFixtureBrowserAudit(browser, server, options = {}) {
  const iterations = Math.max(1, Number(options.iterations || 3));
  const snapshot = String(options.snapshot || FIXTURE_SNAPSHOT);
  const artifactTier = String(options.artifactTier || "representative_fixture");
  const fullRelease = artifactTier === "full_release_snapshot";
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

    coldSearchSamples.push(await submitSearch(browser, "welcome"));
    warmSearchSamples.push(await submitSearch(browser, "welcome"));
    recordDataRequests(browser.network, directGzipPaths, packRequests);

    if (iteration === 0) {
      const sequence = Number(await browser.evaluate("document.documentElement.dataset.routeSequence || 0"));
      await browser.evaluate("document.querySelector('.result-card h3 button').click(); true");
      await browser.waitFor(`Number(document.documentElement.dataset.routeSequence || 0) > ${sequence} && document.activeElement && document.activeElement.id === 'detail-heading'`);
      resultFocus = await browser.evaluate(`({ detail_heading_focused: document.activeElement.id === "detail-heading", canonical_hash_route: decodeURIComponent(location.hash.slice(1)), legacy_query_route_present: new URL(location.href).searchParams.has("route") })`);
    }

    await browser.navigate(routeUrl(server.baseUrl, snapshot).toString(), "document.documentElement.dataset.explorerReady === 'true' && document.getElementById('detail-heading').textContent.includes('Government Digital Service')");
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

  await browser.navigate(routeUrl(server.baseUrl, snapshot, true).toString(), "document.documentElement.dataset.explorerReady === 'true'");
  const legacyAlias = await browser.evaluate(`({ has_query_route: new URL(location.href).searchParams.has("route"), hash: decodeURIComponent(location.hash.slice(1)), heading: document.getElementById("detail-heading").textContent })`);

  const fallback = new URL("missing/nested", server.baseUrl);
  fallback.searchParams.set("snapshot", snapshot);
  fallback.searchParams.set("view", "relationships");
  fallback.hash = ROUTE;
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
    !legacyAlias.has_query_route && legacyAlias.hash === ROUTE && legacyAlias.heading.includes("Government Digital Service") &&
    pagesFallback.pathname === server.basePath && pagesFallback.hash === ROUTE && pagesFallback.view === "relationships" && pagesFallback.heading.includes("Government Digital Service");

  return {
    schema: "govuk-okf-explorer-browser-evidence.v1",
    generated_at: options.generatedAt || new Date().toISOString(),
    snapshot,
    artifact_tier: artifactTier,
    data_plane_index_sha256: packCoverage.indexSha256,
    site_checksums_sha256: siteChecksumsSha256,
    publication_ready: fullRelease && accessibilityPass && routePass && performancePass && browser.consoleErrors.length === 0,
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
      canonical_route_fragment: ROUTE,
      sitemap_routing: sitemapRouting,
      legacy_query_alias: legacyAlias,
      pages_404_fallback: pagesFallback,
      direct_gzip_resources_loaded: [...directGzipPaths].sort(),
      release_data_plane_index_present: packCoverage.indexPresent,
      physical_pack_resources: physicalPackPaths,
      range_requests: packCoverage.requests,
      virtual_resources_loaded: packedVirtualPaths
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
    overall_status: accessibilityPass && routePass && performancePass && browser.consoleErrors.length === 0
      ? (fullRelease ? "automated_full_release_evidence_pass" : "automated_fixture_evidence_pass")
      : "failed"
  };
}
