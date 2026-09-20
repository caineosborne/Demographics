"""Acceptance tests for Phase 4 measured periods and comparison rounding.

These tests deliberately exercise the persisted claim projection rather than
the extraction prompt.  Publication dates are metadata and must not alter the
period used to group claims.
"""

import sqlite3
import unittest

from data import phase4_claims


def _connection():
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


def _finding(url, *, value, period, metric="population", unit="people",
             published_date=None, period_start=None, period_end=None,
             time_period=None):
    statistic = {
        "value": value,
        "unit": unit,
        "measured_period": period,
    }
    if published_date is not None:
        statistic["published_date"] = published_date
    if period_start is not None:
        statistic["period_start"] = period_start
    if period_end is not None:
        statistic["period_end"] = period_end
    if time_period is not None:
        statistic["time_period"] = time_period
    return {
        "url": url,
        "geography_iso3": "TST",
        "statistics": {metric: statistic},
    }


def _sync(conn, finding_id, finding):
    return phase4_claims.sync_finding_claims(
        conn, finding_id, finding, classification="official_publisher"
    )


class Phase4PeriodRoundingTests(unittest.TestCase):
    def setUp(self):
        self.conn = _connection()

    def tearDown(self):
        self.conn.close()

    def _groups(self, metric="population"):
        return self.conn.execute(
            "SELECT observation_period, unit, definition FROM observation_groups "
            "WHERE iso3 = 'TST' AND metric = ? ORDER BY id",
            (metric,),
        ).fetchall()

    def _claim_values(self, metric="population"):
        return self.conn.execute(
            "SELECT value, observation_period FROM metric_claims "
            "WHERE iso3 = 'TST' AND metric = ? ORDER BY id",
            (metric,),
        ).fetchall()

    def _cluster_values(self, metric="population"):
        return self.conn.execute(
            """SELECT vc.normalized_value
               FROM value_clusters vc
               JOIN observation_groups og ON og.id = vc.observation_group_id
               WHERE og.iso3 = 'TST' AND og.metric = ? ORDER BY vc.id""",
            (metric,),
        ).fetchall()

    def test_different_years_are_separate_observations(self):
        _sync(self.conn, 1, _finding("https://source-1.example", value=10_000_000,
                                     period="2025"))
        _sync(self.conn, 2, _finding("https://source-2.example", value=10_100_000,
                                     period="2026"))
        self.assertEqual(len(self._groups()), 2)
        self.assertEqual({row[0] for row in self._groups()}, {"2025-unknown", "2026-unknown"})

    def test_different_months_are_separate_observations(self):
        _sync(self.conn, 1, _finding("https://source-1.example", value=10_000_000,
                                     period="July 2025"))
        _sync(self.conn, 2, _finding("https://source-2.example", value=10_100_000,
                                     period="September 2025"))
        self.assertEqual(len(self._groups()), 2)
        self.assertEqual({row[0] for row in self._groups()}, {"2025-07", "2025-09"})

    def test_dates_in_same_month_group_and_publication_dates_do_not_matter(self):
        _sync(self.conn, 1, _finding(
            "https://source-1.example", value=10_000_000, period="2025-07-01",
            published_date="2025-08-20",
        ))
        _sync(self.conn, 2, _finding(
            "https://source-2.example", value=10_000_000, period="2025-07-15",
            published_date="2026-01-10",
        ))
        self.assertEqual(len(self._groups()), 1)
        self.assertEqual(self._groups()[0][0], "2025-07")

    def test_equivalent_textual_month_forms_group(self):
        _sync(self.conn, 1, _finding("https://source-1.example", value=10_000_000,
                                     period="July 2025"))
        _sync(self.conn, 2, _finding("https://source-2.example", value=10_000_000,
                                     period="2025-07"))
        self.assertEqual(len(self._groups()), 1)
        self.assertEqual(self._groups()[0][0], "2025-07")

    def test_quarter_annual_and_ytd_ranges_remain_distinct(self):
        _sync(self.conn, 1, _finding(
            "https://source-1.example", value=10_000_000, period="Q3 2025",
            period_start="2025-07-01", period_end="2025-09-30", time_period="quarterly",
        ))
        _sync(self.conn, 2, _finding(
            "https://source-2.example", value=10_000_000, period="2025",
            period_start="2025-01-01", period_end="2025-12-31", time_period="annual",
        ))
        _sync(self.conn, 3, _finding(
            "https://source-3.example", value=10_000_000, period="January to September 2025",
            period_start="2025-01-01", period_end="2025-09-30", time_period="year-to-date",
        ))
        self.assertEqual(len(self._groups()), 3)
        self.assertEqual(
            {row[0] for row in self._groups()},
            {"2025-07/2025-09", "2025-01/2025-12", "2025-01/2025-09"},
        )

    def test_mid_year_and_year_end_map_to_their_months(self):
        _sync(self.conn, 1, _finding("https://source-1.example", value=10_000_000,
                                     period="mid-year 2025"))
        _sync(self.conn, 2, _finding("https://source-2.example", value=10_000_000,
                                     period="end of 2025"))
        self.assertEqual({row[0] for row in self._groups()}, {"2025-07", "2025-12"})

    def test_year_only_population_does_not_invent_a_month(self):
        _sync(self.conn, 1, _finding("https://source-1.example", value=10_000_000,
                                     period="2025"))
        self.assertEqual(self._groups()[0][0], "2025-unknown")
        self.assertNotIn("-07", self._groups()[0][0])
        self.assertNotIn("-12", self._groups()[0][0])

    def test_population_rounding_tiers_use_half_up_and_preserve_raw_values(self):
        cases = [
            ("small", 999_500, 1_000_000),       # < 1m -> nearest 1,000
            ("medium", 1_005_000, 1_010_000),    # 1m-10m -> nearest 10,000
            ("large", 10_050_000, 10_100_000),   # > 10m -> nearest 100,000
        ]
        for index, (label, raw, rounded) in enumerate(cases, 1):
            _sync(self.conn, index, _finding(
                f"https://{label}.example", value=raw, period=f"2025-{index:02d}",
            ))
        self.assertEqual([row[0] for row in self._claim_values()], [999_500, 1_005_000, 10_050_000])
        self.assertEqual(
            [row[0] for row in self.conn.execute(
                "SELECT raw_value FROM metric_claims ORDER BY id"
            ).fetchall()],
            [999_500, 1_005_000, 10_050_000],
        )
        self.assertEqual([row[0] for row in self._cluster_values()], [1_000_000, 1_010_000, 10_100_000])

    def test_population_tier_boundaries_are_explicit(self):
        cases = [
            (999_999, 1_000_000),
            (1_000_001, 1_000_000),
            (9_999_999, 10_000_000),
            (10_000_000, 10_000_000),
            (10_000_001, 10_000_000),
        ]
        for index, (raw, rounded) in enumerate(cases, 1):
            _sync(self.conn, index, _finding(
                f"https://boundary-{index}.example", value=raw,
                period=f"2025-{index:02d}",
            ))
            self.assertEqual(
                self.conn.execute(
                    "SELECT normalized_value FROM value_clusters ORDER BY id DESC LIMIT 1"
                ).fetchone()[0],
                rounded,
            )

    def test_flow_metrics_round_to_nearest_thousand(self):
        for index, (metric, raw, rounded) in enumerate([
            ("births", 12_500, 13_000),
            ("deaths", 12_499, 12_000),
            ("natural_change", -12_500, -13_000),
            ("net_overseas_migration", 12_500, 13_000),
        ], 1):
            _sync(self.conn, index, _finding(
                f"https://{metric}.example", value=raw, period="2025", metric=metric,
            ))
            cluster = self.conn.execute(
                """SELECT vc.normalized_value FROM value_clusters vc
                   JOIN observation_groups og ON og.id = vc.observation_group_id
                   WHERE og.metric = ?""", (metric,)
            ).fetchone()
            self.assertEqual(cluster[0], rounded, metric)

    def test_tfr_rounds_to_nearest_tenth(self):
        _sync(self.conn, 1, _finding("https://tfr-1.example", value=2.05,
                                     period="2025", metric="total_fertility_rate"))
        _sync(self.conn, 2, _finding("https://tfr-2.example", value=2.04,
                                     period="2026", metric="total_fertility_rate"))
        values = self.conn.execute(
            "SELECT normalized_value FROM value_clusters ORDER BY id"
        ).fetchall()
        self.assertEqual([row[0] for row in values], [2.1, 2.0])

    def test_repeated_schema_reconciliation_is_idempotent(self):
        _sync(self.conn, 1, _finding("https://source-1.example", value=10_050_000,
                                     period="July 2025"))
        before = {
            "groups": self.conn.execute("SELECT COUNT(*) FROM observation_groups").fetchone()[0],
            "clusters": self.conn.execute("SELECT COUNT(*) FROM value_clusters").fetchone()[0],
            "claims": self.conn.execute("SELECT COUNT(*) FROM metric_claims").fetchone()[0],
        }
        phase4_claims.ensure_schema(self.conn, migrate_legacy=False)
        phase4_claims.ensure_schema(self.conn, migrate_legacy=False)
        after = {
            "groups": self.conn.execute("SELECT COUNT(*) FROM observation_groups").fetchone()[0],
            "clusters": self.conn.execute("SELECT COUNT(*) FROM value_clusters").fetchone()[0],
            "claims": self.conn.execute("SELECT COUNT(*) FROM metric_claims").fetchone()[0],
        }
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
