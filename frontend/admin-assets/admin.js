import { ApiError, createApiClient } from "./api-client.js";
import { GRAPH_METRICS, renderFindingChoices, renderMetricGraph } from "./graph-renderer.js";

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
  return response;
};

const api = createApiClient({ fetchImpl: fixtureMode ? fixtureFetch : window.fetch.bind(window) });

const graphState = {
  payloadByCountry: new Map(),
  hiddenByCountry: new Map(),
  currentCountry: "",
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
  selectView("findings");
  loadRecord(findingId);
}

function renderGraphs(payload) {
  const grid = $(`[data-graph-grid]`);
  const choices = $(`[data-graph-findings]`);
  if (!grid || !choices || !payload) return;
  const metrics = selectedGraphMetrics();
  const hidden = graphState.hiddenByCountry.get(payload.iso3) || new Set();
  grid.replaceChildren();
  metrics.forEach((metric) => renderMetricGraph(grid, payload, metric, { hiddenFindingIds: hidden, onFindingSelect: reviewGraphFinding }));
  renderFindingChoices(choices, payload, metrics, hidden, () => renderGraphs(payload), reviewGraphFinding);
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
  try {
    const payload = force || !graphState.payloadByCountry.has(iso3)
      ? await api.request(`/api/v1/graph-series/${encodeURIComponent(iso3)}?${query}`)
      : graphState.payloadByCountry.get(iso3);
    if (requestNumber !== graphState.requestNumber) return;
    graphState.payloadByCountry.set(iso3, payload);
    if (!graphState.hiddenByCountry.has(iso3)) graphState.hiddenByCountry.set(iso3, new Set());
    renderGraphs(payload);
    const findingCount = (payload.findings || []).length;
    const releaseCount = Object.keys(payload.alternate_releases || {}).length;
    $(`[data-graph-summary]`).textContent = `${payload.country || iso3}: ${payload.historic?.length || 0} WPP historical rows, ${payload.forecast?.length || 0} WPP forecast rows, ${findingCount} stored finding${findingCount === 1 ? "" : "s"}${releaseCount ? `, ${releaseCount} alternate release${releaseCount === 1 ? "" : "s"}` : ""}. Hidden points are browser-only; durable edits reload this series.`;
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

function readCategoryEditors() {
  return $$(`[data-category-row], .category-row`).map((row) => {
    const value = (key) => $(`[data-category="${key}"]`, row)?.value?.trim() || "";
    const list = (key) => value(key).split(",").map((item) => item.trim()).filter(Boolean);
    return { name: value("name"), query: value("query"), topic: "news", max_results: Number(value("max_results") || 10), time_range: value("time_range") || "day", search_depth: value("search_depth") || "basic", include_domains: list("include_domains"), exclude_domains: list("exclude_domains"), enabled: $(`[data-category="enabled"]`, row)?.checked !== false };
  });
}

async function loadResearchSettings() {
  const state = $(`[data-settings-state]`);
  setState(state, "loading", "Loading saved discovery controls…");
  try {
    const payload = await api.request("/api/v1/research/settings");
    const settings = payload?.settings || {};
    renderCategoryEditors(settings.categories || []);
    const form = $(`[data-research-settings-form]`);
    ["max_candidates", "max_per_domain", "reddit_limit"].forEach((key) => { if (form?.elements[key] && settings[key] !== undefined) form.elements[key].value = settings[key]; });
    if (form?.elements.reddit_enabled) form.elements.reddit_enabled.checked = settings.reddit_enabled !== false;
    setState(state, "completed", `${(settings.categories || []).length} news categories loaded.`);
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
    (payload?.items || []).forEach((run) => { const button = document.createElement("button"); button.className = "run-row"; button.type = "button"; button.innerHTML = `<strong>${escapeHtml(run.id)}</strong><span>${escapeHtml(run.status)} · ${escapeHtml(run.started_at || "")}</span>`; button.addEventListener("click", () => loadRunDetail(run.id)); list.append(button); });
    if (!list.children.length) list.innerHTML = '<p class="help">No research runs yet.</p>';
  } catch (error) { list.innerHTML = `<p class="help">${escapeHtml(error.message)}</p>`; showGlobalError(error); }
}

async function loadRunDetail(runId) {
  const detail = $(`[data-run-detail]`);
  try {
    const run = await api.request(`/api/v1/research/jobs/${encodeURIComponent(runId)}`);
    detail.hidden = false;
    $(`[data-run-detail-title]`, detail).textContent = `Run ${run.id} · ${run.status}`;
    $(`[data-run-detail-summary]`, detail).textContent = `${run.candidates?.length || 0} candidates · ${run.events?.length || 0} progress events. Scheduled and bulk paths use deterministic UN lookup only.`;
    const events = $(`[data-run-events]`, detail); events.replaceChildren(); (run.events || []).slice(-30).forEach((event) => { const p = document.createElement("p"); p.textContent = `${event.at || ""} · ${event.event || "progress"} · ${event.message || event.error || JSON.stringify(event.outcomes || {})}`; events.append(p); });
    const candidates = $(`[data-run-candidates]`, detail); candidates.replaceChildren(); (run.candidates || []).forEach((candidate) => { const button = document.createElement("button"); button.className = "candidate-row"; button.type = "button"; const scope = candidate.scope_country_iso3 ? ` · scope ${candidate.scope_country || candidate.scope_country_iso3}` : ""; const mismatch = candidate.scope_mismatch ? " · SCOPE MISMATCH" : ""; button.textContent = `#${candidate.id} · ${candidate.status}${scope}${mismatch} · ${candidate.full_reason || candidate.summary_reason || ""}`; button.addEventListener("click", () => loadCandidateDetail(candidate.id)); candidates.append(button); });
  } catch (error) { showGlobalError(error); }
}

async function loadCandidateDetail(candidateId) {
  const detail = $(`[data-candidate-detail]`);
  try {
    const candidate = await api.request(`/api/v1/research/candidates/${encodeURIComponent(candidateId)}`);
    const fields = candidate.details || {};
    const extraction = fields.extraction || {};
    detail.hidden = false;
    $(`[data-candidate-detail-title]`, detail).textContent = `Candidate #${candidate.id} · ${candidate.status}`;
    const body = $(`[data-candidate-detail-fields]`, detail); body.replaceChildren();
    const values = [
      ["Summary decision", fields.summary_decision || "—"], ["Summary reason", fields.summary_reason || "—"],
      ["Full decision", fields.full_decision || "—"], ["Full reason", fields.full_reason || "—"],
      ["Recovery attempts", JSON.stringify(fields.alternative_sources || fields.recovery_attempts || [])],
      ["Recovery error", fields.alternative_search_error || "—"], ["Scope", fields.scope_country_iso3 || fields.country_iso3 || "—"],
      ["Extracted geography", extraction.geography_iso3 || extraction.geography || "—"],
      ["Scope mismatch", fields.scope_mismatch ? "Yes — excluded from this hunt" : "No"], ["Error", fields.error || "—"],
    ];
    values.forEach(([label, value]) => { const dt = document.createElement("dt"); dt.textContent = label; const dd = document.createElement("dd"); dd.textContent = value; body.append(dt, dd); });
  } catch (error) { showGlobalError(error); }
}

async function runDiscovery(form) {
  const state = $(`[data-settings-state]`); setState(state, "running", "Saving controls and queueing discovery…");
  try {
    const payload = { categories: readCategoryEditors(), max_candidates: Number(form.elements.max_candidates.value), max_per_domain: Number(form.elements.max_per_domain.value), reddit_limit: Number(form.elements.reddit_limit.value), reddit_enabled: form.elements.reddit_enabled.checked };
    await api.request("/api/v1/research/settings", { method: "PUT", body: { settings: payload } });
    const job = await api.request("/api/v1/research/jobs", { method: "POST", body: { settings: payload } });
    const runId = job.run_id || job.id; setState(state, "running", `Run ${runId} is running.`);
    await api.pollJob(`/api/v1/research/jobs/${runId}`, { onUpdate: (current) => setState(state, current.status === "complete" || current.status === "completed_with_errors" ? "completed" : current.status === "interrupted" || current.status === "failed" ? "failed" : "running", `${current.status} · ${(current.events || []).length} durable events.`) });
    await loadRunHistory();
  } catch (error) { setState(state, "failed", error.message); showGlobalError(error); }
}

async function submitCountryHunt(form, bulk = false) {
  const state = $(`[data-${bulk ? "gap" : "country-hunt"}-state]`);
  setState(state, "running", "Queueing country hunt…");
  try {
    const values = Object.fromEntries(new FormData(form));
    const selected = bulk ? $$(`[data-gap-selection] input:checked`).map((input) => input.value) : [values.country_iso3];
    if (!selected.filter(Boolean).length) throw new Error("Choose at least one country.");
    const endpoint = bulk ? "/api/v1/research/bulk-country-hunts" : "/api/v1/research/country-hunts";
    const body = bulk ? { country_iso3s: selected, max_results: Number(values.max_results || 5) } : { country_iso3: selected[0], max_results: Number(values.max_results || 12) };
    const job = await api.request(endpoint, { method: "POST", body });
    const runId = job.run_id || job.id;
    setState(state, "running", `Run ${runId} is running.`);
    await api.pollJob(`/api/v1/research/jobs/${runId}`, { onUpdate: (current) => setState(state, current.status === "complete" || current.status === "completed_with_errors" ? "completed" : current.status === "interrupted" ? "failed" : "running", `${current.status} · ${(current.events || []).length} durable events.`) });
    await Promise.all([loadCountryQueue(), loadRunHistory()]);
  } catch (error) { setState(state, "failed", error.message); showGlobalError(error); }
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
  $$(`[data-country-control]`).forEach((select) => {
    const previous = select.value;
    const findingsFilter = select.hasAttribute("data-findings-country");
    const includeEmpty = findingsFilter || (select.name === "country_iso3" && select.closest("[data-analysis-form], [data-research-form]"));
    select.replaceChildren();
    if (includeEmpty) select.append(new Option(findingsFilter || select.closest("[data-research-form]") ? "All countries" : "No country context", ""));
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
    card.dataset.state = "completed";
    $("[data-health-card-label]").textContent = "API is online";
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
      job = await api.request("/api/v1/analysis/jobs", { method: "POST", body: { url: rawUrl, country_iso3: values.country_iso3 || null, compare: form.compare.checked } });
    }
    else job = await api.request("/api/v1/research/jobs", { method: "POST", body: { settings: { max_articles: Number(values.max_articles || 1), country_iso3: values.country_iso3 || null } } });
    setState(state, "running", `Job ${job.id || job.run_id || "queued"} is running.`);
    const id = job.id || job.run_id;
    const statusPath = kind === "analysis" ? `/api/v1/analysis/jobs/${id}` : `/api/v1/research/jobs/${id}`;
    await api.pollJob(statusPath, { onUpdate: (current) => {
      const currentState = current.status === "failed" ? "failed" : (current.status === "complete" || current.status === "completed" ? "completed" : "running");
      setState(state, currentState, current.progress?.stage ? `Analysis stage: ${current.progress.stage}.` : `Job status: ${current.status}.`);
      if (kind === "analysis") updateAnalysisProgress(current.progress?.stage, current.progress?.fetch_status);
      if (kind === "analysis" && current.status === "complete") renderDraft(current.draft || draftFromResult(current));
    } });
  } catch (error) {
    setState(state, "failed", error.message);
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
    if (!value || value.value === null || value.value === undefined) return;
    const row = document.createElement("tr");
    const evidence = value.evidence_excerpt || "No excerpt supplied";
    row.innerHTML = `<th scope="row">${escapeHtml(metric.replaceAll("_", " "))}</th><td>${escapeHtml(String(value.value))}</td><td>${escapeHtml([value.unit, value.measured_period || finding.effective_date].filter(Boolean).join(" · "))}</td><td>${escapeHtml(evidence)}</td>`;
    body.append(row);
  });
  if (!body.children.length) body.innerHTML = '<tr><td colspan="4" class="empty-cell">No numeric metric was extracted. This draft can be rejected or rerun.</td></tr>';
  const referenceNote = draft.reference_finding_id ? `Existing finding #${draft.reference_finding_id} is already in the record. ` : "";
  $(`[data-review-summary]`, review).textContent = referenceNote + (finding.summary || finding.comments || "Review the evidence trail before choosing an action.");
  const attribution = $(`[data-attribution]`, review);
  attribution.replaceChildren();
  [["Source", finding.source || finding.site_seen || "—"], ["URL", finding.url || "—"], ["Published / effective", finding.effective_date || "Not stated"], ["Quoted source", finding.quoted_source || "None"], ["Comments", finding.comments || "None"]].forEach(([label, value]) => { const dt = document.createElement("dt"); dt.textContent = label; const dd = document.createElement("dd"); dd.textContent = value; attribution.append(dt, dd); });
  const comparison = $(`[data-comparison]`, review);
  comparison.replaceChildren();
  const comparisonValues = draft.comparison || {};
  Object.entries(comparisonValues).filter(([, value]) => value && typeof value === "object" && (value.reported !== null || value.un_expected !== null || value.assessment)).forEach(([metric, value]) => { const p = document.createElement("p"); p.textContent = `${metric.replaceAll("_", " ")}: ${value.assessment || "compared"}${value.difference !== null && value.difference !== undefined ? ` · difference ${value.difference}` : ""}`; comparison.append(p); });
  if (!comparison.children.length) comparison.textContent = draft.un_data?.length ? "UN reference returned; no metric comparison was produced." : "No UN comparison available.";
  $(`[data-draft-editor]`, review).value = JSON.stringify(finding, null, 2);
  const editable = draft.status === "pending_review";
  const referenceOnly = ["existing_record", "suppressed_source"].includes(draft.status);
  $$(`[data-draft-approve], [data-draft-save], [data-draft-reject]`, review).forEach((button) => { button.hidden = !editable; });
  $$(`[data-draft-remove], [data-draft-suppress]`, review).forEach((button) => { button.hidden = referenceOnly || !["pending_review", "approved"].includes(draft.status); });
  $(`[data-draft-rerun]`, review).hidden = ["removed", "suppressed"].includes(draft.status);
}

function escapeHtml(value) { const node = document.createElement("span"); node.textContent = value; return node.innerHTML; }

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
      await api.pollJob(`/api/v1/analysis/jobs/${rerunId}`, { onUpdate: (current) => {
        updateAnalysisProgress(current.progress?.stage, current.progress?.fetch_status);
        setState(state, current.status === "failed" ? "failed" : current.status === "complete" ? "completed" : "running", current.progress?.stage ? `Analysis stage: ${current.progress.stage}.` : `Job status: ${current.status}.`);
        if (current.status === "complete") renderDraft(current.draft || draftFromResult(current));
      }});
      setState(state, "completed", "Rerun complete; review the new draft.");
      return;
    }
    if (payload?.id) renderDraft(payload);
    setState(state, "completed", action === "approve" ? "Approved and stored." : `${action[0].toUpperCase()}${action.slice(1)} complete.`);
    if (["remove", "suppress", "reject"].includes(action)) renderDraft(payload);
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

