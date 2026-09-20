import { ApiError, createApiClient } from "./api-client.js";
import { GRAPH_METRICS, GRAPH_MODES, findingMetricValue, renderMetricGraph, graphValueClusters, visibleGraphClusters } from "./graph-renderer.js";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const params = new URLSearchParams(window.location.search);
const fixtureMode = params.get("fixtures") === "1";

const fixtureRoutes = {
  "/health": "/fixtures/api/health.json",
  "/api/v1/countries": "/fixtures/api/countries.json",
  "/api/v1/graph-series/JPN": "/fixtures/api/graph-series.json",
  "/api/v1/worker/jobs/job-example": "/fixtures/api/worker-job.json",
  "/api/v1/research/candidates/41": "/fixtures/api/candidate-detail.json",
  "/api/v1/research/country-gap-preview": "/fixtures/api/country-gap-preview.json",
  "/api/v1/research/settings": "/fixtures/api/research-settings.json",
  "/api/v1/research/country-queue": "/fixtures/api/country-queue.json",
  "/api/v1/research/runs": "/fixtures/api/research-runs.json",
  "/api/v1/analysis/drafts/draft-example": "/fixtures/api/analysis-draft.json",
  "/api/v1/analysis/drafts/draft-example/actions": "/fixtures/api/analysis-draft-actions.json",
  "/api/v1/findings": "/fixtures/api/findings.json",
  "/api/v1/admin/findings/coverage": "/fixtures/api/finding-coverage.json",
  "/api/v1/admin/source-rules": "/fixtures/api/source-rules.json",
  "/api/v1/admin/source-rule-actions": "/fixtures/api/source-rule-actions.json",
  "/api/v1/admin/blocked-sources": "/fixtures/api/blocked-sources.json",
  "/api/v1/admin/findings/7": "/fixtures/api/finding-detail.json",
  "/api/v1/admin/finding-actions": "/fixtures/api/finding-actions.json",
};

