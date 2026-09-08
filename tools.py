"""Tools for querying the UN World Population Prospects database."""

from pathlib import Path
import sqlite3

import requests
from bs4 import BeautifulSoup
from langchain_core.tools import tool


DB_PATH = (
    Path(__file__).resolve().parent
    / "Data_Files"
    / "WPP2024_GEN_F01_DEMOGRAPHIC_INDICATORS_COMPACT.sqlite"
)


@tool
def get_page_text(url: str) -> str:
    """Retrieve the main text content of a web page from its URL."""
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        return f"Unable to access the page: {exc}"

    soup = BeautifulSoup(response.content, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body
    return main.get_text(" ", strip=True) if main else ""


def get_connection() -> sqlite3.Connection:
    """Open a connection to the local demographics database."""
    return sqlite3.connect(DB_PATH)


def run_query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a parameterized query and return rows as dictionaries."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


@tool
def get_population_forecast(
    country: str,
    years: list[int],
    historic: bool = False,
) -> list[dict]:
    """Retrieve demographic figures for a country across multiple years.

    Use historic=True to query historical estimates. Otherwise, query the
    UN's medium-variant population projections. Population, births, deaths,
    migration, and natural-change values are reported in thousands of people.
    In this database, the historic estimates table ends in 2024. Do not use
    historic=True for 2025 or later; use the medium-variant table instead.
    """
    if not years:
        return []

    table_name = "estimates" if historic else "medium_variant"
    year_placeholders = ", ".join("?" for _ in years)

    sql = f"""
        SELECT
            Country,
            Year,
            "Population 1 Jan",
            "Population 1 Jul",
            "Total Births",
            "Net Migration",
            "Total Deaths",
            "Natural Change"
        FROM "{table_name}"
        WHERE Country = ?
          AND Year IN ({year_placeholders})
        ORDER BY Year
    """
    print("Getting population for", table_name, country, years)
    return run_query(sql, (country, *years))


@tool
def get_list_of_countries() -> list[str]:
    """List the exact country and area names available in the UN dataset."""
    sql = """
        SELECT DISTINCT Country
        FROM medium_variant
        ORDER BY Country
    """
    print("Getting list of countries")
    return [row["Country"] for row in run_query(sql)]


tools = [get_population_forecast, get_list_of_countries]
WEB_TOOLS = [get_page_text]
SQL_TOOLS = tools