function safeSourceLink(url, label = "Open source ↗") {
  try {
    const parsed = new URL(String(url || ""));
    if (!/^https?:$/.test(parsed.protocol) || parsed.username || parsed.password) return document.createTextNode("No safe source URL");
    const link = document.createElement("a"); link.href = parsed.href; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = label; return link;
  } catch { return document.createTextNode("No safe source URL"); }
}

function findingMetrics(item) { return (Array.isArray(item?.Metrics) ? item.Metrics : []).map((metric) => findingMetricLabels[metric] || metric.replaceAll("_", " ")).join(", ") || "—"; }

async function loadFindings() {
  const body = $(`[data-findings-body]`); if (!body) return;
  try {
    const query = new URLSearchParams(); const iso3 = $(`[data-findings-country]`)?.value; const metric = $(`[data-findings-metric]`)?.value;
    if (iso3) query.set("iso3", iso3); if (metric) query.set("metric", metric);
    const payload = await api.request(`/api/v1/findings${query.toString() ? `?${query}` : ""}`); body.replaceChildren();
    (payload.items || []).forEach((item) => { const row = document.createElement("tr"); row.tabIndex = 0; row.innerHTML = `<td>${escapeHtml(String(item.ID))}</td><th>${escapeHtml(item.Country || item.ISO3 || "—")} <small>${escapeHtml(item.ISO3 || "")}</small></th><td>${escapeHtml(item["Effective date"] || "—")}</td><td>${escapeHtml(findingMetrics(item))}</td><td>${escapeHtml(item["Source classification"] || "—")}</td><td>${escapeHtml(item.Source || item["Quoted source"] || "—")}</td><td class="source-cell"></td>`; row.lastElementChild.append(safeSourceLink(item["Webpage URL"])); row.addEventListener("click", () => loadRecord(item.ID)); row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); loadRecord(item.ID); } }); body.append(row); });
    if (!body.children.length) body.innerHTML = '<tr><td colspan="7" class="empty-cell">No findings match the current filters.</td></tr>';
    $(`[data-findings-status]`).textContent = `${(payload.items || []).length} finding${(payload.items || []).length === 1 ? "" : "s"} returned.`;
  } catch (error) { body.innerHTML = `<tr><td colspan="7">${escapeHtml(error.message)}</td></tr>`; showGlobalError(error); }
}