const fixtureFetch = async (path, options = {}) => {
  // The fixture client covers both durable job families: analysis\/jobs|research\/jobs.
  const routePath = path.split("?", 1)[0];
  const method = options.method || "GET";
  if (method !== "GET") {
    if (routePath === "/api/v1/research/settings") {
      const fixture = await fetch("/fixtures/api/research-settings.json");
      return new Response(await fixture.text(), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (path.endsWith("/rerun") && routePath.startsWith("/api/v1/analysis/")) {
      const fixture = await fetch("/fixtures/api/analysis-job.json");
      return new Response(await fixture.text(), { status: 202, headers: { "Content-Type": "application/json" } });
    }
    if (path.endsWith("/approve")) {
      const fixture = await fetch("/fixtures/api/analysis-draft-approved.json");
      return new Response(await fixture.text(), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (path.endsWith("/reject")) {
      const fixture = await fetch("/fixtures/api/analysis-draft-rejected.json");
      return new Response(await fixture.text(), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (path.endsWith("/remove")) {
      const fixture = await fetch("/fixtures/api/analysis-draft-removed.json");
      return new Response(await fixture.text(), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (path.endsWith("/suppress")) {
      const fixture = await fetch("/fixtures/api/analysis-draft-suppressed.json");
      return new Response(await fixture.text(), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (path.endsWith("/delete-metric") || path.endsWith("/rerun") || path.endsWith("/delete-and-block") || path.endsWith("/undo") || (routePath.startsWith("/api/v1/admin/") && (method === "PUT" || method === "DELETE" || method === "POST"))) {
      const fixture = await fetch(path.endsWith("/rerun") ? "/fixtures/api/finding-rerun.json" : path.endsWith("/delete-metric") ? "/fixtures/api/finding-metric-deleted.json" : path.endsWith("/undo") ? "/fixtures/api/source-rule-undone.json" : "/fixtures/api/admin-mutation.json");
      return new Response(await fixture.text(), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (path.startsWith("/api/v1/analysis/drafts/") && method === "PATCH") {
      const fixture = await fetch("/fixtures/api/analysis-draft.json");
      return new Response(await fixture.text(), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    const fixturePath = routePath === "/api/v1/research/country-gap-preview"
      ? "/fixtures/api/country-gap-preview.json"
      : routePath === "/api/v1/analysis/jobs" ? "/fixtures/api/analysis-job.json" : "/fixtures/api/worker-job.json";
    const fixture = await fetch(fixturePath);
    return new Response(await fixture.text(), {
      status: routePath === "/api/v1/research/country-gap-preview" ? 200 : 202,
      headers: { "Content-Type": "application/json" },
    });
  }
  const fixturePath = fixtureRoutes[routePath]
    || (/^\/api\/v1\/graph-series\/[A-Z]{3}$/i.test(routePath) ? "/fixtures/api/graph-series.json" : null)
    || (/^\/api\/v1\/analysis\/jobs\/[^/]+$/.test(routePath) ? "/fixtures/api/analysis-job.json" : null)
    || (/^\/api\/v1\/research\/jobs\/[^/]+$/.test(routePath) ? "/fixtures/api/worker-job.json" : null)
    || (/^\/api\/v1\/research\/candidates\/\d+$/.test(routePath) ? "/fixtures/api/candidate-detail.json" : null);
  if (!fixturePath) {
    return new Response(JSON.stringify({ detail: `No fixture registered for ${path}` }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    });
  }
  const response = await fetch(fixturePath, { ...options, headers: { Accept: "application/json" } });
  if (!response.ok) return response;
  const candidateMatch = routePath.match(/^\/api\/v1\/research\/candidates\/(\d+)$/);
  if (candidateMatch) {
    const payload = await response.json();
    payload.id = Number(candidateMatch[1]);
    return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  const graphMatch = routePath.match(/^\/api\/v1\/graph-series\/([A-Z]{3})$/i);
  if (graphMatch && fixturePath === "/fixtures/api/graph-series.json") {
    const payload = await response.json();
    const iso3 = graphMatch[1].toUpperCase();
    const country = iso3 === "AUS" ? "Australia" : (iso3 === "JPN" ? "Japan" : iso3);
    payload.iso3 = iso3;
    payload.country = country;
    // Article findings belong to the fixture's source country; do not display
    // them as evidence for an adapted country series.
    if (iso3 !== "JPN") payload.findings = [];
    return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  return response;
};

const api = createApiClient({ fetchImpl: fixtureMode ? fixtureFetch : window.fetch.bind(window) });
let countryChoices = [];
let findingsSort = { key: "id", direction: "descending" };

const RESEARCH_POLL_INTERVAL_MS = 5000;
const RESEARCH_POLL_MAX_ATTEMPTS = 180;
const ACTIVE_RESEARCH_STATES = new Set(["running", "stopping"]);
const FINAL_CANDIDATE_STATES = new Set([
  "complete", "duplicate", "discovery_only", "relevant_access_blocked", "unclear_access_blocked",
  "deferred_budget", "failed", "stopped", "cancelled", "interrupted",
]);
let historicRunId = "";
let runHistoryRefreshTimer = null;

function elapsedLabel(startedAt) {
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  const minutes = Math.floor(elapsedSeconds / 60);
  const seconds = elapsedSeconds % 60;
  return minutes ? `${minutes}m ${String(seconds).padStart(2, "0")}s elapsed` : `${seconds}s elapsed`;
}

function researchPollingStatus(current, startedAt) {
  const events = current.events || current.progress?.logs || [];
  return `${current.status} · ${events.length} log entries · ${elapsedLabel(startedAt)}`;
}

async function copyToClipboard(text) {
  const value = String(text || "");
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch (error) {
      // Local HTTP pages often do not receive clipboard permission. Use the
      // legacy browser command as a fallback.
    }
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-1000px";
  textarea.style.opacity = "0";
  document.body.append(textarea);
  textarea.focus();
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("Copy is unavailable in this browser. Select the text and copy it manually.");
}

function showCopied(button, label) {
  const original = button.dataset.label || button.textContent;
  button.dataset.label = original;
  button.textContent = label;
  window.setTimeout(() => { button.textContent = original; }, 1200);
}

const graphState = {
  payloadByCountry: new Map(),
  hiddenByCountry: new Map(),
  currentCountry: "",
  mode: GRAPH_MODES.show_all,
  requestNumber: 0,
};

function selectedGraphMetrics() {
  const selected = $$(`[data-graph-metric]:checked`).map((input) => input.dataset.graphMetric).filter((metric) => GRAPH_METRICS[metric]);
  return selected.length ? selected : ["population"];
}

function selectedGraphRevisions() {
  return $$(`[data-graph-revision]:checked`).map((input) => input.dataset.graphRevision).filter(Boolean);
}

function reviewGraphFinding(findingId) {
  openFindingEditor(findingId);
}

function openGraphForCountry(iso3) {
  const country = String(iso3 || "").trim().toUpperCase();
  if (!country) return;
  const select = $(`[data-graph-country]`);
  if (select && [...select.options].some((option) => option.value === country)) select.value = country;
  window.location.hash = "graphs";
  selectView("graphs");
  loadGraphs({ force: true });
}

function openFindingEditor(findingId) {
  const id = Number(findingId);
  if (!Number.isInteger(id) || id <= 0) return;
  window.location.hash = `finding-edit?id=${encodeURIComponent(id)}`;
  selectView("finding-edit");
  loadRecord(id);
}

function renderGraphs(payload) {
  const grid = $(`[data-graph-grid]`);
  if (!grid || !payload) return;
  const metrics = selectedGraphMetrics();
  const alternateRevisions = selectedGraphRevisions();
  const hidden = graphState.hiddenByCountry.get(payload.iso3) || new Set();
  grid.replaceChildren();
  metrics.forEach((metric) => renderMetricGraph(grid, payload, metric, { hiddenFindingIds: hidden, alternateRevisions, graphMode: graphState.mode, onFindingSelect: reviewGraphFinding }));
}

function graphDataPointCounts(payload, metrics, alternateRevisions) {
  const countReferenceRows = (rows) => (rows || []).reduce((count, row) => (
    count + metrics.filter((metric) => {
      const value = row?.[GRAPH_METRICS[metric]?.column];
      return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
    }).length
  ), 0);
  const reference = countReferenceRows(payload.historic) + countReferenceRows(payload.forecast);
  const alternate = alternateRevisions.reduce((count, revision) => (
    count + countReferenceRows(payload.alternate_releases?.[revision])
  ), 0);
  const clusters = graphValueClusters(payload);
  const article = clusters.length
    ? visibleGraphClusters(payload, graphState.mode).filter((cluster) => metrics.includes(cluster.metric)).length
    : (payload.findings || []).reduce((count, item) => count + metrics.filter((metric) => findingMetricValue(item.finding, metric) !== null).length, 0);
  return { reference, alternate, article, total: reference + alternate + article };
}

function renderGraphDataPointCount(payload, metrics, alternateRevisions) {
  const target = $(`[data-graph-datapoint-count]`);
  if (!target) return;
  const counts = graphDataPointCounts(payload, metrics, alternateRevisions);
  const segments = [`${counts.reference.toLocaleString()} WPP current`];
  if (counts.alternate) segments.push(`${counts.alternate.toLocaleString()} alternate`);
  if (counts.article) segments.push(`${counts.article.toLocaleString()} article evidence`);
  target.textContent = `${counts.total.toLocaleString()} selected datapoint${counts.total === 1 ? "" : "s"} · ${segments.join(" · ")}`;
}

async function loadGraphs({ force = false } = {}) {
  const state = $(`[data-graph-state]`);
  const iso3 = $(`[data-graph-country]`)?.value || "";
  if (!iso3) { setState(state, "empty", "Choose a country to load the reporting series."); return; }
  const metrics = selectedGraphMetrics();
  const revisions = selectedGraphRevisions();
  const query = new URLSearchParams();
  metrics.forEach((metric) => query.append("metrics", metric));
  revisions.forEach((revision) => query.append("revisions", revision));
  const requestNumber = ++graphState.requestNumber;
  graphState.currentCountry = iso3;
  setState(state, "loading", `Loading ${iso3} reporting series…`);
  const findingsPromise = api.request(`/api/v1/findings?iso3=${encodeURIComponent(iso3)}`).then(
    (payload) => ({ payload }),
    (error) => ({ error }),
  );
  try {
    const payload = force || !graphState.payloadByCountry.has(iso3)
      ? await api.request(`/api/v1/graph-series/${encodeURIComponent(iso3)}?${query}`)
      : graphState.payloadByCountry.get(iso3);
    if (requestNumber !== graphState.requestNumber) return;
    graphState.payloadByCountry.set(iso3, payload);
    if (!graphState.hiddenByCountry.has(iso3)) graphState.hiddenByCountry.set(iso3, new Set());
    renderGraphs(payload);
    renderGraphDataPointCount(payload, metrics, revisions);
    try {
      const findingsResult = await findingsPromise;
      if (requestNumber !== graphState.requestNumber) return;
      if (findingsResult.error) throw findingsResult.error;
      renderGraphFindings(findingsResult.payload.items || [], payload.country || iso3);
    } catch (error) {
      if (requestNumber !== graphState.requestNumber) return;
      renderGraphFindingsError(error);
    }
    loadClaimReview(iso3, Boolean($(`[data-claims-include-rejected]`)?.checked));
    const findingCount = (payload.findings || []).length;
    const releaseCount = selectedGraphRevisions().filter((revision) => Array.isArray(payload.alternate_releases?.[revision]) && payload.alternate_releases[revision].length).length;
    const clusterCount = graphValueClusters(payload).length;
    const modeLabel = graphState.mode === GRAPH_MODES.primary ? "primary" : graphState.mode === GRAPH_MODES.primary_approved_secondary ? "primary + approved secondary" : "all evidence";
    $(`[data-graph-summary]`).textContent = `${payload.country || iso3}: ${payload.historic?.length || 0} WPP historical rows, ${payload.forecast?.length || 0} WPP forecast rows, ${clusterCount || findingCount} evidence cluster${(clusterCount || findingCount) === 1 ? "" : "s"} (${modeLabel})${releaseCount ? `, ${releaseCount} alternate release${releaseCount === 1 ? "" : "s"}` : ""}. Rejected claims remain stored but are not plotted.`;
    setState(state, "completed", `Series ready for ${payload.country || iso3}.`);
  } catch (error) {
    if (requestNumber !== graphState.requestNumber) return;
    setState(state, "failed", "Graph series could not be loaded.", error.message);
    showGlobalError(error);
  }
}

function categoryEditor(category = {}) {
  const wrapper = document.createElement("div");
  wrapper.className = "category-row";
  wrapper.innerHTML = `<div class="category-heading"><strong>News category</strong><button class="quiet-button danger-button" type="button" data-remove-category>Remove</button></div><div class="control-grid"><label class="field">Name <input data-category="name" value=""></label><label class="field">Search depth <select data-category="search_depth"><option>basic</option><option>advanced</option><option>fast</option><option>ultra-fast</option></select></label><label class="field">Search window <select data-category="time_range"><option>day</option><option>week</option><option>month</option><option>year</option><option>all</option></select></label><label class="field">Result limit <input data-category="max_results" type="number" min="1" max="20" value="10"></label></div><label class="field">Query <input data-category="query" value=""></label><label class="field">Include domains (comma-separated) <input data-category="include_domains" value=""></label><label class="field">Exclude domains (comma-separated) <input data-category="exclude_domains" value=""></label><label class="check-field"><input data-category="enabled" type="checkbox" checked> Enabled</label>`;
  $$(`[data-category]`, wrapper).forEach((input) => {
    const key = input.dataset.category;
    if (input.type === "checkbox") input.checked = category[key] !== false;
    else if (Array.isArray(category[key])) input.value = category[key].join(", ");
    else if (category[key] !== undefined) input.value = category[key];
  });
  $(`[data-remove-category]`, wrapper).addEventListener("click", () => wrapper.remove());
  return wrapper;
}

function renderCategoryEditors(categories) {
  const list = $(`[data-category-list]`);
  if (!list) return;
  list.replaceChildren(...(categories || []).map(categoryEditor));
}

function coreResearchCategories() {
  const year = new Date().getFullYear();
  return [
    { name: "Population", topic: "news", time_range: "day", search_depth: "advanced", max_results: 10, query: `${year} "national population estimate" census statistical release` },
    { name: "Births, deaths and fertility", topic: "news", time_range: "day", search_depth: "advanced", max_results: 10, query: `${year} "annual vital statistics" births deaths "total fertility rate" national` },
    { name: "Migration", topic: "news", time_range: "day", search_depth: "advanced", max_results: 10, query: `${year} "annual net international migration" immigration emigration national statistics` },
  ];
}

function coreResearchSettings() {
  return { categories: coreResearchCategories(), max_candidates: 40, max_per_domain: 5, reddit_limit: 30, reddit_enabled: true };
}

function readCategoryEditors() {
  return $$(`[data-category-row], .category-row`).map((row) => {
    const value = (key) => $(`[data-category="${key}"]`, row)?.value?.trim() || "";
    const list = (key) => value(key).split(",").map((item) => item.trim()).filter(Boolean);
    return { name: value("name"), query: value("query"), topic: "news", max_results: Number(value("max_results") || 10), time_range: value("time_range") || "day", search_depth: value("search_depth") || "basic", include_domains: list("include_domains"), exclude_domains: list("exclude_domains"), enabled: $(`[data-category="enabled"]`, row)?.checked !== false };
  });
}

async function loadResearchSettings() {
  const state = $(`[data-settings-state]`);
  renderCategoryEditors(coreResearchCategories());
  setState(state, "loading", "Loading core discovery searches…");
  try {
    const payload = await api.request("/api/v1/research/settings");
    const settings = payload?.settings || {};
    const form = $(`[data-research-settings-form]`);
    ["max_candidates", "max_per_domain", "reddit_limit"].forEach((key) => { if (form?.elements[key] && settings[key] !== undefined) form.elements[key].value = settings[key]; });
    if (form?.elements.reddit_enabled) form.elements.reddit_enabled.checked = settings.reddit_enabled !== false;
    setState(state, "completed", "The 3 core searches are ready. Saved limits and Reddit controls were restored.");
  } catch (error) { setState(state, "failed", "Settings could not be loaded.", error.message); showGlobalError(error); }
}

async function saveResearchSettings(form) {
  const state = $(`[data-settings-state]`);
  setState(state, "running", "Saving discovery controls…");
  try {
    const payload = { categories: readCategoryEditors(), max_candidates: Number(form.elements.max_candidates.value), max_per_domain: Number(form.elements.max_per_domain.value), reddit_limit: Number(form.elements.reddit_limit.value), reddit_enabled: form.elements.reddit_enabled.checked };
    await api.request("/api/v1/research/settings", { method: "PUT", body: { settings: payload } });
    setState(state, "completed", "Discovery controls saved.");
  } catch (error) { setState(state, "failed", error.message); showGlobalError(error); }
}

async function loadCountryQueue() {
  const body = $(`[data-country-queue-body]`);
  if (!body) return;
  try {
    const payload = await api.request("/api/v1/research/country-queue");
    body.replaceChildren();
    (payload?.items || []).forEach((item) => { const row = document.createElement("tr"); row.innerHTML = `<th>${escapeHtml(item.country || item.iso3)} <small>${escapeHtml(item.iso3)}</small></th><td>${escapeHtml(item.last_attempt_at || "—")}</td><td>${escapeHtml(item.last_successful_finding_at || "—")}</td><td>${escapeHtml(item.next_eligible_at || "—")}</td><td><span class="state-pill">${escapeHtml(String(item.outcome || "—").replaceAll("_", " "))}</span></td>`; body.append(row); });
    if (!body.children.length) body.innerHTML = '<tr><td colspan="5">No country hunts have been queued.</td></tr>';
  } catch (error) { body.innerHTML = `<tr><td colspan="5">${escapeHtml(error.message)}</td></tr>`; showGlobalError(error); }
}

async function loadRunHistory() {
  const list = $(`[data-run-history]`);
  if (!list) return;
  try {
    const payload = await api.request("/api/v1/research/runs");
    list.replaceChildren();
    const runs = payload?.items || [];
    const entries = new Map();
    runs.forEach((run) => {
      const entry = document.createElement("div");
      entry.className = "run-history-entry";
      entry.dataset.runId = run.id;
      const button = document.createElement("button");
      button.className = "run-row";
      button.type = "button";
      button.setAttribute("aria-expanded", String(historicRunId === run.id));
      button.innerHTML = `<strong>Run ${escapeHtml(run.id)}</strong><span>${escapeHtml(run.status)} · ${escapeHtml(run.started_at || "")}</span>`;
      button.addEventListener("click", () => selectHistoricRun(run.id));
      entry.append(button);
      entries.set(run.id, entry);
      list.append(entry);
    });
    if (!list.children.length) list.innerHTML = '<p class="help">No research runs yet.</p>';
    const historicDetail = $(`[data-historic-run-detail]`);
    if (historicRunId && entries.has(historicRunId) && historicDetail) {
      entries.get(historicRunId).append(historicDetail);
      historicDetail.hidden = false;
      await loadRunDetail(historicRunId, "historic", { resetLogVisibility: false });
    } else {
      historicRunId = "";
      if (historicDetail) historicDetail.hidden = true;
    }
    if (runs.length) await loadRunDetail(runs[0].id, "current");
    scheduleRunHistoryRefresh(runs);
  } catch (error) { list.innerHTML = `<p class="help">${escapeHtml(error.message)}</p>`; showGlobalError(error); }
}

function scheduleRunHistoryRefresh(runs) {
  window.clearTimeout(runHistoryRefreshTimer);
  runHistoryRefreshTimer = null;
  if (!(runs || []).some((run) => ACTIVE_RESEARCH_STATES.has(run.status))) return;
  runHistoryRefreshTimer = window.setTimeout(() => { loadRunHistory(); }, RESEARCH_POLL_INTERVAL_MS);
}

function selectHistoricRun(runId) {
  historicRunId = runId;
  const list = $(`[data-run-history]`);
  const detail = $(`[data-historic-run-detail]`);
  const entry = $$(`[data-run-id]`, list).find((item) => item.dataset.runId === runId);
  if (entry && detail) {
    entry.append(detail);
    detail.hidden = false;
  }
  $$(`.run-row`, list).forEach((button) => button.setAttribute("aria-expanded", String(button.parentElement?.dataset.runId === runId)));
  loadRunDetail(runId, "historic");
}

async function stopActiveRun(button) {
  if (!button || button.disabled) return;
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "Finding active run…";
  try {
    const payload = await api.request("/api/v1/research/runs");
    const active = (payload?.items || []).find((run) => ACTIVE_RESEARCH_STATES.has(run.status));
    if (!active) { button.textContent = "No active run"; return; }
    button.textContent = "Stopping…";
    await api.request(`/api/v1/research/jobs/${encodeURIComponent(active.id)}/stop`, { method: "POST" });
    button.textContent = "Stop requested";
    await loadRunHistory();
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    showGlobalError(error);
  }
}

async function stopAllResearch(button) {
  if (!button || button.disabled) return;
  button.disabled = true;
  button.textContent = "Stopping research…";
  try {
    const payload = await api.request("/api/v1/research/runs");
    const active = (payload?.items || []).filter((run) => ACTIVE_RESEARCH_STATES.has(run.status));
    if (!active.length) { button.textContent = "No active research"; return; }
    await Promise.all(active.map((run) => api.request(`/api/v1/research/jobs/${encodeURIComponent(run.id)}/stop`, { method: "POST" })));
    button.textContent = `Stop requested (${active.length})`;
    await loadRunHistory();
  } catch (error) {
    button.disabled = false;
    button.textContent = "Stop all research";
    showGlobalError(error);
  }
}

function renderResearchRunDetail(run, { resetLogVisibility = false, target = "current" } = {}) {
  const detail = $(`[data-${target === "current" ? "current" : "historic"}-run-detail]`);
  if (!detail) return;
  detail.hidden = false;
  $(`[data-run-detail-title]`, detail).textContent = `Run ${run.id} · ${run.status || "running"}`;
  const events = run.events || run.progress?.logs || [];
  const activityEvents = events.filter((event) => event.event !== "llm_full");
  const llmEvents = events.filter((event) => event.event === "llm_full");
  const runCandidates = run.candidates || [];
  const confirmedIn = runCandidates.filter((candidate) => candidate.status === "complete" && Boolean(candidate.finding_id)).length;
  const confirmedOut = runCandidates.filter((candidate) => {
    if (candidate.status === "complete" && Boolean(candidate.finding_id)) return false;
    return FINAL_CANDIDATE_STATES.has(candidate.status) || candidate.status?.startsWith("excluded_");
  }).length;
  const underReview = runCandidates.length - confirmedIn - confirmedOut;
  const confirmedPercentage = runCandidates.length ? Math.round((confirmedIn / runCandidates.length) * 100) : 0;
  $(`[data-run-detail-summary]`, detail).textContent =
    `${runCandidates.length} candidates · ${confirmedIn} confirmed in · ${confirmedOut} confirmed out · ${underReview} under review · ${confirmedPercentage}% confirmed · ${activityEvents.length} activity log entr${activityEvents.length === 1 ? "y" : "ies"} · ${llmEvents.length} full LLM entr${llmEvents.length === 1 ? "y" : "ies"}.`;
  const stopButton = $(`[data-run-stop]`, detail);
  if (stopButton) {
    if (!stopButton.dataset.bound) {
      stopButton.dataset.bound = "true";
      stopButton.addEventListener("click", async () => {
        if (!stopButton.dataset.runId || stopButton.disabled) return;
        stopButton.disabled = true;
        stopButton.textContent = "Stopping…";
        try {
          await api.request(`/api/v1/research/jobs/${encodeURIComponent(stopButton.dataset.runId)}/stop`, { method: "POST" });
        } catch (error) {
          stopButton.disabled = false;
          stopButton.textContent = "Stop run";
          showGlobalError(error);
        }
      });
    }
    const terminal = ["complete", "completed", "completed_with_errors", "failed", "stopped", "cancelled", "interrupted"].includes(run.status);
    stopButton.dataset.runId = run.id || "";
    stopButton.hidden = terminal || !run.id;
    stopButton.disabled = run.status === "stopping" || stopButton.textContent === "Stopping…";
    if (!stopButton.disabled) stopButton.textContent = "Stop run";
  }
  const toggle = $(`[data-run-log-toggle]`, detail);
  if (toggle && !toggle.dataset.bound) {
    toggle.dataset.bound = "true";
    toggle.addEventListener("click", () => {
      const visible = toggle.getAttribute("aria-expanded") === "true";
      $(`[data-run-events]`, detail).hidden = visible;
      toggle.setAttribute("aria-expanded", String(!visible));
      toggle.textContent = visible ? "Show log" : "Hide log";
    });
  }
  if (resetLogVisibility && toggle) {
    $(`[data-run-events]`, detail).hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    toggle.textContent = "Show log";
  }
  const eventList = $(`[data-run-events]`, detail);
  eventList.replaceChildren();
  activityEvents.forEach((event) => {
    const p = document.createElement("p");
    const subject = event.message || event.error || JSON.stringify(event.outcomes || event);
    const url = event.url || "";
    const candidate = event.candidate_id ? ` · candidate #${event.candidate_id}` : "";
    p.append(document.createTextNode(`${event.at || event.created_at || ""} · ${event.event || event.stage || "progress"}${candidate} · ${subject}`));
    if (url) {
      p.append(document.createTextNode(" · "));
      const link = document.createElement("a");
      link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
      link.textContent = url;
      p.append(link);
    }
    eventList.append(p);
  });
  if (!eventList.children.length) {
    const p = document.createElement("p");
    p.textContent = "No log events have been recorded yet. The run is still working or has not reported progress.";
    eventList.append(p);
  }
  const llmEventList = $(`[data-run-llm-events]`, detail);
  if (llmEventList) {
    llmEventList.replaceChildren();
    llmEvents.forEach((event) => {
      const p = document.createElement("p");
      p.textContent = `${event.at || event.created_at || ""} · ${event.message || ""}`;
      llmEventList.append(p);
    });
    if (!llmEventList.children.length) {
      const p = document.createElement("p");
      p.textContent = "No LLM request has been sent yet.";
      llmEventList.append(p);
    }
  }
  const candidates = $(`[data-run-candidates]`, detail);
  candidates.replaceChildren();
  (run.candidates || []).forEach((candidate) => {
    const row = document.createElement("div"); row.className = "candidate-row-wrap";
    const button = document.createElement("button"); button.className = "candidate-row"; button.type = "button";
    const scope = candidate.scope_country_iso3 ? ` · scope ${candidate.scope_country || candidate.scope_country_iso3}` : "";
    const mismatch = candidate.scope_mismatch ? " · SCOPE MISMATCH" : "";
    const relevance = candidate.full_decision || candidate.summary_decision || "not reviewed";
    const geography = candidate.extracted_country || candidate.extracted_iso3 || "not checked";
    // A relevant model decision is not confirmation. A tick means the
    // article is durably present in the findings database.
    const confirmed = candidate.status === "complete" && Boolean(candidate.finding_id);
    const inProgress = ["reviewing_summary", "fetching", "searching_alternative", "reviewing_full_text", "extracting", "comparing"].includes(candidate.status);
    const iconState = confirmed ? "yes" : inProgress ? "unclear" : "no";
    const label = document.createElement("span");
    const icon = document.createElement("span");
    icon.className = `relevance-icon relevance-${iconState}`;
    icon.textContent = confirmed ? "✓" : inProgress ? "?" : "✕";
    icon.title = confirmed ? "Confirmed: stored in the database" : inProgress ? "Still being processed" : "Not confirmed in the database";
    label.append(icon, document.createTextNode(` #${candidate.id} · ${candidate.status} · ${relevance}${scope}${mismatch} · ${geography} · ${candidate.full_reason || candidate.summary_reason || ""}`));
    button.append(label);
    const url = candidate.url || candidate.loaded_url || candidate.canonical_url;
    if (url) { const link = document.createElement("a"); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = url; link.addEventListener("click", (event) => event.stopPropagation()); button.append(link); }
    button.addEventListener("click", () => loadCandidateDetail(candidate.id, target));
    const forceable = Boolean(candidate.url) && !["complete", "reviewing_summary", "fetching", "searching_alternative", "reviewing_full_text", "extracting", "comparing"].includes(candidate.status);
    if (forceable) {
      const force = document.createElement("button");
      force.className = "force-candidate-button"; force.type = "button"; force.textContent = "⚡";
      force.setAttribute("aria-label", `Force process candidate #${candidate.id}`);
      force.title = "Ignore duplicate, deterministic, and summary checks; send this URL to secondary review.";
      label.classList.add("has-force-action");
      force.addEventListener("click", async (event) => {
        event.stopPropagation();
        force.disabled = true; force.textContent = "…";
        try {
          const job = await api.request(`/api/v1/research/candidates/${encodeURIComponent(candidate.id)}/force-process`, { method: "POST" });
          force.textContent = "✓";
          force.title = `Queued forced run ${job.run_id || job.id || ""}`;
          if (job.run_id || job.id) await loadRunHistory();
        } catch (error) { force.disabled = false; force.textContent = "⚡"; showGlobalError(error); }
      });
      row.append(force);
    }
    row.append(button);
    candidates.append(row);
  });
  const copyText = (text, button, done) => copyToClipboard(text).then(() => showCopied(button, done)).catch(showGlobalError);
  const copyLog = $(`[data-run-copy-log]`, detail);
  const copyLlm = $(`[data-run-copy-llm]`, detail);
  const copyTable = $(`[data-run-copy-table]`, detail);
  const fullscreen = $(`[data-run-table-fullscreen]`, detail);
  if (copyLog) { copyLog.dataset.label ||= copyLog.textContent; copyLog.onclick = () => copyText(activityEvents.map((event) => `${event.at || ""} · ${event.event || "progress"} · ${event.message || event.error || ""}${event.url ? ` · ${event.url}` : ""}`).join("\n"), copyLog, "Copied log"); }
  if (copyLlm) { copyLlm.dataset.label ||= copyLlm.textContent; copyLlm.onclick = () => copyText(llmEvents.map((event) => `${event.at || ""} · ${event.message || ""}`).join("\n\n"), copyLlm, "Copied LLM log"); }
  if (copyTable && !copyTable.dataset.bound) { copyTable.dataset.bound = "true"; copyTable.dataset.label = copyTable.textContent; copyTable.addEventListener("click", () => copyText((run.candidates || []).map((candidate) => [candidate.id, candidate.status, candidate.full_decision || candidate.summary_decision || "", candidate.title || "", candidate.url || "", candidate.full_reason || candidate.summary_reason || ""].join("\t")).join("\n"), copyTable, "Copied table")); }
  if (fullscreen && !fullscreen.dataset.bound) { fullscreen.dataset.bound = "true"; fullscreen.addEventListener("click", () => candidates.requestFullscreen?.()); }
}

async function loadRunDetail(runId, target = "current", { resetLogVisibility = target === "historic" } = {}) {
  try {
    const run = await api.request(`/api/v1/research/jobs/${encodeURIComponent(runId)}`);
    renderResearchRunDetail(run, { resetLogVisibility, target });
  } catch (error) { showGlobalError(error); }
}

function researchPollUpdate(runId, current) {
  renderResearchRunDetail({ ...current, id: runId }, { target: "current" });
  return current.status === "complete" || current.status === "completed_with_errors" ? "completed" : current.status === "interrupted" || current.status === "failed" ? "failed" : "running";
}

async function resetResearchSettings(form) {
  const state = $(`[data-settings-state]`);
  const defaults = coreResearchSettings();
  renderCategoryEditors(defaults.categories);
  ["max_candidates", "max_per_domain", "reddit_limit"].forEach((key) => { if (form?.elements[key]) form.elements[key].value = defaults[key]; });
  if (form?.elements.reddit_enabled) form.elements.reddit_enabled.checked = defaults.reddit_enabled;
  setState(state, "running", "Restoring the core research defaults…");
  try {
    await api.request("/api/v1/research/settings", { method: "PUT", body: { settings: defaults } });
    setState(state, "completed", "Default research settings restored and saved.");
  } catch (error) { setState(state, "failed", "Defaults are shown locally but could not be saved.", error.message); showGlobalError(error); }
}

async function loadCandidateDetail(candidateId, target = "current") {
  const detail = $(`[data-${target === "current" ? "current" : "historic"}-run-detail] [data-candidate-detail]`);
  try {
    const candidate = await api.request(`/api/v1/research/candidates/${encodeURIComponent(candidateId)}`);
    const fields = candidate.details || {};
    const extraction = fields.extraction || {};
    const storage = fields.storage || {};
    detail.hidden = false;
    $(`[data-candidate-detail-title]`, detail).textContent = `Candidate #${candidate.id} · ${candidate.status}`;
    const body = $(`[data-candidate-detail-fields]`, detail); body.replaceChildren();
    const values = [
      ["Database confirmation", fields.finding_id ? `Confirmed as finding #${fields.finding_id}` : "Not stored in the findings database"],
      ["Outcome", candidate.status || "—"],
      ["Title", fields.title || "—"],
      ["Search result snippet", fields.snippet || "—"],
      ["URL", fields.url || candidate.url || fields.loaded_url || fields.canonical_url || "—"],
      ["Summary decision", fields.summary_decision || "—"], ["Summary reason", fields.summary_reason || "—"],
      ["Full decision", fields.full_decision || "—"], ["Full reason", fields.full_reason || "—"],
      ["Recovery attempts", JSON.stringify(fields.alternative_sources || fields.recovery_attempts || [])],
      ["Recovery error", fields.alternative_search_error || "—"], ["Scope", fields.scope_country_iso3 || fields.country_iso3 || "—"],
      ["Extracted geography", extraction.geography_iso3 || extraction.geography || "—"],
      ["Extraction status", storage.status || "—"],
      ["UN comparison / storage reason", storage.reason || "—"],
      ["Search context vs allocated country", fields.scope_mismatch ? "Different country — retained and allocated from article evidence" : "Same country or not applicable"], ["Error", fields.error || "—"],
    ];
    values.forEach(([label, value]) => {
      const dt = document.createElement("dt"); dt.textContent = label;
      const dd = document.createElement("dd");
      if (label === "URL" && value !== "—") {
        const link = document.createElement("a"); link.href = value; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = value; dd.append(link);
      } else dd.textContent = value;
      body.append(dt, dd);
    });
  } catch (error) { showGlobalError(error); }
}

async function runDiscovery(form) {
  // Polling was previously configured as maxAttempts: 60 (one minute).
  const state = $(`[data-settings-state]`); setState(state, "running", "Saving controls and queueing discovery…");
  let runId = "";
  let startedAt = 0;
  try {
    const payload = { categories: readCategoryEditors(), max_candidates: Number(form.elements.max_candidates.value), max_per_domain: Number(form.elements.max_per_domain.value), reddit_limit: Number(form.elements.reddit_limit.value), reddit_enabled: form.elements.reddit_enabled.checked };
    await api.request("/api/v1/research/settings", { method: "PUT", body: { settings: payload } });
    const job = await api.request("/api/v1/research/jobs", { method: "POST", body: { settings: payload } });
    runId = job.run_id || job.id; setState(state, "running", `Run ${runId} is running.`);
    startedAt = Date.now();
    await api.pollJob(`/api/v1/research/jobs/${runId}`, { intervalMs: RESEARCH_POLL_INTERVAL_MS, maxAttempts: RESEARCH_POLL_MAX_ATTEMPTS, onUpdate: (current) => { const currentState = researchPollUpdate(runId, current); setState(state, currentState, researchPollingStatus(current, startedAt)); } });
    await loadRunHistory();
  } catch (error) { if (runId && error.message.includes("timed out")) { await loadRunHistory(); await loadRunDetail(runId); setState(state, "running", "Status polling paused after 15 minutes; the run may still be working. Open its log or use Stop run.", error.message); } else { setState(state, "failed", error.message); showGlobalError(error); } }
}

async function submitCountryHunt(form, bulk = false) {
  const state = $(`[data-${bulk ? "gap" : "country-hunt"}-state]`);
  setState(state, "running", "Queueing country hunt…");
  let runId = "";
  let startedAt = 0;
  try {
    const values = Object.fromEntries(new FormData(form));
    const selected = bulk ? $$(`[data-gap-selection] input:checked`).map((input) => input.value) : [values.country_iso3];
    if (!selected.filter(Boolean).length) throw new Error("Choose at least one country.");
    const endpoint = bulk ? "/api/v1/research/bulk-country-hunts" : "/api/v1/research/country-hunts";
    const body = bulk ? { country_iso3s: selected, max_results: Number(values.max_results || 5) } : { country_iso3: selected[0], max_results: Number(values.max_results || 12), topic: values.topic || "general" };
    const job = await api.request(endpoint, { method: "POST", body });
    runId = job.run_id || job.id;
    setState(state, "running", `Run ${runId} is running.`);
    startedAt = Date.now();
    await api.pollJob(`/api/v1/research/jobs/${runId}`, { intervalMs: RESEARCH_POLL_INTERVAL_MS, maxAttempts: RESEARCH_POLL_MAX_ATTEMPTS, onUpdate: (current) => { const currentState = researchPollUpdate(runId, current); setState(state, currentState, researchPollingStatus(current, startedAt)); } });
    await Promise.all([loadCountryQueue(), loadRunHistory(), loadFindings()]);
    if (bulk) {
      window.location.hash = "findings";
      setState(state, "completed", "Batch complete. Results are open in the Findings table.");
    }
  } catch (error) { if (runId && error.message.includes("timed out")) { await loadRunHistory(); await loadRunDetail(runId); setState(state, "running", "Status polling paused after 15 minutes; the run may still be working. Open its log or use Stop run.", error.message); } else { setState(state, "failed", error.message); showGlobalError(error); } }
}

async function previewGaps(form) {
  const state = $(`[data-gap-state]`); setState(state, "loading", "Previewing country gaps…");
  try {
    const values = Object.fromEntries(new FormData(form));
    const query = new URLSearchParams({ prefix: values.prefix, days: values.days, country_count: values.country_count, start_at: values.start_at }).toString();
    const preview = await api.request(`/api/v1/research/country-gap-preview?${query}`);
    const panel = $(`[data-gap-preview]`); panel.hidden = false; $(`[data-gap-summary]`).textContent = `${preview.total_gaps} eligible gaps from ${preview.total_matches} matching countries; ${preview.remaining} remain after this batch.`;
    const selection = $(`[data-gap-selection]`); selection.replaceChildren();
    (preview.selected || []).forEach((item) => { const label = document.createElement("label"); label.className = "check-field"; label.innerHTML = `<input type="checkbox" value="${escapeHtml(item.iso3)}" checked> ${escapeHtml(item.name)} <small>${escapeHtml(item.iso3)}</small>`; selection.append(label); });
    (preview.excluded || []).forEach((item) => { const p = document.createElement("p"); p.className = "excluded-scope"; p.textContent = `${item.name} · ${item.iso3} excluded: ${item.reason}`; selection.append(p); });
    $(`[data-queue-batch]`).disabled = !(preview.selected || []).length; setState(state, "completed", "Gap preview ready.");
  } catch (error) { setState(state, "failed", error.message); showGlobalError(error); }
}

function setState(node, state, message, detail = "") {
  if (!node) return;
  node.dataset.state = state;
  const pill = $(".state-pill", node);
  if (pill) pill.textContent = state[0].toUpperCase() + state.slice(1);
  const text = $("[data-state-message]", node) || $("span:last-child", node);
  if (text && message) text.textContent = message;
  if (detail) node.title = detail;
}

function showGlobalError(error) {
  const notice = $(`[data-global-error]`);
  if (!notice) return;
  notice.hidden = false;
  notice.textContent = error instanceof ApiError ? `API error${error.status ? ` (${error.status})` : ""}: ${error.message}` : `Error: ${error.message}`;
}

function populateCountries(items) {
  countryChoices = items;
  $$(`[data-country-control]`).forEach((select) => {
    const previous = select.value;
    const findingsFilter = select.hasAttribute("data-findings-country");
    const coverageFilter = select.hasAttribute("data-coverage-country");
    const countryHunt = select.hasAttribute("data-country-hunt-country");
    const optionalContext = select.name === "country_iso3" && select.closest("[data-analysis-form], [data-research-form]");
    const graphCountry = select.hasAttribute("data-graph-country");
    select.replaceChildren();
    if (findingsFilter || coverageFilter) select.append(new Option("All countries", ""));
    else if (countryHunt || graphCountry) select.append(new Option("Choose a country", ""));
    else if (optionalContext) select.append(new Option("No country context", ""));
    items.forEach(({ iso3, name }) => select.append(new Option(`${name} · ${iso3}`, iso3)));
    if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  });
  const findingsCountry = $(`[data-findings-country]`);
  if (findingsCountry) {
    const previous = findingsCountry.value;
    findingsCountry.replaceChildren(new Option("All countries", ""));
    items.forEach(({ iso3, name }) => findingsCountry.append(new Option(`${name} · ${iso3}`, iso3)));
    if ([...findingsCountry.options].some((option) => option.value === previous)) findingsCountry.value = previous;
  }
  const overview = $(`[data-country-control]`);
  if (overview?.value) updateSelected(overview);
}

function updateSelected(select) {
  const option = select?.selectedOptions?.[0];
  $("[data-selected-country]").textContent = option?.value || "—";
  $("[data-selected-country-label]").textContent = option?.textContent || "Select a country below";
  if (select?.hasAttribute("data-graph-country") && option?.value) loadGraphs();
}

async function loadCountries() {
  const state = $(`[data-country-state]`);
  setState(state, "loading", "Loading country choices…");
  try {
    const payload = await api.request("/api/v1/countries");
    const items = Array.isArray(payload?.items) ? payload.items : [];
    populateCountries(items);
    // Coverage and countries load independently at startup; refresh this panel
    // once the full country register is available for its no-data comparison.
    if ($(`[data-coverage-body]`)) loadCoverage();
    $("[data-country-count]").textContent = String(items.length);
    if (!items.length) setState(state, "empty", "No country choices are available.");
    else {
      setState(state, "completed", `${items.length} country choices ready.`);
      updateSelected($(`[data-country-control]`));
      if ($(`[data-graph-country]`)?.value) loadGraphs();
    }
  } catch (error) {
    setState(state, "failed", "Country choices could not be loaded.", error.message);
    showGlobalError(error);
  }
}

async function checkHealth() {
  const card = $(`[data-health-card]`);
  try {
    const health = await api.request("/health");
    setState(card, "completed", "API is online");
    $("[data-health-detail]").textContent = `${health.version || "v1"} · ${health.environment || "unknown"}`;
    $("[data-health-label]").textContent = "Connected";
    $("[data-health-dot]").dataset.state = "completed";
  } catch (error) {
    setState(card, "failed", "API is unavailable", error.message);
    $("[data-health-label]").textContent = "Unavailable";
    $("[data-health-dot]").dataset.state = "failed";
    showGlobalError(error);
  }
}

async function submitJob(form, kind) {
  const state = $(`[data-${kind}-state]`);
  let lastAnalysisProgress = { logs: [] };
  setState(state, "running", "Queueing job…");
  const values = Object.fromEntries(new FormData(form));
  try {
    let job;
    if (kind === "analysis") {
      const rawUrl = String(values.url || "").trim();
      let parsed;
      try { parsed = new URL(rawUrl); } catch { throw new Error("Enter one complete HTTP(S) URL."); }
      if (!/^https?:$/.test(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password || /\s/.test(rawUrl) || (rawUrl.match(/:\/\//g) || []).length !== 1) throw new Error("Enter one complete HTTP(S) URL without credentials.");
      $(`[data-analysis-review]`).hidden = true;
      updateAnalysisLog({ logs: [] });
      job = await api.request("/api/v1/analysis/jobs", { method: "POST", body: { url: rawUrl, compare: form.compare.checked, review_before_store: form.review_before_store.checked } });
    }
    else job = await api.request("/api/v1/research/jobs", { method: "POST", body: { settings: { max_articles: Number(values.max_articles || 1), country_iso3: values.country_iso3 || null } } });
    setState(state, "running", `Job ${job.id || job.run_id || "queued"} is running.`);
    const id = job.id || job.run_id;
    const statusPath = kind === "analysis" ? `/api/v1/analysis/jobs/${id}` : `/api/v1/research/jobs/${id}`;
    // Poll every five seconds for up to three minutes. This leaves room for
    // the 20-second Requests attempt and 40-second Playwright fallback.
    await api.pollJob(statusPath, { intervalMs: 5000, maxAttempts: 36, onUpdate: (current) => {
      const currentState = current.status === "failed" ? "failed" : (current.status === "complete" || current.status === "completed" ? "completed" : "running");
      const statusMessage = current.status === "failed" ? (current.error || "Analysis failed.") : current.progress?.stage ? `Analysis stage: ${current.progress.stage}.` : `Job status: ${current.status}.`;
      setState(state, currentState, statusMessage);
      if (kind === "analysis") {
        lastAnalysisProgress = current.progress || { logs: current.logs || [] };
        updateAnalysisProgress(current.progress?.stage, current.progress?.fetch_status);
        updateAnalysisLog(lastAnalysisProgress, current.error);
      }
      if (kind === "analysis" && current.status === "complete") renderDraft(current.draft || draftFromResult(current));
    } });
  } catch (error) {
    setState(state, "failed", error.message);
    if (kind === "analysis") updateAnalysisLog(lastAnalysisProgress, error.message);
    showGlobalError(error);
  }
}

function draftFromResult(job) {
  const result = job?.result || {};
  if (!result.draft_id && !result.finding) return null;
  return { id: result.draft_id, job_id: job.id, status: result.draft_status || "pending_review", finding: result.finding || result.result || {}, comparison: result.comparison, un_data: result.un_data || [], validation: result.validation || {}, revision: 1 };
}

function updateAnalysisProgress(stage, fetchStatus) {
  const progress = $(`[data-analysis-progress]`);
  if (!progress) return;
  const stages = ["fetching", "extracting", "comparing", "complete"];
  const current = stages.indexOf(stage);
  $$(`[data-stage]`, progress).forEach((item, index) => {
    item.dataset.state = current > index ? "completed" : current === index ? "running" : "empty";
  });
  if (fetchStatus) progress.title = fetchStatus;
}

function updateAnalysisLog(progress = {}, error = "") {
  const list = $(`[data-analysis-log-list]`);
  const status = $(`[data-analysis-log-status]`);
  const copyButton = $(`[data-analysis-copy-log]`);
  const llmList = $(`[data-analysis-llm-log-list]`);
  const copyLlmButton = $(`[data-analysis-copy-llm]`);
  if (!list) return;
  list.replaceChildren();
  const entries = Array.isArray(progress.logs) ? progress.logs : [];
  const llmEntries = Array.isArray(progress.llm_logs) ? progress.llm_logs : [];
  entries.forEach((entry) => {
    const item = document.createElement("li");
    item.textContent = [entry.at, entry.message].filter(Boolean).join(" · ");
    list.append(item);
  });
  if (error) {
    const item = document.createElement("li");
    item.className = "log-error";
    item.textContent = `Error · ${error}`;
    list.append(item);
  }
  if (!list.children.length) {
    const item = document.createElement("li");
    item.className = "log-empty";
    item.textContent = "No activity yet.";
    list.append(item);
  }
  if (copyButton) {
    copyButton.onclick = async () => {
      const lines = entries.map((entry) => [entry.at, entry.message].filter(Boolean).join(" · "));
      if (error) lines.push(`Error · ${error}`);
      try {
        await copyToClipboard(lines.join("\n"));
        showCopied(copyButton, "Copied log");
      } catch (copyError) {
        showGlobalError(copyError);
      }
    };
  }
  if (llmList) {
    llmList.replaceChildren();
    llmEntries.forEach((entry) => {
      const item = document.createElement("li");
      item.textContent = [entry.at, entry.message].filter(Boolean).join(" · ");
      llmList.append(item);
    });
    if (!llmList.children.length) {
      const item = document.createElement("li");
      item.className = "log-empty";
      item.textContent = "No LLM request has been sent yet.";
      llmList.append(item);
    }
  }
  if (copyLlmButton) {
    copyLlmButton.onclick = () => copyToClipboard(
      llmEntries.map((entry) => [entry.at, entry.message].filter(Boolean).join(" · ")).join("\n\n")
    ).then(() => {
      showCopied(copyLlmButton, "Copied LLM log");
    }).catch(showGlobalError);
  }
  if (status) status.textContent = error ? "failed" : entries.length ? "live" : "waiting";
}

function renderDraft(draft) {
  if (!draft?.id) return;
  const review = $(`[data-analysis-review]`);
  if (!review) return;
  review.hidden = false;
  review.dataset.draftId = draft.id;
  review.dataset.revision = String(draft.revision || 1);
  $(`[data-draft-status]`, review).textContent = String(draft.status || "pending_review").replaceAll("_", " ");
  const finding = draft.finding || {};
  const stats = finding.statistics || {};
  const body = $(`[data-metrics-body]`, review);
  body.replaceChildren();
  Object.entries(stats).forEach(([metric, value]) => {
    const reportedValue = value?.value ?? value?.source_value;
    if (!value || reportedValue === null || reportedValue === undefined) return;
    const row = document.createElement("tr");
    const evidence = value.evidence_excerpt || "No excerpt supplied";
    row.innerHTML = `<th scope="row">${escapeHtml(metric.replaceAll("_", " "))}</th><td>${escapeHtml(String(reportedValue))}</td><td>${escapeHtml([value.unit, value.measured_period || finding.effective_date].filter(Boolean).join(" · "))}</td><td>${escapeHtml(evidence)}</td>`;
    body.append(row);
  });
  if (!body.children.length) body.innerHTML = '<tr><td colspan="4" class="empty-cell">No numeric metric was extracted. This draft can be rejected or rerun.</td></tr>';
  const referenceNote = draft.reference_finding_id ? `Existing finding #${draft.reference_finding_id} is already in the record. ` : "";
  $(`[data-review-summary]`, review).textContent = referenceNote + (finding.summary || finding.comments || "Review the evidence trail before choosing an action.");
  const attribution = $(`[data-attribution]`, review);
  attribution.replaceChildren();
  [["Source", finding.source || finding.site_seen || "—"], ["URL", finding.url || "—"], ["Published / effective", finding.effective_date || "Not stated"], ["Quoted source", finding.quoted_source || "None"]].forEach(([label, value]) => { const dt = document.createElement("dt"); dt.textContent = label; const dd = document.createElement("dd"); dd.textContent = value; attribution.append(dt, dd); });
  const extractionComment = $(`[data-extraction-comment]`, review);
  extractionComment.textContent = finding.comments || "None";
  const comparison = $(`[data-comparison]`, review);
  comparison.replaceChildren();
  const comparisonValues = draft.comparison || {};
  Object.entries(comparisonValues).filter(([, value]) => value && typeof value === "object" && (value.reported !== null || value.un_expected !== null || value.assessment)).forEach(([metric, value]) => { const p = document.createElement("p"); p.textContent = `${metric.replaceAll("_", " ")}: ${value.assessment || "compared"}${value.difference !== null && value.difference !== undefined ? ` · difference ${value.difference}` : ""}`; comparison.append(p); });
  if (!comparison.children.length) comparison.textContent = draft.un_data?.length ? "UN reference returned; no metric comparison was produced." : "No UN comparison available.";
  $(`[data-draft-editor]`, review).value = JSON.stringify(finding, null, 2);
  loadDraftActions(draft.id);
  const editable = draft.status === "pending_review";
  const referenceOnly = ["existing_record", "suppressed_source"].includes(draft.status);
  $$(`[data-draft-approve], [data-draft-save], [data-draft-reject]`, review).forEach((button) => { button.hidden = !editable; });
  $$(`[data-draft-remove], [data-draft-suppress]`, review).forEach((button) => { button.hidden = referenceOnly || !["pending_review", "approved"].includes(draft.status); });
  $(`[data-draft-rerun]`, review).hidden = ["removed", "suppressed"].includes(draft.status);
}

function escapeHtml(value) { const node = document.createElement("span"); node.textContent = value; return node.innerHTML; }

async function loadDraftActions(draftId) {
  const box = $(`[data-draft-actions]`);
  if (!box) return;
  box.replaceChildren();
  try {
    const payload = await api.request(`/api/v1/analysis/drafts/${encodeURIComponent(draftId)}/actions`);
    (payload.items || []).forEach((item) => {
      const event = document.createElement("p");
      event.textContent = `${item.acted_at || ""} · ${String(item.action || "action").replaceAll("_", " ")}${item.note ? ` · ${item.note}` : ""}`;
      box.append(event);
    });
    if (!box.children.length) box.textContent = "No draft actions recorded.";
  } catch (error) {
    box.textContent = `Draft action history unavailable: ${error.message}`;
  }
}

async function draftAction(action, body) {
  const review = $(`[data-analysis-review]`);
  const draftId = review?.dataset.draftId;
  if (!draftId) return;
  const state = $(`[data-draft-action-state]`);
  setState(state, "running", `${action}…`);
  try {
    const payload = await api.request(`/api/v1/analysis/drafts/${draftId}/${action}`, { method: "POST", body });
    if (action === "rerun") {
      const rerunId = payload.id || payload.job_id;
      if (!rerunId) throw new Error("Rerun did not return a job identifier.");
      await api.pollJob(`/api/v1/analysis/jobs/${rerunId}`, { intervalMs: 5000, maxAttempts: 36, onUpdate: (current) => {
        updateAnalysisProgress(current.progress?.stage, current.progress?.fetch_status);
        updateAnalysisLog(current.progress, current.error);
        setState(state, current.status === "failed" ? "failed" : current.status === "complete" ? "completed" : "running", current.status === "failed" ? (current.error || "Analysis failed.") : current.progress?.stage ? `Analysis stage: ${current.progress.stage}.` : `Job status: ${current.status}.`);
        if (current.status === "complete") renderDraft(current.draft || draftFromResult(current));
      }});
      setState(state, "completed", "Rerun complete; review the new draft.");
      return;
    }
    if (payload?.id) renderDraft(payload);
    setState(state, "completed", action === "approve" ? "Approved and stored." : `${action[0].toUpperCase()}${action.slice(1)} complete.`);
    if (["remove", "suppress", "reject"].includes(action)) renderDraft(payload);
    if (["approve", "remove", "suppress"].includes(action)) await loadGraphs({ force: true });
    await loadDraftActions(draftId);
  } catch (error) { setState(state, "failed", error.message); showGlobalError(error); }
}

async function saveDraftEdits() {
  const review = $(`[data-analysis-review]`);
  try {
    const finding = JSON.parse($(`[data-draft-editor]`, review).value);
    const payload = await api.request(`/api/v1/analysis/drafts/${review.dataset.draftId}`, { method: "PATCH", body: { finding, expected_revision: Number(review.dataset.revision) } });
    renderDraft(payload); setState($(`[data-draft-action-state]`), "completed", "Edits saved.");
  } catch (error) { setState($(`[data-draft-action-state]`), "failed", error.message); showGlobalError(error); }
}

const findingMetricLabels = {
  population: "Population", births: "Births", deaths: "Deaths",
  natural_change: "Natural change", net_migration: "Net migration",
  total_fertility_rate: "Total fertility rate",
};

const findingCoverageMetrics = [
  ["population", "Population"],
  ["births", "Births"],
  ["deaths", "Deaths"],
  ["natural_change", "Natural change"],
  ["migration_arrivals", "Migration arrivals"],
  ["migration_departures", "Migration departures"],
  ["net_overseas_migration", "Net migration"],
  ["total_fertility_rate", "Total fertility rate"],
];

function safeSourceLink(url, label = "Open source ↗") {
  try {
    const parsed = new URL(String(url || ""));
    if (!/^https?:$/.test(parsed.protocol) || parsed.username || parsed.password) return document.createTextNode("No safe source URL");
    const link = document.createElement("a"); link.href = parsed.href; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = label; return link;
  } catch { return document.createTextNode("No safe source URL"); }
}

const findingMetricFields = {
  population: "Population", births: "Births", deaths: "Deaths",
  natural_change: "Natural change", net_migration: "Net migration", total_fertility_rate: "TFR",
};

function findingMetricValues(item) {
  return (Array.isArray(item?.Metrics) ? item.Metrics : []).map((metric) => {
    const label = findingMetricFields[metric] || findingMetricLabels[metric] || metric.replaceAll("_", " ");
    const metricValues = item?.["Metric values"] || {};
    const value = metricValues[metric] ?? metricValues[metric === "net_migration" ? "net_overseas_migration" : metric]
      ?? item[metric === "total_fertility_rate" ? "TFR" : (metric === "net_migration" ? "Net migration" : findingMetricFields[metric])];
    return `${label}: ${value === null || value === undefined || value === "" ? "value not returned" : value}`;
  }) || [];
}

function findingSortValue(item, key) {
  if (key === "id") return Number(item.ID ?? item.id ?? 0);
  if (key === "country") return item.Country || item.ISO3 || "";
  if (key === "effective") return item["Effective date"] || "";
  if (key === "processed") return item["Processed date"] || item["Extracted at (UTC)"] || "";
  if (key === "metrics") return findingMetricValues(item).join(" · ");
  if (key === "source-class") return item["Source classification"] || "";
  if (key === "source") return item.Source || item["Quoted source"] || "";
  return "";
}

function sortedFindings(items) {
  const multiplier = findingsSort.direction === "ascending" ? 1 : -1;
  return [...items].sort((left, right) => {
    const leftValue = findingSortValue(left, findingsSort.key);
    const rightValue = findingSortValue(right, findingsSort.key);
    if (typeof leftValue === "number" && typeof rightValue === "number") return multiplier * (leftValue - rightValue);
    return multiplier * String(leftValue).localeCompare(String(rightValue), undefined, { numeric: true, sensitivity: "base" });
  });
}

function updateFindingsSortHeaders() {
  $$(`[data-findings-sort]`).forEach((button) => {
    const active = button.dataset.findingsSort === findingsSort.key;
    button.dataset.sortDirection = active ? findingsSort.direction : "";
    button.closest("th").setAttribute("aria-sort", active ? findingsSort.direction : "none");
  });
}

function appendFindingIdLink(cell, item) {
  const id = item.ID ?? item.id;
  const link = document.createElement("a");
  link.href = `#finding-edit?id=${encodeURIComponent(id)}`;
  link.textContent = `#${id}`;
  link.title = "Open finding record";
  link.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); openFindingEditor(id); });
  cell.append(link);
}

function renderFindingRows(body, items) {
  body.replaceChildren();
  (items || []).forEach((item) => {
    const row = document.createElement("tr");
    row.tabIndex = 0;
    row.innerHTML = `<td></td><th class="country-cell"></th><td>${escapeHtml(item["Effective date"] || "—")}</td><td>${escapeHtml(item["Processed date"] || item["Extracted at (UTC)"] || "—")}</td><td class="finding-metrics"></td><td>${escapeHtml(item["Source classification"] || "—")}</td><td>${escapeHtml(item.Source || item["Quoted source"] || "—")}</td><td class="source-cell"></td><td><button class="quiet-button row-edit-button" type="button">Edit</button></td>`;
    const countryLink = document.createElement("a");
    countryLink.href = `#graphs?iso3=${encodeURIComponent(item.ISO3 || "")}`;
    countryLink.textContent = item.Country || item.ISO3 || "—";
    countryLink.title = "Open graphs filtered to this country";
    countryLink.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); openGraphForCountry(item.ISO3); });
    row.querySelector(".country-cell").append(countryLink);
    const iso = document.createElement("small"); iso.textContent = item.ISO3 || ""; row.querySelector(".country-cell").append(iso);
    appendFindingIdLink(row.firstElementChild, item);
    const metricCell = row.querySelector(".finding-metrics");
    const metricValues = findingMetricValues(item);
    if (!metricValues.length) metricCell.textContent = "—";
    metricValues.forEach((value) => {
      const line = document.createElement("span");
      line.textContent = value;
      metricCell.append(line);
    });
    row.lastElementChild.append(safeSourceLink(item["Webpage URL"] || item["Canonical URL"] || item.url || item.canonical_url));
    row.querySelector(".row-edit-button").addEventListener("click", (event) => { event.stopPropagation(); openFindingEditor(item.ID ?? item.id); });
    body.append(row);
  });
  if (!body.children.length) body.innerHTML = '<tr><td colspan="9" class="empty-cell">No findings match the current filters.</td></tr>';
}

async function loadFindings() {
  const body = $(`[data-findings-body]`); if (!body) return;
  try {
    const query = new URLSearchParams(); const iso3 = $(`[data-findings-country]`)?.value; const metric = $(`[data-findings-metric]`)?.value;
    if (iso3) query.set("iso3", iso3); if (metric) query.set("metric", metric);
    const payload = await api.request(`/api/v1/findings${query.toString() ? `?${query}` : ""}`);
    renderFindingRows(body, sortedFindings(payload.items || []));
    updateFindingsSortHeaders();
    $(`[data-findings-status]`).textContent = `${(payload.items || []).length} finding${(payload.items || []).length === 1 ? "" : "s"} returned.`;
  } catch (error) { body.innerHTML = `<tr><td colspan="7">${escapeHtml(error.message)}</td></tr>`; showGlobalError(error); }
}

function renderGraphFindings(items, country) {
  const body = $(`[data-graph-findings-body]`);
  if (!body) return;
  renderFindingRows(body, items);
  const title = $(`[data-graph-findings-title]`);
  if (title) title.textContent = `${country} findings`;
  const status = $(`[data-graph-findings-status]`);
  if (status) status.textContent = `${items.length} finding${items.length === 1 ? "" : "s"} for ${country}. Select an ID or Edit to open the record.`;
}

function renderGraphFindingsError(error) {
  const body = $(`[data-graph-findings-body]`);
  if (body) body.innerHTML = `<tr><td colspan="9" class="empty-cell">${escapeHtml(error.message)}</td></tr>`;
  const status = $(`[data-graph-findings-status]`);
  if (status) status.textContent = "The country findings table could not be loaded.";
}

function renderClaimReviewRows(items) {
  const card = $(`[data-claims-review-card]`);
  const body = $(`[data-claims-body]`);
  if (!card || !body) return;
  card.hidden = false;
  body.replaceChildren();
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty-cell">No Phase 4 claims are available for this country.</td></tr>';
    return;
  }
  items.forEach((claim) => {
    const row = document.createElement("tr");
    if (claim.display_disposition === "rejected") row.className = "claim-rejected-row";
    const identity = document.createElement("td");
    identity.textContent = `#${claim.id} · ${claim.metric || "metric"}`;
    if (Number(claim.conflicting_cluster_count || 0) > 0) { const conflict = document.createElement("span"); conflict.className = "status-pill failed"; conflict.textContent = " conflict"; identity.append(conflict); }
    const value = document.createElement("td"); value.textContent = `${claim.value ?? "—"} · ${claim.observation_period || "period not specified"}`;
    const evidence = document.createElement("td"); evidence.textContent = `${claim.effective_points ?? 0} effective · ${claim.raw_points ?? 0} raw · ${claim.supporting_document_count ?? 0} source${Number(claim.supporting_document_count) === 1 ? "" : "s"}`;
    const classification = document.createElement("td");
    const classSelect = document.createElement("select"); classSelect.className = "claim-review-select";
    [["official_publisher", "Official publisher"], ["secondary_attributed", "Secondary · named source"], ["secondary_unattributed", "Secondary · unnamed source"], ["legacy_unreviewed", "Legacy · unreviewed"]].forEach(([valueOption, label]) => classSelect.append(new Option(label, valueOption)));
    classSelect.value = claim.source_classification || "secondary_unattributed"; classification.append(classSelect);
    const disposition = document.createElement("td");
    const dispositionSelect = document.createElement("select"); dispositionSelect.className = "claim-review-select";
    [["primary", "Primary"], ["approved_secondary", "Approved secondary"], ["rejected", "Rejected"]].forEach(([valueOption, label]) => dispositionSelect.append(new Option(label, valueOption)));
    dispositionSelect.value = claim.display_disposition || "approved_secondary"; disposition.append(dispositionSelect);
    const grouping = document.createElement("td");
    const groupingSelect = document.createElement("select"); groupingSelect.className = "claim-review-select"; groupingSelect.setAttribute("aria-label", `Grouping action for claim ${claim.id}`);
    [["", "No grouping change"], ["merge_equivalent", "Mark equivalent / rounded"], ["separate_definition", "Separate definition"]].forEach(([valueOption, label]) => groupingSelect.append(new Option(label, valueOption)));
    const peerIds = document.createElement("input"); peerIds.type = "text"; peerIds.placeholder = "Peer claim IDs"; peerIds.className = "claim-review-peer-ids"; peerIds.setAttribute("aria-label", `Peer claim IDs for claim ${claim.id}`);
    const definition = document.createElement("input"); definition.type = "text"; definition.placeholder = "Definition (if separate)"; definition.className = "claim-review-definition"; definition.setAttribute("aria-label", `Definition for claim ${claim.id}`);
    grouping.append(groupingSelect, peerIds, definition);
    const actions = document.createElement("td"); actions.className = "claim-review-actions";
    const points = document.createElement("input"); points.type = "number"; points.min = "0"; points.max = "16"; points.step = "1"; points.value = claim.manual_points ?? ""; points.placeholder = "points"; points.className = "claim-review-points"; points.setAttribute("aria-label", `Manual points for claim ${claim.id}`);
    const reason = document.createElement("input"); reason.type = "text"; reason.placeholder = "Reason (optional)"; reason.className = "claim-review-reason"; reason.setAttribute("aria-label", `Override reason for claim ${claim.id}`);
    const save = document.createElement("button"); save.type = "button"; save.className = "quiet-button"; save.textContent = "Save";
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        const selectedAction = groupingSelect.value || null;
        const peers = peerIds.value.split(",").map((value) => Number(value.trim())).filter((value) => Number.isInteger(value) && value > 0);
        if (selectedAction && !peers.length) throw new Error("Enter at least one peer claim ID for a grouping action.");
        const reasonText = reason.value.trim() || null;
        const body = { actor: "local", reason: reasonText };
        if (selectedAction) {
          body.action = selectedAction;
          body.peer_claim_ids = peers;
          if (selectedAction === "separate_definition" && definition.value.trim()) body.definition = definition.value.trim();
        } else {
          body.classification = classSelect.value;
          body.disposition = dispositionSelect.value;
          body.points = points.value === "" ? null : Number(points.value);
        }
        await api.request(`/api/v1/admin/claims/${encodeURIComponent(claim.id)}`, { method: "PUT", body });
        if ($(`[data-graph-country]`)?.value) await loadGraphs({ force: true });
      } catch (error) { showGlobalError(error); save.disabled = false; }
    });
    const undo = document.createElement("button"); undo.type = "button"; undo.className = "quiet-button"; undo.textContent = "Undo"; undo.disabled = claim.decision_origin !== "manual_override";
    undo.addEventListener("click", async () => {
      undo.disabled = true;
      try { await api.request(`/api/v1/admin/claims/${encodeURIComponent(claim.id)}/undo`, { method: "POST", body: { actor: "local", reason: "Reverted in graph review." } }); await loadGraphs({ force: true }); }
      catch (error) { showGlobalError(error); undo.disabled = false; }
    });
    actions.append(points, reason, save, undo);
    row.append(identity, value, evidence, classification, disposition, grouping, actions); body.append(row);
  });
  const status = $(`[data-claims-status]`); if (status) status.textContent = `${items.length} claim${items.length === 1 ? "" : "s"} returned. Conflicts are marked inline; overrides are audited and reversible.`;
}

async function loadClaimReview(iso3, includeRejected = false) {
  const card = $(`[data-claims-review-card]`);
  if (!card || !iso3 || fixtureMode) return;
  try {
    const include = includeRejected ? "&include_rejected=true" : "";
    const payload = await api.request(`/api/v1/claims?iso3=${encodeURIComponent(iso3)}&mode=all${include}`);
    renderClaimReviewRows(payload.items || []);
  } catch (error) {
    card.hidden = false;
    const status = $(`[data-claims-status]`); if (status) status.textContent = `Claims could not be loaded: ${error.message}`;
  }
}

async function loadCoverage() {
  const body = $(`[data-coverage-body]`); if (!body) return;
  const emptyBody = $(`[data-coverage-empty-body]`); const emptyCount = $(`[data-coverage-empty-count]`);
  try {
    const iso3 = $(`[data-coverage-country]`)?.value;
    const payload = await api.request(`/api/v1/admin/findings/coverage${iso3 ? `?iso3=${encodeURIComponent(iso3)}` : ""}`);
    const coverage = payload.items || [];
    body.replaceChildren();
    coverage.forEach((item) => { const row = document.createElement("tr"); row.innerHTML = `<th>${escapeHtml(item.country)} <small>${escapeHtml(item.iso3)}</small></th><td>${item.findings || 0}</td><td>${item.population || 0}</td><td>${item.births || 0}</td><td>${item.deaths || 0}</td><td>${item.natural_change || 0}</td><td>${item.net_migration || 0}</td><td>${item.total_fertility_rate || 0}</td>`; body.append(row); });
    if (!body.children.length) body.innerHTML = '<tr><td colspan="8" class="empty-cell">No coverage data.</td></tr>';

    const coverageByIso3 = new Map(coverage.map((item) => [item.iso3, item]));
    const countriesInView = iso3 ? countryChoices.filter((country) => country.iso3 === iso3) : countryChoices;
    const noData = countriesInView.filter((country) => {
      const item = coverageByIso3.get(country.iso3);
      return !item || ["population", "births", "deaths", "natural_change", "net_migration", "total_fertility_rate"].every((metric) => !Number(item[metric] || 0));
    });
    if (emptyCount) emptyCount.textContent = `${noData.length} countr${noData.length === 1 ? "y" : "ies"}`;
    if (emptyBody) {
      emptyBody.replaceChildren();
      noData.forEach((country) => { const item = document.createElement("li"); item.textContent = country.name; const code = document.createElement("small"); code.textContent = country.iso3; item.append(code); emptyBody.append(item); });
      if (!noData.length) emptyBody.innerHTML = '<li class="empty-cell">Every country in this view has at least one data point.</li>';
    }
  } catch (error) {
    body.innerHTML = `<tr><td colspan="8">${escapeHtml(error.message)}</td></tr>`;
    if (emptyCount) emptyCount.textContent = "Unavailable";
    if (emptyBody) emptyBody.innerHTML = `<li>${escapeHtml(error.message)}</li>`;
    showGlobalError(error);
  }
}

function renderPersistedComparison(container, finding) {
  container.replaceChildren();
  const comparison = finding?.comparison || finding?.wpp_comparison;
  if (!comparison || typeof comparison !== "object" || !Object.keys(comparison).length) {
    container.textContent = "No persisted WPP comparison on this finding.";
    return;
  }
  const heading = document.createElement("div");
  heading.className = "comparison-heading";
  heading.innerHTML = '<div><p class="kicker">Reference check</p><h4>Persisted WPP comparison</h4></div>';
  container.append(heading);

  const metadata = document.createElement("dl");
  metadata.className = "comparison-meta";
  [["Country", comparison.country], ["ISO3", comparison.country_iso3], ["Year", comparison.year], ["Overall assessment", comparison.overall_assessment]]
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .forEach(([label, value]) => {
      const term = document.createElement("dt"); const description = document.createElement("dd");
      term.textContent = label; description.textContent = String(value);
      metadata.append(term, description);
    });
  if (metadata.children.length) container.append(metadata);

  const tableWrap = document.createElement("div");
  tableWrap.className = "table-wrap comparison-table-wrap";
  const table = document.createElement("table");
  table.className = "comparison-table";
  table.innerHTML = "<thead><tr><th>Metric</th><th>Assessment</th><th>Reported</th><th>WPP reference</th><th>Difference</th><th>Difference %</th></tr></thead>";
  const body = document.createElement("tbody");
  const formatValue = (value) => value === null || value === undefined || value === "" ? "—" : typeof value === "number" ? value.toLocaleString(undefined, { maximumFractionDigits: 3 }) : String(value);
  Object.entries(comparison)
    .filter(([, value]) => value && typeof value === "object" && !Array.isArray(value)
      && (value.assessment || value.reported !== null && value.reported !== undefined
        || value.un_expected !== null && value.un_expected !== undefined
        || value.outlier_excluded || value.period_excluded))
    .forEach(([metric, value]) => {
      const row = document.createElement("tr");
      const label = findingMetricLabels[metric] || metric.replaceAll("_", " ");
      [label, value.assessment || "Not compared", formatValue(value.reported), formatValue(value.un_expected), formatValue(value.difference), formatValue(value.percentage_difference)]
        .forEach((cellValue, index) => {
          const cell = document.createElement(index === 0 ? "th" : "td");
          if (index === 0) cell.scope = "row";
          cell.textContent = cellValue;
          row.append(cell);
        });
      body.append(row);
    });
  if (body.children.length) {
    table.append(body); tableWrap.append(table); container.append(tableWrap);
  } else {
    const empty = document.createElement("p");
    empty.className = "comparison-notes";
    empty.textContent = "No metric values were available for comparison.";
    container.append(empty);
  }

  if (comparison.notes) {
    const notes = document.createElement("p");
    notes.className = "comparison-notes";
    notes.textContent = `Notes: ${comparison.notes}`;
    container.append(notes);
  }
}

async function loadFindingActions(findingId) {
  const box = $(`[data-record-actions]`);
  if (!box) return;
  box.replaceChildren();
  try {
    const payload = await api.request(`/api/v1/admin/finding-actions?finding_id=${encodeURIComponent(findingId)}`);
    (payload.items || []).forEach((item) => {
      const event = document.createElement("p");
      event.textContent = `${item.acted_at || ""} · ${String(item.action || "action").replaceAll("_", " ")}${item.note ? ` · ${item.note}` : ""}`;
      box.append(event);
    });
    if (!box.children.length) box.textContent = "No finding actions recorded.";
  } catch (error) {
    box.textContent = `Finding action history unavailable: ${error.message}`;
  }
}

async function loadRecord(findingId) {
  const editor = $(`[data-record-editor]`); const state = $(`[data-record-state]`); setState(state, "loading", `Loading finding #${findingId}…`);
  try {
    const finding = await api.request(`/api/v1/admin/findings/${encodeURIComponent(findingId)}`);
    editor.hidden = false; editor.dataset.findingId = String(findingId);
    $(`[data-record-title]`, editor).textContent = `Finding #${findingId} · ${finding.source_classification || finding.source || "stored record"}`;
    const countryLink = $(`[data-record-country]`, editor);
    const country = finding.geography || finding.country || finding.geography_iso3 || finding.iso3;
    const iso3 = finding.geography_iso3 || finding.iso3;
    if (country && iso3) {
      countryLink.hidden = false;
      countryLink.href = `#graphs?iso3=${encodeURIComponent(iso3)}`;
      countryLink.textContent = `${country} · View visualisation →`;
      countryLink.title = `Open the ${country} visualisation`;
      countryLink.onclick = (event) => { event.preventDefault(); openGraphForCountry(iso3); };
    } else {
      countryLink.hidden = true;
      countryLink.removeAttribute("href");
      countryLink.textContent = "";
      countryLink.onclick = null;
    }
    $(`[data-record-json]`, editor).value = JSON.stringify(finding, null, 2);
    const source = $(`[data-record-source]`, editor); source.replaceChildren();
    source.append(safeSourceLink(finding.url), document.createTextNode(` · ${finding.url || "No source URL"}`));
    const metrics = $(`[data-record-metrics]`, editor); metrics.replaceChildren();
    const statistics = finding.statistics || {};
    findingCoverageMetrics.forEach(([metric, label]) => {
      const value = statistics[metric];
      const reportedValue = value?.value ?? value?.source_value;
      const present = reportedValue !== null && reportedValue !== undefined && reportedValue !== "";
      const row = document.createElement("tr");
      const period = value?.measured_period || value?.time_period || value?.cadence || "—";
      const displayValue = present ? `${reportedValue}${value?.unit ? ` ${value.unit}` : ""}` : "—";
      row.innerHTML = `<th scope="row">${escapeHtml(label)}</th><td><span class="metric-state metric-state-${present ? "present" : "na"}">${present ? "Present" : "NA"}</span></td><td>${escapeHtml(String(displayValue))}</td><td>${escapeHtml(String(period))}</td>`;
      metrics.append(row);
    });
    renderPersistedComparison($(`[data-record-comparison]`, editor), finding);
    setState(state, "completed", "Record loaded. Review JSON before saving.");
    await loadFindingActions(findingId);
  } catch (error) { setState(state, "failed", error.message); showGlobalError(error); }
}

async function recordMutation(action) {
  const editor = $(`[data-record-editor]`); const findingId = editor?.dataset.findingId; const state = $(`[data-record-state]`);
  if (!findingId) { setState(state, "failed", "Choose a finding first."); return; }
  if (editor.dataset.mutationBusy === "true") return;
  let finding = {};
  try { finding = JSON.parse($(`[data-record-json]`, editor).value); } catch { /* the save path reports malformed JSON below */ }
  if (["delete", "block", "rerun", "delete-metric"].includes(action)) {
    const metric = $(`[data-record-metric]`)?.value;
    if (action === "delete-metric" && !metric) { setState(state, "failed", "Choose a metric first."); return; }
    const url = finding.url || finding.canonical_url || "No canonical URL available";
    const description = action === "delete-metric" ? `delete metric “${metric}” from` : action === "block" ? "delete and block" : action === "rerun" ? "remove and allow rerun for" : "delete";
    if (!window.confirm(`Confirm: ${description} finding #${findingId}?\nSource: ${url}`)) return;
  }
  editor.dataset.mutationBusy = "true";
  $$(`[data-record-action]`, editor).forEach((button) => { button.disabled = true; });
  setState(state, "running", `${action}…`);
  try {
    let path = `/api/v1/admin/findings/${findingId}`; let method = "PUT"; let body = { finding };
    if (action === "delete") { method = "DELETE"; body = undefined; }
    if (action === "block") { path += "/delete-and-block"; method = "POST"; body = undefined; }
    if (action === "rerun") { path += "/rerun"; method = "POST"; body = undefined; }
    if (action === "delete-metric") { path += "/delete-metric"; method = "POST"; body = { metric: $(`[data-record-metric]`).value }; }
    const result = await api.request(path, { method, body });
    setState(state, "completed", result.status ? `${action} complete: ${String(result.status).replaceAll("_", " ")}.` : `${action} complete.`);
    if (["delete", "block", "rerun", "delete-metric"].includes(action)) {
      editor.hidden = action === "delete-metric" ? false : true;
      await Promise.all([loadFindings(), loadCoverage(), loadBlockedSources(), loadGraphs({ force: true })]);
    }
    if (action === "save" || action === "delete-metric") { await loadRecord(findingId); if (action === "save") await loadGraphs({ force: true }); }
  } catch (error) { setState(state, "failed", error.message); showGlobalError(error); }
  finally { editor.dataset.mutationBusy = "false"; $$(`[data-record-action]`, editor).forEach((button) => { button.disabled = false; }); }
}

async function loadBlockedSources() { const body = $(`[data-blocked-body]`); if (!body) return; try { const payload = await api.request("/api/v1/admin/blocked-sources"); body.replaceChildren(); (payload.items || []).forEach((item) => { const row = document.createElement("tr"); row.innerHTML = `<td class="source-cell"></td><td>${escapeHtml(item.blocked_at || "—")}</td><td><button class="quiet-button" type="button">Unblock</button></td>`; row.firstElementChild.append(safeSourceLink(item.canonical_url, item.canonical_url)); row.lastElementChild.firstElementChild.addEventListener("click", async () => { try { await api.request("/api/v1/admin/blocked-sources/unblock", { method: "POST", body: { url: item.canonical_url } }); await loadBlockedSources(); } catch (error) { showGlobalError(error); } }); body.append(row); }); if (!body.children.length) body.innerHTML = '<tr><td colspan="3" class="empty-cell">No blocked source URLs.</td></tr>'; } catch (error) { body.innerHTML = `<tr><td colspan="3">${escapeHtml(error.message)}</td></tr>`; showGlobalError(error); } }

async function loadSourceRules() { const body = $(`[data-source-rules-body]`); if (!body) return; try { const payload = await api.request("/api/v1/admin/source-rules"); body.replaceChildren(); (payload.items || []).forEach((rule) => { const row = document.createElement("tr"); row.innerHTML = `<td>${escapeHtml(rule.match_type)}<br><code>${escapeHtml(rule.match_value)}</code></td><td>${escapeHtml(rule.action)}</td><td>${escapeHtml(rule.classification || "—")}</td><td>${rule.enabled ? "Enabled" : "Disabled"}</td><td><button class="quiet-button" type="button" data-rule-audit>View audit</button></td><td><button class="quiet-button" type="button" data-rule-edit>Edit</button><button class="quiet-button" type="button" data-rule-undo>Undo</button>${rule.enabled ? '<button class="quiet-button danger-button" type="button" data-rule-disable>Disable</button>' : ""}</td>`; row.querySelector("[data-rule-audit]").addEventListener("click", () => loadSourceRuleHistory(rule.id)); row.querySelector("[data-rule-edit]").addEventListener("click", () => { const form = $(`[data-source-rule-form]`); form.elements.match_type.value = rule.match_type; form.elements.match_value.value = rule.match_value; form.elements.action.value = rule.action; form.elements.classification.value = rule.classification || "official_publisher"; form.elements.note.value = rule.note || ""; form.scrollIntoView({ behavior: "smooth", block: "center" }); }); row.querySelector("[data-rule-undo]").addEventListener("click", async () => { try { await api.request(`/api/v1/admin/source-rules/${rule.id}/undo`, { method: "POST" }); await loadSourceRules(); } catch (error) { showGlobalError(error); } }); row.querySelector("[data-rule-disable]")?.addEventListener("click", async () => { try { await api.request(`/api/v1/admin/source-rules/${rule.id}`, { method: "DELETE" }); await loadSourceRules(); } catch (error) { showGlobalError(error); } }); body.append(row); }); if (!body.children.length) body.innerHTML = '<tr><td colspan="6" class="empty-cell">No source rules configured.</td></tr>'; } catch (error) { body.innerHTML = `<tr><td colspan="6">${escapeHtml(error.message)}</td></tr>`; showGlobalError(error); } }

async function loadSourceRuleHistory(ruleId) { try { const payload = await api.request(`/api/v1/admin/source-rule-actions?rule_id=${ruleId}`); const box = $(`[data-source-rule-history]`); box.replaceChildren(); (payload.items || []).forEach((item) => { const p = document.createElement("p"); p.textContent = `${item.acted_at} · ${item.action}${item.note ? ` · ${item.note}` : ""}`; box.append(p); }); if (!box.children.length) box.textContent = "No source-rule audit events."; } catch (error) { showGlobalError(error); } }

async function saveSourceRule(form) { try { const body = Object.fromEntries(new FormData(form)); body.enabled = true; if (body.action === "exclude") body.classification = null; await api.request("/api/v1/admin/source-rules", { method: "POST", body }); form.reset(); await loadSourceRules(); } catch (error) { showGlobalError(error); } }

async function refreshAllPanels() {
  const notice = $(`[data-global-error]`); if (notice) notice.hidden = true;
  const tasks = [checkHealth(), loadCountries(), loadResearchSettings(), loadCountryQueue(), loadRunHistory(), loadFindings(), loadCoverage(), loadBlockedSources(), loadSourceRules()];
  if ($(`[data-graph-country]`)?.value) tasks.push(loadGraphs({ force: true }));
  const results = await Promise.allSettled(tasks);
  if (results.some((result) => result.status === "rejected")) {
    showGlobalError(new Error("One or more panels could not be refreshed. Review the panel-level status messages."));
  }
}

function selectView(view) {
  const name = view || "overview";
  $$(`[data-view-panel]`).forEach((panel) => { const active = panel.dataset.viewPanel === name; panel.hidden = !active; panel.classList.toggle("active-view", active); });
  $$(`[data-view]`).forEach((link) => link.classList.toggle("active", link.dataset.view === name));
  const title = $(`[data-page-title]`);
  title.textContent = $(`[data-view="${name}"]`)?.textContent || (name === "finding-edit" ? "Edit finding" : "Overview");
}

function selectViewFromLocation() {
  const requested = window.location.hash.replace(/^#/, "").split("?", 1)[0];
  const known = requested && $(`[data-view-panel="${requested}"]`) ? requested : "overview";
  selectView(known);
  const locationParams = new URLSearchParams(window.location.hash.split("?", 2)[1] || "");
  if (known === "graphs" && locationParams.get("iso3")) {
    const select = $(`[data-graph-country]`);
    if (select) select.value = locationParams.get("iso3").toUpperCase();
    loadGraphs({ force: true });
  }
  if (known === "finding-edit" && locationParams.get("id")) loadRecord(locationParams.get("id"));
}

function selectResearchTab(name) {
  $$(`[data-research-tab]`).forEach((tab) => {
    const active = tab.dataset.researchTab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  $$(`[data-research-panel]`).forEach((panel) => {
    panel.hidden = panel.dataset.researchPanel !== name;
    panel.classList.toggle("active", panel.dataset.researchPanel === name);
  });
}

function wire() {
  if (fixtureMode) { const badge = $(`[data-mode-badge]`); badge.hidden = false; $("[data-mode-label]").textContent = "Fixtures"; }
  $$(`[data-view]`).forEach((link) => link.addEventListener("click", () => selectView(link.dataset.view)));
  $$(`[data-research-tab]`).forEach((tab) => tab.addEventListener("click", () => selectResearchTab(tab.dataset.researchTab)));
  window.addEventListener("hashchange", selectViewFromLocation);
  $$(`[data-country-control]`).forEach((select) => select.addEventListener("change", () => updateSelected(select)));
  $$(`[data-graph-metric], [data-graph-revision]`).forEach((input) => input.addEventListener("change", () => loadGraphs({ force: true })));
  $$(`[data-graph-mode]`).forEach((input) => input.addEventListener("change", () => {
    if (!input.checked) return;
    graphState.mode = input.value || GRAPH_MODES.show_all;
    const payload = graphState.payloadByCountry.get(graphState.currentCountry);
    if (payload) { renderGraphs(payload); renderGraphDataPointCount(payload, selectedGraphMetrics(), selectedGraphRevisions()); }
  }));
  $(`[data-claims-include-rejected]`)?.addEventListener("change", () => {
    const iso3 = $(`[data-graph-country]`)?.value || "";
    if (iso3) loadClaimReview(iso3, Boolean($(`[data-claims-include-rejected]`)?.checked));
  });
  $(`[data-load-graphs]`)?.addEventListener("click", () => loadGraphs({ force: true }));
  $(`[data-refresh-graphs]`)?.addEventListener("click", () => loadGraphs({ force: true }));
  $(`[data-analysis-form]`)?.addEventListener("submit", (event) => { event.preventDefault(); submitJob(event.currentTarget, "analysis"); });
  $(`[data-draft-approve]`)?.addEventListener("click", () => draftAction("approve"));
  $(`[data-draft-reject]`)?.addEventListener("click", () => draftAction("reject", { note: "Rejected during evidence review." }));
  $(`[data-draft-remove]`)?.addEventListener("click", () => draftAction("remove"));
  $(`[data-draft-suppress]`)?.addEventListener("click", () => draftAction("suppress"));
  $(`[data-draft-rerun]`)?.addEventListener("click", () => draftAction("rerun"));
  $(`[data-draft-save]`)?.addEventListener("click", saveDraftEdits);
  $(`[data-research-settings-form]`)?.addEventListener("submit", (event) => { event.preventDefault(); saveResearchSettings(event.currentTarget); });
  $(`[data-run-discovery]`)?.addEventListener("click", () => runDiscovery($(`[data-research-settings-form]`)));
  $(`[data-add-category]`)?.addEventListener("click", () => $(`[data-category-list]`)?.append(categoryEditor()));
  $(`[data-reset-research-settings]`)?.addEventListener("click", () => resetResearchSettings($(`[data-research-settings-form]`)));
  $(`[data-country-hunt-form]`)?.addEventListener("submit", (event) => { event.preventDefault(); submitCountryHunt(event.currentTarget); });
  $(`[data-stop-active-run]`)?.addEventListener("click", (event) => stopActiveRun(event.currentTarget));
  $(`[data-stop-all-research]`)?.addEventListener("click", (event) => stopAllResearch(event.currentTarget));
  $(`[data-gap-form]`)?.addEventListener("submit", (event) => { event.preventDefault(); previewGaps(event.currentTarget); });
  $(`[data-queue-batch]`)?.addEventListener("click", () => submitCountryHunt($(`[data-gap-form]`), true));
  $(`[data-refresh-queue]`)?.addEventListener("click", loadCountryQueue);
  $(`[data-refresh-runs]`)?.addEventListener("click", loadRunHistory);
  $(`[data-refresh-findings]`)?.addEventListener("click", loadFindings);
  $(`[data-refresh-coverage]`)?.addEventListener("click", loadCoverage);
  $$(`[data-findings-sort]`).forEach((button) => button.addEventListener("click", () => {
    const key = button.dataset.findingsSort;
    findingsSort = {
      key,
      direction: findingsSort.key === key && findingsSort.direction === "ascending" ? "descending" : "ascending",
    };
    loadFindings();
  }));
  $(`[data-record-refresh]`)?.addEventListener("click", () => {
    const findingId = $(`[data-record-editor]`)?.dataset.findingId;
    if (findingId) loadRecord(findingId);
  });
  $(`[data-findings-country]`)?.addEventListener("change", loadFindings);
  $(`[data-coverage-country]`)?.addEventListener("change", loadCoverage);
  $(`[data-findings-metric]`)?.addEventListener("change", loadFindings);
  $(`[data-record-save]`)?.addEventListener("click", () => recordMutation("save"));
  $(`[data-record-delete]`)?.addEventListener("click", () => recordMutation("delete"));
  $(`[data-record-block]`)?.addEventListener("click", () => recordMutation("block"));
  $(`[data-record-rerun]`)?.addEventListener("click", () => recordMutation("rerun"));
  $(`[data-record-delete-metric]`)?.addEventListener("click", () => recordMutation("delete-metric"));
  $(`[data-refresh-blocked]`)?.addEventListener("click", loadBlockedSources);
  $(`[data-refresh-source-rules]`)?.addEventListener("click", loadSourceRules);
  $(`[data-source-rule-form]`)?.addEventListener("submit", (event) => { event.preventDefault(); saveSourceRule(event.currentTarget); });
  $(`[data-refresh]`)?.addEventListener("click", refreshAllPanels);
}

document.addEventListener("DOMContentLoaded", async () => {
  wire();
  selectViewFromLocation();
  await Promise.all([checkHealth(), loadCountries(), loadResearchSettings(), loadCountryQueue(), loadRunHistory(), loadFindings(), loadCoverage(), loadBlockedSources(), loadSourceRules()]);
});
