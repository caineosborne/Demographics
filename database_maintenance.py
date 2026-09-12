"""Preserve and inventory the current SQLite database.

This command is the Phase 1.1 rollback-point workflow. It reads the source
database without opening it for writes, copies the exact SQLite file to a
dated backup, marks that copy read-only, and writes a sidecar inventory.

Example:

    uv run python database_maintenance.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from database_config import BACKUP_DIR, DEFAULT_DB_PATH


def sha256_file(path: Path) -> str:
    """Return the SHA-256 checksum of a file without loading it all at once."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_only_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {path}")
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


def _table_names(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def inventory_database(path: Path) -> dict[str, Any]:
    """Collect the migration inventory from a read-only database connection."""
    path = Path(path).expanduser().resolve()
    stat = path.stat()
    with _read_only_connection(path) as connection:
        tables = _table_names(connection)
        table_counts = {
            table: connection.execute(
                f'SELECT COUNT(*) FROM "{table.replace(chr(34), chr(34) * 2)}"'
            ).fetchone()[0]
            for table in tables
        }
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]

    return {
        "inventory_schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": path.name,
        "database_size_bytes": stat.st_size,
        "sha256": sha256_file(path),
        "sqlite_quick_check": quick_check,
        "table_counts": table_counts,
        "accepted_finding_count": table_counts.get("webpage_findings", 0),
        "candidate_count": table_counts.get("search_candidates", 0),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o444)


def preserve_database(
    source: Path = DEFAULT_DB_PATH,
    backup_dir: Path = BACKUP_DIR,
    timestamp: str | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Create an exact read-only backup and its inventory sidecar.

    The source is never opened for writing. A checksum comparison after the
    copy guards against a partial backup before it is accepted as a rollback
    point.
    """
    source = Path(source).expanduser().resolve()
    backup_dir = Path(backup_dir).expanduser().resolve()
    inventory = inventory_database(source)
    timestamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not re.fullmatch(r"[0-9T Z_-]+", timestamp):
        raise ValueError("timestamp must contain only safe filename characters")
    timestamp = timestamp.replace(" ", "")

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{source.stem}.{timestamp}{source.suffix}"
    if backup_path.exists():
        raise FileExistsError(f"Backup already exists: {backup_path}")
    shutil.copy2(source, backup_path)
    if sha256_file(backup_path) != inventory["sha256"]:
        backup_path.unlink(missing_ok=True)
        raise IOError("Backup checksum does not match the source database")
    os.chmod(backup_path, 0o444)

    inventory = {
        **inventory,
        "source_path": str(source),
        "backup_path": str(backup_path),
        "backup_sha256": sha256_file(backup_path),
        "backup_read_only": not bool(os.stat(backup_path).st_mode & 0o222),
    }
    inventory_path = backup_path.with_name(backup_path.name + ".inventory.json")
    _write_json(inventory_path, inventory)
    return backup_path, inventory_path, inventory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--backup-dir", type=Path, default=BACKUP_DIR)
    args = parser.parse_args()
    backup_path, inventory_path, inventory = preserve_database(args.source, args.backup_dir)
    print(f"Created read-only rollback backup: {backup_path}")
    print(f"Wrote inventory: {inventory_path}")
    print(json.dumps(inventory, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