async function loadCoverage() {
  const body = $(`[data-coverage-body]`); if (!body) return;
  try { const iso3 = $(`[data-findings-country]`)?.value; const payload = await api.request(`/api/v1/admin/findings/coverage${iso3 ? `?iso3=${encodeURIComponent(iso3)}` : ""}`); body.replaceChildren(); (payload.items || []).forEach((item) => { const row = document.createElement("tr"); row.innerHTML = `<th>${escapeHtml(item.country)} <small>${escapeHtml(item.iso3)}</small></th><td>${item.findings || 0}</td><td>${item.population || 0}</td><td>${item.births || 0}</td><td>${item.deaths || 0}</td><td>${item.natural_change || 0}</td><td>${item.net_migration || 0}</td><td>${item.total_fertility_rate || 0}</td>`; body.append(row); }); if (!body.children.length) body.innerHTML = '<tr><td colspan="8" class="empty-cell">No coverage data.</td></tr>'; } catch (error) { body.innerHTML = `<tr><td colspan="8">${escapeHtml(error.message)}</td></tr>`; showGlobalError(error); }
}

async function loadRecord(findingId) {
  const editor = $(`[data-record-editor]`); const state = $(`[data-record-state]`); setState(state, "loading", `Loading finding #${findingId}…`);
  try { const finding = await api.request(`/api/v1/admin/findings/${encodeURIComponent(findingId)}`); editor.hidden = false; editor.dataset.findingId = String(findingId); $(`[data-record-title]`, editor).textContent = `Finding #${findingId} · ${finding.source_classification || finding.source || "stored record"}`; $(`[data-record-json]`, editor).value = JSON.stringify(finding, null, 2); const source = $(`[data-record-source]`, editor); source.replaceChildren(); source.append(safeSourceLink(finding.url), document.createTextNode(` · ${finding.url || ""}`)); setState(state, "completed", "Record loaded. Review JSON before saving."); } catch (error) { setState(state, "failed", error.message); showGlobalError(error); }
}

