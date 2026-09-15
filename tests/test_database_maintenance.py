import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from data.database_config import DEFAULT_DB_PATH
from data.database_maintenance import (
    build_wpp_serving_database, compact_candidate_audit, inventory_database, preserve_database,
    remove_embedded_wpp_tables,
)


class DatabaseMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = self.root / "source.sqlite"
        with sqlite3.connect(self.source) as connection:
            connection.execute("CREATE TABLE webpage_findings (id INTEGER PRIMARY KEY)")
            connection.execute("CREATE TABLE search_candidates (id INTEGER PRIMARY KEY)")
            connection.execute("CREATE TABLE search_runs (id INTEGER PRIMARY KEY)")
            connection.executemany("INSERT INTO webpage_findings VALUES (?)", [(1,), (2,)])
            connection.executemany("INSERT INTO search_candidates VALUES (?)", [(1,), (2,), (3,)])
            connection.execute("INSERT INTO search_runs VALUES (1)")

    def tearDown(self):
        self.directory.cleanup()

    def test_default_runtime_path_is_not_in_legacy_data_folder(self):
        self.assertEqual(DEFAULT_DB_PATH.parent.name, "runtime")
        self.assertNotIn("data_files", str(DEFAULT_DB_PATH).casefold())

    def test_inventory_records_table_counts_and_checksum(self):
        inventory = inventory_database(self.source)
        self.assertEqual(inventory["accepted_finding_count"], 2)
        self.assertEqual(inventory["candidate_count"], 3)
        self.assertEqual(inventory["table_counts"]["search_runs"], 1)
        self.assertEqual(len(inventory["sha256"]), 64)
        self.assertEqual(inventory["sqlite_quick_check"], "ok")

    def test_backup_is_exact_read_only_and_has_inventory_sidecar(self):
        backup, inventory_path, inventory = preserve_database(
            self.source, self.root / "backups", timestamp="20260912T000000Z"
        )
        self.assertEqual(backup.read_bytes(), self.source.read_bytes())
        self.assertFalse(os.stat(backup).st_mode & 0o222)
        saved = json.loads(inventory_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["backup_sha256"], inventory["sha256"])
        self.assertEqual(saved["backup_path"], str(backup))

        with self.assertRaises(sqlite3.OperationalError):
            with sqlite3.connect(backup) as connection:
                connection.execute("CREATE TABLE should_not_be_written (id INTEGER)")

    def test_candidate_compaction_preserves_loaded_history_and_compact_audit(self):
        with sqlite3.connect(self.source) as connection:
            connection.execute("ALTER TABLE search_candidates ADD COLUMN details_json TEXT")
            connection.execute(
                "UPDATE search_candidates SET details_json = ? WHERE id = 1",
                (json.dumps({
                    "url": "https://example.test/article", "title": "Release", "raw": {"large": "x" * 10_000},
                    "full_text": "Article body " * 10_000, "loaded_url": "https://example.test/article",
                    "alternative_sources": [{"url": "https://alternative.test", "status": "accessed", "raw": "x" * 1_000}],
                    "full_reason": "National figures", "source_classification": "official_publisher",
                }),),
            )
            connection.execute("UPDATE search_candidates SET details_json = '{}' WHERE id != 1")

        report = compact_candidate_audit(self.source)

        self.assertEqual(report["changed_rows"], 1)
        with sqlite3.connect(self.source) as connection:
            details = json.loads(connection.execute(
                "SELECT details_json FROM search_candidates WHERE id = 1"
            ).fetchone()[0])
            log = connection.execute(
                "SELECT candidate_rows, changed_rows, bytes_removed FROM audit_cleanup_log"
            ).fetchone()
        self.assertTrue(details["page_loaded"])
        self.assertNotIn("full_text", details)
        self.assertNotIn("raw", details)
        self.assertEqual(details["alternative_sources"], [{"url": "https://alternative.test", "status": "accessed"}])
        self.assertEqual(log[:2], (3, 1))
        self.assertGreater(log[2], 10_000)

    def test_build_wpp_serving_database_filters_types_and_preserves_rows(self):
        columns = '''
            Country TEXT, "ISO3 Alpha-code" TEXT, Year TEXT,
            "Population 1 Jan" TEXT, "Population 1 Jul" TEXT,
            "Total Births" TEXT, "Total Deaths" TEXT, "Natural Change" TEXT,
            "Net Migration" TEXT, "Total Fertility Rate (live births per woman)" TEXT
        '''
        with sqlite3.connect(self.source) as connection:
            for table in ("estimates", "medium_variant"):
                connection.execute(f"CREATE TABLE {table} ({columns})")
                connection.execute(
                    f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("Japan", "JPN", "2026", "123", "124", "1.2", "0.8", "0.4", "0.1", "1.3"),
                )
            connection.execute('''CREATE TABLE wpp_release_history (
                revision INTEGER NOT NULL, Country TEXT NOT NULL, "ISO3 Alpha-code" TEXT,
                Year INTEGER NOT NULL, "Population 1 Jul" REAL, "Total Births" REAL,
                "Total Deaths" REAL, "Natural Change" REAL, "Net Migration" REAL,
                "Total Fertility Rate (live births per woman)" REAL, cadence_years INTEGER NOT NULL,
                source_url TEXT NOT NULL, PRIMARY KEY (revision, Country, Year)
            )''')
            connection.execute(
                "INSERT INTO wpp_release_history VALUES (2024, 'Japan', 'JPN', 2026, 124, 1.2, 0.8, 0.4, 0.1, 1.3, 1, 'https://un.example')"
            )
            connection.execute(
                "INSERT INTO estimates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("Aggregate", None, "", "...", "", "...", "", "", "", "..."),
            )

        destination = self.root / "wpp_serving.sqlite"
        manifest = build_wpp_serving_database(self.source, destination)

        self.assertEqual(manifest["table_counts"], {
            "estimates": 2, "medium_variant": 1, "wpp_release_history": 1,
        })
        self.assertFalse(os.stat(destination).st_mode & 0o222)
        with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT Year, \"Population 1 Jul\" FROM medium_variant WHERE \"ISO3 Alpha-code\" = 'JPN'"
            ).fetchone()
            manifest_columns = connection.execute("PRAGMA table_info(medium_variant)").fetchall()
        self.assertEqual(row, (2026, 124.0))
        self.assertEqual(len(manifest_columns), 10)

    def test_build_wpp_archive_keeps_full_source_tables_offline(self):
        with sqlite3.connect(self.source) as connection:
            connection.execute('CREATE TABLE estimates (Country TEXT, "ISO3 Alpha-code" TEXT, Year TEXT, extra TEXT)')
            connection.execute('CREATE TABLE medium_variant (Country TEXT, "ISO3 Alpha-code" TEXT, Year TEXT, extra TEXT)')
            connection.execute("INSERT INTO estimates VALUES ('Japan', 'JPN', '2023', 'kept exactly')")
            connection.execute("INSERT INTO medium_variant VALUES ('Japan', 'JPN', '2024', 'kept exactly')")
        from data.database_maintenance import build_wpp_archive
        archive = self.root / "wpp_full_archive.sqlite"
        report = build_wpp_archive(self.source, archive)
        self.assertEqual(report["table_counts"], {"estimates": 1, "medium_variant": 1})
        self.assertFalse(os.stat(archive).st_mode & 0o222)
        with sqlite3.connect(f"file:{archive}?mode=ro", uri=True) as connection:
            self.assertEqual(connection.execute("SELECT extra FROM estimates").fetchone()[0], "kept exactly")

    def test_removing_embedded_wpp_tables_preserves_research_tables(self):
        with sqlite3.connect(self.source) as connection:
            connection.execute("CREATE TABLE estimates (id INTEGER)")
            connection.execute("CREATE TABLE medium_variant (id INTEGER)")
            connection.execute("CREATE TABLE wpp_release_history (id INTEGER)")
        report = remove_embedded_wpp_tables(self.source)
        self.assertEqual(report["removed_tables"], ["estimates", "medium_variant", "wpp_release_history"])
        with sqlite3.connect(self.source) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM search_candidates").fetchone()[0], 3)
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'estimates'"
            ).fetchone())
