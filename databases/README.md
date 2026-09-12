# Runtime database storage

The application database belongs in this directory. `Data_Files` is an
offline source/archive location only and must not be used by the application
or by future database-building commands.

The local filename is:

```text
WPP2024_GEN_F01_DEMOGRAPHIC_INDICATORS_COMPACT.sqlite
```

Before changing any table or record, create a dated rollback point with:

```sh
uv run python database_maintenance.py
```

The command writes a read-only SQLite backup and a JSON inventory under
`databases/backups/`. Runtime database paths can be explicitly overridden with
`DEMOGRAPHICS_DB_PATH`; there is no fallback to `Data_Files`.

