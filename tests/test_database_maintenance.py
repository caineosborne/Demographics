import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database_config import DEFAULT_DB_PATH
from database_maintenance import inventory_database, preserve_database


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
        self.assertEqual(DEFAULT_DB_PATH.parent.name, "databases")
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
