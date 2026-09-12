# Runtime database storage

The writable application database belongs in this directory. `Data_Files` is
an offline source/archive location only and must not be used at runtime.

The local filename is:

```text
WPP2024_GEN_F01_DEMOGRAPHIC_INDICATORS_COMPACT.sqlite
```

The generated read-only WPP query database is `wpp_serving.sqlite`. Rebuild it
from the complete offline WPP archive with:

```sh
uv run python database_maintenance.py \
  --source Data_Files/WPP2024_GEN_F01_DEMOGRAPHIC_INDICATORS_COMPACT.sqlite \
  --build-wpp-serving databases/wpp_serving.sqlite
```

The application uses `wpp_serving.sqlite` for UN comparisons and the writable
file above for research settings, audit records, and findings. Set
`DEMOGRAPHICS_WPP_DB_PATH` to mount the serving file elsewhere in deployment.

Before changing any table or record, create a dated rollback point with:

```sh
uv run python database_maintenance.py
```

The command writes a read-only SQLite backup and a JSON inventory under
`databases/backups/`. Runtime database paths can be explicitly overridden with
`DEMOGRAPHICS_DB_PATH`; there is no fallback to `Data_Files`.
