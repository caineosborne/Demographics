/**
 * Browser-side graph renderer for the versioned graph-series contract.
 *
 * This deliberately uses SVG instead of a network-loaded chart bundle. The
 * graph is an evidence view: values, dates, source classes, and finding IDs
 * stay inspectable in the DOM and remain usable on a narrow screen.
 */

export const GRAPH_METRICS = Object.freeze({
  population: { label: "Population", column: "Population 1 Jul", unit: "People", scale: 1000 },
  births: { label: "Births", column: "Total Births", unit: "People", scale: 1000 },
  deaths: { label: "Deaths", column: "Total Deaths", unit: "People", scale: 1000 },
  natural_change: { label: "Natural change", column: "Natural Change", unit: "People", scale: 1000 },
  net_migration: { label: "Net migration", column: "Net Migration", unit: "People", scale: 1000 },
  total_fertility_rate: {
    label: "Total fertility rate",
    column: "Total Fertility Rate (live births per woman)",
    unit: "Live births per woman",
    scale: 1,
  },
});

export const SOURCE_CLASS_LABELS = Object.freeze({
  official_publisher: "Official publisher",
  secondary_attributed: "Secondary · named source",
  secondary_unattributed: "Secondary · unnamed source",
  legacy_unreviewed: "Legacy · unreviewed",
});

const SOURCE_CLASS_COLORS = Object.freeze({
  official_publisher: "#167d73",
  secondary_attributed: "#b7791f",
  secondary_unattributed: "#6b7280",
  legacy_unreviewed: "#8a9690",
});

const SOURCE_CLASS_MARKERS = Object.freeze({
  official_publisher: "diamond",
  secondary_attributed: "circle-open",
  secondary_unattributed: "cross",
  // Legacy rows have no stronger provenance guarantee. The star is a neutral
  // historical marker so they cannot be mistaken for reviewed evidence.
  legacy_unreviewed: "star",
});

const WPP_REVISION_COLORS = Object.freeze({ 2022: "#6f4e9b", 2017: "#3a8d7d", 2012: "#8b6f47" });

const SVG_NS = "http://www.w3.org/2000/svg";

function svgElement(name, attributes = {}) {
  const element = document.createElementNS(SVG_NS, name);
  Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
  return element;
}