async function recordMutation(action) {
  const editor = $(`[data-record-editor]`); const findingId = editor?.dataset.findingId; const state = $(`[data-record-state]`); if (!findingId) { setState(state, "failed", "Choose a finding first."); return; } setState(state, "running", `${action}…`);
  try { let path = `/api/v1/admin/findings/${findingId}`; let method = "PUT"; let body = { finding: JSON.parse($(`[data-record-json]`, editor).value) }; if (action === "delete") { method = "DELETE"; body = undefined; } if (action === "block") { path += "/delete-and-block"; method = "POST"; body = undefined; } if (action === "rerun") { path += "/rerun"; method = "POST"; body = undefined; } if (action === "delete-metric") { path += "/delete-metric"; method = "POST"; body = { metric: $(`[data-record-metric]`).value }; } const result = await api.request(path, { method, body }); setState(state, "completed", result.status ? `${action} complete: ${String(result.status).replaceAll("_", " ")}.` : `${action} complete.`); if (["delete", "block", "rerun", "delete-metric"].includes(action)) { editor.hidden = action === "delete-metric" ? false : true; await Promise.all([loadFindings(), loadCoverage(), loadBlockedSources(), loadGraphs({ force: true })]); } if (action === "save" || action === "delete-metric") { await loadRecord(findingId); if (action === "save") await loadGraphs({ force: true }); } } catch (error) { setState(state, "failed", error.message); showGlobalError(error); }
}

