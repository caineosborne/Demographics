"""Tools for querying the UN World Population Prospects database."""

from pathlib import Path
from io import BytesIO
import sqlite3
import time
import json
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import requests
from bs4 import BeautifulSoup
from langchain_core.tools import tool
from langgraph.config import get_stream_writer
from pypdf import PdfReader


DB_PATH = (
    Path(__file__).resolve().parent
    / "Data_Files"
    / "WPP2024_GEN_F01_DEMOGRAPHIC_INDICATORS_COMPACT.sqlite"
)


# Common article country names which differ from the World Population Prospects
# labels. Keep this deliberately small and explicit: these aliases determine
# which UN series is shown and used for comparison.
COUNTRY_ALIASES = {
    "taiwan": "China, Taiwan Province of China",
    "twn": "China, Taiwan Province of China",
    "us": "United States of America",
    "usa": "United States of America",
    "united states": "United States of America",
    "united states of america": "United States of America",
    "uk": "United Kingdom",
    "gb": "United Kingdom",
    "gbr": "United Kingdom",
    "britain": "United Kingdom",
    "south korea": "Republic of Korea",
    "korea, republic of": "Republic of Korea",
    "sk": "Republic of Korea",
    "turkey": "Türkiye",
    # Common article labels which differ from the WPP country names.
    "russia": "Russian Federation",
    "russian federation": "Russian Federation",
}


class PageAccessError(RuntimeError):
    """Neither fetch method could retrieve usable page content."""


def report_activity(message: str) -> None:
    """Write an activity message to the console and active streamed UI run."""
    print(message, flush=True)
    try:
        writer = get_stream_writer()
    except (RuntimeError, KeyError):
        return  # The tool can also be called outside LangGraph.
    writer({"log": message})


def report_fetch_status(status: str) -> None:
    """Send web-fetch progress to the console and active streamed UI run."""
    message = f"[Web] {status}"
    print(message, flush=True)
    try:
        writer = get_stream_writer()
    except (RuntimeError, KeyError):
        return  # The tool can also be called outside LangGraph.
    writer({"fetch_status": status, "log": message})


def extract_page_text(html: str | bytes) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "noscript"]):
        tag.decompose()
    # Some publishers put the article body in sibling sections rather than
    # inside <main>. Prefer the longest cleaned content container so that a
    # valid article is not silently truncated just because its markup is
    # unconventional.
    containers = [node for node in (soup.find("main"), soup.find("article"), soup.body, soup) if node]
    text = max((node.get_text(" ", strip=True) for node in containers), key=len, default="")
    # Common successful HTTP responses that contain a challenge or JS shell.
    placeholders = (
        "enable javascript", "javascript is required", "just a moment",
        "verify you are human", "checking your browser", "access denied",
        "please turn javascript on",
    )
    if (
        not text
        or text.lower().strip(" .…") == "loading"
        or (len(text) < 1000 and any(p in text.lower() for p in placeholders))
    ):
        raise ValueError("The page contained no usable text or displayed an access challenge.")
    return text


def response_is_pdf(url: str, response: requests.Response) -> bool:
    """Identify PDFs from their response content rather than the URL alone."""
    content_type = str(response.headers.get("Content-Type", "")).lower()
    return (
        "application/pdf" in content_type
        or response.content.startswith(b"%PDF-")
        or url.split("?", 1)[0].lower().endswith(".pdf")
    )


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract readable text from a PDF returned by Requests."""
    reader = PdfReader(BytesIO(pdf_bytes))
    if reader.is_encrypted:
        raise ValueError("The PDF is encrypted and cannot be read without a password.")
    text = "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()
    if not text:
        raise ValueError("The PDF contains no extractable text; it may be a scanned document.")
    return text


def _fetch_pdf(url: str) -> str:
    report_fetch_status("Trying Requests")
    with requests.get(url, timeout=30) as response:
        response.raise_for_status()
        if not response_is_pdf(url, response):
            raise ValueError("The URL did not return a PDF document.")
        report_fetch_status("Reading PDF")
        text = extract_pdf_text(response.content)
    report_fetch_status("PDF loaded via Requests — summarising")
    return text


def fetch_with_playwright(url: str) -> str:
    """Render the page in Chromium and extract text from the resulting HTML."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            response = page.goto(url, wait_until="load", timeout=30_000)
            if response is None or not response.ok:
                status = response.status if response else "no response"
                raise ValueError(f"Browser navigation failed: {status}")
            # Give asynchronously rendered content time to replace an empty shell.
            deadline = time.monotonic() + 10
            while True:
                try:
                    return extract_page_text(page.content())
                except ValueError:
                    if time.monotonic() >= deadline:
                        raise
                    page.wait_for_timeout(250)
        finally:
            browser.close()