function number(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function articleStatistic(finding, metric) {
  const statistics = finding?.statistics || {};
  return statistics[metric] || (metric === "net_migration" ? statistics.net_overseas_migration || statistics.net_migration : null) || {};
}

/** Match the retained Gradio visibility safeguards for article points. */
export function findingMetricValue(finding, metric) {
  const statistic = articleStatistic(finding, metric);
  let value = number(statistic.value);
  if (value === null) return null;
  if (metric === "population") {
    const text = `${finding?.title || ""} ${finding?.summary || ""} ${statistic.evidence_excerpt || ""}`.toLowerCase();
    if (/(?:increase|decrease|change|gain|loss)\s+(?:of|by)\s+(?:approximately\s+)?[\d,.]+\s*(?:million|m)/.test(text)) return null;
    // Apply the subgroup guard to the clause containing the extracted number.
    // A later clause such as “including 5 million immigrants” must not hide a
    // valid national total from the earlier clause.
    const normalizedValue = value < 10000 && /\bmillions?\b/.test(text) ? value * 1000000 : value;
    const numeric = text.match(/(?<![\w])([+-]?\d[\d,]*(?:\.\d+)?)\s*(billion|bn|b|million|mn|m|thousand|k)?\b/gi) || [];
    const target = numeric.find((token) => {
      const raw = token.replace(/,/g, "").match(/([+-]?\d+(?:\.\d+)?)/);
      if (!raw) return false;
      const multiplier = /billion|bn|\bb\b/i.test(token) ? 1e9 : /million|mn|\bm\b/i.test(token) ? 1e6 : /thousand|\bk\b/i.test(token) ? 1e3 : 1;
      return Math.abs(Number(raw[1]) * multiplier - normalizedValue) <= Math.max(1e-6, Math.abs(normalizedValue) * 0.005);
    });
    if (target) {
      const targetIndex = text.indexOf(target);
      const sentenceStart = Math.max(text.lastIndexOf(".", targetIndex), text.lastIndexOf("!", targetIndex), text.lastIndexOf("?", targetIndex), text.lastIndexOf("\n", targetIndex)) + 1;
      const sentenceEndCandidates = [".", "!", "?", "\n"].map((mark) => text.indexOf(mark, targetIndex + target.length)).filter((index) => index >= 0);
      const sentence = text.slice(sentenceStart, sentenceEndCandidates.length ? Math.min(...sentenceEndCandidates) : text.length);
      const offset = targetIndex - sentenceStart;
      const splits = [...sentence.matchAll(/(?:(?:,(?!\d)|;(?!\d))|\bincluding\b|\bof\s+whom\b|\bamong\s+them\b)/gi)];
      const before = splits.filter((split) => split.index < offset).at(-1);
      const after = splits.find((split) => split.index >= offset);
      const claim = sentence.slice(before ? before.index + before[0].length : 0, after ? after.index : sentence.length);
      if (/(?:\bsubset\b|\bsubgroup\b|\bmigration\s+background\b|\bforeign[- ]born\b|\brefugee(?:s)?\b|\basylum\b|\bvisa\s+(?:holder|holders|application|applications|grant|grants)\b|\bimmigrant(?:s)?\s+from\b|\bimmigrant(?:s)?\b|\bmigrant(?:s)?\b|\b(?:people|persons|individuals|residents?)\s+(?:living\s+with|diagnosed\s+with|affected\s+by|suffering\s+from|with)\s+\w+|\b\w+\s+patients?\b|\bpatients?\s+with\b|\b(?:patient|disease|condition)\s+cohort(?:s)?\b|\b(?:people|persons|individuals)\s+with\s+\w+|\bpeople\s+from\b|\bby\s+(?:age|cause|sex|gender|religion|origin)\b|\bunder\s+\d+\b|\baged\s+\d+(?:\s+and\s+over)?\b|\bage\s+\d+\b|\b(?:citizenship|nationality|citizens?|non[- ]citizens?|foreign nationals?)\b)/i.test(claim)) return null;
    } else if (/(?:\b(?:immigrant|migrant|foreign[- ]born|refugee|asylum|visa holder|patient|patients|disease|subgroup|subset)\b|\bpeople\s+(?:living\s+with|with)\b|\bresidents?\s+(?:living\s+with|with)\b)/i.test(text)
               && !/\b(?:total|national|whole|entire)\s+population\b/i.test(text)) {
      // Legacy rows may omit evidence excerpts. In that case only suppress a
      // clearly subgroup-labelled record when there is no explicit total
      // population qualifier to anchor it.
      return null;
    }
    if (value < 10000 && /\bmillions?\b/.test(text)) value *= 1000000;
  }
  return value;
}

/** Return a decimal year for the exact July 1 WPP observation date. */
export function observationYearPosition(value) {
  const year = number(value);
  if (year === null) return null;
  const start = Date.UTC(year, 0, 1);
  const july = Date.UTC(year, 6, 1);
  const next = Date.UTC(year + 1, 0, 1);
  return year + (july - start) / (next - start);
}

/** Return a decimal year while retaining an article's actual effective date. */
export function dateYearPosition(value) {
  const match = String(value || "").match(/^(\d{4})(?:-(\d{1,2})-(\d{1,2}))?/);
  if (!match) return null;
  const year = Number(match[1]);
  if (!match[2]) return observationYearPosition(year);
  const start = Date.UTC(year, 0, 1);
  const date = Date.UTC(year, Number(match[2]) - 1, Number(match[3]));
  const next = Date.UTC(year + 1, 0, 1);
  return year + (date - start) / (next - start);
}

function rowPoints(rows, metric) {
  const config = GRAPH_METRICS[metric];
  return (rows || []).flatMap((row) => {
    const year = observationYearPosition(row.Year);
    const value = number(row[config.column]);
    return year === null || value === null ? [] : [{ x: year, y: value * config.scale, row }];
  });
}

function findingPoints(findings, metric, hiddenFindingIds) {
  return (findings || []).flatMap((item) => {
    const id = Number(item.id);
    const x = dateYearPosition(item.effective_date);
    const y = findingMetricValue(item.finding || {}, metric);
    if (!Number.isFinite(id) || hiddenFindingIds.has(id) || x === null || y === null) return [];
    return [{ x, y, id, item }];
  });
}

function formatValue(value, metric) {
  return GRAPH_METRICS[metric].scale === 1
    ? value.toLocaleString(undefined, { maximumFractionDigits: 2 })
    : Math.round(value).toLocaleString();
}

function addTooltipInteractions(node, chart, details) {
  const tooltip = chart.querySelector(".graph-tooltip");
  if (!tooltip) return;
  const show = (event) => {
    tooltip.textContent = details;
    tooltip.hidden = false;
    const chartBox = chart.getBoundingClientRect();
    const nodeBox = event?.currentTarget?.getBoundingClientRect();
    const x = event?.clientX ?? (nodeBox ? nodeBox.left + nodeBox.width / 2 : chartBox.left + chartBox.width / 2);
    const y = event?.clientY ?? (nodeBox ? nodeBox.top : chartBox.top + 20);
    const left = x - chartBox.left + 12;
    const top = y - chartBox.top - tooltip.offsetHeight - 10;
    tooltip.style.left = `${Math.min(Math.max(8, left), Math.max(8, chart.clientWidth - tooltip.offsetWidth - 8))}px`;
    tooltip.style.top = `${Math.max(8, top)}px`;
  };
  node.addEventListener("mouseenter", show);
  node.addEventListener("mouseleave", () => { tooltip.hidden = true; });
  node.addEventListener("focus", show);
  node.addEventListener("blur", () => { tooltip.hidden = true; });
}

function findingTooltipDetails(point, metric) {
  const finding = point.item.finding || {};
  const statistic = articleStatistic(finding, metric);
  const sourceName = finding.source || finding.quoted_source || "Source not named";
  const period = statistic.time_period || statistic.measured_period || point.item.effective_date || "Period not specified";
  const classification = SOURCE_CLASS_LABELS[point.item.source_type] || "Secondary · unnamed source";
  return `Finding #${point.id}\nMetric: ${GRAPH_METRICS[metric].label}\nValue: ${formatValue(point.y, metric)}\nEffective date: ${point.item.effective_date || "No effective date"}\nPeriod: ${period}\nSource: ${sourceName}\nClassification: ${classification}${finding.url || point.item.source_url ? `\nURL: ${finding.url || point.item.source_url}` : ""}`;
}

function marker(chart, point, markerType, color, metric) {
  const finding = point.item.finding || {};
  const statistic = articleStatistic(finding, metric);
  const sourceName = finding.source || finding.quoted_source || "Source not named";
  const period = statistic.time_period || statistic.measured_period || point.item.effective_date || "Period not specified";
  const classification = SOURCE_CLASS_LABELS[point.item.source_type] || "Secondary · unnamed source";
  const evidenceLabel = `Finding ${point.id} · ${sourceName} · ${classification} · ${point.item.effective_date || "No effective date"} · ${period} · ${formatValue(point.y, metric)}`;
  const group = svgElement("g", { class: `graph-finding-marker marker-${markerType}`, tabindex: "0", "data-finding-id": point.id, role: "button", "aria-label": evidenceLabel });
  if (markerType === "diamond") {
    group.append(svgElement("path", { d: "M 0 -7 L 7 0 L 0 7 L -7 0 Z", fill: color, stroke: color, "stroke-width": 1.5 }));
  } else if (markerType === "circle-open") {
    group.append(svgElement("circle", { cx: 0, cy: 0, r: 6, fill: "#fcfdf9", stroke: color, "stroke-width": 2 }));
  } else if (markerType === "star") {
    group.append(svgElement("path", { d: "M 0 -8 L 2.2 -2.5 L 8 -2.5 L 3.2 1 L 5 7 L 0 3.5 L -5 7 L -3.2 1 L -8 -2.5 L -2.2 -2.5 Z", fill: color, stroke: color, "stroke-width": 1.25 }));
  } else {
    group.append(svgElement("path", { d: "M -6 -6 L 6 6 M 6 -6 L -6 6", stroke: color, "stroke-width": 2.25, "stroke-linecap": "round" }));
  }
  const title = svgElement("title");
  title.textContent = evidenceLabel;
  group.append(title);
  group.setAttribute("transform", `translate(${point.px} ${point.py})`);
  addTooltipInteractions(group, chart, findingTooltipDetails(point, metric));
  return group;
}

function linePath(points) {
  return points.map((point, index) => `${index ? "L" : "M"}${point.px.toFixed(2)} ${point.py.toFixed(2)}`).join(" ");
}

function referencePoint(svg, chart, point, label, color, metric) {
  const year = point.row?.Year || "No year";
  const value = formatValue(point.y, metric);
  const details = `${label}\nMetric: ${GRAPH_METRICS[metric].label}\nYear: ${year}\nValue: ${value}`;
  const group = svgElement("g", {
    class: "graph-reference-point",
    role: "img",
    tabindex: "0",
    "aria-label": details.replaceAll("\n", " · "),
  });
  const title = svgElement("title");
  title.textContent = details.replaceAll("\n", " · ");
  group.append(title, svgElement("circle", {
    cx: point.px, cy: point.py, r: 7, fill: color, opacity: 0.01,
  }));
  addTooltipInteractions(group, chart, details);
  svg.append(group);
}

function text(svg, value, attributes) {
  const element = svgElement("text", attributes);
  element.textContent = value;
  svg.append(element);
}

function addLegend(container, alternateRevisions) {
  const legend = document.createElement("div");
  legend.className = "graph-legend";
  legend.setAttribute("aria-label", "Graph provenance legend");
  [
    ["line solid", "WPP 2024 historic"], ["line dashed", "WPP 2024 forecast"],
    ["diamond official", "Official publisher"], ["circle-open attributed", "Secondary · named source"], ["cross unattributed", "Secondary · no named source"], ["star legacy", "Legacy · unreviewed"],
  ].concat((alternateRevisions || []).map((revision) => ["line dotted", `WPP ${revision}`])).forEach(([className, label]) => {
    const item = document.createElement("span"); item.className = "graph-legend-item";
    const swatch = document.createElement("i"); swatch.className = `graph-swatch ${className}`; swatch.setAttribute("aria-hidden", "true");
    item.append(swatch, document.createTextNode(label)); legend.append(item);
  });
  container.append(legend);
}

/** Plotly-like padded extents without flattening positive-only series to zero. */
export function calculateYAxisExtent(values, scale = 1000) {
  const numbers = (values || []).map(number).filter((value) => value !== null);
  if (!numbers.length) return { min: 0, max: 1 };
  const rawMin = Math.min(...numbers);
  const rawMax = Math.max(...numbers);
  const span = rawMax - rawMin;
  const padding = Math.max(span * 0.08, scale === 1 ? 0.05 : Math.max(Math.abs(rawMax) * 0.01, 1));
  if (rawMin > 0) return { min: rawMin - padding, max: rawMax + padding };
  if (rawMax < 0) return { min: rawMin - padding, max: rawMax + padding };
  if (rawMin === 0 && rawMax === 0) return { min: 0, max: padding || 1 };
  // A mixed-sign series gets a padded extent around both sides, preserving
  // zero as a visible reference line.
  return { min: rawMin - padding, max: rawMax + padding };
}

/** Render one metric as an accessible, responsive SVG chart. */
export function renderMetricGraph(container, payload, metric, { hiddenFindingIds = new Set(), alternateRevisions = [], onFindingSelect = () => {} } = {}) {
  const config = GRAPH_METRICS[metric];
  if (!container || !config) return;
  const chart = document.createElement("article"); chart.className = "graph-panel";
  chart.style.position = "relative";
  const tooltip = document.createElement("div"); tooltip.className = "graph-tooltip"; tooltip.hidden = true; tooltip.setAttribute("role", "status");
  chart.append(tooltip);
  const heading = document.createElement("div"); heading.className = "graph-panel-heading";
  const title = document.createElement("h3"); title.textContent = `${config.label} · ${payload.country || payload.iso3 || "Selected country"}`;
  const unit = document.createElement("span"); unit.className = "graph-unit"; unit.textContent = config.unit;
  const actions = document.createElement("div"); actions.className = "graph-panel-actions";
  const fullscreen = document.createElement("button"); fullscreen.type = "button"; fullscreen.className = "quiet-button graph-fullscreen"; fullscreen.textContent = "Full screen"; fullscreen.title = "Open this graph full screen";
  fullscreen.addEventListener("click", async () => {
    try {
      if (document.fullscreenElement === chart) await document.exitFullscreen();
      else if (chart.requestFullscreen) await chart.requestFullscreen();
    } catch (error) { fullscreen.title = `Full screen unavailable: ${error.message}`; }
  });
  actions.append(unit, fullscreen); heading.append(title, actions); chart.append(heading);

  const historic = rowPoints(payload.historic, metric);
  const forecast = rowPoints(payload.forecast, metric);
  const alternate = Object.entries(payload.alternate_releases || {})
    .filter(([revision]) => alternateRevisions.includes(revision))
    .flatMap(([revision, rows]) => [{ revision, points: rowPoints(rows, metric) }]);
  const findings = findingPoints(payload.findings, metric, hiddenFindingIds);
  const allPoints = [...historic, ...forecast, ...alternate.flatMap((item) => item.points), ...findings];
  if (!allPoints.length) {
    const empty = document.createElement("p"); empty.className = "graph-empty"; empty.textContent = "No observations are available for this metric."; chart.append(empty); container.append(chart); return;
  }

  const width = 1000; const height = 350; const margin = { top: 22, right: 24, bottom: 48, left: 82 };
  const plotWidth = width - margin.left - margin.right; const plotHeight = height - margin.top - margin.bottom;
  const minX = Math.min(2014, ...allPoints.map((point) => point.x)); const maxX = Math.max(2033, ...allPoints.map((point) => point.x));
  const yExtent = calculateYAxisExtent(allPoints.map((point) => point.y), config.scale);
  const minY = yExtent.min; const maxY = yExtent.max || 1;
  const xPosition = (value) => margin.left + ((value - minX) / Math.max(maxX - minX, 1)) * plotWidth;
  const yPosition = (value) => margin.top + ((maxY - value) / Math.max(maxY - minY, 1)) * plotHeight;
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `${config.label} chart for ${payload.country || payload.iso3 || "selected country"}`, focusable: "false" });
  const grid = svgElement("g", { class: "graph-grid-lines", "aria-hidden": "true" });
  for (let index = 0; index <= 4; index += 1) {
    const y = margin.top + (plotHeight / 4) * index;
    const value = maxY - ((maxY - minY) / 4) * index;
    grid.append(svgElement("line", { x1: margin.left, x2: width - margin.right, y1: y, y2: y }));
    text(svg, formatValue(value, metric), { x: margin.left - 10, y: y + 4, "text-anchor": "end", class: "graph-axis-label" });
  }
  svg.append(grid);
  const firstTick = Math.ceil(minX); const lastTick = Math.floor(maxX);
  for (let year = firstTick; year <= lastTick; year += 2) {
    const x = xPosition(year);
    svg.append(svgElement("line", { x1: x, x2: x, y1: margin.top + plotHeight, y2: margin.top + plotHeight + 5, class: "graph-axis-tick" }));
    text(svg, String(year), { x, y: height - 21, "text-anchor": "middle", class: "graph-axis-label" });
  }
  svg.append(svgElement("line", { x1: margin.left, x2: width - margin.right, y1: margin.top + plotHeight, y2: margin.top + plotHeight, class: "graph-axis" }));
  svg.append(svgElement("line", { x1: margin.left, x2: margin.left, y1: margin.top, y2: margin.top + plotHeight, class: "graph-axis" }));
  text(svg, "Observation year", { x: margin.left + plotWidth / 2, y: height - 3, "text-anchor": "middle", class: "graph-axis-title" });
  text(svg, config.unit, { x: 17, y: margin.top + plotHeight / 2, "text-anchor": "middle", transform: `rotate(-90 17 ${margin.top + plotHeight / 2})`, class: "graph-axis-title" });

  const series = [
    [historic, "WPP 2024 historic", "#4c78a8", ""], [forecast, "WPP 2024 forecast", "#f58518", "6 5"],
    ...alternate.map((item) => [item.points, `WPP ${item.revision}`, WPP_REVISION_COLORS[item.revision] || "#6f4e9b", "2 5"]),
  ];
  series.forEach(([points, label, color, dash]) => {
    if (!points.length) return;
    const positioned = points.map((point) => ({ ...point, px: xPosition(point.x), py: yPosition(point.y) }));
    const path = svgElement("path", { d: linePath(positioned), class: "graph-series-line", fill: "none", stroke: color, "stroke-width": 2.25, "stroke-dasharray": dash });
    const pathTitle = svgElement("title"); pathTitle.textContent = label; path.append(pathTitle); svg.append(path);
    positioned.forEach((point) => referencePoint(svg, chart, point, label, color, metric));
  });
  findings.forEach((point) => { const positioned = { ...point, px: xPosition(point.x), py: yPosition(point.y) }; const className = SOURCE_CLASS_MARKERS[point.item.source_type] || "cross"; const color = SOURCE_CLASS_COLORS[point.item.source_type] || SOURCE_CLASS_COLORS.secondary_unattributed; const node = marker(chart, positioned, className, color, metric); node.addEventListener("click", () => onFindingSelect(point.id)); node.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onFindingSelect(point.id); } }); svg.append(node); });
  chart.append(svg); container.append(chart);
  addLegend(chart, alternate.filter((item) => item.points.length).map((item) => item.revision));
}

