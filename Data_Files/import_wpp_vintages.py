"""Download selected UN WPP releases and load them into one isolated SQLite table.

The current WPP 2024 tables are intentionally not modified.  Run this once
with official archive files (or their direct download URLs), for example:

    uv run python Data_Files/import_wpp_vintages.py \
      --revision 2022 --source /path/to/WPP2022_DB1_Medium.xlsx \
      --revision 2017 --source /path/to/WPP2017_DB1_Medium.xlsx \
      --revision 2012 --source /path/to/WPP2012_DB1_Medium.xlsx

`--source` also accepts an https URL. Raw source copies are retained under
``Data_Files/UN_archives`` as offline source material. The offline archive is
updated first, then its generated serving copy under ``databases`` is rebuilt.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database_maintenance import build_wpp_serving_database

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB_PATH = PROJECT_ROOT / "Data_Files" / "WPP2024_GEN_F01_DEMOGRAPHIC_INDICATORS_COMPACT.sqlite"
ARCHIVE_DIR = ROOT / "UN_archives"

FIELDS = {
    "Population 1 Jul": ("Population 1 July", "Population at mid-year", "PopTotal", "Pop"),
    "Total Births": ("Births", "Births (thousands)"),
    "Total Deaths": ("Deaths", "Deaths (thousands)"),
    "Natural Change": ("Natural increase", "Natural Change", "NatIncr"),
    "Net Migration": ("Net migrations", "NetMigrations", "Net migration"),
    "Total Fertility Rate (live births per woman)": ("TFR", "Total fertility", "Total Fertility Rate"),
}


def _normalise_column(name: str) -> str:
    return "".join(char for char in str(name).casefold() if char.isalnum())


def _find_column(frame: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    lookup = {_normalise_column(column): str(column) for column in frame.columns}
    for name in names:
        candidate = lookup.get(_normalise_column(name))
        if candidate:
            return candidate
    return None


def _country_key(value: object) -> str:
    return ''.join(char for char in str(value).casefold() if char.isalnum())


def _populate_iso3(result: pd.DataFrame) -> pd.DataFrame:
    """Backfill release ISO3 values from the current WPP country reference.

    Archived workbooks do not consistently carry ISO3. The current compact
    WPP source contains both canonical labels and numeric location codes, so
    use either stable key before the release is written to the serving copy.
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                'SELECT DISTINCT Country, "Location code", "ISO3 Alpha-code" '
                'FROM medium_variant WHERE "ISO3 Alpha-code" IS NOT NULL'
            ).fetchall()
    except sqlite3.Error:
        rows = []
    by_name = {_country_key(row[0]): str(row[2]).upper() for row in rows if row[0]}
    by_location = {str(row[1]).split('.')[0]: str(row[2]).upper()
                   for row in rows if row[1] is not None and row[2]}
    existing = result.get("ISO3 Alpha-code")
    result["ISO3 Alpha-code"] = existing.where(existing.notna(), None) if existing is not None else None
    missing = result["ISO3 Alpha-code"].isna() | (result["ISO3 Alpha-code"].astype(str).str.strip() == "")
    if "Location code" in result:
        result.loc[missing, "ISO3 Alpha-code"] = result.loc[missing, "Location code"].map(
            lambda value: by_location.get(str(value).split('.')[0])
        )
    missing = result["ISO3 Alpha-code"].isna() | (result["ISO3 Alpha-code"].astype(str).str.strip() == "")
    result.loc[missing, "ISO3 Alpha-code"] = result.loc[missing, "Country"].map(
        lambda value: by_name.get(_country_key(value))
    )
    result["ISO3 Alpha-code"] = result["ISO3 Alpha-code"].where(
        result["ISO3 Alpha-code"].notna(), None
    )
    return result


def download_source(source: str) -> Path:
    """Return a local, reproducible copy of an official UN archive file."""
    ARCHIVE_DIR.mkdir(exist_ok=True)
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        destination = ARCHIVE_DIR / Path(parsed.path).name
        response = requests.get(source, timeout=180)
        response.raise_for_status()
        destination.write_bytes(response.content)
        return destination
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if ROOT in path.parents:
        return path
    destination = ARCHIVE_DIR / path.name
    if path != destination:
        shutil.copy2(path, destination)
    return destination


def _read_wpp2017_interpolated(path: Path) -> pd.DataFrame:
    """Combine WPP 2017's annual indicators and annual total population files."""
    population_path = path.parents[1] / "1_Population" / "WPP2017_POP_F01_1_TOTAL_POPULATION_BOTH_SEXES.xlsx"
    if not population_path.is_file():
        raise FileNotFoundError(f"WPP 2017 population companion not found: {population_path}")
    frames = []
    for indicators_sheet, population_sheet in (("ESTIMATES", "ESTIMATES"), ("MEDIUM VARIANT", "MEDIUM VARIANT")):
        indicators = pd.read_excel(path, sheet_name=indicators_sheet, header=16)
        population = pd.read_excel(population_path, sheet_name=population_sheet, header=16)
        country = "Region, subregion, country or area *"
        code = "Country code"
        year = "Reference date (1 January - 31 December)"
        population = population.melt(id_vars=[country, code], value_vars=[column for column in population.columns if str(column).isdigit()], var_name="Year", value_name="Population 1 Jul")
        indicators = indicators.rename(columns={year: "Year"})
        indicators["Year"] = pd.to_numeric(indicators["Year"], errors="coerce")
        population["Year"] = pd.to_numeric(population["Year"], errors="coerce")
        merged = indicators.merge(population, on=[country, code, "Year"], how="left")
        frames.append(merged)
    frame = pd.concat(frames, ignore_index=True)
    result = pd.DataFrame({
        "Country": frame["Region, subregion, country or area *"],
        "ISO3 Alpha-code": None,
        "Location code": frame["Country code"],
        "Year": frame["Year"],
        "Population 1 Jul": frame["Population 1 Jul"],
        "Total Births": frame["Births (thousands)"],
        "Total Deaths": frame["Deaths (thousands)"],
        "Natural Change": frame["Total population natural change / increase (thousands)"],
        "Net Migration": None,
        "Total Fertility Rate (live births per woman)": frame["Total fertility (live births per woman)"],
    })
    result["Year"] = pd.to_numeric(result["Year"], errors="coerce")
    result = result.dropna(subset=["Country", "Year"])
    result["Year"] = result["Year"].astype(int)
    return _populate_iso3(result.drop_duplicates(subset=["Country", "Year"], keep="first"))