@tool
def get_page_text(url: str) -> str:
    """Retrieve HTML or PDF text using Requests, falling back to a rendered browser for HTML."""
    report_fetch_status("Trying Requests")
    try:
        with requests.get(url, timeout=30) as response:
            response.raise_for_status()
            if response_is_pdf(url, response):
                report_fetch_status("Reading PDF")
                text = extract_pdf_text(response.content)
                report_fetch_status("PDF loaded via Requests — summarising")
                return text
            text = extract_page_text(response.content)
    except (requests.RequestException, ValueError) as exc:
        requests_error = str(exc)
    else:
        report_fetch_status("Page loaded via Requests — summarising")
        return text

    report_fetch_status("Trying Playwright")
    try:
        text = fetch_with_playwright(url)
    except Exception as exc:
        report_fetch_status("Failed — Requests and Playwright could not access the page")
        raise PageAccessError(
            f"Unable to access the page using Requests or Playwright. "
            f"Requests: {requests_error}. Playwright: {exc}"
        ) from exc
    report_fetch_status("Page loaded via Playwright — summarising")
    return text


@tool
def get_pdf_text(url: str) -> str:
    """Retrieve and extract text from a PDF URL using Requests.

    Use for a URL known to point to a PDF. This preserves the normal research
    flow by returning the document text for summarisation.
    """
    try:
        return _fetch_pdf(url)
    except (requests.RequestException, ValueError) as exc:
        report_fetch_status("Failed — the PDF could not be read")
        raise PageAccessError(f"Unable to read the PDF: {exc}") from exc


def get_connection() -> sqlite3.Connection:
    """Open a connection to the local demographics database."""
    return sqlite3.connect(DB_PATH)


def initialise_findings_table() -> None:
    """Create the durable store for webpage extraction results if needed."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS webpage_findings (
                id INTEGER PRIMARY KEY,
                source_url TEXT NOT NULL UNIQUE,
                effective_date TEXT,
                population_value REAL,
                official_source INTEGER NOT NULL,
                quoted_source TEXT,
                quoted_source_url TEXT,
                extracted_at TEXT NOT NULL,
                finding_json TEXT NOT NULL
            )
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(webpage_findings)")}
        for name, definition in {
            "submission_type": "TEXT NOT NULL DEFAULT 'legacy_unknown'",
            "discovery_source": "TEXT",
            "search_run_id": "TEXT",
            "search_candidate_id": "INTEGER",
            "source_classification": "TEXT NOT NULL DEFAULT 'secondary_unattributed'",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE webpage_findings ADD COLUMN {name} {definition}")
        # The label is presentation/audit metadata derived from existing fields,
        # so older records receive the same treatment as new ones.
        conn.execute("""
            UPDATE webpage_findings
            SET source_classification = CASE
                WHEN official_source THEN 'official_publisher'
                WHEN quoted_source IS NOT NULL AND trim(quoted_source) <> '' THEN 'secondary_attributed'
                ELSE 'secondary_unattributed'
            END
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_webpage_findings_report
            ON webpage_findings (effective_date, population_value)
        """)


def source_classification(finding: dict[str, Any]) -> str:
    """Return a display/audit label without changing the extraction contract."""
    if finding.get("official_source"):
        return "official_publisher"
    if str(finding.get("quoted_source") or "").strip():
        return "secondary_attributed"
    return "secondary_unattributed"


def store_webpage_finding(finding: dict[str, Any], provenance: dict | None = None) -> dict[str, str | int]:
    """Store an extracted finding unless it matches an existing source or report.

    A matching report has both the same extracted effective date and population
    total. If either value is unavailable, only the exact-URL check is used.
    """
    provenance = provenance or {"submission_type": "manual", "discovery_source": "manual"}
    initialise_findings_table()
    canonical_country = normalise_country_name(finding.get("geography") or "")
    if canonical_country:
        finding = {**finding, "geography": canonical_country}
    source_url = finding["url"]
    population = (finding.get("statistics") or {}).get("population") or {}
    effective_date = finding.get("effective_date")
    population_value = population.get("value")

    with get_connection() as conn:
        duplicate_url = conn.execute(
            "SELECT id FROM webpage_findings WHERE source_url = ?", (source_url,)
        ).fetchone()
        if duplicate_url:
            return {"status": "excluded_duplicate_url", "existing_id": duplicate_url[0]}

        if effective_date is not None and population_value is not None:
            duplicate_report = conn.execute(
                """SELECT id FROM webpage_findings
                   WHERE effective_date = ? AND population_value = ?
                   LIMIT 1""",
                (effective_date, population_value),
            ).fetchone()
            if duplicate_report:
                return {
                    "status": "excluded_duplicate_report",
                    "existing_id": duplicate_report[0],
                }

        cursor = conn.execute(
            """INSERT INTO webpage_findings (
                   source_url, effective_date, population_value, official_source,
                   quoted_source, quoted_source_url, extracted_at, finding_json,
                   submission_type, discovery_source, search_run_id, search_candidate_id,
                   source_classification
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_url,
                effective_date,
                population_value,
                int(bool(finding.get("official_source"))),
                finding.get("quoted_source"),
                finding.get("quoted_source_url"),
                datetime.now(timezone.utc).isoformat(),
                json.dumps(finding, sort_keys=True),
                provenance.get("submission_type", "manual"),
                provenance.get("discovery_source", "manual"),
                provenance.get("search_run_id"),
                provenance.get("search_candidate_id"),
                source_classification(finding),
            ),
        )
        return {"status": "stored", "id": cursor.lastrowid}


