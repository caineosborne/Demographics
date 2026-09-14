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

from database_config import BACKUP_DIR, DEFAULT_DB_PATH, DEFAULT_WPP_SERVING_PATH, WPP_ARCHIVE_PATH


# Candidate audit data is operational evidence, not an article archive. Keep
# the information an administrator needs to understand an outcome while
# removing provider payloads and retrieved page bodies.
CANDIDATE_DETAIL_FIELDS = {
    "url", "canonical_url", "title", "snippet", "source", "category", "query",
    "published_date", "score", "time_range", "provider_note", "submission_url",
    "submission_published", "submission_created_utc", "transport", "discovery_only",
    "summary_decision", "summary_reason", "summary_batch_seconds", "summary_batch_size",
    "summary_warning", "discovery_warning", "review_warning",
    "full_decision", "full_reason", "full_review_seconds", "error", "finding_id",
    "duplicate_candidate_id", "duplicate_of", "duplicate_kind", "source_classification",
    "storage", "extraction", "extraction_seconds", "original_access_error",
    "alternative_search_error", "recovered_from_url", "replacement_url", "loaded_url",
    "page_loaded", "fetch_seconds", "model_text_characters", "original_text_characters",
    "content_transport", "tavily_extract_seconds", "tavily_extract_error",
    "automatic_recheck", "automatic_recheck_requested_at",
    "extraction_prompt_version", "extraction_rule_version",
    "country_iso3", "scope_country_iso3", "scope_country", "scope_mismatch",
}
ALTERNATIVE_ATTEMPT_FIELDS = {
    "url", "canonical_url", "title", "snippet", "published_date", "status", "error", "reason",
}

WPP_DATA_COLUMNS = (
    ("Country", "TEXT"),
    ("ISO3 Alpha-code", "TEXT"),
    ("Year", "INTEGER"),
    ("Population 1 Jan", "REAL"),
    ("Population 1 Jul", "REAL"),
    ("Total Births", "REAL"),
    ("Total Deaths", "REAL"),
    ("Natural Change", "REAL"),
    ("Net Migration", "REAL"),
    ("Total Fertility Rate (live births per woman)", "REAL"),
)
WPP_TABLES = ("estimates", "medium_variant")
SERVING_SCHEMA_VERSION = 1


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


def _compact_alternative_attempts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    compact = []
    for attempt in value:
        if isinstance(attempt, dict):
            compact.append({key: attempt[key] for key in ALTERNATIVE_ATTEMPT_FIELDS if key in attempt})
    return compact


def compact_candidate_details(details: dict[str, Any]) -> dict[str, Any]:
    """Remove large audit payloads while preserving duplicate/retry semantics."""
    compact = {key: details[key] for key in CANDIDATE_DETAIL_FIELDS if key in details}
    if details.get("page_loaded") is True or "full_text" in details:
        # Older runs prove a load only through full_text. Preserve that fact
        # before discarding the body so historical duplicate protection holds.
        compact["page_loaded"] = True
    if "alternative_sources" in details:
        compact["alternative_sources"] = _compact_alternative_attempts(details["alternative_sources"])
    return compact