/** Render per-view hide/show choices while preserving stable finding IDs. */
export function renderFindingChoices(container, payload, metric, hiddenFindingIds, onChange, onFindingSelect = () => {}) {
  if (!container) return;
  container.replaceChildren();
  const metrics = Array.isArray(metric) ? metric : [metric];
  const relevant = (payload.findings || []).filter((item) => metrics.some((name) => findingMetricValue(item.finding || {}, name) !== null));
  if (!relevant.length) { const empty = document.createElement("p"); empty.className = "help"; empty.textContent = "No stored finding has a numeric value for this metric."; container.append(empty); return; }
  relevant.forEach((item) => {
    const id = Number(item.id); const row = document.createElement("div"); row.className = "graph-finding-choice";
    const label = document.createElement("label"); const input = document.createElement("input"); input.type = "checkbox"; input.checked = hiddenFindingIds.has(id); input.setAttribute("aria-label", `Hide finding ${id}`); input.addEventListener("change", () => { if (input.checked) hiddenFindingIds.add(id); else hiddenFindingIds.delete(id); onChange(); });
    const copy = document.createElement("span"); copy.textContent = `#${id} · ${SOURCE_CLASS_LABELS[item.source_type] || "Secondary · unnamed source"} · ${item.effective_date || "No date"}`; label.append(input, copy);
    const review = document.createElement("button"); review.type = "button"; review.className = "quiet-button"; review.textContent = "Review"; review.addEventListener("click", () => onFindingSelect(id)); row.append(label, review); container.append(row);
  });
}
