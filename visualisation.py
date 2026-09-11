"""Build on-demand comparison charts from UN data and stored webpage findings."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from typing import Any

import plotly.graph_objects as go
import gradio as gr
from plotly.subplots import make_subplots

from tools import (
    get_connection, initialise_findings_table,
    normalise_country_name, resolve_country_iso3,
)


METRICS = {
    "population": ("Population", "Population 1 Jul", "People", 1_000),
    "births": ("Births", "Total Births", "People", 1_000),
    "deaths": ("Deaths", "Total Deaths", "People", 1_000),
    "natural_change": ("Natural change", "Natural Change", "People", 1_000),
    "net_migration": ("Net migration", "Net Migration", "People", 1_000),
    "total_fertility_rate": (
        "Total fertility rate", "Total Fertility Rate (live births per woman)",
        "Live births per woman", 1,
    ),
}

# Keep every UN vintage on the same visual comparison window.  The full series
# remains in SQLite for later analysis/imports.
CHART_START_YEAR = 2014
CHART_END_YEAR = 2033


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _empty_figure(title: str, y_axis: str, message: str) -> go.Figure:
    figure = go.Figure()
    figure.update_layout(title=title, yaxis_title=y_axis, template="plotly_white")
    figure.add_annotation(text=message, showarrow=False, x=0.5, y=0.5, xref="paper", yref="paper")
    return figure


def _un_rows(country: str) -> tuple[list[dict], list[dict]]:
    # The local WPP 2024 revision has historical estimates through 2023 and
    # projections from 2024. Keep ten annual observations on each side.
    historic_years = tuple(range(CHART_START_YEAR, 2024))
    forecast_years = tuple(range(2024, CHART_END_YEAR + 1))
    columns = ', '.join(f'"{column}"' for _, column, _, _ in METRICS.values())
    iso3 = resolve_country_iso3(country)
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        has_iso = any(row[1] == "ISO3 Alpha-code" for row in conn.execute("PRAGMA table_info(estimates)"))
        if has_iso and iso3:
            historic = conn.execute(
                f'''SELECT Year, {columns} FROM estimates
                    WHERE "ISO3 Alpha-code" = ? AND CAST(Year AS INTEGER) IN ({','.join('?' * len(historic_years))})
                    ORDER BY CAST(Year AS INTEGER)''',
                (iso3, *historic_years),
            ).fetchall()
            forecast = conn.execute(
                f'''SELECT Year, {columns} FROM medium_variant
                    WHERE "ISO3 Alpha-code" = ? AND CAST(Year AS INTEGER) IN ({','.join('?' * len(forecast_years))})
                    ORDER BY CAST(Year AS INTEGER)''',
                (iso3, *forecast_years),
            ).fetchall()
        else:
            # Small isolated databases used by callers may only contain names.
            historic = conn.execute(
                f'''SELECT Year, {columns} FROM estimates
                    WHERE Country = ? AND CAST(Year AS INTEGER) IN ({','.join('?' * len(historic_years))})
                    ORDER BY CAST(Year AS INTEGER)''',
                (country, *historic_years),
            ).fetchall()
            forecast = conn.execute(
                f'''SELECT Year, {columns} FROM medium_variant
                    WHERE Country = ? AND CAST(Year AS INTEGER) IN ({','.join('?' * len(forecast_years))})
                    ORDER BY CAST(Year AS INTEGER)''',
                (country, *forecast_years),
            ).fetchall()
    return [dict(row) for row in historic], [dict(row) for row in forecast]


def _release_rows(country: str, revisions: list[int] | None) -> dict[int, list[dict]]:
    """Return selected historical WPP releases without touching 2024 tables."""
    requested = tuple(sorted({int(revision) for revision in (revisions or []) if int(revision) in {2012, 2017, 2022}}))
    if not requested:
        return {}
    iso3 = resolve_country_iso3(country)
    placeholders = ",".join("?" for _ in requested)
    columns = ', '.join(f'"{column}"' for _, column, _, _ in METRICS.values())
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        try:
            if iso3:
                rows = conn.execute(
                    f'''SELECT revision, Year, {columns}, cadence_years FROM wpp_release_history
                        WHERE "ISO3 Alpha-code" = ? AND revision IN ({placeholders})
                          AND CAST(Year AS INTEGER) BETWEEN ? AND ?
                        ORDER BY revision DESC, Year''', (iso3, *requested, CHART_START_YEAR, CHART_END_YEAR)
                ).fetchall()
            else:
                rows = []
            # Earlier WPP releases use UN numeric location codes rather than
            # ISO3.  Fall back to the stable canonical country label.
            if not rows:
                rows = conn.execute(
                    f'''SELECT revision, Year, {columns}, cadence_years FROM wpp_release_history
                        WHERE Country = ? AND revision IN ({placeholders})
                          AND CAST(Year AS INTEGER) BETWEEN ? AND ?
                        ORDER BY revision DESC, Year''', (country, *requested, CHART_START_YEAR, CHART_END_YEAR)
                ).fetchall()
        except sqlite3.OperationalError:
            return {}
    result: dict[int, list[dict]] = {revision: [] for revision in requested}
    for row in rows:
        result[int(row["revision"])].append(dict(row))
    return result


def _stored_findings(country: str) -> list[dict]:
    initialise_findings_table()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, source_url, effective_date, extracted_at, finding_json, "
            "official_source, quoted_source FROM webpage_findings"
        ).fetchall()
    findings = []
    for row in rows:
        finding = json.loads(row["finding_json"])
        finding_country = normalise_country_name(finding.get("geography") or "")
        if finding_country == country:
            source_type = (
                "Official publisher" if row["official_source"]
                else "Secondary, official source named" if str(row["quoted_source"] or "").strip()
                else "Secondary, source not named"
            )
            findings.append({**dict(row), "source_type": source_type, "finding": finding})
    return findings


def finding_choices(country: str, metrics: list[str], hidden: list[int] | None = None) -> list[tuple[str, str]]:
    """Return review-friendly article choices for the selected chart metrics."""
    hidden = {int(value) for value in (hidden or [])}
    choices = []
    for item in _stored_findings(country):
        if item["id"] in hidden:
            continue
        if not any(_metric_value(item["finding"], metric) is not None for metric in metrics):
            continue
        finding = item["finding"]
        label = f'#{item["id"]} · {item["source_type"]} · {item["effective_date"] or "No date"} · {finding.get("title") or finding.get("source") or "Article"}'
        choices.append((label[:180], str(item["id"])))
    return choices


def all_finding_choices(country: str) -> list[tuple[str, str]]:
    return finding_choices(country, list(METRICS), [])


def finding_link(country: str, finding_id: str | int | None) -> str:
    """Render the selected article as a safe link below a chart."""
    if not finding_id:
        return "Select a plotted article to open its source."
    try:
        target = next(item for item in _stored_findings(country) if item["id"] == int(finding_id))
    except (TypeError, ValueError, StopIteration):
        return "The selected article is no longer available."
    url = target["finding"].get("url") or target.get("source_url") or ""
    from html import escape
    if not url.startswith(("http://", "https://")):
        return "No valid source URL is recorded for this finding."
    return f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener">Open source article ↗</a>'


def _metric_value(finding: dict, metric: str) -> float | None:
    statistics = finding.get("statistics") or {}
    if metric == "net_migration":
        statistic = statistics.get("net_overseas_migration") or statistics.get("net_migration") or {}
    else:
        statistic = statistics.get(metric) or {}
    # Comparison eligibility controls UN matching, not visibility. Monthly,
    # partial-year, and projected article figures should remain reviewable on
    # the chart with their period/caveat shown in the hover text.
    value = _number(statistic.get("value"))
    if value is None:
        return None
    if metric == "population":
        # A common extraction error is storing a reported change as the total.
        # Keep the original evidence, but do not plot a misleading point.
        text = f"{finding.get('title') or ''} {finding.get('summary') or ''}".casefold()
        if re.search(r"(?:increase|decrease|change|gain|loss)\s+(?:of|by)\s+(?:approximately\s+)?[\d,.]+\s*(?:million|m)", text):
            return None
        # Articles often report population in millions while the UN series is
        # in people. Convert the unambiguous small values at chart time.
        if value < 10_000 and re.search(r"\bmillions?\b", text):
            return value * 1_000_000
    return value


def _metric_period(finding: dict, metric: str) -> str:
    statistics = finding.get("statistics") or {}
    if metric == "net_migration":
        statistic = statistics.get("net_overseas_migration") or statistics.get("net_migration") or {}
    else:
        statistic = statistics.get(metric) or {}
    return statistic.get("time_period") or "Period not specified"


def _add_un_trace(
    figure: go.Figure, rows: list[dict], metric: str, name: str, dash: str,
    color: str, row: int, showlegend: bool,
) -> None:
    label, column, _, scale = METRICS[metric]
    value_format = ",.2f" if scale == 1 else ",.0f"
    values = [(f'{int(row["Year"])}-07-01', _number(row[column])) for row in rows]
    values = [(year, value * scale) for year, value in values if value is not None]
    if values:
        figure.add_trace(go.Scatter(
            x=[year for year, _ in values], y=[value for _, value in values],
            mode="lines+markers", name=name, legendgroup=name, showlegend=showlegend,
            line={"dash": dash, "color": color, "width": 2},
            marker={"size": 6, "color": color},
            hovertemplate=f"{label}<br>%{{x}}: %{{y:{value_format}}}<extra>{name}</extra>",
        ), row=row, col=1)


VINTAGE_COLORS = {2022: "#6f4e9b", 2017: "#3a8d7d", 2012: "#8b6f47"}


def _add_release_trace(figure: go.Figure, rows: list[dict], metric: str, revision: int, row: int, showlegend: bool) -> None:
    """Overlay a prior UN release, intentionally subordinate to WPP 2024."""
    _add_un_trace(
        figure, rows, metric, f"UN {revision} alternate history", "dot",
        VINTAGE_COLORS[revision], row, showlegend,
    )


def _add_stored_traces(
    figure: go.Figure, findings: list[dict], metric: str, row: int, showlegend: bool,
) -> None:
    values = []
    for item in findings:
        value = _metric_value(item["finding"], metric)
        effective_date = item["effective_date"]
        if value is not None and effective_date:
            finding = item["finding"]
            values.append((effective_date, value, item["id"], finding, item["source_type"]))
    label, _, _, scale = METRICS[metric]
    value_format = ",.2f" if scale == 1 else ",.0f"
    if not values:
        return

    values.sort(key=lambda item: (item[0], item[2]))
    latest_id = max(values, key=lambda item: next(
        finding["extracted_at"] for finding in findings if finding["id"] == item[2]
    ))[2]
    customdata = [[
        item[3].get("source") or "Unknown source",
        item[4],
        item[3].get("quoted_source") or "Not quoted",
        item[3].get("url") or "",
        _metric_period(item[3], metric),
        item[2],
        ((item[3].get("statistics") or {}).get(metric) or {}).get("comparison_reason") or "Comparable to UN series",
        item[3].get("title") or "Untitled article",
    ] for item in values]
    figure.add_trace(go.Scatter(
        x=[item[0] for item in values], y=[item[1] for item in values],
        mode="markers", name="Stored estimates (◆ official · ● secondary)", legendgroup="stored",
        showlegend=showlegend,
        marker={
            "size": 9,
            "symbol": [
                "diamond" if item[4] == "Official publisher" else "circle" if "official source named" in item[4] else "x"
                for item in values
            ],
            "color": [
                "#167d73" if item[4] == "Official publisher" else "#b7791f" if "official source named" in item[4] else "#6b7280"
                for item in values
            ],
        },
        customdata=customdata,
        hovertemplate=(f"{label}<br>%{{x}}: %{{y:{value_format}}}<br>Period: %{{customdata[4]}}<br>"
                       "Article: %{customdata[7]} (finding #%{customdata[5]})<br>"
                       "Source: %{customdata[0]}<br>Source status: %{customdata[1]}<br>"
                       "Quoted: %{customdata[2]}<br>Comparison: %{customdata[6]}<br>"
                       "%{customdata[3]}<extra>Stored estimate</extra>"),
    ), row=row, col=1)
    latest = [item for item in values if item[2] == latest_id]
    if latest:
        figure.add_trace(go.Scatter(
            x=[item[0] for item in latest], y=[item[1] for item in latest],
            mode="markers", name="Current stored estimate", legendgroup="current",
            showlegend=showlegend, marker={"size": 13, "symbol": "star", "color": "#d1495b"}, customdata=[customdata[values.index(latest[0])]],
            hovertemplate=(f"Current {label.lower()}<br>%{{x}}: %{{y:{value_format}}}<br>"
                           "Article: %{customdata[7]} (finding #%{customdata[5]})<br>"
                           "Source: %{customdata[0]}<br>Source status: %{customdata[1]}<extra>Current stored estimate</extra>"),
        ), row=row, col=1)


def _make_figure(title: str, metrics: list[str], historic: list[dict], forecast: list[dict], release_rows: dict[int, list[dict]], findings: list[dict], hidden_by_metric: dict[str, set[int]] | None = None) -> go.Figure:
    if not metrics:
        return _empty_figure(title, "People", "Select at least one metric.")
    is_small_multiples = len(metrics) > 1
    figure = make_subplots(
        rows=len(metrics), cols=1, shared_xaxes=True,
        vertical_spacing=0.08 if is_small_multiples else 0.12,
        subplot_titles=[METRICS[metric][0] for metric in metrics] if is_small_multiples else None,
    )
    for index, metric in enumerate(metrics, start=1):
        showlegend = index == 1
        _add_un_trace(figure, historic, metric, "UN historic", "solid", "#4c78a8", index, showlegend)
        _add_un_trace(figure, forecast, metric, "UN forecast", "dash", "#f58518", index, showlegend)
        for revision in sorted(release_rows, reverse=True):
            _add_release_trace(figure, release_rows[revision], metric, revision, index, showlegend)
        hidden = (hidden_by_metric or {}).get(metric, set())
        _add_stored_traces(figure, [item for item in findings if item["id"] not in hidden], metric, index, showlegend)
        _, _, unit, scale = METRICS[metric]
        figure.update_yaxes(
            title_text=unit,
            tickformat=("," if scale == 1_000 else ".2f"),
            row=index,
            col=1,
        )
    figure.update_layout(
        title=title, template="plotly_white", hovermode="closest",
        legend_title="Series", height=360 if not is_small_multiples else 250 * len(metrics),
        margin={"l": 80, "r": 180, "t": 70, "b": 55},
    )
    # All series are plotted as dates (UN observations use 1 July; article
    # findings use their effective date). Formatting the axis as a date while
    # showing year ticks prevents partial-year article points from looking like
    # extra annual observations or a broken 2023–2024 scale.
    figure.update_xaxes(type="date", tickformat="%Y", title_text="Observation date", row=len(metrics), col=1)
    return figure


def build_visualisation(country: str, selected_metrics: list[str], population_hidden: list[int] | None = None, flow_hidden: list[int] | None = None, hidden_by_metric: dict[str, list[int]] | None = None, alternate_revisions: list[int] | None = None):
    """Return the two requested interactive figures after the user presses draw."""
    entered_country = country.strip()
    if not entered_country:
        message = "Enter the country name from the extracted result, then draw the charts."
        return _empty_figure("Population in context", "People", message), _empty_figure("Demographic flows in context", "People", message), message

    country = normalise_country_name(entered_country)
    if not country:
        message = f"No UN country matches “{entered_country}”. Choose a country from the list."
        return _empty_figure("Population in context", "People", message), _empty_figure("Demographic flows in context", "People", message), message

    historic, forecast = _un_rows(country)
    releases = _release_rows(country, alternate_revisions)
    all_findings = _stored_findings(country)
    metric_hidden = {metric: set(values or []) for metric, values in (hidden_by_metric or {}).items()}
    if population_hidden:
        metric_hidden.setdefault("population", set()).update(population_hidden)
    for metric in ("births", "deaths", "natural_change", "net_migration", "total_fertility_rate"):
        if flow_hidden:
            metric_hidden.setdefault(metric, set()).update(flow_hidden)
    if not historic and not forecast:
        message = f"No UN records were found for “{country}”. Use the exact UN country name."
    else:
        loaded_releases = [str(revision) for revision in sorted(releases, reverse=True) if releases[revision]]
        if loaded_releases:
            alternate_note = f" Alternate histories: WPP {', '.join(loaded_releases)}."
        elif alternate_revisions:
            alternate_note = " The selected alternate history data has not been imported yet."
        else:
            alternate_note = ""
        message = f"Showing {len(historic)} UN historical years, {len(forecast)} UN forecast years, and {len(all_findings)} webpage finding(s) for {country}. WPP 2024 remains primary.{alternate_note}"
    population = ["population"] if "population" in selected_metrics else []
    flows = [metric for metric in selected_metrics if metric != "population"]
    return (
        _make_figure(f"Population in context — {country}", population, historic, forecast, releases, all_findings, metric_hidden),
        _make_figure(f"Births, deaths, migration and fertility — {country}", flows, historic, forecast, releases, all_findings, metric_hidden),
        message,
    )


def build_visualisation_for_latest_analysis(
    country: str, latest_analysis_country: str | None, selected_metrics: list[str], population_hidden: list[int] | None = None, flow_hidden: list[int] | None = None, hidden_by_metric: dict[str, list[int]] | None = None, alternate_revisions: list[int] | None = None
):
    """Use the country from the most recent analysis when no override is entered."""
    return build_visualisation(country or latest_analysis_country or "", selected_metrics, population_hidden, flow_hidden, hidden_by_metric, alternate_revisions)


def refresh_visualisation_controls(country: str, latest: str | None, metrics: list[str], population_hidden=None, flow_hidden=None):
    target = country or latest or ""
    population_metrics = ["population"] if "population" in (metrics or []) else []
    flow_metrics = [metric for metric in (metrics or []) if metric != "population"]
    charts = build_visualisation_for_latest_analysis(country, latest, metrics or [], population_hidden or [], flow_hidden or [])
    return (*charts[:2], gr.update(choices=finding_choices(target, population_metrics, population_hidden), value=None), gr.update(choices=finding_choices(target, flow_metrics, flow_hidden), value=None), charts[2])