def list_webpage_findings() -> list[dict[str, Any]]:
    """Return stored findings with the newest numeric ID first for review."""
    initialise_findings_table()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, source_url, effective_date, population_value,
                   official_source, quoted_source, quoted_source_url,
                   extracted_at, finding_json, submission_type, discovery_source,
                   search_run_id, search_candidate_id, source_classification
            FROM webpage_findings
        """).fetchall()

    findings = []
    for row in rows:
        stored = dict(row)
        finding = json.loads(stored.pop("finding_json"))
        findings.append({
            "ID": stored["id"],
            "Submission type": stored["submission_type"],
            "Discovery source": stored["discovery_source"],
            "Search run ID": stored["search_run_id"],
            "Search candidate ID": stored["search_candidate_id"],
            "Country": finding.get("geography") or "",
            "Effective date": stored["effective_date"] or "",
            "Population": stored["population_value"],
            "TFR": ((finding.get("statistics") or {}).get("total_fertility_rate") or {}).get("value"),
            "Official source": "Yes" if stored["official_source"] else "No",
            "Source classification": stored["source_classification"],
            "Source": finding.get("source") or "",
            "Quoted source": stored["quoted_source"] or "",
            "Quoted source URL": stored["quoted_source_url"] or "",
            "Webpage URL": stored["source_url"],
            "Extracted at (UTC)": stored["extracted_at"],
            "Extracted JSON": json.dumps(finding, ensure_ascii=False, sort_keys=True),
        })
    return sorted(findings, key=lambda finding: finding["ID"], reverse=True)


def get_webpage_finding(finding_id: int) -> dict[str, Any]:
    """Return one stored finding for the database editor."""
    initialise_findings_table()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT finding_json FROM webpage_findings WHERE id = ?", (finding_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"No stored finding exists with ID {finding_id}.")
    return json.loads(row[0])


def _finding_storage_fields(finding: dict[str, Any]) -> tuple:
    if not isinstance(finding, dict) or not finding.get("url"):
        raise ValueError("The finding JSON must be an object with a non-empty url.")
    canonical_country = normalise_country_name(finding.get("geography") or "")
    if canonical_country:
        finding["geography"] = canonical_country
    population = ((finding.get("statistics") or {}).get("population") or {})
    return (
        finding["url"],
        finding.get("effective_date"),
        population.get("value"),
        int(bool(finding.get("official_source"))),
        finding.get("quoted_source"),
        finding.get("quoted_source_url"),
        json.dumps(finding, sort_keys=True),
        source_classification(finding),
    )


def update_webpage_finding(finding_id: int, finding_json: str) -> None:
    """Validate and save a manual edit to one stored extraction."""
    try:
        finding = json.loads(finding_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"The finding JSON is invalid: {exc.msg}.") from exc
    source_url, effective_date, population_value, official_source, quoted_source, quoted_source_url, payload, classification = _finding_storage_fields(finding)
    initialise_findings_table()
    with get_connection() as conn:
        exists = conn.execute("SELECT 1 FROM webpage_findings WHERE id = ?", (finding_id,)).fetchone()
        if not exists:
            raise ValueError(f"No stored finding exists with ID {finding_id}.")
        duplicate_url = conn.execute(
            "SELECT id FROM webpage_findings WHERE source_url = ? AND id != ?", (source_url, finding_id)
        ).fetchone()
        if duplicate_url:
            raise ValueError(f"This webpage URL is already stored as ID {duplicate_url[0]}.")
        if effective_date is not None and population_value is not None:
            duplicate_report = conn.execute(
                """SELECT id FROM webpage_findings
                   WHERE effective_date = ? AND population_value = ? AND id != ?""",
                (effective_date, population_value, finding_id),
            ).fetchone()
            if duplicate_report:
                raise ValueError(f"This effective date and population already exist in ID {duplicate_report[0]}.")
        conn.execute(
            """UPDATE webpage_findings SET
                   source_url = ?, effective_date = ?, population_value = ?,
                   official_source = ?, quoted_source = ?, quoted_source_url = ?,
                   finding_json = ?, source_classification = ?
               WHERE id = ?""",
            (source_url, effective_date, population_value, official_source,
             quoted_source, quoted_source_url, payload, classification, finding_id),
        )


def delete_webpage_finding(finding_id: int) -> None:
    """Delete one explicitly selected stored finding."""
    initialise_findings_table()
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM webpage_findings WHERE id = ?", (finding_id,))
        if cursor.rowcount != 1:
            raise ValueError(f"No stored finding exists with ID {finding_id}.")


def delete_finding_metric(finding_id: int, metric: str) -> None:
    """Remove one metric from a stored finding while preserving its other data."""
    metric_keys = {
        'population': 'population',
        'births': 'births',
        'deaths': 'deaths',
        'natural_change': 'natural_change',
        'net_migration': 'net_overseas_migration',
        'total_fertility_rate': 'total_fertility_rate',
    }
    key = metric_keys.get(str(metric or '').strip())
    if not key:
        raise ValueError(f"Unknown finding metric: {metric}.")
    finding = get_webpage_finding(finding_id)
    statistics = finding.get('statistics')
    if not isinstance(statistics, dict) or key not in statistics:
        raise ValueError(f"Finding #{finding_id} has no {metric} datapoint.")
    statistics = dict(statistics)
    statistics.pop(key, None)
    finding['statistics'] = statistics
    update_webpage_finding(finding_id, json.dumps(finding, ensure_ascii=False))


def normalise_stored_finding_geographies() -> int:
    """Migrate stored country labels from ISO3 codes to canonical names."""
    initialise_findings_table()
    updates = 0
    with get_connection() as conn:
        rows = conn.execute("SELECT id, finding_json FROM webpage_findings").fetchall()
        for finding_id, payload in rows:
            finding = json.loads(payload)
            canonical_country = normalise_country_name(finding.get("geography") or "")
            if canonical_country and canonical_country != finding.get("geography"):
                finding["geography"] = canonical_country
                conn.execute(
                    "UPDATE webpage_findings SET finding_json = ? WHERE id = ?",
                    (json.dumps(finding, sort_keys=True), finding_id),
                )
                updates += 1
    return updates


def run_query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a parameterized query and return rows as dictionaries."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def resolve_country_iso3(country_name: str) -> str | None:
    """Return the database's ISO3 code for a name, alias, or ISO3 input."""
    canonical = normalise_country_name(country_name)
    if not canonical:
        return None
    sql = '''
        SELECT DISTINCT "ISO3 Alpha-code" AS iso3
        FROM medium_variant
        WHERE Country = ?
          AND "ISO3 Alpha-code" IS NOT NULL
          AND "ISO3 Alpha-code" != ''
    '''
    rows = run_query(sql, (canonical,))
    return rows[0]["iso3"] if len(rows) == 1 else None


