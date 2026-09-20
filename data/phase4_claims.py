"""Phase 4 claims, provenance, scoring, and review persistence.

The claim tables deliberately sit beside ``webpage_findings``.  The older
finding row (including its provider/search score in ``finding_json``) remains
the immutable extraction record; this module is the typed projection used by
the Phase 4 graph and review APIs.
"""

from __future__ import annotations

import json
import math
import sqlite3
import re
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
# Values within half a percent are treated as harmless publisher rounding.
# Unit conversion is applied before this comparison; materially different
# values remain separate conflict clusters.
ROUNDING_RELATIVE_TOLERANCE = 0.005


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
            unit TEXT NOT NULL DEFAULT '',
            definition TEXT NOT NULL DEFAULT '',
            value REAL NOT NULL,
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
    if "assessment_rule_version" not in claim_columns:
        conn.execute("ALTER TABLE metric_claims ADD COLUMN assessment_rule_version TEXT")
    if "underlying_source" not in claim_columns:
        conn.execute("ALTER TABLE metric_claims ADD COLUMN underlying_source TEXT NOT NULL DEFAULT 'other'")
    if migrate_legacy:
        _migrate_legacy_findings(conn)
    _reconcile_normalized_groups(conn)


def _canonical_url(url: str) -> str:
    parts = urlsplit(str(url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return str(url or "").strip()
    query = sorted((key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                   if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"})
    return urlunsplit(("https", parts.hostname.lower().removeprefix("www."),
                       parts.path.rstrip("/") or "/", urlencode(query), ""))


def _period(statistic: dict[str, Any], fallback: Any) -> str:
    start, end = str(statistic.get("period_start") or "").strip(), str(statistic.get("period_end") or "").strip()
    if start and end:
        return f"{start}/{end}"
    return " ".join(str(statistic.get("measured_period") or fallback or "").split()).casefold()


def _unit_factor(unit: str) -> float:
    normalized = str(unit or "").casefold().strip()
    factors = {"thousand": 1_000.0, "thousands": 1_000.0, "million": 1_000_000.0,
               "millions": 1_000_000.0, "billion": 1_000_000_000.0,
               "billions": 1_000_000_000.0}
    factor = 1.0
    for token in re.findall(r"[a-z]+", normalized):
        factor *= factors.get(token, 1.0)
    return factor


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
            raw_value = float(statistic["value"])
        except (TypeError, ValueError):
            continue
        unit = str(statistic.get("unit") or "").strip()
        normalized = raw_value * _unit_factor(unit)
        period = _period(statistic, finding.get("effective_date"))
        if not period:
            continue
        dimensions.append({
            "iso3": iso3, "metric": str(metric), "period": period,
            "unit": unit, "group_unit": _group_unit(unit),
            "definition": str(statistic.get("definition") or "").strip(),
            "value": raw_value, "normalized_value": normalized,
        })
    return dimensions


def _reconcile_normalized_groups(conn: sqlite3.Connection) -> None:
    """Merge pre-normalization unit groups into their canonical identities."""
    rows = conn.execute(
        "SELECT id, iso3, metric, observation_period, unit, definition FROM observation_groups ORDER BY id"
    ).fetchall()
    grouped: dict[tuple[str, str, str, str, str], list[tuple[Any, ...]]] = {}
    for group_id, iso3, metric, period, unit, definition in rows:
        key = (iso3, metric, period, _group_unit(unit), definition)
        grouped.setdefault(key, []).append((group_id, iso3, metric, period, unit, definition))
    for key, candidates in grouped.items():
        canonical_unit = key[3]
        destination_row = next((row for row in candidates if _group_unit(row[4]) == row[4] == canonical_unit), candidates[0])
        destination = int(destination_row[0])
        # Canonicalize the surviving group itself.  Prefer an existing
        # canonical row above so this UPDATE cannot collide with a UNIQUE key.
        if destination_row[4] != canonical_unit:
            conn.execute("UPDATE observation_groups SET unit = ? WHERE id = ?", (canonical_unit, destination))
        for group_id, _iso3, _metric, _period, _unit, _definition in candidates:
            if int(group_id) == destination:
                continue
            old_clusters = conn.execute(
                "SELECT id, normalized_value FROM value_clusters WHERE observation_group_id = ?",
                (group_id,),
            ).fetchall()
            for old_cluster, normalized_value in old_clusters:
                new_cluster, _ = _find_cluster(conn, destination, float(normalized_value))
                conn.execute("UPDATE metric_claims SET observation_group_id = ?, value_cluster_id = ? WHERE value_cluster_id = ?", (destination, new_cluster, old_cluster))
                conn.execute("DELETE FROM value_clusters WHERE id = ?", (old_cluster,))
            conn.execute("DELETE FROM observation_groups WHERE id = ?", (group_id,))
        clusters = conn.execute("SELECT id FROM value_clusters WHERE observation_group_id = ?", (destination,)).fetchall()
        for (cluster_id,) in clusters:
            _recalculate_cluster(conn, int(cluster_id))
        _auto_select_group(conn, destination)


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
        tolerance = max(1e-9, abs(float(existing)) * ROUNDING_RELATIVE_TOLERANCE)
        if math.isclose(float(existing), normalized_value, rel_tol=1e-9, abs_tol=tolerance):
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
        cluster_id, _ = _find_cluster(conn, group_id, item["normalized_value"])
        existing = conn.execute("SELECT id FROM metric_claims WHERE source_document_id = ? AND finding_id = ? AND metric = ? AND observation_period = ? AND value = ?", (document_id, finding_id, item["metric"], item["period"], item["value"])).fetchone()
        if existing:
            claim_id = int(existing[0])
        else:
            cursor = conn.execute("""INSERT INTO metric_claims(
                source_document_id, finding_id, observation_group_id, value_cluster_id,
                iso3, metric, observation_period, unit, definition, value,
                underlying_source,
                source_classification, automated_classification, automated_reason,
                assessment_model, assessment_prompt_version, assessment_rule_version, assessed_at,
                evidence_points, decision_origin, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (document_id, finding_id, group_id, cluster_id, item["iso3"], item["metric"], item["period"], item["unit"], item["definition"], item["value"], str(finding.get("underlying_source") or "other"), assessment["classification"], assessment["classification"], assessment["reason"], assessment["model"], assessment["prompt_version"], assessment["rule_version"], assessment["assessed_at"], assessment["points"], assessment["decision_origin"], _now()))
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
            cluster, _ = _find_cluster(conn, group, item["normalized_value"])
            conn.execute("""INSERT OR IGNORE INTO metric_claims(
                source_document_id, finding_id, observation_group_id, value_cluster_id,
                iso3, metric, observation_period, unit, definition, value,
                source_classification, automated_classification, automated_reason,
                evidence_points, raw_points, effective_points, display_disposition,
                decision_origin, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'legacy_unreviewed', NULL,
                        'Retained legacy finding; no historical assessment inferred.', 0, 0, 0,
                        'approved_secondary', 'migration', ?)""",
                (source_doc, finding_id, group, cluster, item["iso3"], item["metric"], item["period"], item["unit"], item["definition"], item["value"], _now()))
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
