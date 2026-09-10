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

    def test_same_effective_date_and_population_is_excluded(self):
        tools.store_webpage_finding(self.finding)
        duplicate = {**self.finding, "url": "https://mirror.test/report"}
        self.assertEqual(tools.store_webpage_finding(duplicate)["status"], "excluded_duplicate_report")

    def test_missing_population_does_not_exclude_different_url(self):
        finding = {**self.finding, "statistics": {"population": {"value": None}}}
        tools.store_webpage_finding(finding)
        other_url = {**finding, "url": "https://mirror.test/report"}
        self.assertEqual(tools.store_webpage_finding(other_url)["status"], "stored")

    def test_database_dump_is_sorted_by_country_then_effective_date(self):
        japan_later = {**self.finding, "url": "https://example.test/japan-later", "geography": "Japan"}
        australia = {**self.finding, "url": "https://example.test/australia", "geography": "Australia", "effective_date": "2026-07-01"}
        japan_earlier = {**self.finding, "url": "https://example.test/japan-earlier", "geography": "Japan", "effective_date": "2026-01-01"}
        for finding in (japan_later, australia, japan_earlier):
            tools.store_webpage_finding(finding)
        dump = tools.list_webpage_findings()
        self.assertEqual([(row["Country"], row["Effective date"]) for row in dump], [
            ("Australia", "2026-07-01"), ("Japan", "2026-01-01"), ("Japan", "2026-06-30"),
        ])
        self.assertIn('"geography": "Japan"', dump[1]["Extracted JSON"])

    def test_can_edit_and_delete_a_selected_record(self):
        stored = tools.store_webpage_finding({**self.finding, "geography": "Japan"})
        finding = tools.get_webpage_finding(stored["id"])
        finding["quoted_source"] = "Edited source"
        tools.update_webpage_finding(stored["id"], json.dumps(finding))
        self.assertEqual(tools.get_webpage_finding(stored["id"])["quoted_source"], "Edited source")
        tools.delete_webpage_finding(stored["id"])
        with self.assertRaisesRegex(ValueError, 'No stored finding'):
            tools.get_webpage_finding(stored["id"])

    def test_iso3_geography_is_normalised_before_storage(self):
        with patch.object(tools, "normalise_country_name", return_value="Japan"):
            stored = tools.store_webpage_finding({**self.finding, "url": "https://example.test/iso", "geography": "JPN"})
        self.assertEqual(tools.get_webpage_finding(stored["id"])["geography"], "Japan")