@lru_cache(maxsize=1)
def _country_reference() -> tuple[tuple[str, str], ...]:
    """Cache the small, static UN country/name-to-ISO reference in-process."""
    rows = run_query('''
        SELECT DISTINCT Country, "ISO3 Alpha-code" AS ISO3
        FROM medium_variant
        ORDER BY Country
    ''')
    return tuple((str(row["Country"]), str(row["ISO3"] or "")) for row in rows)


def normalise_country_name(country_name: str) -> str | None:
    """Return the canonical database country name from a name or ISO3 code."""
    candidate = country_name.strip().casefold()
    if not candidate:
        return None
    candidate = COUNTRY_ALIASES.get(candidate, candidate).casefold()
    try:
        rows = _country_reference()
    except sqlite3.OperationalError:
        # Isolated storage tests and manually supplied databases may not include
        # the UN reference tables. Preserve the supplied geography in that case.
        return None
    matches = [name for name, iso3 in rows
               if name.casefold() == candidate or iso3.casefold() == candidate]
    return matches[0] if len(matches) == 1 else None


def list_country_names() -> list[str]:
    """List canonical country names for user-interface selectors."""
    return [row["Country"] for row in run_query(
        '''SELECT DISTINCT Country FROM medium_variant
           WHERE "ISO3 Alpha-code" IS NOT NULL
             AND length(trim("ISO3 Alpha-code")) = 3
           ORDER BY Country'''
    )]