def compact_candidate_audit(path: Path = DEFAULT_DB_PATH) -> dict[str, int]:
    """Compact completed audit payloads and reclaim SQLite pages with VACUUM.

    Callers must create a rollback backup first. This function records the
    removal totals inside the database, then vacuums after its transaction has
    committed so the file size can actually shrink.
    """
    path = Path(path).expanduser().resolve()
    before_size = path.stat().st_size
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS audit_cleanup_log (
            id INTEGER PRIMARY KEY,
            completed_at TEXT NOT NULL,
            candidate_rows INTEGER NOT NULL,
            changed_rows INTEGER NOT NULL,
            bytes_removed INTEGER NOT NULL
        )""")
        rows = connection.execute("SELECT id, details_json FROM search_candidates").fetchall()
        changed_rows = 0
        bytes_removed = 0
        for candidate_id, details_json in rows:
            try:
                details = json.loads(details_json)
            except (TypeError, json.JSONDecodeError):
                continue
            compact = compact_candidate_details(details)
            encoded = json.dumps(compact, default=str, separators=(",", ":"))
            before_bytes = len(str(details_json).encode("utf-8"))
            after_bytes = len(encoded.encode("utf-8"))
            if encoded == details_json:
                continue
            connection.execute(
                "UPDATE search_candidates SET details_json = ? WHERE id = ?", (encoded, candidate_id)
            )
            changed_rows += 1
            bytes_removed += max(0, before_bytes - after_bytes)
        connection.execute(
            """INSERT INTO audit_cleanup_log
               (completed_at, candidate_rows, changed_rows, bytes_removed)
               VALUES (?, ?, ?, ?)""",
            (datetime.now(timezone.utc).isoformat(), len(rows), changed_rows, bytes_removed),
        )
    with sqlite3.connect(path) as connection:
        connection.execute("VACUUM")
    return {
        "candidate_rows": len(rows),
        "changed_rows": changed_rows,
        "bytes_removed": bytes_removed,
        "database_bytes_before": before_size,
        "database_bytes_after": path.stat().st_size,
    }


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _optional_number(value: Any) -> float | None:
    """Convert WPP numeric cells, treating source placeholders as missing."""
    if value is None or not str(value).strip() or str(value).strip() == "...":
        return None
    return float(value)


def _serving_manifest(source: Path, table_counts: dict[str, int]) -> dict[str, Any]:
    vintage = re.search(r"WPP(\d{4})", source.name)
    return {
        "schema_version": SERVING_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_database": source.name,
        "source_sha256": sha256_file(source),
        "wpp_vintage": vintage.group(1) if vintage else "unknown",
        "included_columns": [name for name, _kind in WPP_DATA_COLUMNS],
        "table_counts": table_counts,
    }


def build_wpp_archive(
    source: Path = DEFAULT_DB_PATH,
    destination: Path = WPP_ARCHIVE_PATH,
) -> dict[str, Any]:
    """Extract the complete WPP source tables into an offline archive.

    Research tables stay in the writable application database.  The archive
    preserves each WPP column and value exactly, so it remains a rebuildable
    source for the smaller serving database.
    """
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if source == destination:
        raise ValueError("The WPP archive must be separate from the working database.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    table_counts: dict[str, int] = {}
    try:
        with _read_only_connection(source) as source_conn, sqlite3.connect(temporary) as archive_conn:
            for table in WPP_TABLES + ("wpp_release_history",):
                row = source_conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
                ).fetchone()
                if not row:
                    if table in WPP_TABLES:
                        raise ValueError(f"Required WPP table is missing: {table}")
                    continue
                archive_conn.execute(row[0])
                cursor = source_conn.execute(f"SELECT * FROM {_quote_identifier(table)}")
                first_batch = cursor.fetchmany(1_000)
                table_counts[table] = 0
                if first_batch:
                    placeholders = ", ".join("?" for _ in first_batch[0])
                    statement = f"INSERT INTO {_quote_identifier(table)} VALUES ({placeholders})"
                    archive_conn.executemany(statement, first_batch)
                    table_counts[table] += len(first_batch)
                    while batch := cursor.fetchmany(1_000):
                        archive_conn.executemany(statement, batch)
                        table_counts[table] += len(batch)
                for index_sql, in source_conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
                    (table,),
                ):
                    archive_conn.execute(index_sql)
        os.chmod(temporary, 0o444)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "source_database": source.name,
        "source_sha256": sha256_file(source),
        "destination": str(destination),
        "archive_sha256": sha256_file(destination),
        "table_counts": table_counts,
        "database_size_bytes": destination.stat().st_size,
    }


def build_wpp_serving_database(
    source: Path = WPP_ARCHIVE_PATH,
    destination: Path | None = None,
) -> dict[str, Any]:
    """Build a small, reproducible read-only WPP serving SQLite database."""
    source = Path(source).expanduser().resolve()
    destination = Path(destination or DEFAULT_WPP_SERVING_PATH).expanduser().resolve()
    if source == destination:
        raise ValueError("The WPP serving database must be a separate output file.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    columns_sql = ", ".join(f"{_quote_identifier(name)} {kind}" for name, kind in WPP_DATA_COLUMNS)
    column_names = ", ".join(_quote_identifier(name) for name, _kind in WPP_DATA_COLUMNS)
    table_counts: dict[str, int] = {}
    try:
        with _read_only_connection(source) as source_conn, sqlite3.connect(temporary) as serving_conn:
            for table in WPP_TABLES:
                source_count = source_conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                ).fetchone()[0]
                table_counts[table] = source_count
                serving_conn.execute(f"CREATE TABLE {_quote_identifier(table)} ({columns_sql})")
                rows = source_conn.execute(f"SELECT {column_names} FROM {_quote_identifier(table)}")
                placeholders = ", ".join("?" for _name, _kind in WPP_DATA_COLUMNS)
                insert = f"INSERT INTO {_quote_identifier(table)} ({column_names}) VALUES ({placeholders})"
                for batch in iter(lambda: rows.fetchmany(1_000), []):
                    normalized = []
                    for row in batch:
                        values = list(row)
                        values[2] = int(values[2]) if str(values[2]).strip() else None
                        for index in range(3, len(values)):
                            values[index] = _optional_number(values[index])
                        normalized.append(values)
                    serving_conn.executemany(insert, normalized)
                serving_conn.execute(
                    f"CREATE INDEX idx_{table}_iso3_year ON {_quote_identifier(table)} "
                    f"({_quote_identifier('ISO3 Alpha-code')}, {_quote_identifier('Year')})"
                )
            overlay_exists = source_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'wpp_release_history'"
            ).fetchone()
            if overlay_exists:
                schema = source_conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'wpp_release_history'"
                ).fetchone()[0]
                serving_conn.execute(schema)
                overlay_rows = source_conn.execute("SELECT * FROM wpp_release_history").fetchall()
                if overlay_rows:
                    placeholders = ", ".join("?" for _ in overlay_rows[0])
                    serving_conn.executemany(
                        f"INSERT INTO wpp_release_history VALUES ({placeholders})", overlay_rows
                    )
                serving_conn.execute(
                    "CREATE INDEX idx_wpp_release_history_lookup ON wpp_release_history "
                    "(revision, \"ISO3 Alpha-code\", Year)"
                )
                table_counts["wpp_release_history"] = len(overlay_rows)
            manifest = _serving_manifest(source, table_counts)
            serving_conn.execute("CREATE TABLE serving_manifest (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)")
            serving_conn.executemany(
                "INSERT INTO serving_manifest VALUES (?, ?)",
                [(key, json.dumps(value, sort_keys=True)) for key, value in manifest.items()],
            )
        os.chmod(temporary, 0o444)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {**manifest, "destination": str(destination), "database_size_bytes": destination.stat().st_size}


def remove_embedded_wpp_tables(path: Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Remove the redundant full WPP tables from the writable research store.

    Call only after a verified offline archive and serving file exist. The
    caller-facing command always creates a rollback backup first.
    """
    path = Path(path).expanduser().resolve()
    removed: list[str] = []
    with sqlite3.connect(path) as connection:
        for table in WPP_TABLES + ("wpp_release_history",):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            if exists:
                connection.execute(f"DROP TABLE {_quote_identifier(table)}")
                removed.append(table)
    with sqlite3.connect(path) as connection:
        connection.execute("VACUUM")
    return {"database": str(path), "removed_tables": removed, "database_size_bytes": path.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--backup-dir", type=Path, default=BACKUP_DIR)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--compact-candidate-audit", action="store_true")
    actions.add_argument("--build-wpp-archive", type=Path, metavar="DESTINATION")
    actions.add_argument("--build-wpp-serving", type=Path, metavar="DESTINATION")
    actions.add_argument("--remove-embedded-wpp", action="store_true")
    args = parser.parse_args()
    source = args.source or (WPP_ARCHIVE_PATH if args.build_wpp_serving else DEFAULT_DB_PATH)
    backup_path, inventory_path, inventory = preserve_database(source, args.backup_dir)
    print(f"Created read-only rollback backup: {backup_path}")
    print(f"Wrote inventory: {inventory_path}")
    if args.compact_candidate_audit:
        print(json.dumps(compact_candidate_audit(source), indent=2, sort_keys=True))
    elif args.build_wpp_archive:
        print(json.dumps(build_wpp_archive(source, args.build_wpp_archive), indent=2, sort_keys=True))
    elif args.build_wpp_serving:
        print(json.dumps(build_wpp_serving_database(source, args.build_wpp_serving), indent=2, sort_keys=True))
    elif args.remove_embedded_wpp:
        print(json.dumps(remove_embedded_wpp_tables(source), indent=2, sort_keys=True))
    else:
        print(json.dumps(inventory, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
