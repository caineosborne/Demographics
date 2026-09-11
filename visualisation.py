"""Build on-demand comparison charts from UN data and stored webpage findings."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from typing import Any

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from tools import get_connection, initialise_findings_table, normalise_country_name, resolve_country_iso3


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
    historic_years = tuple(range(2014, 2024))
    forecast_years = tuple(range(2024, 2034))
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


def _stored_findings(country: str) -> list[dict]:
    initialise_findings_table()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, source_url, effective_date, extracted_at, finding_json FROM webpage_findings"
        ).fetchall()
    findings = []
    for row in rows:
        finding = json.loads(row["finding_json"])
        finding_country = normalise_country_name(finding.get("geography") or "")
        if finding_country == country:
            findings.append({**dict(row), "finding": finding})
    return findings


def _metric_value(finding: dict, metric: str) -> float | None:
    statistics = finding.get("statistics") or {}
    if metric == "net_migration":
        statistic = statistics.get("net_overseas_migration") or statistics.get("net_migration") or {}
    else:
        statistic = statistics.get(metric) or {}
    if statistic.get("comparison_eligible") is False:
        return None
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


def _add_stored_traces(
    figure: go.Figure, findings: list[dict], metric: str, row: int, showlegend: bool,
) -> None:
    values = []
    for item in findings:
        value = _metric_value(item["finding"], metric)
        effective_date = item["effective_date"]
        if value is not None and effective_date:
            finding = item["finding"]
            values.append((effective_date, value, item["id"], finding))
    label, _, _, scale = METRICS[metric]
    value_format = ",.2f" if scale == 1 else ",.0f"
    if not values:
        # Keep the timing cue but do not add a long source annotation to every panel.
        for item in findings:
            if item["effective_date"]:
                figure.add_vline(
                    x=item["effective_date"], line_dash="dot", line_color="#8c8c8c",
                    line_width=1, opacity=0.45, row=row, col=1,
                )
        return

    values.sort(key=lambda item: (item[0], item[2]))
    latest_id = max(values, key=lambda item: next(
        finding["extracted_at"] for finding in findings if finding["id"] == item[2]
    ))[2]
    customdata = [[
        item[3].get("source") or "Unknown source",
        item[3].get("quoted_source") or "Not quoted",
        item[3].get("url") or "",
        _metric_period(item[3], metric),
    ] for item in values]
    figure.add_trace(go.Scatter(
        x=[item[0] for item in values], y=[item[1] for item in values],
        mode="markers", name="Stored webpage estimate", legendgroup="stored",
        showlegend=showlegend, marker={"size": 9, "symbol": "diamond", "color": "#2a9d8f"}, customdata=customdata,
        hovertemplate=(f"{label}<br>%{{x}}: %{{y:{value_format}}}<br>Period: %{{customdata[3]}}<br>"
                       "Source: %{customdata[0]}<br>Quoted: %{customdata[1]}<br>"
                       "%{customdata[2]}<extra>Stored estimate</extra>"),
    ), row=row, col=1)
    latest = [item for item in values if item[2] == latest_id]
    if latest:
        figure.add_trace(go.Scatter(
            x=[item[0] for item in latest], y=[item[1] for item in latest],
            mode="markers", name="Current stored estimate", legendgroup="current",
            showlegend=showlegend, marker={"size": 13, "symbol": "star", "color": "#d1495b"}, customdata=[customdata[values.index(latest[0])]],
            hovertemplate=(f"Current {label.lower()}<br>%{{x}}: %{{y:{value_format}}}<br>"
                           "Source: %{customdata[0]}<extra>Current stored estimate</extra>"),
        ), row=row, col=1)


def _make_figure(title: str, metrics: list[str], historic: list[dict], forecast: list[dict], findings: list[dict]) -> go.Figure:
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
        _add_stored_traces(figure, findings, metric, index, showlegend)
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
    figure.update_xaxes(title_text="Year / effective date", row=len(metrics), col=1)
    return figure


def build_visualisation(country: str, selected_metrics: list[str]):
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
    findings = _stored_findings(country)
    if not historic and not forecast:
        message = f"No UN records were found for “{country}”. Use the exact UN country name."
    else:
        message = f"Showing {len(historic)} UN historical years, {len(forecast)} UN forecast years, and {len(findings)} stored webpage finding(s) for {country}. The star marks the most recently stored finding that reports each metric."
    population = ["population"] if "population" in selected_metrics else []
    flows = [metric for metric in selected_metrics if metric != "population"]
    return (
        _make_figure(f"Population in context — {country}", population, historic, forecast, findings),
        _make_figure(f"Births, deaths, migration and fertility — {country}", flows, historic, forecast, findings),
        message,
    )


def build_visualisation_for_latest_analysis(
    country: str, latest_analysis_country: str | None, selected_metrics: list[str]
):
    """Use the country from the most recent analysis when no override is entered."""
    return build_visualisation(country or latest_analysis_country or "", selected_metrics)