@tool
def get_population_forecast(
    country_iso3: str,
    years: list[int],
    historic: bool = False,
) -> list[dict]:
    """Retrieve demographic figures for an ISO3 country code across multiple years.

    Use historic=True to query historical estimates. Otherwise, query the
    UN's medium-variant population projections. Population, births, deaths,
    migration, and natural-change values are reported in thousands of people.
    This is the UN WPP 2024 revision, created in 2024; its effective as-of
    vintage is 2024, not the current date. In this database, the historical
    estimates end in 2023. For 2024 or later use historic=False to retrieve
    medium-variant projections made in the 2024 revision.
    Use a three-letter ISO3 code from get_list_of_countries, such as JPN for
    Japan. Do not pass a country name. The response includes the resolved
    country name and ISO3 code for verification.
    """
    if not years:
        return []

    table_name = "estimates" if historic else "medium_variant"
    year_placeholders = ", ".join("?" for _ in years)

    sql = f"""
        SELECT
            Country,
            "ISO3 Alpha-code" AS ISO3,
            Year,
            "Population 1 Jan",
            "Population 1 Jul",
            "Total Births",
            "Net Migration",
            "Total Deaths",
            "Natural Change"
            , "Total Fertility Rate (live births per woman)"
        FROM "{table_name}"
        WHERE "ISO3 Alpha-code" = ?
          AND Year IN ({year_placeholders})
        ORDER BY Year
    """
    country_iso3 = country_iso3.strip().upper()
    if len(country_iso3) != 3 or not country_iso3.isalpha():
        raise ValueError("country_iso3 must be a three-letter ISO country code, such as JPN.")
    report_activity(f"[UN query] table={table_name} ISO3={country_iso3} years={years}")
    return run_query(sql, (country_iso3, *years))


@tool
def get_list_of_countries() -> list[dict]:
    """List country and area names with their ISO3 codes in the UN dataset.

    Use this reference to resolve a country name before calling
    get_population_forecast. Areas without an ISO3 code remain listed, but
    cannot be queried by the ISO3-based forecast tool.
    """
    sql = """
        SELECT DISTINCT Country, "ISO3 Alpha-code" AS ISO3
        FROM medium_variant
        ORDER BY Country, ISO3
    """
    report_activity("[UN query] listing country and ISO3 reference data")
    return run_query(sql)


tools = [get_population_forecast, get_list_of_countries]
WEB_TOOLS = [get_page_text, get_pdf_text]
SQL_TOOLS = tools
