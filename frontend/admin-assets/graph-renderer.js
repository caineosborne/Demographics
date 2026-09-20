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

// Evidence is deliberately shown in bounded bands.  A score is not a second
// quantitative axis and must never make a marker appear infinitely more
// important than another one.
export const EVIDENCE_BANDS = Object.freeze({
  limited: { label: "Limited evidence", min: 0, max: 4, size: 7 },
  supported: { label: "Supported evidence", min: 5, max: 6, size: 9 },
  strong: { label: "Strong evidence", min: 7, max: Infinity, size: 11 },
});

export const GRAPH_MODES = Object.freeze({
  show_all: "show_all",
  primary: "primary",
  primary_approved_secondary: "primary_approved_secondary",
});

const SOURCE_CLASS_ORDER = Object.freeze({
  official_publisher: 4,
  secondary_attributed: 3,
  secondary_unattributed: 2,
  legacy_unreviewed: 1,
});

function sourceClassWeight(sourceClass) {
  return SOURCE_CLASS_ORDER[sourceClass] || 0;
}

export function evidenceBand(points) {
  const score = number(points) ?? 0;
  if (score >= EVIDENCE_BANDS.strong.min) return EVIDENCE_BANDS.strong;
  if (score >= EVIDENCE_BANDS.supported.min) return EVIDENCE_BANDS.supported;
  return EVIDENCE_BANDS.limited;
}

function clusterId(cluster, index) {
  return String(cluster?.id ?? cluster?.value_cluster_id ?? cluster?.cluster_id ?? `cluster-${index}`);
}

function clusterObservationId(cluster) {
  return String(cluster?.observation_group_id ?? cluster?.observation_id ?? cluster?.group_id ?? cluster?.observation_group?.id ?? "ungrouped");
}

function clusterValue(cluster, metric) {
  const metricValue = cluster?.values?.[metric] ?? cluster?.metrics?.[metric];
  const candidate = metricValue && typeof metricValue === "object" ? metricValue.value : metricValue;
  return number(cluster?.value ?? cluster?.claim_value ?? cluster?.metric_value ?? candidate ?? cluster?.claim?.value);
}

function clusterDate(cluster) {
  return cluster?.effective_date || cluster?.date || cluster?.period_start || cluster?.period || cluster?.observation_date
    || cluster?.claim?.effective_date || cluster?.claim?.period || cluster?.observation_group?.period;
}

function clusterDocuments(cluster) {
  const documents = cluster?.source_documents || cluster?.documents || cluster?.sources || cluster?.supporting_documents;
  if (Array.isArray(documents)) return documents;
  if (Array.isArray(cluster?.claims)) return cluster.claims;
  return [];
}

function strongestClusterSourceClass(cluster) {
  const direct = cluster?.effective_classification || cluster?.source_class || cluster?.source_type || cluster?.classification;
  const classes = [direct, ...clusterDocuments(cluster).map((document) => document?.effective_classification || document?.source_class || document?.source_type || document?.classification)]
    .filter(Boolean);
  return classes.sort((left, right) => sourceClassWeight(right) - sourceClassWeight(left))[0] || "legacy_unreviewed";
}

function clusterEvidencePoints(cluster) {
  return number(cluster?.effective_points ?? cluster?.evidence_points ?? cluster?.score) ?? 0;
}

function clusterRawPoints(cluster) {
  return number(cluster?.raw_points ?? cluster?.raw_evidence_points ?? cluster?.evidence_points ?? cluster?.effective_points) ?? 0;
}

function clusterSourceCount(cluster) {
  return number(cluster?.supporting_document_count ?? cluster?.source_count ?? clusterDocuments(cluster).length) ?? 0;
}

function clusterIsRejected(cluster) {
  return String(cluster?.display_disposition || cluster?.disposition || "").toLowerCase() === "rejected"
    || String(cluster?.status || "").toLowerCase() === "rejected";
}

function clusterIsPrimary(cluster) {
  return cluster?.is_primary === true || cluster?.primary === true || String(cluster?.display_disposition || cluster?.disposition || "").toLowerCase() === "primary";
}

