"""Import the WPP 2024 demographic workbook into SQLite.

The workbook has descriptive rows above the column headings. The headings
are on Excel row 17, so pandas uses ``header=16`` below.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd


DEFAULT_INPUT = Path(
    "Data_Files/WPP2024_GEN_F01_DEMOGRAPHIC_INDICATORS_COMPACT.xlsx"
)
DEFAULT_OUTPUT = Path(
    "Data_Files/WPP2024_GEN_F01_DEMOGRAPHIC_INDICATORS_COMPACT.sqlite"
)

SHEETS = {
    "Estimates": "estimates",
    "Medium variant": "medium_variant",
}

COLUMN_RENAMES = {
    "Region, subregion, country or area *": "Country",
    "Total Population, as of 1 January (thousands)": "Population 1 Jan",
    "Total Population, as of 1 July (thousands)": "Population 1 Jul",
    "Natural Change, Births minus Deaths (thousands)": "Natural Change",
    "Total Deaths (thousands)": "Total Deaths",
    "Births (thousands)": "Total Births",
    "Net Number of Migrants (thousands)": "Net Migration",
}


def import_workbook(input_path: Path, output_path: Path, header_row: int = 17) -> None:
    """Import both data worksheets into separate SQLite tables.

    Values are loaded without cleaning or coercion so source markers such as
    ``...`` are retained exactly as they appear in the workbook. Excel column
    names are also retained, keeping the database faithful to the source.
    """
    if not input_path.is_file():
        raise FileNotFoundError(f"Input workbook not found: {input_path}")
    if header_row < 1:
        raise ValueError("header_row must be a 1-based Excel row number")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)

    header_index = header_row - 1
    frames: dict[str, pd.DataFrame] = {}
    expected_columns: list[str] | None = None

    for sheet_name, table_name in SHEETS.items():
        frame = pd.read_excel(
            input_path,
            sheet_name=sheet_name,
            header=header_index,
            keep_default_na=False,
        ).dropna(how="all")

        columns = [str(column).strip() for column in frame.columns]
        if any(not column for column in columns):
            raise ValueError(f"{sheet_name!r} contains a blank column heading")
        if len(columns) != len(set(columns)):
            raise ValueError(f"{sheet_name!r} contains duplicate column headings")
        frame.columns = columns

        if expected_columns is None:
            expected_columns = columns
        elif columns != expected_columns:
            raise ValueError(
                f"Worksheet {sheet_name!r} does not have the same columns as "
                "the Estimates worksheet"
            )

        frame = frame.rename(columns=COLUMN_RENAMES)
        frames[table_name] = frame

    with sqlite3.connect(output_path) as connection:
        for table_name, frame in frames.items():
            frame.to_sql(table_name, connection, if_exists="replace", index=False)

        # Add a useful lookup index without changing the imported data.
        for table_name in frames:
            connection.execute(
                f'CREATE INDEX "idx_{table_name}_location_year" '
                f'ON "{table_name}" ("Location code", "Year")'
            )

    print(f"Created {output_path}")
    for table_name, frame in frames.items():
        print(f"  {table_name}: {len(frame):,} rows x {len(frame.columns)} columns")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--header-row",
        type=int,
        default=17,
        help="1-based Excel row containing the column headings (default: 17)",
    )
    args = parser.parse_args()
    import_workbook(args.input, args.output, args.header_row)


if __name__ == "__main__":
    main()
