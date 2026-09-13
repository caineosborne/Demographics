"""Framework-independent read services for the Phase 2 API.

These functions return plain Python data and deliberately do not import
FastAPI, Gradio, or Plotly. UI adapters can continue to render the same data
without making the database their transport boundary.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
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


def list_findings(iso3: str | None = None, metric: str | None = None) -> list[dict[str, Any]]:
    """Return stored findings, optionally filtered by ISO3 and metric.

    The API deliberately returns the same flattened display fields as the
    existing database table. Metric filtering happens before private JSON is
    redacted so the browser never needs to open SQLite or receive its raw
    storage columns.
    """

    metric_keys = {
        'population': 'population', 'births': 'births', 'deaths': 'deaths',
        'natural_change': 'natural_change', 'net_migration': 'net_overseas_migration',
        'total_fertility_rate': 'total_fertility_rate',
    }
    metric_key = None
    if metric:
        metric_key = metric_keys.get(str(metric).strip())
        if metric_key is None:
            raise ValueError(f"Unknown finding metric: {metric}.")

    internal_fields = {'Extracted JSON', 'Search run ID', 'Search candidate ID', 'Extracted at (UTC)'}
    findings = []
    for finding in list_webpage_findings():
        if metric_key:
            try:
                payload = json.loads(finding.get('Extracted JSON') or '{}')
            except (TypeError, json.JSONDecodeError):
                payload = {}
            value = ((payload.get('statistics') or {}).get(metric_key) or {}).get('value')
            if value is None:
                continue
        display = {key: value for key, value in finding.items() if key not in internal_fields}
        try:
            payload = json.loads(finding.get('Extracted JSON') or '{}')
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload.get('statistics'), dict):
            statistics = payload['statistics']
            display['Metrics'] = [key for key, metric_value in statistics.items()
                                  if isinstance(metric_value, dict) and metric_value.get('value') is not None]
            # Expose only the concise values needed by the admin table. The
            # full extraction JSON remains private to the record detail route.
            display['Metric values'] = {
                key: metric_value.get('value')
                for key, metric_value in statistics.items()
                if isinstance(metric_value, dict) and metric_value.get('value') is not None
            }
        findings.append(display)
    if iso3 is None:
        return findings
    resolved = _strict_iso3(iso3)
    return [finding for finding in findings if str(finding.get("ISO3") or "").upper() == resolved]


def finding_coverage(iso3: str | None = None) -> list[dict[str, Any]]:
    """Pivot stored findings into the country/metric coverage table."""
    raw_findings = list_webpage_findings()
    if iso3 is not None:
        resolved = _strict_iso3(iso3)
        raw_findings = [row for row in raw_findings if str(row.get('ISO3') or '').upper() == resolved]
    metric_names = ('population', 'births', 'deaths', 'natural_change',
                    'net_migration', 'total_fertility_rate')
    coverage: dict[tuple[str, str], dict[str, Any]] = {}
    for finding in raw_findings:
        country = str(finding.get('Country') or 'Unknown')
        iso = str(finding.get('ISO3') or '')
        key = (country, iso)
        row = coverage.setdefault(key, {'country': country, 'iso3': iso, 'findings': 0})
        row['findings'] += 1
        try:
            payload = json.loads(finding.get('Extracted JSON') or '{}')
        except (TypeError, json.JSONDecodeError):
            payload = {}
        statistics = payload.get('statistics') or {}
        for name in metric_names:
            source_key = 'net_overseas_migration' if name == 'net_migration' else name
            value = (statistics.get(source_key) or {}).get('value')
            if value is not None:
                row[name] = int(row.get(name, 0)) + 1
            else:
                row.setdefault(name, 0)
    return sorted(coverage.values(), key=lambda row: (row['country'], row['iso3']))


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
    try:
        with get_wpp_connection() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f'''SELECT revision, Year, {columns}, cadence_years
                    FROM wpp_release_history
                    WHERE "ISO3 Alpha-code" = ? AND revision IN ({placeholders})
                      AND CAST(Year AS INTEGER) BETWEEN ? AND ?
                    ORDER BY revision DESC, Year''',
                (iso3, *requested, CHART_START_YEAR, CHART_END_YEAR),
            ).fetchall()
            # Some imported WPP vintages have blank ISO3 values. Fill only
            # missing revisions from the canonical country label, preserving
            # ISO3-matched rows and avoiding duplicate overlay points.
            found_revisions = {int(row["revision"]) for row in rows}
            missing_revisions = tuple(revision for revision in requested if revision not in found_revisions)
            if missing_revisions:
                fallback_placeholders = ",".join("?" for _ in missing_revisions)
                country = normalise_country_name(iso3)
                if country:
                    rows += connection.execute(
                        f'''SELECT revision, Year, {columns}, cadence_years
                            FROM wpp_release_history
                            WHERE Country = ? AND revision IN ({fallback_placeholders})
                              AND CAST(Year AS INTEGER) BETWEEN ? AND ?
                            ORDER BY revision DESC, Year''',
                        (country, *missing_revisions, CHART_START_YEAR, CHART_END_YEAR),
                    ).fetchall()
    except sqlite3.OperationalError:
        return result
    for row in rows:
        result[str(int(row["revision"]))].append(dict(row))
    return result


def _finding_graph_rows(iso3: str) -> list[dict[str, Any]]:
    rows = []
    # Graph assembly is an internal service operation and needs the stored
    # extraction payload; the public findings list intentionally redacts it.
    for finding in list_webpage_findings():
        if str(finding.get('ISO3') or '').upper() != iso3.upper():
            continue
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

    displayed = []
    for run in research_store.list_runs():
        # JSON blobs and lease ownership are storage/audit details. The list
        # contract exposes stable run state; detail requests expose parsed
        # settings/events separately.
        displayed.append({
            key: value for key, value in run.items()
            if key not in {'settings_json', 'events_json', 'owner_id', 'heartbeat_at', 'lease_expires_at'}
        })
    return displayed


def list_candidate_history(run_id: str | None = None) -> list[dict[str, Any]]:
    """Return persisted candidate audit rows, optionally scoped to a run."""

    return research_store.list_candidates(run_id)


def get_candidate(candidate_id: int) -> dict[str, Any]:
    """Return one structured candidate audit row without raw storage columns."""
    return research_store.get_candidate(int(candidate_id))


def country_gap_preview(prefix: str, days: int = 31, country_count: int = 5,
                        start_at: int = 1, scope_iso3s: list[str] | None = None) -> dict[str, Any]:
    """Return a deterministic ISO3 gap preview for the admin batch picker.

    Freshness is based on extraction time, matching the retained Gradio
    workflow.  ``excluded`` is explicit when a caller supplies a narrower
    ISO3 scope, so the UI cannot mistake scope filtering for missing data.
    """
    prefix = str(prefix or '').strip()
    if not prefix or not prefix.isalpha():
        raise ValueError('prefix must contain one or more letters.')
    try:
        days = int(days); country_count = int(country_count); start_at = int(start_at)
    except (TypeError, ValueError) as exc:
        raise ValueError('days, country_count, and start_at must be whole numbers.') from exc
    if days < 1:
        raise ValueError('days must be at least 1.')
    if country_count < 1 or country_count > 100:
        raise ValueError('country_count must be between 1 and 100.')
    if start_at < 1:
        raise ValueError('start_at must be at least 1.')
    scope = None
    if scope_iso3s is not None:
        scope = {_strict_iso3(value) for value in scope_iso3s}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent: set[str] = set()
    for finding in list_webpage_findings():
        iso3 = str(finding.get('ISO3') or '').strip().upper()
        if not iso3:
            continue
        stamp = str(finding.get('Extracted at (UTC)') or '')
        try:
            extracted_at = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            if extracted_at.tzinfo is None:
                extracted_at = extracted_at.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if extracted_at >= cutoff:
            recent.add(iso3)
    choices = list_country_choices()
    matching = [choice for choice in choices if choice['name'].casefold().startswith(prefix.casefold())]
    excluded = []
    eligible = []
    for choice in matching:
        if scope is not None and choice['iso3'].upper() not in scope:
            excluded.append({**choice, 'reason': 'outside_requested_scope'})
        elif choice['iso3'].upper() not in recent:
            eligible.append({**choice, 'reason': 'missing_recent_finding'})
    if eligible and start_at > len(eligible):
        raise ValueError(f'start_at is beyond the {len(eligible)} matching country gaps.')
    if start_at > len(eligible):
        selected = []
    else:
        selected = eligible[start_at - 1:start_at - 1 + country_count]
    return {
        'prefix': prefix,
        'days': days,
        'total_matches': len(matching),
        'total_gaps': len(eligible),
        'start_at': start_at,
        'country_count': country_count,
        'selected': selected,
        'excluded': excluded,
        'remaining': max(0, len(eligible) - (start_at - 1 + len(selected))),
    }
