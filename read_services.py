"""Framework-independent read services for the Phase 2 API.

These functions return plain Python data and deliberately do not import
FastAPI, Gradio, or Plotly. UI adapters can continue to render the same data
without making the database their transport boundary.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import research_store
from tools import (
    get_wpp_connection,
    list_webpage_findings,
    normalise_country_name,
    resolve_country_iso3,
)


CHART_START_YEAR = 2014
CHART_END_YEAR = 2033
ALTERNATE_REVISIONS = frozenset({2012, 2017, 2022})
GRAPH_COLUMNS = (
    "Population 1 Jul",
    "Total Births",
    "Total Deaths",
    "Natural Change",
    "Net Migration",
    "Total Fertility Rate (live births per woman)",
)
GRAPH_METRICS = frozenset({
    "population", "births", "deaths", "natural_change", "net_migration",
    "total_fertility_rate",
})


def list_country_choices() -> list[dict[str, str]]:
    """Return the canonical country selector values used by the current UI."""

    with get_wpp_connection() as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute('''
            SELECT DISTINCT Country, "ISO3 Alpha-code" AS iso3
            FROM medium_variant
            WHERE "ISO3 Alpha-code" IS NOT NULL
              AND length(trim("ISO3 Alpha-code")) = 3
            ORDER BY Country
        ''').fetchall()
    return [{"name": row["Country"], "iso3": row["iso3"]} for row in rows]


def list_findings(iso3: str | None = None) -> list[dict[str, Any]]:
    """Return stored findings, optionally filtered by ISO3 identity."""

    findings = list_webpage_findings()
    if iso3 is None:
        return findings
    resolved = _strict_iso3(iso3)
    return [finding for finding in findings if str(finding.get("ISO3") or "").upper() == resolved]


def _strict_iso3(value: str) -> str:
    """Validate an API country identity without accepting names or aliases."""
    candidate = str(value or "").strip().upper()
    if len(candidate) != 3 or not candidate.isalpha():
        raise ValueError("iso3 must be a three-letter ISO3 code.")
    resolved = resolve_country_iso3(candidate)
    if not resolved or resolved.upper() != candidate:
        raise ValueError(f'Unknown ISO3 country code: {candidate}.')
    return candidate


def _year_placeholders(years: range) -> str:
    return ", ".join("?" for _ in years)


def _select_graph_rows(table: str, country_column: str, country_value: str, years: range) -> list[dict[str, Any]]:
    columns = ", ".join(f'"{column}"' for column in GRAPH_COLUMNS)
    year_values = tuple(years)
    country_column_sql = f'"{country_column}"'
    sql = f'''SELECT Year, {columns}
              FROM "{table}"
              WHERE {country_column_sql} = ?
                AND CAST(Year AS INTEGER) IN ({_year_placeholders(years)})
              ORDER BY CAST(Year AS INTEGER)'''
    with get_wpp_connection() as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, (country_value, *year_values)).fetchall()]


def _un_series(iso3: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    historic_years = range(CHART_START_YEAR, 2024)
    forecast_years = range(2024, CHART_END_YEAR + 1)
    historic = _select_graph_rows("estimates", "ISO3 Alpha-code", iso3, historic_years)
    forecast = _select_graph_rows("medium_variant", "ISO3 Alpha-code", iso3, forecast_years)
    return historic, forecast


def _release_series(iso3: str, revisions: list[int] | None) -> dict[str, list[dict[str, Any]]]:
    requested = sorted({int(revision) for revision in (revisions or []) if int(revision) in ALTERNATE_REVISIONS})
    result: dict[str, list[dict[str, Any]]] = {str(revision): [] for revision in requested}
    if not requested:
        return result
    columns = ", ".join(f'"{column}"' for column in GRAPH_COLUMNS)
    placeholders = ",".join("?" for _ in requested)
    params: tuple[Any, ...]
    params = (iso3, *requested, CHART_START_YEAR, CHART_END_YEAR)
    where = '"ISO3 Alpha-code" = ?'
    sql = f'''SELECT revision, Year, {columns}, cadence_years
              FROM wpp_release_history
              WHERE {where} AND revision IN ({placeholders})
                AND CAST(Year AS INTEGER) BETWEEN ? AND ?
              ORDER BY revision DESC, Year'''
    try:
        with get_wpp_connection() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return result
    for row in rows:
        result[str(int(row["revision"]))].append(dict(row))
    return result


def _finding_graph_rows(iso3: str) -> list[dict[str, Any]]:
    rows = []
    for finding in list_findings(iso3):
        extracted = finding.get("Extracted JSON")
        try:
            payload = json.loads(extracted) if isinstance(extracted, str) else extracted
        except json.JSONDecodeError:
            payload = {}
        rows.append({
            "id": finding.get("ID"),
            "effective_date": finding.get("Effective date") or "",
            "source_type": finding.get("Source classification") or "",
            "source_url": finding.get("Webpage URL") or "",
            "finding": payload or {},
        })
    return rows


def graph_series(iso3: str, metrics: list[str] | None = None, revisions: list[int] | None = None) -> dict[str, Any]:
    """Assemble the raw series consumed by the current graph renderer."""

    iso3 = _strict_iso3(iso3)
    canonical = normalise_country_name(iso3)
    selected = list(metrics or sorted(GRAPH_METRICS))
    invalid = sorted(set(selected) - GRAPH_METRICS)
    if invalid:
        raise ValueError(f"Unknown graph metric(s): {', '.join(invalid)}.")
    historic, forecast = _un_series(iso3)
    return {
        "country": canonical,
        "iso3": iso3,
        "metrics": selected,
        "historic": historic,
        "forecast": forecast,
        "alternate_releases": _release_series(iso3, revisions),
        "findings": _finding_graph_rows(iso3),
    }


def list_run_history() -> list[dict[str, Any]]:
    """Return persisted research runs in newest-first order."""

    return research_store.list_runs()


def list_candidate_history(run_id: str | None = None) -> list[dict[str, Any]]:
    """Return persisted candidate audit rows, optionally scoped to a run."""

    return research_store.list_candidates(run_id)