function clusterIsApprovedSecondary(cluster) {
  return String(cluster?.display_disposition || cluster?.disposition || "").toLowerCase() === "approved_secondary";
}

function clusterSortKey(cluster, index) {
  return String(clusterId(cluster, index));
}

/**
 * Flatten both the Phase 4 graph contract and the legacy findings contract.
 * The adapter is intentionally permissive while API deployments roll forward:
 * old fixtures continue to render one marker per finding.
 */
export function graphValueClusters(payload) {
  const direct = payload?.value_clusters || payload?.clusters;
  if (Array.isArray(direct)) return direct;
  const groups = payload?.observation_groups || payload?.observations;
  if (Array.isArray(groups)) return groups.flatMap((group) => (group?.value_clusters || group?.clusters || []).map((cluster) => ({ ...cluster, observation_group_id: cluster.observation_group_id ?? group.id, observation_group: cluster.observation_group || group })));
  return [];
}

function choosePrimaryCluster(clusters) {
  return [...clusters].filter((cluster) => !clusterIsRejected(cluster)).sort((left, right) => {
    const primaryDelta = Number(clusterIsPrimary(right)) - Number(clusterIsPrimary(left));
    if (primaryDelta) return primaryDelta;
    const scoreDelta = clusterEvidencePoints(right) - clusterEvidencePoints(left);
    if (scoreDelta) return scoreDelta;
    const countDelta = clusterSourceCount(right) - clusterSourceCount(left);
    if (countDelta) return countDelta;
    return clusterSortKey(left, 0).localeCompare(clusterSortKey(right, 0));
  })[0];
}

function visibleClusters(clusters, mode) {
  const usable = clusters.filter((cluster) => !clusterIsRejected(cluster));
  if (!mode || mode === GRAPH_MODES.show_all) return usable;
  const byGroup = new Map();
  usable.forEach((cluster) => {
    const group = clusterObservationId(cluster);
    if (!byGroup.has(group)) byGroup.set(group, []);
    byGroup.get(group).push(cluster);
  });
  const primaryByGroup = new Map([...byGroup.entries()].map(([group, groupClusters]) => [group, choosePrimaryCluster(groupClusters)]));
  if (mode === GRAPH_MODES.primary) return [...primaryByGroup.values()].filter(Boolean);
  return usable.filter((cluster) => cluster === primaryByGroup.get(clusterObservationId(cluster)) || clusterIsApprovedSecondary(cluster));
}

export function visibleGraphClusters(payload, mode = GRAPH_MODES.show_all) {
  return visibleClusters(graphValueClusters(payload), mode);
}

function clusterSourceSummary(cluster) {
  return clusterDocuments(cluster).map((document) => ({
    label: document?.title || document?.source || document?.publisher || document?.name || document?.url || document?.source_url || "Source document",
    url: document?.url || document?.source_url || document?.canonical_url || document?.canonicalUrl || "",
  }));
}

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

