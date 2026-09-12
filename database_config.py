"""Runtime database locations.

``Data_Files`` contains source archives and is deliberately not a runtime
database location. Keeping this policy in one small module makes accidental
reintroduction of the old path easy to detect in review and allows local or
production deployments to provide an explicit database path when needed.
"""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DATABASE_DIR = PROJECT_ROOT / "databases"
DATABASE_FILENAME = "WPP2024_GEN_F01_DEMOGRAPHIC_INDICATORS_COMPACT.sqlite"
DEFAULT_DB_PATH = DATABASE_DIR / DATABASE_FILENAME
BACKUP_DIR = DATABASE_DIR / "backups"
WPP_ARCHIVE_PATH = PROJECT_ROOT / "Data_Files" / "WPP2024_GEN_F01_DEMOGRAPHIC_INDICATORS_COMPACT.sqlite"
DEFAULT_WPP_SERVING_PATH = DATABASE_DIR / "wpp_serving.sqlite"


def configured_database_path() -> Path:
    """Return the explicitly configured database or the local production path.

    The environment override is useful for Railway/local development and is
    intentionally explicit: there is no fallback to the legacy data folder.
    """
    configured = os.environ.get("DEMOGRAPHICS_DB_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_DB_PATH


def configured_wpp_database_path() -> Path:
    """Return the generated, read-only WPP serving database.

    The optional override permits deployments to mount the serving data
    independently from the writable research database.  It deliberately has
    no fallback to ``Data_Files``: that directory is the offline archive.
    """
    configured = os.environ.get("DEMOGRAPHICS_WPP_DB_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_WPP_SERVING_PATH