async function loadBlockedSources() { const body = $(`[data-blocked-body]`); if (!body) return; try { const payload = await api.request("/api/v1/admin/blocked-sources"); body.replaceChildren(); (payload.items || []).forEach((item) => { const row = document.createElement("tr"); row.innerHTML = `<td class="source-cell"></td><td>${escapeHtml(item.blocked_at || "—")}</td><td><button class="quiet-button" type="button">Unblock</button></td>`; row.firstElementChild.append(safeSourceLink(item.canonical_url, item.canonical_url)); row.lastElementChild.firstElementChild.addEventListener("click", async () => { try { await api.request("/api/v1/admin/blocked-sources/unblock", { method: "POST", body: { url: item.canonical_url } }); await loadBlockedSources(); } catch (error) { showGlobalError(error); } }); body.append(row); }); if (!body.children.length) body.innerHTML = '<tr><td colspan="3" class="empty-cell">No blocked source URLs.</td></tr>'; } catch (error) { body.innerHTML = `<tr><td colspan="3">${escapeHtml(error.message)}</td></tr>`; showGlobalError(error); } }

async function loadSourceRules() { const body = $(`[data-source-rules-body]`); if (!body) return; try { const payload = await api.request("/api/v1/admin/source-rules"); body.replaceChildren(); (payload.items || []).forEach((rule) => { const row = document.createElement("tr"); row.innerHTML = `<td>${escapeHtml(rule.match_type)}<br><code>${escapeHtml(rule.match_value)}</code></td><td>${escapeHtml(rule.action)}</td><td>${escapeHtml(rule.classification || "—")}</td><td>${rule.enabled ? "Enabled" : "Disabled"}</td><td><button class="quiet-button" type="button" data-rule-audit>View audit</button></td><td><button class="quiet-button" type="button" data-rule-edit>Edit</button><button class="quiet-button" type="button" data-rule-undo>Undo</button>${rule.enabled ? '<button class="quiet-button danger-button" type="button" data-rule-disable>Disable</button>' : ""}</td>`; row.querySelector("[data-rule-audit]").addEventListener("click", () => loadSourceRuleHistory(rule.id)); row.querySelector("[data-rule-edit]").addEventListener("click", () => { const form = $(`[data-source-rule-form]`); form.elements.match_type.value = rule.match_type; form.elements.match_value.value = rule.match_value; form.elements.action.value = rule.action; form.elements.classification.value = rule.classification || "official_publisher"; form.elements.note.value = rule.note || ""; form.scrollIntoView({ behavior: "smooth", block: "center" }); }); row.querySelector("[data-rule-undo]").addEventListener("click", async () => { try { await api.request(`/api/v1/admin/source-rules/${rule.id}/undo`, { method: "POST" }); await loadSourceRules(); } catch (error) { showGlobalError(error); } }); row.querySelector("[data-rule-disable]")?.addEventListener("click", async () => { try { await api.request(`/api/v1/admin/source-rules/${rule.id}`, { method: "DELETE" }); await loadSourceRules(); } catch (error) { showGlobalError(error); } }); body.append(row); }); if (!body.children.length) body.innerHTML = '<tr><td colspan="6" class="empty-cell">No source rules configured.</td></tr>'; } catch (error) { body.innerHTML = `<tr><td colspan="6">${escapeHtml(error.message)}</td></tr>`; showGlobalError(error); } }