function clusterPoints(payload, metric, mode, hiddenFindingIds) {
  const clusters = visibleGraphClusters(payload, mode);
  return clusters.flatMap((cluster, index) => {
    if (cluster?.metric && String(cluster.metric) !== metric) return [];
    const id = clusterId(cluster, index);
    const sourceClaimId = Number(cluster?.finding_id ?? cluster?.claim_id ?? cluster?.claim?.finding_id);
    const x = dateYearPosition(clusterDate(cluster));
    const value = clusterValue(cluster, metric);
    if (hiddenFindingIds.has(sourceClaimId) || x === null || value === null) return [];
    return [{
      x,
      y: value * GRAPH_METRICS[metric].scale,
      id,
      cluster,
      sourceClaimId: Number.isFinite(sourceClaimId) ? sourceClaimId : null,
      sourceType: strongestClusterSourceClass(cluster),
      rawPoints: clusterRawPoints(cluster),
      effectivePoints: clusterEvidencePoints(cluster),
      sourceCount: clusterSourceCount(cluster),
      documents: clusterSourceSummary(cluster),
      observationGroupId: clusterObservationId(cluster),
      isPrimary: clusterIsPrimary(cluster),
      disposition: cluster?.display_disposition || cluster?.disposition || "automatic",
      conflict: cluster?.unresolved_conflict !== undefined
        ? Boolean(cluster.unresolved_conflict)
        : Boolean(cluster?.conflict || cluster?.conflicting_cluster_count > 0 || cluster?.conflict_state === "unresolved"),
    }];
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

function clusterTooltipDetails(point, metric) {
  const cluster = point.cluster || {};
  const documents = point.documents.length ? point.documents.map((document) => `${document.label}${document.url ? `\n${document.url}` : ""}`).join("\n") : "No supporting source links returned";
  const reason = cluster.automated_reason || cluster.assessment_reason || cluster.reason || "No automated reasoning returned";
  const decisionOrigin = cluster.decision_origin || cluster.source_decision_origin || "automated_assessment";
  const conflict = point.conflict ? "\nConflict: unresolved" : "\nConflict: none";
  return `Value cluster ${point.id}\nMetric: ${GRAPH_METRICS[metric].label}\nValue: ${formatValue(point.y, metric)}\nEvidence: ${point.effectivePoints} effective (${point.rawPoints} raw)\nSupporting documents: ${point.sourceCount}\nClassification: ${SOURCE_CLASS_LABELS[point.sourceType] || point.sourceType}\nDisposition: ${point.disposition}\nDecision origin: ${decisionOrigin}${conflict}\nReason: ${reason}\nSources:\n${documents}`;
}

function marker(chart, point, markerType, color, metric, { onClusterToggle = () => {} } = {}) {
  if (point.cluster) {
    const band = evidenceBand(point.effectivePoints);
    const scale = band.size / 7;
    const conflictHalo = point.conflict ? svgElement("circle", { cx: 0, cy: 0, r: 11, fill: "none", stroke: "#c44f3f", "stroke-width": 2.5, "stroke-dasharray": "3 2", class: "graph-conflict-halo" }) : null;
    const markerGroup = svgElement("g", { class: `graph-cluster-marker marker-${markerType} evidence-${band === EVIDENCE_BANDS.strong ? "strong" : band === EVIDENCE_BANDS.supported ? "supported" : "limited"}${point.conflict ? " conflict" : ""}`, tabindex: "0", "data-cluster-id": point.id, role: "button", "aria-label": clusterTooltipDetails(point, metric).replaceAll("\n", " · ") });
    if (conflictHalo) markerGroup.append(conflictHalo);
    const content = svgElement("g", { transform: `scale(${scale})` });
    if (markerType === "diamond") content.append(svgElement("path", { d: "M 0 -7 L 7 0 L 0 7 L -7 0 Z", fill: color, stroke: color, "stroke-width": 1.5 }));
    else if (markerType === "circle-open") content.append(svgElement("circle", { cx: 0, cy: 0, r: 6, fill: "#fcfdf9", stroke: color, "stroke-width": 2 }));
    else if (markerType === "star") content.append(svgElement("path", { d: "M 0 -8 L 2.2 -2.5 L 8 -2.5 L 3.2 1 L 5 7 L 0 3.5 L -5 7 L -3.2 1 L -8 -2.5 L -2.2 -2.5 Z", fill: color, stroke: color, "stroke-width": 1.25 }));
    else content.append(svgElement("path", { d: "M -6 -6 L 6 6 M 6 -6 L -6 6", stroke: color, "stroke-width": 2.25, "stroke-linecap": "round" }));
    markerGroup.append(content);
    const title = svgElement("title"); title.textContent = clusterTooltipDetails(point, metric); markerGroup.append(title);
    markerGroup.setAttribute("transform", `translate(${point.px} ${point.py})`);
    addTooltipInteractions(markerGroup, chart, clusterTooltipDetails(point, metric));
    markerGroup.addEventListener("click", () => onClusterToggle(point.id));
    markerGroup.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onClusterToggle(point.id); } });
    return markerGroup;
  }
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
export function renderMetricGraph(container, payload, metric, { hiddenFindingIds = new Set(), alternateRevisions = [], graphMode = GRAPH_MODES.show_all, onFindingSelect = () => {} } = {}) {
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
  const clusters = clusterPoints(payload, metric, graphMode, hiddenFindingIds);
  // Once the additive Phase 4 projection is present it is authoritative for
  // article evidence.  Falling back to findings is kept for old deployments
  // and fixtures that have not started returning value clusters yet.
  const findings = graphValueClusters(payload).length ? [] : findingPoints(payload.findings, metric, hiddenFindingIds);
  const allPoints = [...historic, ...forecast, ...alternate.flatMap((item) => item.points), ...findings, ...clusters];
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
  const detailId = `graph-sources-${String(payload.iso3 || payload.country || "country").replace(/[^a-z0-9_-]/gi, "-")}-${metric}`;
  const sourceDetails = document.createElement("div"); sourceDetails.className = "graph-source-details"; sourceDetails.id = detailId;
  const toggleCluster = (clusterIdValue) => {
    const detail = [...sourceDetails.querySelectorAll("[data-cluster-details]")].find((item) => item.dataset.clusterDetails === String(clusterIdValue));
    if (detail) { detail.open = !detail.open; if (detail.open) detail.scrollIntoView({ block: "nearest" }); }
  };
  clusters.forEach((point) => {
    const positioned = { ...point, px: xPosition(point.x), py: yPosition(point.y) };
    const className = SOURCE_CLASS_MARKERS[point.sourceType] || "cross";
    const color = SOURCE_CLASS_COLORS[point.sourceType] || SOURCE_CLASS_COLORS.secondary_unattributed;
    svg.append(marker(chart, positioned, className, color, metric, { onClusterToggle: toggleCluster }));
    const details = document.createElement("details"); details.className = "graph-cluster-details"; details.dataset.clusterDetails = point.id;
    const summary = document.createElement("summary"); summary.textContent = `${formatValue(point.y, metric)} · ${point.effectivePoints} evidence points · ${point.sourceCount} source${point.sourceCount === 1 ? "" : "s"}${point.conflict ? " · unresolved conflict" : ""}`; details.append(summary);
    const metadata = document.createElement("p"); metadata.className = "graph-cluster-metadata"; metadata.textContent = `${SOURCE_CLASS_LABELS[point.sourceType] || point.sourceType} · ${point.disposition} · raw ${point.rawPoints} · ${point.cluster?.decision_origin || "automated_assessment"}`; details.append(metadata);
    const reason = point.cluster?.automated_reason || point.cluster?.assessment_reason || point.cluster?.reason;
    if (reason) { const reasonNode = document.createElement("p"); reasonNode.className = "graph-cluster-reason"; reasonNode.textContent = `Assessment: ${reason}`; details.append(reasonNode); }
    const sources = document.createElement("ul"); sources.className = "graph-source-list";
    if (point.documents.length) point.documents.forEach((source) => { const item = document.createElement("li"); if (source.url) { const link = document.createElement("a"); link.href = source.url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = source.label; item.append(link); } else item.textContent = source.label; sources.append(item); });
    else { const item = document.createElement("li"); item.textContent = "No supporting source documents returned."; sources.append(item); }
    details.append(sources); sourceDetails.append(details);
  });
  findings.forEach((point) => { const positioned = { ...point, px: xPosition(point.x), py: yPosition(point.y) }; const className = SOURCE_CLASS_MARKERS[point.item.source_type] || "cross"; const color = SOURCE_CLASS_COLORS[point.item.source_type] || SOURCE_CLASS_COLORS.secondary_unattributed; const node = marker(chart, positioned, className, color, metric); node.addEventListener("click", () => onFindingSelect(point.id)); node.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onFindingSelect(point.id); } }); svg.append(node); });
  chart.append(svg); container.append(chart);
  if (clusters.length) chart.append(sourceDetails);
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