def read_release(path: Path) -> pd.DataFrame:
    """Read a WPP DB1/period-indicators sheet and retain a common schema."""
    path = Path(path)
    if path.name == "WPP2017_INT_F01_ANNUAL_DEMOGRAPHIC_INDICATORS.xlsx":
        return _read_wpp2017_interpolated(path)
    if path.suffix.casefold() in {".csv", ".gz"}:
        frame = pd.read_csv(path, compression="infer")
    else:
        workbook = pd.ExcelFile(path)
        frame = None
        # The current and archived UN workbooks put the real headings beneath
        # descriptive title rows.  Inspect the first 30 possible header rows.
        for sheet_name in workbook.sheet_names:
            for header in range(30):
                sheet = pd.read_excel(workbook, sheet_name=sheet_name, header=header)
                if _find_column(sheet, ("Country", "Region, subregion, country or area *")) and _find_column(sheet, ("Year", "Period")):
                    frame = sheet
                    break
            if frame is not None:
                break
        if frame is None:
            raise ValueError(f"No country/year data sheet found in {path.name}.")
    country = _find_column(frame, ("Country", "Region, subregion, country or area *", "Location"))
    year = _find_column(frame, ("Year", "Period", "Reference date (1 January - 31 December)"))
    iso3 = _find_column(frame, ("ISO3 Alpha-code", "ISO3", "ISO3_code"))
    if not country or not year:
        raise ValueError("The release must contain country and year/period columns.")
    result = pd.DataFrame({"Country": frame[country], "Year": frame[year]})
    location = _find_column(frame, ("Location code", "Country code"))
    result["Location code"] = frame[location] if location else None
    result["ISO3 Alpha-code"] = frame[iso3] if iso3 else None
    for canonical, aliases in FIELDS.items():
        source = _find_column(frame, (canonical, *aliases))
        result[canonical] = frame[source] if source else None
    # Old releases label a five-year interval such as 2010-2015. Its end year
    # is the comparable UN observation date and deliberately stays five-yearly.
    result["Year"] = result["Year"].astype(str).str.extract(r"(\d{4})(?:\D+\d{4})?", expand=False)
    result["Year"] = pd.to_numeric(result["Year"], errors="coerce")
    result = result.dropna(subset=["Country", "Year"]).copy()
    result["Year"] = result["Year"].astype(int)
    for column in FIELDS:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    # The WPP 2017 estimates and medium-variant sheets overlap at the base
    # year.  Keep the estimate row, which was concatenated first.
    return _populate_iso3(result.drop_duplicates(subset=["Country", "Year"], keep="first"))


def import_release(revision: int, source: str) -> int:
    path = download_source(source)
    rows = read_release(path)
    cadence = 1 if revision == 2022 else 5
    rows.insert(0, "revision", revision)
    rows["cadence_years"] = cadence
    rows["source_url"] = source
    columns = ["revision", "Country", "ISO3 Alpha-code", "Year", *FIELDS, "cadence_years", "source_url"]
    os.chmod(DB_PATH, 0o644)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS wpp_release_history (
            revision INTEGER NOT NULL, Country TEXT NOT NULL, "ISO3 Alpha-code" TEXT,
            Year INTEGER NOT NULL, "Population 1 Jul" REAL, "Total Births" REAL,
            "Total Deaths" REAL, "Natural Change" REAL, "Net Migration" REAL,
            "Total Fertility Rate (live births per woman)" REAL, cadence_years INTEGER NOT NULL,
            source_url TEXT NOT NULL, PRIMARY KEY (revision, Country, Year))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wpp_release_history_lookup ON wpp_release_history (revision, \"ISO3 Alpha-code\", Year)")
        conn.execute("DELETE FROM wpp_release_history WHERE revision = ?", (revision,))
        rows[columns].to_sql("wpp_release_history", conn, if_exists="append", index=False)
    os.chmod(DB_PATH, 0o444)
    build_wpp_serving_database(DB_PATH, PROJECT_ROOT / "databases" / "wpp_serving.sqlite")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", action="append", type=int, required=True)
    parser.add_argument("--source", action="append", required=True)
    args = parser.parse_args()
    if len(args.revision) != len(args.source) or set(args.revision) - {2012, 2017, 2022}:
        parser.error("Provide matching --revision/--source pairs for 2012, 2017, and/or 2022.")
    for revision, source in zip(args.revision, args.source, strict=True):
        print(f"WPP {revision}: imported {import_release(revision, source):,} rows")


if __name__ == "__main__":
    main()
