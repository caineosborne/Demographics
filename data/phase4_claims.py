"""Phase 4 claims, provenance, scoring, and review persistence.

The claim tables deliberately sit beside ``webpage_findings``.  The older
finding row (including its provider/search score in ``finding_json``) remains
the immutable extraction record; this module is the typed projection used by
the Phase 4 graph and review APIs.
"""

from __future__ import annotations

import json
import sqlite3
import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SOURCE_POINTS = {
    "official_publisher": 5,
    "secondary_attributed": 2,
    "secondary_unattributed": 1,
    "legacy_unreviewed": 0,
}
SOURCE_CAPS = {
    "official_publisher": 10,
    "secondary_attributed": 4,
    "secondary_unattributed": 2,
    "legacy_unreviewed": 0,
}
SOURCE_CLASSES = frozenset(SOURCE_POINTS)
DISPLAY_DISPOSITIONS = frozenset({"primary", "approved_secondary", "rejected"})
DECISION_ORIGINS = frozenset({"automated_assessment", "configured_rule", "migration", "manual_override"})
STRONG_EVIDENCE_THRESHOLD = 7
# Comparison values are rounded by metric after unit conversion.  The raw
# extracted value remains in ``metric_claims.value`` and is never overwritten.
ROUNDING_RULE_VERSION = "phase4-period-monthly-value-v2"
ROUNDING_METRICS = {
    "births": Decimal("1000"),
    "deaths": Decimal("1000"),
    "natural_change": Decimal("1000"),
    "net_overseas_migration": Decimal("1000"),
    "net_migration": Decimal("1000"),
    "tfr": Decimal("0.1"),
    "total_fertility_rate": Decimal("0.1"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(conn: sqlite3.Connection, *, migrate_legacy: bool = True) -> None:
    """Create Phase 4 tables and project retained findings exactly once.

    The migration is idempotent and intentionally assigns legacy claims zero
    points.  No historical source classification is inferred or rewritten.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_documents (
            id INTEGER PRIMARY KEY,
            canonical_url TEXT NOT NULL UNIQUE,
            source_url TEXT NOT NULL DEFAULT '',
            publisher_domain TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS observation_groups (
            id INTEGER PRIMARY KEY,
            iso3 TEXT NOT NULL,
            metric TEXT NOT NULL,
            observation_period TEXT NOT NULL,
            raw_observation_period TEXT,
            normalized_period TEXT,
            unit TEXT NOT NULL DEFAULT '',
            definition TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(iso3, metric, observation_period, unit, definition)
        );
        CREATE TABLE IF NOT EXISTS value_clusters (
            id INTEGER PRIMARY KEY,
            observation_group_id INTEGER NOT NULL REFERENCES observation_groups(id),
            normalized_value REAL NOT NULL,
            cluster_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(observation_group_id, cluster_key)
        );
        CREATE TABLE IF NOT EXISTS metric_claims (
            id INTEGER PRIMARY KEY,
            source_document_id INTEGER NOT NULL REFERENCES source_documents(id),
            finding_id INTEGER,
            observation_group_id INTEGER NOT NULL REFERENCES observation_groups(id),
            value_cluster_id INTEGER NOT NULL REFERENCES value_clusters(id),
            iso3 TEXT NOT NULL,
            metric TEXT NOT NULL,
            observation_period TEXT NOT NULL,
            raw_observation_period TEXT,
            normalized_period TEXT,
            unit TEXT NOT NULL DEFAULT '',
            definition TEXT NOT NULL DEFAULT '',
            value REAL NOT NULL,
            raw_value REAL,
            normalized_value REAL,
            underlying_source TEXT NOT NULL DEFAULT 'other',
            source_classification TEXT NOT NULL CHECK(source_classification IN
                ('official_publisher','secondary_attributed','secondary_unattributed','legacy_unreviewed')),
            automated_classification TEXT,
            automated_reason TEXT,
            assessment_model TEXT,
            assessment_prompt_version TEXT,
            assessment_rule_version TEXT,
            assessed_at TEXT,
            evidence_points INTEGER NOT NULL DEFAULT 0,
            raw_points INTEGER NOT NULL DEFAULT 0,
            effective_points INTEGER NOT NULL DEFAULT 0,
            display_disposition TEXT NOT NULL DEFAULT 'approved_secondary' CHECK(
                display_disposition IN ('primary','approved_secondary','rejected')),
            decision_origin TEXT NOT NULL CHECK(decision_origin IN
                ('automated_assessment','configured_rule','migration','manual_override')),
            manual_points INTEGER,
            manual_override_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(source_document_id, finding_id, metric, observation_period, value)
        );
        CREATE TABLE IF NOT EXISTS claim_decision_audit (
            id INTEGER PRIMARY KEY,
            claim_id INTEGER NOT NULL REFERENCES metric_claims(id),
            action TEXT NOT NULL,
            before_json TEXT,
            after_json TEXT,
            actor TEXT NOT NULL,
            reason TEXT,
            acted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_claims_group ON metric_claims(observation_group_id);
        CREATE INDEX IF NOT EXISTS idx_claims_cluster ON metric_claims(value_cluster_id);
        CREATE INDEX IF NOT EXISTS idx_claims_iso3 ON metric_claims(iso3);
        """
    )
    claim_columns = {row[1] for row in conn.execute("PRAGMA table_info(metric_claims)")}
    # Older local Phase 4 databases may have the first projection tables but
    # not the later scoring/review columns.  Add each column independently so
    # startup migration remains safe and repeatable.
    legacy_columns = {
        "source_classification": "TEXT NOT NULL DEFAULT 'legacy_unreviewed'",
        "automated_classification": "TEXT",
        "automated_reason": "TEXT",
        "assessment_model": "TEXT",
        "assessment_prompt_version": "TEXT",
        "assessed_at": "TEXT",
        "evidence_points": "INTEGER NOT NULL DEFAULT 0",
        "raw_points": "INTEGER NOT NULL DEFAULT 0",
        "effective_points": "INTEGER NOT NULL DEFAULT 0",
        "display_disposition": "TEXT NOT NULL DEFAULT 'approved_secondary'",
        "decision_origin": "TEXT NOT NULL DEFAULT 'migration'",
        "manual_points": "INTEGER",
        "manual_override_json": "TEXT",
    }
    for column, definition in legacy_columns.items():
        if column not in claim_columns:
            conn.execute(f"ALTER TABLE metric_claims ADD COLUMN {column} {definition}")
    if "assessment_rule_version" not in claim_columns:
        conn.execute("ALTER TABLE metric_claims ADD COLUMN assessment_rule_version TEXT")
    if "underlying_source" not in claim_columns:
        conn.execute("ALTER TABLE metric_claims ADD COLUMN underlying_source TEXT NOT NULL DEFAULT 'other'")
    if "raw_observation_period" not in claim_columns:
        conn.execute("ALTER TABLE metric_claims ADD COLUMN raw_observation_period TEXT")
    if "normalized_period" not in claim_columns:
        conn.execute("ALTER TABLE metric_claims ADD COLUMN normalized_period TEXT")
    if "raw_value" not in claim_columns:
        conn.execute("ALTER TABLE metric_claims ADD COLUMN raw_value REAL")
    if "normalized_value" not in claim_columns:
        conn.execute("ALTER TABLE metric_claims ADD COLUMN normalized_value REAL")
    group_columns = {row[1] for row in conn.execute("PRAGMA table_info(observation_groups)")}
    if "raw_observation_period" not in group_columns:
        conn.execute("ALTER TABLE observation_groups ADD COLUMN raw_observation_period TEXT")
    if "normalized_period" not in group_columns:
        conn.execute("ALTER TABLE observation_groups ADD COLUMN normalized_period TEXT")
    if migrate_legacy:
        _migrate_legacy_findings(conn)
    _reconcile_normalized_groups(conn)
    _repair_claim_scale_from_findings(conn)


def _canonical_url(url: str) -> str:
    parts = urlsplit(str(url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return str(url or "").strip()
    query = sorted((key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                   if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"})
    return urlunsplit(("https", parts.hostname.lower().removeprefix("www."),
                       parts.path.rstrip("/") or "/", urlencode(query), ""))


_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _month_text(year: str | int, month: str | int) -> str:
    return f"{int(year):04d}-{int(month):02d}"


def _period_endpoint(value: Any, *, end: bool = False) -> str | None:
    """Return the month containing a date/month/year endpoint."""
    text = " ".join(str(value or "").strip().split()).casefold()
    if not text:
        return None
    match = re.fullmatch(r"(\d{4})[-/]?(\d{1,2})[-/]?(\d{1,2})?", text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return _month_text(year, month)
    match = re.fullmatch(r"(\d{4})[-/]?(\d{1,2})", text)
    if match and 1 <= int(match.group(2)) <= 12:
        return _month_text(match.group(1), match.group(2))
    match = re.fullmatch(r"(\d{4})", text)
    if match:
        return _month_text(match.group(1), 12 if end else 1)
    match = re.fullmatch(r"([a-z]+)\s+(\d{4})", text)
    if match and match.group(1) in _MONTHS:
        return _month_text(match.group(2), _MONTHS[match.group(1)])
    match = re.fullmatch(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", text)
    if match and match.group(2) in _MONTHS:
        return _month_text(match.group(3), _MONTHS[match.group(2)])
    return None


def _canonical_period_text(raw_period: Any, metric: str = "") -> str:
    """Normalize measured periods to monthly comparison resolution.

    A publication date is deliberately not passed here.  A year-only
    population claim is retained as ``YYYY-unknown`` because assigning July
    without explicit mid-year semantics would invent a measurement date.
    """
    raw = " ".join(str(raw_period or "").strip().split())
    text = raw.casefold()
    if not text:
        return ""

    # Explicit slash/range dates are common in stored extraction output.
    parts = [part.strip() for part in re.split(r"\s*/\s*", text)]
    if len(parts) == 2:
        start, end = _period_endpoint(parts[0]), _period_endpoint(parts[1], end=True)
        if start and end:
            return start if start == end else f"{start}/{end}"

    # ISO month/date forms are already unambiguous and should not fall
    # through to the year-only handling below.
    if re.fullmatch(r"\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?", text):
        endpoint = _period_endpoint(text)
        if endpoint:
            return endpoint

    iso_range = re.search(
        r"(\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?)\s+(?:to|through|until)\s+"
        r"(\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?)",
        text,
    )
    if iso_range:
        start, end = _period_endpoint(iso_range.group(1)), _period_endpoint(iso_range.group(2), end=True)
        if start and end:
            return start if start == end else f"{start}/{end}"

    quarter = re.search(r"(?:q\s*([1-4])|([1-4])(?:st|nd|rd|th)?\s+quarter)\s*(?:(?:of|/)\s*)?(\d{4})", text)
    if quarter:
        number = int(quarter.group(1) or quarter.group(2))
        year = int(quarter.group(3))
        start_month = (number - 1) * 3 + 1
        return f"{_month_text(year, start_month)}/{_month_text(year, start_month + 2)}"

    year_match = re.search(r"\b(\d{4})\b", text)
    if year_match:
        year = year_match.group(1)
        month_names = "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
        range_match = re.search(
            rf"\b({month_names})\b\s*(?:to|through|until|[-–])\s*\b({month_names})\b\s*(?:,|of\s+)?{year}\b",
            text,
        )
        if not range_match:
            range_match = re.search(
                rf"\b({month_names})\s+\d{{4}}\b\s*(?:to|through|until|[-–])\s*"
                rf"\b({month_names})\s+{year}\b",
                text,
            )
        if range_match:
            start_month = _MONTHS[range_match.group(1)]
            end_month = _MONTHS[range_match.group(2)]
            return f"{_month_text(year, start_month)}/{_month_text(year, end_month)}"
        month_match = re.search(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b", text)
        if month_match:
            month = _MONTHS[month_match.group(1)]
            return _month_text(year, month)
        if re.search(r"\b(mid[- ]?year|midyear)\b", text):
            return _month_text(year, 7)
        if re.search(r"\b(end|year[- ]?end|year[- ]?ended|dec(?:ember)?\s+31)\b", text):
            return _month_text(year, 12)
        if re.search(r"\b(start|beginning|year[- ]?start|january\s+1)\b", text):
            return _month_text(year, 1)
        if re.search(r"\b(ytd|year[- ]?to[- ]?date|year[- ]?through)\b", text):
            return f"{_month_text(year, 1)}/{_month_text(year, 12)}"
        # Population's year-only value is commonly a point estimate but its
        # month is not known.  Do not silently turn it into July.
        if str(metric).casefold() == "population":
            # The explicit unknown suffix prevents this point estimate from
            # being mistaken for an annual January-to-December measurement.
            return f"{year}-unknown"
        return f"{_month_text(year, 1)}/{_month_text(year, 12)}"

    return text


def _period(statistic: dict[str, Any], fallback: Any, metric: str = "") -> str:
    start = str(statistic.get("period_start") or "").strip()
    end = str(statistic.get("period_end") or "").strip()
    raw = f"{start}/{end}" if start and end else (statistic.get("measured_period") or fallback)
    return _canonical_period_text(raw, metric)


def _raw_period(statistic: dict[str, Any], fallback: Any) -> str:
    start = str(statistic.get("period_start") or "").strip()
    end = str(statistic.get("period_end") or "").strip()
    if start and end:
        return f"{start}/{end}"
    return " ".join(str(statistic.get("measured_period") or fallback or "").split())


def _unit_factor(unit: str) -> float:
    normalized = str(unit or "").casefold().strip()
    factors = {"thousand": 1_000.0, "thousands": 1_000.0, "million": 1_000_000.0,
               "millions": 1_000_000.0, "billion": 1_000_000_000.0,
               "billions": 1_000_000_000.0}
    factor = 1.0
    for token in re.findall(r"[a-z]+", normalized):
        factor *= factors.get(token, 1.0)
    return factor


def _comparison_value(metric: str, raw_value: Any, unit: str) -> Decimal:
    converted = Decimal(str(raw_value)) * Decimal(str(_unit_factor(unit)))
    metric_name = str(metric or "").casefold()
    if metric_name == "population":
        absolute = abs(converted)
        # Keep very small synthetic/test populations meaningful rather than
        # collapsing every value below one thousand to zero.  The first
        # population tier still applies from one thousand upward.
        if absolute < Decimal("1000"):
            return converted
        quantum = Decimal("1000") if absolute < Decimal("1000000") else (
            Decimal("10000") if absolute < Decimal("10000000") else Decimal("100000")
        )
    else:
        quantum = ROUNDING_METRICS.get(metric_name)
    if quantum is None:
        return converted
    # Decimal.quantize controls decimal exponent, not multiples (quantizing
    # to Decimal("1000") would only remove decimals).  Divide first so 1,000,
    # 10,000 and 100,000 are genuine increments and ties are half-up.
    return (converted / quantum).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * quantum


def _group_unit(unit: str) -> str:
    """Return a unit identity after harmless count-scale normalization."""
    normalized = str(unit or "").casefold().strip()
    if normalized in {"", "person", "people", "persons", "count", "counts", "unit", "units"}:
        return "base"
    if _unit_factor(normalized) != 1.0:
        return "base"
    return normalized


def _cluster_key(value: float) -> str:
    # Stable representation treats ordinary decimal formatting/rounding as
    # one value, while leaving materially different values separate.
    if value == 0:
        return "0"
    return f"{value:.12g}"


def _claim_dimensions(finding: dict[str, Any]) -> list[dict[str, Any]]:
    iso3 = str(finding.get("geography_iso3") or "").strip().upper()
    if not iso3:
        iso3 = str(finding.get("geography") or "").strip().casefold()
    dimensions = []
    for metric, statistic in (finding.get("statistics") or {}).items():
        if not isinstance(statistic, dict) or statistic.get("value") is None:
            continue
        try:
            stored_value = float(statistic["value"])
        except (TypeError, ValueError):
            continue
        # The extraction contract stores a count metric in base units while
        # source_value preserves the number printed by the publisher.  Phase
        # 4 clusters on the latter after applying its displayed unit.  Using
        # value here would multiply already-normalised counts a second time
        # (for example, 27,900,000 "million people" became 27.9 trillion).
        try:
            reported_value = float(statistic.get("source_value"))
        except (TypeError, ValueError):
            reported_value = stored_value
        unit = str(statistic.get("unit") or "").strip()
        # A displayed scale such as “million people” is reconstructed from
        # source_value. With no scale, value is already the base comparison
        # value; source_value may instead be a monthly/quarterly or table
        # display value and must not shrink the plotted observation.
        normalized_input = reported_value if _unit_factor(unit) != 1.0 else stored_value
        normalized = _comparison_value(str(metric), normalized_input, unit)
        # Article/effective publication dates are deliberately not used as a
        # measured period fallback.  A missing measured period stays missing
        # rather than being assigned the publication month.
        period = _period(statistic, None, str(metric))
        raw_period = _raw_period(statistic, None)
        if not period:
            continue
        dimensions.append({
            "iso3": iso3, "metric": str(metric), "period": period,
            "raw_period": raw_period, "normalized_period": period,
            "unit": unit, "group_unit": _group_unit(unit),
            "definition": str(statistic.get("definition") or "").strip(),
            "value": reported_value, "raw_value": reported_value,
            "normalized_value": float(normalized),
        })
    return dimensions


def _repair_claim_scale_from_findings(conn: sqlite3.Connection) -> None:
    """Correct historical Phase 4 claims created before source_value was used.

    This is deliberately a narrow, idempotent data repair. Only claims whose
    stored finding supplies an explicit source_value are moved; unannotated
    legacy records retain their original representation.
    """
    rows = conn.execute("SELECT id, finding_json FROM webpage_findings").fetchall()
    affected_clusters: set[int] = set()
    affected_groups: set[int] = set()
    for finding_id, payload in rows:
        try:
            finding = json.loads(payload or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        for item in _claim_dimensions(finding):
            statistic = (finding.get("statistics") or {}).get(item["metric"]) or {}
            if statistic.get("source_value") is None:
                continue
            claims = conn.execute(
                "SELECT id, observation_group_id, value_cluster_id, value, raw_value, normalized_value "
                "FROM metric_claims WHERE finding_id = ? AND metric = ? AND observation_period = ?",
                (int(finding_id), item["metric"], item["period"]),
            ).fetchall()
            for claim_id, group_id, cluster_id, value, raw_value, normalized_value in claims:
                if (value == item["value"] and raw_value == item["raw_value"]
                        and normalized_value == item["normalized_value"]):
                    continue
                destination, _ = _find_cluster(conn, int(group_id), item["normalized_value"])
                conn.execute(
                    "UPDATE metric_claims SET value = ?, raw_value = ?, normalized_value = ?, value_cluster_id = ? WHERE id = ?",
                    (item["value"], item["raw_value"], item["normalized_value"], destination, int(claim_id)),
                )
                affected_clusters.update({int(cluster_id), destination})
                affected_groups.add(int(group_id))
    for cluster_id in affected_clusters:
        _recalculate_cluster(conn, cluster_id)
    for group_id in affected_groups:
        _auto_select_group(conn, group_id)
    conn.execute("DELETE FROM value_clusters WHERE id NOT IN (SELECT DISTINCT value_cluster_id FROM metric_claims)")


def _reconcile_normalized_groups(conn: sqlite3.Connection) -> None:
    """Idempotently migrate old groups to canonical periods, units and values."""
    rows = conn.execute(
        "SELECT id, iso3, metric, observation_period, raw_observation_period, unit, definition "
        "FROM observation_groups ORDER BY id"
    ).fetchall()
    destinations: dict[tuple[str, str, str, str, str], int] = {}
    for group_id, iso3, metric, period, raw_period, unit, definition in rows:
        canonical_period = _canonical_period_text(period, metric)
        canonical_unit = _group_unit(unit)
        key = (iso3, metric, canonical_period, canonical_unit, definition)
        destination = destinations.get(key)
        if destination is None:
            existing = conn.execute(
                "SELECT id FROM observation_groups WHERE iso3 = ? AND metric = ? "
                "AND observation_period = ? AND unit = ? AND definition = ? ORDER BY id LIMIT 1",
                key,
            ).fetchone()
            if existing:
                destination = int(existing[0])
            else:
                destination = int(group_id)
            destinations[key] = destination
            conn.execute(
                "UPDATE observation_groups SET observation_period = ?, normalized_period = ?, "
                "raw_observation_period = COALESCE(raw_observation_period, ?) , unit = ? WHERE id = ?",
                (canonical_period, canonical_period, raw_period or period, canonical_unit, destination),
            )

        claims = conn.execute(
            "SELECT id, value, unit, raw_observation_period, value_cluster_id FROM metric_claims "
            "WHERE observation_group_id = ? ORDER BY id", (int(group_id),)
        ).fetchall()
        for claim_id, raw_value, claim_unit, claim_raw_period, old_cluster in claims:
            # Existing claims may have retained a pre-Phase-4 raw period.  If
            # not, the old group period is the only available source value.
            raw_claim_period = claim_raw_period or raw_period or period
            comparison = _comparison_value(metric, raw_value, claim_unit or unit)
            cluster_id, _ = _find_cluster(conn, destination, float(comparison))
            conn.execute(
                "UPDATE metric_claims SET observation_group_id = ?, value_cluster_id = ?, "
                "observation_period = ?, normalized_period = ?, raw_observation_period = ?, "
                "raw_value = COALESCE(raw_value, value), normalized_value = ? WHERE id = ?",
                (destination, cluster_id, canonical_period, canonical_period, raw_claim_period,
                 float(comparison), int(claim_id)),
            )
        if int(group_id) != destination:
            conn.execute("DELETE FROM value_clusters WHERE observation_group_id = ?", (int(group_id),))
            conn.execute("DELETE FROM observation_groups WHERE id = ?", (int(group_id),))

    # Remove any orphaned clusters left by a merge, then refresh score and
    # primary selection state for every surviving group.
    conn.execute("DELETE FROM value_clusters WHERE id NOT IN (SELECT DISTINCT value_cluster_id FROM metric_claims)")
    for (group_id,) in conn.execute("SELECT id FROM observation_groups").fetchall():
        for (cluster_id,) in conn.execute("SELECT id FROM value_clusters WHERE observation_group_id = ?", (group_id,)).fetchall():
            _recalculate_cluster(conn, int(cluster_id))
        _auto_select_group(conn, int(group_id))


def assess_source(finding: dict[str, Any], *, classification: str | None = None,
                  source_assessment: dict[str, Any] | None = None,
                  decision_origin: str = "automated_assessment") -> dict[str, Any]:
    """Validate an LLM/rule proposal and map it to deterministic points."""
    proposal = source_assessment if isinstance(source_assessment, dict) else {}
    selected = classification or proposal.get("classification")
    if selected not in SOURCE_CLASSES or selected == "legacy_unreviewed":
        selected = "official_publisher" if finding.get("official_source") else (
            "secondary_attributed" if str(finding.get("quoted_source") or "").strip()
            else "secondary_unattributed"
        )
    if decision_origin not in DECISION_ORIGINS:
        decision_origin = "automated_assessment"
    return {
        "classification": selected,
        "points": SOURCE_POINTS[selected],
        "reason": str(proposal.get("reason") or "Deterministic source classification from extraction provenance."),
        "model": proposal.get("model"),
        "prompt_version": proposal.get("prompt_version"),
        "rule_version": proposal.get("rule_version"),
        "assessed_at": proposal.get("assessed_at") or _now(),
        "decision_origin": decision_origin,
    }


def _get_or_create(conn: sqlite3.Connection, table: str, columns: tuple[str, ...], values: tuple[Any, ...]) -> int:
    where = " AND ".join(f"{column} = ?" for column in columns)
    row = conn.execute(f"SELECT id FROM {table} WHERE {where}", values).fetchone()
    if row:
        return int(row[0])
    timestamp = _now()
    names = ", ".join((*columns, "created_at"))
    placeholders = ", ".join("?" for _ in (*values, timestamp))
    cursor = conn.execute(f"INSERT INTO {table} ({names}) VALUES ({placeholders})", (*values, timestamp))
    return int(cursor.lastrowid)


def _find_cluster(conn: sqlite3.Connection, group_id: int, normalized_value: float) -> tuple[int, float]:
    rows = conn.execute("SELECT id, normalized_value FROM value_clusters WHERE observation_group_id = ?", (group_id,)).fetchall()
    for cluster_id, existing in rows:
        # Values have already been converted and rounded by metric.  Exact
        # comparison here makes the grouping rule deterministic and removes
        # the former blanket 0.5% tolerance.
        if Decimal(str(existing)) == Decimal(str(normalized_value)):
            return int(cluster_id), float(existing)
    key = _cluster_key(normalized_value)
    cursor = conn.execute(
        "INSERT INTO value_clusters(observation_group_id, normalized_value, cluster_key, created_at) VALUES (?, ?, ?, ?)",
        (group_id, normalized_value, key, _now()),
    )
    return int(cursor.lastrowid), normalized_value


def _recalculate_cluster(conn: sqlite3.Connection, cluster_id: int) -> dict[str, Any]:
    rows = conn.execute("SELECT source_classification, evidence_points, manual_points, display_disposition FROM metric_claims WHERE value_cluster_id = ? AND display_disposition != 'rejected'", (cluster_id,)).fetchall()
    raw = sum(int(row[2] if row[2] is not None else SOURCE_POINTS[row[0]]) for row in rows)
    effective = 0
    for classification in SOURCE_CLASSES:
        class_rows = [row for row in rows if row[0] == classification]
        manual = sum(int(row[2]) for row in class_rows if row[2] is not None)
        automatic = sum(SOURCE_POINTS[classification] for row in class_rows if row[2] is None)
        # A deliberate administrator points override is an exceptional
        # decision and may exceed the normal class cap, but the cluster-wide
        # maximum remains 16.
        effective += manual + min(automatic, SOURCE_CAPS[classification])
    effective = min(effective, 16)
    conn.execute("UPDATE metric_claims SET raw_points = ?, effective_points = ? WHERE value_cluster_id = ?", (raw, effective, cluster_id))
    return {"raw_points": raw, "effective_points": effective,
            "supporting_document_count": len(rows),
            "strong_evidence": effective >= STRONG_EVIDENCE_THRESHOLD}


def _auto_select_group(conn: sqlite3.Connection, group_id: int) -> None:
    clusters = conn.execute("""SELECT vc.id, MAX(mc.effective_points), COUNT(DISTINCT mc.source_document_id), MIN(mc.id)
        FROM value_clusters vc JOIN metric_claims mc ON mc.value_cluster_id = vc.id
        WHERE vc.observation_group_id = ? AND mc.display_disposition != 'rejected'
        GROUP BY vc.id ORDER BY MAX(mc.effective_points) DESC, COUNT(DISTINCT mc.source_document_id) DESC, MIN(mc.id) ASC""", (group_id,)).fetchall()
    if not clusters:
        return
    # A manually selected primary is preserved.  Automated selection only
    # changes claims which have not been explicitly selected as primary.
    manual = conn.execute("SELECT 1 FROM metric_claims WHERE observation_group_id = ? AND display_disposition = 'primary' AND decision_origin = 'manual_override' LIMIT 1", (group_id,)).fetchone()
    if manual:
        return
    primary_cluster = int(clusters[0][0])
    conn.execute("UPDATE metric_claims SET display_disposition = CASE WHEN value_cluster_id = ? THEN 'primary' ELSE 'approved_secondary' END WHERE observation_group_id = ? AND display_disposition != 'rejected'", (primary_cluster, group_id))


def sync_finding_claims(conn: sqlite3.Connection, finding_id: int, finding: dict[str, Any],
                        *, classification: str | None = None,
                        provenance: dict[str, Any] | None = None,
                        decision_origin: str | None = None) -> list[int]:
    """Project all numeric metrics in a finding into grouped claims."""
    provenance = provenance or {}
    assessment = assess_source(
        finding, classification=classification,
        source_assessment=provenance.get("source_assessment"),
        decision_origin=decision_origin or provenance.get("decision_origin") or "automated_assessment",
    )
    source_url = str(finding.get("url") or "").strip()
    canonical = _canonical_url(source_url)
    document_id = _get_or_create(conn, "source_documents", ("canonical_url",), (canonical,))
    conn.execute("UPDATE source_documents SET source_url = ?, publisher_domain = ? WHERE id = ?", (source_url, urlsplit(canonical).hostname, document_id))
    claim_ids = []
    for item in _claim_dimensions(finding):
        group_id = _get_or_create(conn, "observation_groups", ("iso3", "metric", "observation_period", "unit", "definition"), (item["iso3"], item["metric"], item["period"], item["group_unit"], item["definition"]))
        conn.execute(
            "UPDATE observation_groups SET raw_observation_period = COALESCE(raw_observation_period, ?), normalized_period = ? WHERE id = ?",
            (item["raw_period"], item["normalized_period"], group_id),
        )
        cluster_id, _ = _find_cluster(conn, group_id, item["normalized_value"])
        existing = conn.execute("SELECT id FROM metric_claims WHERE source_document_id = ? AND finding_id = ? AND metric = ? AND observation_period = ? AND value = ?", (document_id, finding_id, item["metric"], item["period"], item["value"])).fetchone()
        if existing:
            claim_id = int(existing[0])
        else:
            cursor = conn.execute("""INSERT INTO metric_claims(
                source_document_id, finding_id, observation_group_id, value_cluster_id,
                iso3, metric, observation_period, raw_observation_period, normalized_period,
                unit, definition, value, raw_value, normalized_value,
                underlying_source,
                source_classification, automated_classification, automated_reason,
                assessment_model, assessment_prompt_version, assessment_rule_version, assessed_at,
                evidence_points, decision_origin, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (document_id, finding_id, group_id, cluster_id, item["iso3"], item["metric"], item["period"], item["raw_period"], item["normalized_period"], item["unit"], item["definition"], item["value"], item["raw_value"], item["normalized_value"], str(finding.get("underlying_source") or "other"), assessment["classification"], assessment["classification"], assessment["reason"], assessment["model"], assessment["prompt_version"], assessment["rule_version"], assessment["assessed_at"], assessment["points"], assessment["decision_origin"], _now()))
            claim_id = int(cursor.lastrowid)
        claim_ids.append(claim_id)
        _recalculate_cluster(conn, cluster_id)
        _auto_select_group(conn, group_id)
    return claim_ids


def _migrate_legacy_findings(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, source_url, canonical_url, finding_json, source_classification FROM webpage_findings ORDER BY id").fetchall()
    for finding_id, source_url, canonical_url, payload, classification in rows:
        existing = conn.execute("SELECT 1 FROM metric_claims WHERE finding_id = ? LIMIT 1", (finding_id,)).fetchone()
        if existing:
            continue
        try:
            finding = json.loads(payload or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        # Legacy rows are never retrospectively assessed.  The classification
        # is retained only as a source label; points remain zero until manual
        # or future explicit reclassification.
        source_doc = _get_or_create(conn, "source_documents", ("canonical_url",), (_canonical_url(canonical_url or source_url),))
        conn.execute("UPDATE source_documents SET source_url = ? WHERE id = ?", (source_url, source_doc))
        for item in _claim_dimensions(finding):
            group = _get_or_create(conn, "observation_groups", ("iso3", "metric", "observation_period", "unit", "definition"), (item["iso3"], item["metric"], item["period"], item["group_unit"], item["definition"]))
            conn.execute(
                "UPDATE observation_groups SET raw_observation_period = COALESCE(raw_observation_period, ?), normalized_period = ? WHERE id = ?",
                (item["raw_period"], item["normalized_period"], group),
            )
            cluster, _ = _find_cluster(conn, group, item["normalized_value"])
            conn.execute("""INSERT OR IGNORE INTO metric_claims(
                source_document_id, finding_id, observation_group_id, value_cluster_id,
                iso3, metric, observation_period, raw_observation_period, normalized_period,
                unit, definition, value, raw_value, normalized_value,
                source_classification, automated_classification, automated_reason,
                evidence_points, raw_points, effective_points, display_disposition,
                decision_origin, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'legacy_unreviewed', NULL,
                        'Retained legacy finding; no historical assessment inferred.', 0, 0, 0,
                        'approved_secondary', 'migration', ?)""",
                (source_doc, finding_id, group, cluster, item["iso3"], item["metric"], item["period"], item["raw_period"], item["normalized_period"], item["unit"], item["definition"], item["value"], item["raw_value"], item["normalized_value"], _now()))
            _recalculate_cluster(conn, cluster)
            _auto_select_group(conn, group)


def list_claims(conn: sqlite3.Connection, *, iso3: str | None = None, metric: str | None = None,
                mode: str = "all", include_rejected: bool = False) -> list[dict[str, Any]]:
    if mode not in {"all", "primary", "primary_approved_secondary"}:
        raise ValueError("mode must be all, primary, or primary_approved_secondary")
    where, params = ["1 = 1"], []
    if iso3:
        where.append("mc.iso3 = ?"); params.append(str(iso3).upper())
    if metric:
        where.append("mc.metric = ?"); params.append(str(metric))
    if mode == "primary":
        where.append("mc.display_disposition = 'primary'")
    elif mode == "primary_approved_secondary":
        where.append("mc.display_disposition IN ('primary', 'approved_secondary')")
    conn.row_factory = sqlite3.Row
    rejected_clause = "" if include_rejected else "AND mc.display_disposition != 'rejected'"
    rows = conn.execute(f"""SELECT mc.*, sd.source_url, sd.canonical_url, vc.observation_group_id,
        vc.normalized_value AS normalized_value,
        (SELECT COUNT(DISTINCT source_document_id) FROM metric_claims c2 WHERE c2.value_cluster_id = mc.value_cluster_id AND c2.display_disposition != 'rejected') AS supporting_document_count,
        (SELECT COUNT(DISTINCT c3.value_cluster_id) FROM metric_claims c3 WHERE c3.observation_group_id = mc.observation_group_id AND c3.value_cluster_id != mc.value_cluster_id AND c3.display_disposition != 'rejected') AS conflicting_cluster_count
        FROM metric_claims mc JOIN source_documents sd ON sd.id = mc.source_document_id
        JOIN value_clusters vc ON vc.id = mc.value_cluster_id WHERE {' AND '.join(where)}
        {rejected_clause} ORDER BY mc.iso3, mc.metric, mc.observation_period, mc.id""", params).fetchall()
    return [dict(row) for row in rows]


def override_claim(conn: sqlite3.Connection, claim_id: int, *, actor: str,
                   classification: str | None = None, points: int | None = None,
                   disposition: str | None = None, reason: str | None = None,
                   action: str | None = None, peer_claim_ids: list[int] | None = None,
                   definition: str | None = None) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM metric_claims WHERE id = ?", (int(claim_id),)).fetchone()
    if row is None:
        raise ValueError(f"No metric claim exists with ID {claim_id}.")
    before = dict(zip([column[1] for column in conn.execute("PRAGMA table_info(metric_claims)")], row))
    if classification is not None and classification not in SOURCE_CLASSES:
        raise ValueError("Invalid source classification.")
    if disposition is not None and disposition not in DISPLAY_DISPOSITIONS:
        raise ValueError("Invalid display disposition.")
    if points is not None and (int(points) < 0 or int(points) > 16):
        raise ValueError("Manual evidence points must be between 0 and 16.")
    if action not in {None, "merge_equivalent", "separate_definition"}:
        raise ValueError("Unsupported claim review action.")
    merge_clusters: set[int] = set()
    merge_groups: set[int] = set()
    grouping_snapshot: list[dict[str, Any]] = []
    if action == "merge_equivalent":
        peer_ids = [int(value) for value in (peer_claim_ids or [])]
        peer_ids = list(dict.fromkeys([int(claim_id), *peer_ids]))
        peer_rows = conn.execute(
            f"SELECT id, observation_group_id, value_cluster_id, decision_origin, manual_override_json, display_disposition FROM metric_claims WHERE id IN ({','.join('?' for _ in peer_ids)})",
            peer_ids,
        ).fetchall()
        if len(peer_rows) != len(peer_ids) or len({row[1] for row in peer_rows}) != 1:
            raise ValueError("Equivalent claims must belong to one observation group.")
        destination = next(row[2] for row in peer_rows if row[0] == int(claim_id))
        merge_clusters = {int(row[2]) for row in peer_rows}
        merge_groups = {int(row[1]) for row in peer_rows}
        grouping_snapshot = [dict(zip(["claim_id", "observation_group_id", "value_cluster_id", "decision_origin", "manual_override_json", "display_disposition"], row)) for row in peer_rows]
        conn.executemany("UPDATE metric_claims SET value_cluster_id = ?, decision_origin = 'manual_override' WHERE id = ?", [(destination, value) for value in peer_ids])
    if action == "separate_definition":
        if not str(definition or "").strip():
            raise ValueError("A separate_definition action requires a non-empty definition.")
        row_definition = str(definition).strip()
        group_id = _get_or_create(conn, "observation_groups", ("iso3", "metric", "observation_period", "unit", "definition"), (row[5], row[6], row[7], row[8], row_definition))
        normalized_value = conn.execute("SELECT normalized_value FROM value_clusters WHERE id = ?", (int(row[4]),)).fetchone()[0]
        cluster_id, _ = _find_cluster(conn, group_id, float(normalized_value))
        conn.execute("UPDATE metric_claims SET definition = ?, observation_group_id = ?, value_cluster_id = ?, decision_origin = 'manual_override' WHERE id = ?", (row_definition, group_id, cluster_id, int(claim_id)))
        _recalculate_cluster(conn, cluster_id)
    updates, params = ["decision_origin = 'manual_override'", "manual_override_json = ?"], [json.dumps({"actor": actor, "reason": reason, "overridden_at": _now()})]
    if classification is not None:
        updates.append("source_classification = ?"); params.append(classification)
        if points is None:
            updates.append("evidence_points = ?"); params.append(SOURCE_POINTS[classification])
    if points is not None:
        updates.extend(["manual_points = ?", "evidence_points = ?"]); params.extend([int(points), int(points)])
    if disposition is not None: updates.append("display_disposition = ?"); params.append(disposition)
    params.append(int(claim_id))
    conn.execute(f"UPDATE metric_claims SET {', '.join(updates)} WHERE id = ?", params)
    after_row = conn.execute("SELECT * FROM metric_claims WHERE id = ?", (int(claim_id),)).fetchone()
    columns = [column[1] for column in conn.execute("PRAGMA table_info(metric_claims)")]
    after = dict(zip(columns, after_row))
    _recalculate_cluster(conn, int(after["value_cluster_id"]))
    _auto_select_group(conn, int(after["observation_group_id"]))
    for cluster_id in merge_clusters:
        _recalculate_cluster(conn, cluster_id)
    for group_id in merge_groups:
        _auto_select_group(conn, group_id)
    refreshed = conn.execute("SELECT * FROM metric_claims WHERE id = ?", (int(claim_id),)).fetchone()
    final = dict(zip(columns, refreshed))
    if grouping_snapshot:
        before["_grouping_snapshot"] = grouping_snapshot
    audit_action = action or "manual_override"
    conn.execute("INSERT INTO claim_decision_audit(claim_id, action, before_json, after_json, actor, reason, acted_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (claim_id, audit_action, json.dumps(before, default=str), json.dumps(final, default=str), actor, reason, _now()))
    return final


def reject_finding_claims(conn: sqlite3.Connection, finding_id: int, *, actor: str = "system",
                          reason: str = "Underlying finding was removed.") -> int:
    """Remove deleted findings from ordinary graph modes while retaining audit data."""
    affected = conn.execute("SELECT DISTINCT value_cluster_id, observation_group_id FROM metric_claims WHERE finding_id = ? AND display_disposition != 'rejected'", (int(finding_id),)).fetchall()
    cursor = conn.execute(
        "UPDATE metric_claims SET display_disposition = 'rejected', decision_origin = 'manual_override', manual_override_json = ? WHERE finding_id = ? AND display_disposition != 'rejected'",
        (json.dumps({"actor": actor, "reason": reason, "overridden_at": _now()}), int(finding_id)),
    )
    for cluster_id, group_id in affected:
        _recalculate_cluster(conn, int(cluster_id))
        _auto_select_group(conn, int(group_id))
    return int(cursor.rowcount)


def reject_finding_metric_claims(conn: sqlite3.Connection, finding_id: int, metric: str,
                                 *, actor: str = "system",
                                 reason: str = "Metric was removed from the underlying finding.") -> int:
    """Hide claims for one deleted metric while retaining their audit history."""
    affected = conn.execute("SELECT DISTINCT value_cluster_id, observation_group_id FROM metric_claims WHERE finding_id = ? AND metric = ? AND display_disposition != 'rejected'", (int(finding_id), str(metric))).fetchall()
    cursor = conn.execute(
        "UPDATE metric_claims SET display_disposition = 'rejected', decision_origin = 'manual_override', manual_override_json = ? WHERE finding_id = ? AND metric = ? AND display_disposition != 'rejected'",
        (json.dumps({"actor": actor, "reason": reason, "overridden_at": _now()}), int(finding_id), str(metric)),
    )
    for cluster_id, group_id in affected:
        _recalculate_cluster(conn, int(cluster_id))
        _auto_select_group(conn, int(group_id))
    return int(cursor.rowcount)
