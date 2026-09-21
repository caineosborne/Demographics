import json
import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from data import phase4_claims
from services import claim_services, read_services


def _schema_connection():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE webpage_findings (
            id INTEGER PRIMARY KEY,
            source_url TEXT NOT NULL,
            canonical_url TEXT,
            finding_json TEXT NOT NULL,
            source_classification TEXT
        )"""
    )
    phase4_claims.ensure_schema(conn, migrate_legacy=False)
    return conn


def _finding(url, value=100.0, *, unit="people", iso3="JPN", period="2025"):
    return {
        "url": url,
        "geography_iso3": iso3,
        "statistics": {
            "population": {
                "value": value,
                "unit": unit,
                "measured_period": period,
            }
        },
    }


class Phase4ClaimsTests(unittest.TestCase):
    def tearDown(self):
        if getattr(self, "conn", None) is not None:
            self.conn.close()

    def setUp(self):
        self.conn = _schema_connection()

    def test_ten_attributed_reports_are_retained_but_class_cap_is_applied(self):
        official = _finding("https://gov.example/population", 100.0)
        phase4_claims.sync_finding_claims(
            self.conn, 1, official, classification="official_publisher"
        )
        for finding_id in range(2, 12):
            phase4_claims.sync_finding_claims(
                self.conn,
                finding_id,
                _finding(f"https://news-{finding_id}.example/report", 100.0),
                classification="secondary_attributed",
            )

        claims = phase4_claims.list_claims(self.conn, iso3="JPN", metric="population")
        self.assertEqual(len(claims), 11)
        self.assertEqual({claim["supporting_document_count"] for claim in claims}, {11})
        self.assertEqual({claim["raw_points"] for claim in claims}, {25})
        self.assertEqual({claim["effective_points"] for claim in claims}, {9})

    def test_legacy_migration_starts_at_zero_without_touching_provider_score(self):
        payload = _finding("https://legacy.example/report", 100.0)
        payload["score"] = 0.91
        self.conn.execute(
            "INSERT INTO webpage_findings VALUES (?, ?, ?, ?, ?)",
            (41, payload["url"], payload["url"], json.dumps(payload), "official_publisher"),
        )
        phase4_claims.ensure_schema(self.conn)

        claim = phase4_claims.list_claims(self.conn, iso3="JPN", metric="population")[0]
        self.assertEqual(claim["source_classification"], "legacy_unreviewed")
        self.assertEqual(claim["evidence_points"], 0)
        self.assertEqual(claim["raw_points"], 0)
        self.assertEqual(claim["effective_points"], 0)
        self.assertEqual(claim["decision_origin"], "migration")
        self.assertEqual(claim["display_disposition"], "primary")
        stored_payload = json.loads(
            self.conn.execute("SELECT finding_json FROM webpage_findings WHERE id = 41").fetchone()[0]
        )
        self.assertEqual(stored_payload["score"], 0.91)

    def test_materially_different_values_are_separate_conflicting_clusters(self):
        phase4_claims.sync_finding_claims(
            self.conn, 1, _finding("https://a.example/report", 100_000.0),
            classification="official_publisher",
        )
        phase4_claims.sync_finding_claims(
            self.conn, 2, _finding("https://b.example/report", 110_000.0),
            classification="secondary_attributed",
        )
        claims = phase4_claims.list_claims(self.conn, iso3="JPN", metric="population")
        self.assertEqual({claim["value_cluster_id"] for claim in claims}.__len__(), 2)
        self.assertEqual({claim["conflicting_cluster_count"] for claim in claims}, {1})

    def test_graph_modes_filter_rejected_claims_and_approved_secondaries(self):
        phase4_claims.sync_finding_claims(
            self.conn, 1, _finding("https://a.example/report", 100.0),
            classification="official_publisher",
        )
        phase4_claims.sync_finding_claims(
            self.conn, 2, _finding("https://b.example/report", 110.0),
            classification="secondary_attributed",
        )
        claims = phase4_claims.list_claims(self.conn, iso3="JPN", metric="population")
        lower_score = min(claims, key=lambda claim: claim["effective_points"])
        phase4_claims.override_claim(
            self.conn, lower_score["id"], disposition="rejected", actor="test", reason="bad claim"
        )
        self.assertEqual(
            len(phase4_claims.list_claims(self.conn, iso3="JPN", metric="population", mode="all")),
            1,
        )
        self.assertEqual(
            len(phase4_claims.list_claims(self.conn, iso3="JPN", metric="population", mode="primary")),
            1,
        )
        self.assertEqual(
            len(phase4_claims.list_claims(self.conn, iso3="JPN", metric="population", mode="primary_approved_secondary")),
            1,
        )
        self.assertEqual(
            len(phase4_claims.list_claims(self.conn, iso3="JPN", metric="population", include_rejected=True)),
            2,
        )

    def test_rejection_removes_active_conflict_but_preserves_audit_claim(self):
        phase4_claims.sync_finding_claims(
            self.conn, 1, _finding("https://a.example/report", 100_000.0),
            classification="official_publisher",
        )
        phase4_claims.sync_finding_claims(
            self.conn, 2, _finding("https://b.example/report", 110_000.0),
            classification="secondary_attributed",
        )
        claims = phase4_claims.list_claims(self.conn, iso3="JPN", metric="population")
        rejected_id = next(claim["id"] for claim in claims if claim["display_disposition"] == "approved_secondary")
        phase4_claims.override_claim(self.conn, rejected_id, disposition="rejected", actor="test", reason="not applicable")
        active = phase4_claims.list_claims(self.conn, iso3="JPN", metric="population")
        self.assertEqual({claim["conflicting_cluster_count"] for claim in active}, {0})
        audit = phase4_claims.list_claims(self.conn, iso3="JPN", metric="population", include_rejected=True)
        self.assertEqual(len(audit), 2)
        self.assertEqual(next(claim for claim in audit if claim["id"] == rejected_id)["display_disposition"], "rejected")

    def test_supporting_document_count_excludes_rejected_support(self):
        phase4_claims.sync_finding_claims(
            self.conn, 1, _finding("https://a.example/report", 100.0),
            classification="official_publisher",
        )
        phase4_claims.sync_finding_claims(
            self.conn, 2, _finding("https://b.example/report", 100.0),
            classification="secondary_attributed",
        )
        claims = phase4_claims.list_claims(self.conn, iso3="JPN", metric="population")
        phase4_claims.override_claim(
            self.conn, claims[0]["id"], disposition="rejected", actor="test", reason="remove support"
        )
        active = phase4_claims.list_claims(self.conn, iso3="JPN", metric="population")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["supporting_document_count"], 1)

    def test_classification_override_recalculates_points_and_is_audited(self):
        claim_id = phase4_claims.sync_finding_claims(
            self.conn, 1, _finding("https://news.example/report", 100.0),
            classification="secondary_attributed",
        )[0]
        updated = phase4_claims.override_claim(
            self.conn,
            claim_id,
            classification="official_publisher",
            actor="reviewer",
            reason="confirmed official release",
        )
        self.assertEqual(updated["decision_origin"], "manual_override")
        self.assertEqual(updated["source_classification"], "official_publisher")
        self.assertEqual(updated["effective_points"], 5)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM claim_decision_audit").fetchone()[0], 1)

    def test_undo_restores_points_and_writes_audit_with_recalculated_values(self):
        claim_id = phase4_claims.sync_finding_claims(
            self.conn, 1, _finding("https://news.example/report", 100.0),
            classification="secondary_attributed",
        )[0]
        phase4_claims.override_claim(
            self.conn, claim_id, classification="official_publisher", actor="reviewer", reason="confirmed"
        )

        @contextmanager
        def connection():
            yield self.conn

        with patch.object(claim_services.tools, "initialise_findings_table", lambda: None), \
             patch.object(claim_services.tools, "get_connection", connection):
            restored = claim_services.undo_claim_override(claim_id, actor="reviewer", reason="revert")

        self.assertEqual(restored["source_classification"], "secondary_attributed")
        self.assertEqual(restored["effective_points"], 2)
        actions = self.conn.execute(
            "SELECT action, before_json, after_json FROM claim_decision_audit ORDER BY id"
        ).fetchall()
        self.assertEqual([row[0] for row in actions], ["manual_override", "undo_override"])
        self.assertEqual(json.loads(actions[-1][2])["effective_points"], 2)

    def test_grouping_override_is_audited_and_undo_restores_clusters(self):
        first = phase4_claims.sync_finding_claims(
            self.conn, 1, _finding("https://a.example/report", 100.0),
            classification="official_publisher",
        )[0]
        second = phase4_claims.sync_finding_claims(
            self.conn, 2, _finding("https://b.example/report", 101.0),
            classification="secondary_attributed",
        )[0]
        before = {
            row[0]: row[1]
            for row in self.conn.execute("SELECT id, value_cluster_id FROM metric_claims")
        }
        before_origins = {
            row[0]: row[1]
            for row in self.conn.execute("SELECT id, decision_origin FROM metric_claims")
        }
        phase4_claims.override_claim(
            self.conn, first, action="merge_equivalent", peer_claim_ids=[second],
            actor="reviewer", reason="rounding difference",
        )
        self.assertEqual(len({row[0] for row in self.conn.execute("SELECT value_cluster_id FROM metric_claims")}), 1)
        self.assertEqual(self.conn.execute("SELECT action FROM claim_decision_audit").fetchone()[0], "merge_equivalent")

        @contextmanager
        def connection():
            yield self.conn

        with patch.object(claim_services.tools, "initialise_findings_table", lambda: None), \
             patch.object(claim_services.tools, "get_connection", connection):
            claim_services.undo_claim_override(first, actor="reviewer", reason="revert grouping")

        after = {
            row[0]: row[1]
            for row in self.conn.execute("SELECT id, value_cluster_id FROM metric_claims")
        }
        self.assertEqual(after, before)
        self.assertEqual(
            {
                row[0]: row[1]
                for row in self.conn.execute("SELECT id, decision_origin FROM metric_claims")
            },
            before_origins,
        )
        self.assertEqual(
            [row[0] for row in self.conn.execute("SELECT action FROM claim_decision_audit ORDER BY id")],
            ["merge_equivalent", "undo_override"],
        )

    def test_unit_conversion_is_equivalent_within_one_observation_group(self):
        phase4_claims.sync_finding_claims(
            self.conn, 1, _finding("https://a.example/report", 1.0, unit="million"),
            classification="official_publisher",
        )
        phase4_claims.sync_finding_claims(
            self.conn, 2, _finding("https://b.example/report", 1_000_000.0, unit="people"),
            classification="secondary_attributed",
        )
        groups = self.conn.execute("SELECT COUNT(*) FROM observation_groups").fetchone()[0]
        clusters = self.conn.execute("SELECT COUNT(*) FROM value_clusters").fetchone()[0]
        self.assertEqual(groups, 1)
        self.assertEqual(clusters, 1)

    def test_composite_count_units_share_the_base_count_group(self):
        phase4_claims.sync_finding_claims(
            self.conn, 1, _finding("https://a.example/report", 1.0, unit="million people"),
            classification="official_publisher",
        )
        phase4_claims.sync_finding_claims(
            self.conn, 2, _finding("https://b.example/report", 1_000_000.0, unit="people"),
            classification="secondary_attributed",
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM observation_groups").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM value_clusters").fetchone()[0], 1)

    def test_graph_value_uses_canonical_base_units_and_preserves_raw_claim(self):
        phase4_claims.sync_finding_claims(
            self.conn, 1, _finding("https://a.example/report", 1.0, unit="million"),
            classification="official_publisher",
        )
        claim = phase4_claims.list_claims(self.conn, iso3="JPN", metric="population")[0]
        self.assertEqual(claim["value"], 1.0)
        self.assertEqual(claim["unit"], "million")
        self.assertEqual(claim["normalized_value"], 1_000_000.0)

    def test_already_normalized_count_uses_source_value_without_double_scaling(self):
        finding = _finding("https://scaled.example/report", 27_900_000.0, unit="million people")
        finding["statistics"]["population"]["source_value"] = 27.9
        phase4_claims.sync_finding_claims(
            self.conn, 1, finding, classification="official_publisher"
        )
        claim = phase4_claims.list_claims(self.conn, iso3="JPN", metric="population")[0]
        self.assertEqual(claim["value"], 27.9)
        self.assertEqual(claim["normalized_value"], 27_900_000.0)

    def test_base_value_is_not_reduced_to_an_unlabelled_table_source_value(self):
        finding = _finding("https://table.example/report", 27_724_700.0, unit="")
        finding["statistics"]["population"]["source_value"] = 27_724.7
        phase4_claims.sync_finding_claims(
            self.conn, 1, finding, classification="official_publisher"
        )
        claim = phase4_claims.list_claims(self.conn, iso3="JPN", metric="population")[0]
        self.assertEqual(claim["value"], 27_724.7)
        self.assertEqual(claim["normalized_value"], 27_700_000.0)

    def test_removed_metric_claim_is_rejected_but_retained_for_audit(self):
        finding = _finding("https://a.example/report", 100.0)
        finding["statistics"]["births"] = {"value": 10.0, "measured_period": "2025"}
        phase4_claims.sync_finding_claims(
            self.conn, 1, finding, classification="official_publisher"
        )
        self.assertEqual(phase4_claims.reject_finding_metric_claims(self.conn, 1, "births"), 1)
        self.assertEqual(
            len(phase4_claims.list_claims(self.conn, iso3="JPN", metric="births")), 0
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT display_disposition FROM metric_claims WHERE finding_id = 1 AND metric = 'births'"
            ).fetchone()[0],
            "rejected",
        )

    def test_graph_contract_uses_ui_metric_name_for_net_migration(self):
        finding = _finding("https://migration.example/report", 42.0)
        finding["statistics"] = {
            "net_overseas_migration": {
                "value": 42.0,
                "unit": "people",
                "measured_period": "2025",
            }
        }
        phase4_claims.sync_finding_claims(
            self.conn, 1, finding, classification="official_publisher"
        )

        @contextmanager
        def connection():
            yield self.conn

        with patch.object(read_services, "get_connection", connection):
            clusters = read_services._phase4_graph_clusters("JPN")
        self.assertEqual([cluster["metric"] for cluster in clusters], ["net_migration"])

    def test_graph_contract_uses_normalized_value_for_scaled_claims(self):
        finding = _finding("https://scaled.example/report", 1.0, unit="million")
        phase4_claims.sync_finding_claims(
            self.conn, 1, finding, classification="official_publisher"
        )

        @contextmanager
        def connection():
            yield self.conn

        with patch.object(read_services, "get_connection", connection):
            cluster = read_services._phase4_graph_clusters("JPN")[0]
        self.assertEqual(cluster["value"], 1_000_000.0)
        self.assertEqual(cluster["graph_value"], 1_000_000.0)
        self.assertEqual(cluster["raw_value"], 1.0)

    def test_reconciliation_canonicalizes_destination_unit_and_is_idempotent(self):
        phase4_claims.sync_finding_claims(
            self.conn, 1, _finding("https://legacy-unit.example/report", 1.0, unit="million people"),
            classification="official_publisher",
        )
        self.conn.execute("UPDATE observation_groups SET unit = 'million people'")
        phase4_claims.ensure_schema(self.conn, migrate_legacy=False)
        phase4_claims.ensure_schema(self.conn, migrate_legacy=False)
        phase4_claims.sync_finding_claims(
            self.conn, 2, _finding("https://base-unit.example/report", 1_000_000.0, unit="people"),
            classification="secondary_attributed",
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM observation_groups").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT unit FROM observation_groups").fetchone()[0], "base")


if __name__ == "__main__":
    unittest.main()
