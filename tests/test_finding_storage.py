import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tools


class FindingStorageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "findings.sqlite"
        self.path_patch = patch.object(tools, "DB_PATH", self.db_path)
        self.path_patch.start()
        self.finding = {
            "url": "https://example.test/report",
            "effective_date": "2026-06-30",
            "official_source": False,
            "quoted_source": "Japan Statistics Bureau",
            "quoted_source_url": "https://stat.go.jp/report",
            "statistics": {"population": {"value": 123456.0}},
        }

    def tearDown(self):
        self.path_patch.stop()
        self.tempdir.cleanup()

    def test_stores_full_extraction_and_provenance(self):
        result = tools.store_webpage_finding(self.finding)
        self.assertEqual(result["status"], "stored")
        with tools.get_connection() as conn:
            conn.row_factory = tools.sqlite3.Row
            row = conn.execute("SELECT * FROM webpage_findings").fetchone()
        self.assertEqual(row["source_url"], self.finding["url"])
        self.assertEqual(row["quoted_source"], "Japan Statistics Bureau")
        self.assertEqual(json.loads(row["finding_json"]), self.finding)

    def test_exact_url_is_excluded(self):
        tools.store_webpage_finding(self.finding)
        self.assertEqual(tools.store_webpage_finding(self.finding)["status"], "excluded_duplicate_url")

    def test_canonical_url_variants_are_excluded(self):
        tools.store_webpage_finding({
            **self.finding,
            "url": "http://www.example.test/report/?utm_source=search&b=2&a=1#figures",
        })
        duplicate = {**self.finding, "url": "https://example.test/report?a=1&b=2"}
        self.assertEqual(tools.store_webpage_finding(duplicate)["status"], "excluded_duplicate_url")

    def test_meaningful_query_parameters_remain_distinct(self):
        base = {**self.finding, "effective_date": None, "statistics": {"population": {"value": None}}}
        self.assertEqual(tools.store_webpage_finding(base)["status"], "stored")
        self.assertEqual(
            tools.store_webpage_finding({**base, "url": "https://example.test/report?edition=mobile"})["status"],
            "stored",
        )

    def test_legacy_canonical_collision_archives_older_record(self):
        with tools.get_connection() as conn:
            conn.execute("""CREATE TABLE webpage_findings (
                id INTEGER PRIMARY KEY, source_url TEXT NOT NULL UNIQUE,
                effective_date TEXT, population_value REAL, official_source INTEGER NOT NULL,
                quoted_source TEXT, quoted_source_url TEXT, extracted_at TEXT NOT NULL,
                finding_json TEXT NOT NULL
            )""")
            conn.executemany(
                "INSERT INTO webpage_findings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (1, "http://www.example.test/report/?utm_source=x", None, None, 0, None, None, "2026-01-01T00:00:00+00:00", json.dumps({"url": "http://www.example.test/report/?utm_source=x"})),
                    (2, "https://example.test/report", None, None, 0, None, None, "2026-02-01T00:00:00+00:00", json.dumps({"url": "https://example.test/report"})),
                ],
            )
        tools.initialise_findings_table()
        with tools.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM webpage_findings").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM finding_legacy_duplicates").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT id FROM webpage_findings").fetchone()[0], 2)

    def test_same_effective_date_and_population_is_excluded(self):
        tools.store_webpage_finding(self.finding)
        duplicate = {**self.finding, "url": "https://mirror.test/report"}
        self.assertEqual(tools.store_webpage_finding(duplicate)["status"], "excluded_duplicate_report")

    def test_missing_population_does_not_exclude_different_url(self):
        finding = {**self.finding, "statistics": {"population": {"value": None}}}
        tools.store_webpage_finding(finding)
        other_url = {**finding, "url": "https://mirror.test/report"}
        self.assertEqual(tools.store_webpage_finding(other_url)["status"], "stored")

    def test_database_dump_is_sorted_by_descending_numeric_id(self):
        japan_later = {**self.finding, "url": "https://example.test/japan-later", "geography": "Japan"}
        australia = {**self.finding, "url": "https://example.test/australia", "geography": "Australia", "effective_date": "2026-07-01"}
        japan_earlier = {**self.finding, "url": "https://example.test/japan-earlier", "geography": "Japan", "effective_date": "2026-01-01"}
        for finding in (japan_later, australia, japan_earlier):
            tools.store_webpage_finding(finding)
        dump = tools.list_webpage_findings()
        self.assertEqual([(row["Country"], row["Effective date"]) for row in dump], [
            ("Japan", "2026-01-01"), ("Australia", "2026-07-01"), ("Japan", "2026-06-30"),
        ])
        self.assertIn('"geography": "Japan"', dump[0]["Extracted JSON"])

    def test_can_edit_and_delete_a_selected_record(self):
        stored = tools.store_webpage_finding({**self.finding, "geography": "Japan"})
        finding = tools.get_webpage_finding(stored["id"])
        finding["quoted_source"] = "Edited source"
        tools.update_webpage_finding(stored["id"], json.dumps(finding))
        self.assertEqual(tools.get_webpage_finding(stored["id"])["quoted_source"], "Edited source")
        tools.delete_webpage_finding(stored["id"])
        with self.assertRaisesRegex(ValueError, 'No stored finding'):
            tools.get_webpage_finding(stored["id"])

    def test_delete_and_block_prevents_the_same_article_returning(self):
        stored = tools.store_webpage_finding({**self.finding, "url": "http://www.example.test/report?utm_source=search"})
        canonical = tools.delete_and_block_webpage_finding(stored["id"])
        self.assertEqual(canonical, "https://example.test/report")
        self.assertIn(canonical, tools.blocked_source_urls())
        self.assertEqual(
            tools.store_webpage_finding({**self.finding, "url": "https://example.test/report"})["status"],
            "excluded_blocked_source",
        )

    def test_remove_and_allow_rerun_leaves_url_eligible(self):
        stored = tools.store_webpage_finding(self.finding)
        tools.delete_webpage_finding(stored["id"])
        self.assertEqual(tools.store_webpage_finding(self.finding)["status"], "stored")
        action = tools.run_query(
            "SELECT action FROM finding_actions WHERE finding_id = ? ORDER BY id DESC LIMIT 1",
            (stored["id"],),
        )
        self.assertEqual(action[0]["action"], "removed_allow_rerun")

    def test_unblocking_restores_url_eligibility(self):
        stored = tools.store_webpage_finding(self.finding)
        canonical = tools.delete_and_block_webpage_finding(stored["id"])
        tools.unblock_source_url(canonical)
        self.assertEqual(tools.store_webpage_finding(self.finding)["status"], "stored")

    def test_can_delete_one_metric_without_deleting_other_metrics(self):
        finding = {**self.finding, "statistics": {
            "population": {"value": 100}, "births": {"value": 2},
        }}
        stored = tools.store_webpage_finding(finding)
        tools.delete_finding_metric(stored["id"], "births")
        updated = tools.get_webpage_finding(stored["id"])
        self.assertNotIn("births", updated["statistics"])
        self.assertEqual(updated["statistics"]["population"]["value"], 100)

    def test_legacy_monthly_flows_are_removed_on_database_initialisation(self):
        legacy = {**self.finding, "statistics": {
            "births": {"value": 23_111, "time_period": "2026-06"},
            "deaths": {"value": 27_794, "time_period": "2026-06"},
        }}
        stored = tools.store_webpage_finding({**legacy, "statistics": {"births": {"value": 277_332, "time_period": "annual"}}})
        # Simulate the pre-normalisation payload that was already on disk.
        with tools.get_connection() as conn:
            conn.execute("UPDATE webpage_findings SET finding_json = ? WHERE id = ?", (json.dumps(legacy), stored["id"]))
        tools.initialise_findings_table()
        with self.assertRaisesRegex(ValueError, "No stored finding"):
            tools.get_webpage_finding(stored["id"])

    def test_iso3_geography_is_normalised_before_storage(self):
        with patch.object(tools, "normalise_country_name", return_value="Japan"):
            stored = tools.store_webpage_finding({**self.finding, "url": "https://example.test/iso", "geography": "JPN"})
        self.assertEqual(tools.get_webpage_finding(stored["id"])["geography"], "Japan")