async function loadSourceRuleHistory(ruleId) { try { const payload = await api.request(`/api/v1/admin/source-rule-actions?rule_id=${ruleId}`); const box = $(`[data-source-rule-history]`); box.replaceChildren(); (payload.items || []).forEach((item) => { const p = document.createElement("p"); p.textContent = `${item.acted_at} · ${item.action}${item.note ? ` · ${item.note}` : ""}`; box.append(p); }); if (!box.children.length) box.textContent = "No source-rule audit events."; } catch (error) { showGlobalError(error); } }

async function saveSourceRule(form) { try { const body = Object.fromEntries(new FormData(form)); body.enabled = true; if (body.action === "exclude") body.classification = null; await api.request("/api/v1/admin/source-rules", { method: "POST", body }); form.reset(); await loadSourceRules(); } catch (error) { showGlobalError(error); } }

function selectView(view) {
  const name = view || "overview";
  $$(`[data-view-panel]`).forEach((panel) => { const active = panel.dataset.viewPanel === name; panel.hidden = !active; panel.classList.toggle("active-view", active); });
  $$(`[data-view]`).forEach((link) => link.classList.toggle("active", link.dataset.view === name));
  const title = $(`[data-page-title]`);
  title.textContent = $(`[data-view="${name}"]`)?.textContent || "Overview";
}

