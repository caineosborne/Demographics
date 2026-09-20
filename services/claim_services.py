"""Phase 4 claim read/write services used by the private admin boundary."""

from __future__ import annotations

import json
from typing import Any

from data import phase4_claims, tools


def list_claims(iso3: str | None = None, metric: str | None = None,
                mode: str = "all", include_rejected: bool = False) -> list[dict[str, Any]]:
    tools.initialise_findings_table()
    with tools.get_connection() as conn:
        return phase4_claims.list_claims(conn, iso3=iso3, metric=metric, mode=mode,
                                         include_rejected=include_rejected)


def override_claim(claim_id: int, *, actor: str = "local", classification: str | None = None,
                   points: int | None = None, disposition: str | None = None,
                   reason: str | None = None, action: str | None = None,
                   peer_claim_ids: list[int] | None = None,
                   definition: str | None = None) -> dict[str, Any]:
    tools.initialise_findings_table()
    with tools.get_connection() as conn:
        return phase4_claims.override_claim(
            conn, claim_id, actor=actor, classification=classification,
            points=points, disposition=disposition, reason=reason,
            action=action, peer_claim_ids=peer_claim_ids, definition=definition,
        )


def undo_claim_override(claim_id: int, *, actor: str = "local", reason: str | None = None) -> dict[str, Any]:
    """Restore the latest audited claim state without deleting its audit row."""
    tools.initialise_findings_table()
    with tools.get_connection() as conn:
        audit = conn.execute(
            "SELECT id, action, before_json FROM claim_decision_audit WHERE claim_id = ? ORDER BY id DESC LIMIT 1",
            (int(claim_id),),
        ).fetchone()
        if audit is None:
            raise ValueError(f"No override exists for claim {claim_id}.")
        if audit[1] not in {"manual_override", "merge_equivalent", "separate_definition"}:
            raise ValueError(f"Claim {claim_id} has no active override to undo.")
        before = json.loads(audit[2] or "{}")
        allowed = {
            "source_classification", "manual_points", "display_disposition",
            "evidence_points", "decision_origin", "manual_override_json",
            "observation_group_id", "value_cluster_id", "definition",
        }
        values = {key: before.get(key) for key in allowed if key in before}
        if not values:
            raise ValueError(f"Override history for claim {claim_id} is not restorable.")
        current_row = conn.execute("SELECT * FROM metric_claims WHERE id = ?", (int(claim_id),)).fetchone()
        columns = [column[1] for column in conn.execute("PRAGMA table_info(metric_claims)")]
        current = dict(zip(columns, current_row))
        assignments = ", ".join(f"{key} = ?" for key in values)
        conn.execute(
            f"UPDATE metric_claims SET {assignments} WHERE id = ?",
            (*values.values(), int(claim_id)),
        )
        snapshots = before.get("_grouping_snapshot") or []
        for snapshot in snapshots:
            conn.execute(
                "UPDATE metric_claims SET observation_group_id = ?, value_cluster_id = ?, decision_origin = ?, manual_override_json = ?, display_disposition = ? WHERE id = ?",
                (snapshot["observation_group_id"], snapshot["value_cluster_id"], snapshot["decision_origin"], snapshot.get("manual_override_json"), snapshot["display_disposition"], snapshot["claim_id"]),
            )
        restored = conn.execute("SELECT * FROM metric_claims WHERE id = ?", (int(claim_id),)).fetchone()
        result = dict(zip(columns, restored))
        undo_audit = conn.execute(
            "INSERT INTO claim_decision_audit(claim_id, action, before_json, after_json, actor, reason, acted_at) VALUES (?, 'undo_override', ?, ?, ?, ?, ?)",
            (claim_id, json.dumps(current, default=str), json.dumps(result, default=str), actor, reason, phase4_claims._now()),
        )
        phase4_claims._recalculate_cluster(conn, int(result["value_cluster_id"]))
        phase4_claims._auto_select_group(conn, int(result["observation_group_id"]))
        affected_clusters = {int(result["value_cluster_id"]), int(before.get("value_cluster_id", result["value_cluster_id"]))}
        affected_groups = {int(result["observation_group_id"]), int(before.get("observation_group_id", result["observation_group_id"]))}
        for snapshot in snapshots:
            affected_clusters.add(int(snapshot["value_cluster_id"]))
            affected_groups.add(int(snapshot["observation_group_id"]))
        for cluster_id in affected_clusters:
            phase4_claims._recalculate_cluster(conn, cluster_id)
        for group_id in affected_groups:
            phase4_claims._auto_select_group(conn, group_id)
        refreshed = conn.execute("SELECT * FROM metric_claims WHERE id = ?", (int(claim_id),)).fetchone()
        final = dict(zip(columns, refreshed))
        conn.execute(
            "UPDATE claim_decision_audit SET after_json = ? WHERE id = ?",
            (json.dumps(final, default=str), undo_audit.lastrowid),
        )
        return final
