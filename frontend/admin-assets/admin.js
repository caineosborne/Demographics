import { ApiError, createApiClient } from "./api-client.js";

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
};

const fixtureFetch = async (path, options = {}) => {
  const method = options.method || "GET";
  if (method !== "GET") {
    const fixturePath = path === "/api/v1/research/country-gap-preview"
      ? "/fixtures/api/country-gap-preview.json" : "/fixtures/api/worker-job.json";
    const fixture = await fetch(fixturePath);
    return new Response(await fixture.text(), {
      status: path === "/api/v1/research/country-gap-preview" ? 200 : 202,
      headers: { "Content-Type": "application/json" },
    });
  }
  const fixturePath = fixtureRoutes[path]
    || (/^\/api\/v1\/(?:analysis\/jobs|research\/jobs)\/[^/]+$/.test(path) ? "/fixtures/api/worker-job.json" : null);
  if (!fixturePath) {
    return new Response(JSON.stringify({ detail: `No fixture registered for ${path}` }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    });
  }
  const response = await fetch(fixturePath, { ...options, headers: { Accept: "application/json" } });
  if (!response.ok) return response;
  return response;
};

const api = createApiClient({ fetchImpl: fixtureMode ? fixtureFetch : window.fetch.bind(window) });

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
    const includeEmpty = select.name === "country_iso3" && select.closest("[data-analysis-form], [data-research-form]");
    select.replaceChildren();
    if (includeEmpty) select.append(new Option(select.closest("[data-research-form]") ? "All countries" : "No country context", ""));
    items.forEach(({ iso3, name }) => select.append(new Option(`${name} · ${iso3}`, iso3)));
    if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  });
  const overview = $(`[data-country-control]`);
  if (overview?.value) updateSelected(overview);
}

function updateSelected(select) {
  const option = select?.selectedOptions?.[0];
  $("[data-selected-country]").textContent = option?.value || "—";
  $("[data-selected-country-label]").textContent = option?.textContent || "Select a country below";
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
    else { setState(state, "completed", `${items.length} country choices ready.`); updateSelected($(`[data-country-control]`)); }
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
    if (kind === "analysis") job = await api.request("/api/v1/analysis/jobs", { method: "POST", body: { url: values.url, country_iso3: values.country_iso3 || null, compare: form.compare.checked } });
    else job = await api.request("/api/v1/research/jobs", { method: "POST", body: { settings: { max_articles: Number(values.max_articles || 1), country_iso3: values.country_iso3 || null } } });
    setState(state, "running", `Job ${job.id || job.run_id || "queued"} is running.`);
    const id = job.id || job.run_id;
    const statusPath = kind === "analysis" ? `/api/v1/analysis/jobs/${id}` : `/api/v1/research/jobs/${id}`;
    await api.pollJob(statusPath, { onUpdate: (current) => setState(state, current.status === "failed" ? "failed" : (current.status === "complete" || current.status === "completed" ? "completed" : "running"), `Job status: ${current.status}.`) });
  } catch (error) {
    setState(state, "failed", error.message);
    showGlobalError(error);
  }
}

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
  $(`[data-analysis-form]`)?.addEventListener("submit", (event) => { event.preventDefault(); submitJob(event.currentTarget, "analysis"); });
  $(`[data-research-form]`)?.addEventListener("submit", (event) => { event.preventDefault(); submitJob(event.currentTarget, "research"); });
  $(`[data-refresh]`)?.addEventListener("click", () => { $("[data-global-error]").hidden = true; checkHealth(); loadCountries(); });
}

document.addEventListener("DOMContentLoaded", async () => {
  wire();
  await Promise.all([checkHealth(), loadCountries()]);
});