function wire() {
  if (fixtureMode) { const badge = $(`[data-mode-badge]`); badge.hidden = false; $("[data-mode-label]").textContent = "Fixtures"; }
  $$(`[data-view]`).forEach((link) => link.addEventListener("click", () => selectView(link.dataset.view)));
  $$(`[data-country-control]`).forEach((select) => select.addEventListener("change", () => updateSelected(select)));
  $$(`[data-graph-metric], [data-graph-revision]`).forEach((input) => input.addEventListener("change", () => loadGraphs({ force: true })));
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
  $(`[data-country-hunt-form]`)?.addEventListener("submit", (event) => { event.preventDefault(); submitCountryHunt(event.currentTarget); });
  $(`[data-gap-form]`)?.addEventListener("submit", (event) => { event.preventDefault(); previewGaps(event.currentTarget); });
  $(`[data-queue-batch]`)?.addEventListener("click", () => submitCountryHunt($(`[data-gap-form]`), true));
  $(`[data-refresh-queue]`)?.addEventListener("click", loadCountryQueue);
  $(`[data-refresh-runs]`)?.addEventListener("click", loadRunHistory);
  $(`[data-refresh-findings]`)?.addEventListener("click", loadFindings);
  $(`[data-refresh-coverage]`)?.addEventListener("click", loadCoverage);
  $(`[data-findings-country]`)?.addEventListener("change", () => { loadFindings(); loadCoverage(); });
  $(`[data-findings-metric]`)?.addEventListener("change", loadFindings);
  $(`[data-record-save]`)?.addEventListener("click", () => recordMutation("save"));
  $(`[data-record-delete]`)?.addEventListener("click", () => recordMutation("delete"));
  $(`[data-record-block]`)?.addEventListener("click", () => recordMutation("block"));
  $(`[data-record-rerun]`)?.addEventListener("click", () => recordMutation("rerun"));
  $(`[data-record-delete-metric]`)?.addEventListener("click", () => recordMutation("delete-metric"));
  $(`[data-refresh-blocked]`)?.addEventListener("click", loadBlockedSources);
  $(`[data-refresh-source-rules]`)?.addEventListener("click", loadSourceRules);
  $(`[data-source-rule-form]`)?.addEventListener("submit", (event) => { event.preventDefault(); saveSourceRule(event.currentTarget); });
  $(`[data-refresh]`)?.addEventListener("click", () => { $("[data-global-error]").hidden = true; checkHealth(); loadCountries(); });
}

document.addEventListener("DOMContentLoaded", async () => {
  wire();
  await Promise.all([checkHealth(), loadCountries(), loadResearchSettings(), loadCountryQueue(), loadRunHistory(), loadFindings(), loadCoverage(), loadBlockedSources(), loadSourceRules()]);
});
