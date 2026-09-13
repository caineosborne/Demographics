"""Tools for querying the UN World Population Prospects database."""

from io import BytesIO
import sqlite3
import time
import json
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from contextvars import ContextVar
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from langchain_core.tools import tool
from langgraph.config import get_stream_writer
from pypdf import PdfReader

from database_config import configured_database_path, configured_wpp_database_path


DB_PATH = configured_database_path()
WPP_DB_PATH = configured_wpp_database_path()
_COUNTRY_MIGRATION_IN_PROGRESS = False
_progress_callback: ContextVar[Any] = ContextVar("progress_callback", default=None)


def set_progress_callback(callback):
    """Install a thread-local progress sink for callers such as API jobs."""
    return _progress_callback.set(callback)


def reset_progress_callback(token) -> None:
    _progress_callback.reset(token)


def initialise_wpp_vintages_table() -> None:
    """Create the optional, release-specific UN history store.

    This deliberately lives beside, rather than inside, the 2024 `estimates`
    and `medium_variant` tables.  The latter remain the authoritative source
    for extraction comparisons and all existing application behaviour.
    """
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wpp_release_history (
                revision INTEGER NOT NULL,
                Country TEXT NOT NULL,
                "ISO3 Alpha-code" TEXT,
                Year INTEGER NOT NULL,
                "Population 1 Jul" REAL,
                "Total Births" REAL,
                "Total Deaths" REAL,
                "Natural Change" REAL,
                "Net Migration" REAL,
                "Total Fertility Rate (live births per woman)" REAL,
                cadence_years INTEGER NOT NULL,
                source_url TEXT NOT NULL,
                PRIMARY KEY (revision, Country, Year)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_wpp_release_history_lookup
            ON wpp_release_history (revision, "ISO3 Alpha-code", Year)
        """)


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
    callback = _progress_callback.get()
    if callback:
        callback({"type": "log", "message": message})
    try:
        writer = get_stream_writer()
    except (RuntimeError, KeyError):
        return  # The tool can also be called outside LangGraph.
    writer({"log": message})


def report_fetch_status(status: str) -> None:
    """Send web-fetch progress to the console and active streamed UI run."""
    message = f"[Web] {status}"
    print(message, flush=True)
    callback = _progress_callback.get()
    if callback:
        callback({"type": "fetch_status", "status": status, "message": message})
    try:
        writer = get_stream_writer()
    except (RuntimeError, KeyError):
        return  # The tool can also be called outside LangGraph.
    writer({"fetch_status": status, "log": message})


def extract_page_text(html: str | bytes) -> str:
    soup = BeautifulSoup(html, "html.parser")
    declared_article = soup.find(
        "meta", attrs={"property": "og:type", "content": re.compile(r"article", re.IGNORECASE)}
    ) is not None
    for tag in soup(["script", "style", "nav", "footer", "noscript"]):
        tag.decompose()
    # Some publishers put the article body in sibling sections rather than
    # inside <main>. Prefer the longest cleaned content container so that a
    # valid article is not silently truncated just because its markup is
    # unconventional.
    containers = [node for node in (soup.find("main"), soup.find("article"), soup.body, soup) if node]
    text = max((node.get_text(" ", strip=True) for node in containers), key=len, default="")
    # A server response can contain an article title/metadata while the body
    # is only a client-side shell. Do not treat that shell as usable article
    # text; let the rendered-browser fallback inspect the populated page.
    semantic_containers = [node for node in (soup.find("main"), soup.find("article")) if node]
    semantic_text = max((node.get_text(" ", strip=True) for node in semantic_containers), key=len, default="")
    if declared_article and len(semantic_text) < 200 and len(text) < 500:
        raise ValueError("The response contains article metadata but no server-rendered article body.")
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
    with requests.get(url, timeout=20) as response:
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
            # The browser fallback gets one 40-second budget in total: this
            # includes navigation and any wait for a client-rendered article.
            deadline = time.monotonic() + 40
            response = page.goto(url, wait_until="load", timeout=40_000)
            if response is None or not response.ok:
                status = response.status if response else "no response"
                raise ValueError(f"Browser navigation failed: {status}")
            # Give asynchronously rendered content the remainder of the same
            # budget to replace an empty shell.
            while True:
                texts = []
                for frame in page.frames:
                    try:
                        texts.append(extract_page_text(frame.content()))
                    except Exception:
                        continue
                text = max(texts, key=len, default="")
                if len(text) >= 200:
                    return text
                if time.monotonic() >= deadline:
                    raise ValueError("The rendered page contained no usable article text.")
                try:
                    page.wait_for_timeout(250)
                except Exception:
                    if time.monotonic() >= deadline:
                        raise
        finally:
            browser.close()


@tool
def get_page_text(url: str) -> str:
    """Retrieve HTML or PDF text using Requests, falling back to a rendered browser for HTML."""
    report_fetch_status("Trying Requests")
    raw_response_text = ""
    try:
        with requests.get(url, timeout=20) as response:
            response.raise_for_status()
            if response_is_pdf(url, response):
                report_fetch_status("Reading PDF")
                text = extract_pdf_text(response.content)
                report_fetch_status("PDF loaded via Requests — summarising")
                return text
            raw_response_text = response.text if isinstance(response.text, str) else response.content.decode("utf-8", errors="replace")
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
        # Preserve the original document for the extractor when Requests
        # received a substantial HTML document but its article body was
        # encoded in a shell/script structure that BeautifulSoup could not
        # expose as ordinary text. The extraction prompt treats this as
        # untrusted page content and is bounded before the model sees it.
        if len(raw_response_text) >= 1_000:
            report_fetch_status("Using raw HTML response — article body was not parsed")
            return raw_response_text
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


def get_wpp_connection() -> sqlite3.Connection:
    """Open the generated WPP serving database without granting write access."""
    if not WPP_DB_PATH.is_file():
        raise FileNotFoundError(
            f"WPP serving database not found: {WPP_DB_PATH}. Run database_maintenance.py "
            "--build-wpp-serving after preparing the archive."
        )
    return sqlite3.connect(f"file:{WPP_DB_PATH.resolve().as_posix()}?mode=ro", uri=True)


def canonicalise_source_url(url: str) -> str:
    """Return the shared canonical key used for URL deduplication."""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Expected an HTTP(S) article URL without credentials.")
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    )
    return urlunsplit((
        "https",
        parts.hostname.lower().removeprefix("www."),
        parts.path.rstrip("/") or "/",
        urlencode(query),
        "",
    ))


def initialise_findings_table() -> None:
    """Create the durable store for webpage extraction results if needed."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS webpage_findings (
                id INTEGER PRIMARY KEY,
                source_url TEXT NOT NULL,
                canonical_url TEXT,
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
            "canonical_url": "TEXT",
            "submission_type": "TEXT NOT NULL DEFAULT 'legacy_unknown'",
            "discovery_source": "TEXT",
            "search_run_id": "TEXT",
            "search_candidate_id": "INTEGER",
            "source_classification": "TEXT NOT NULL DEFAULT 'legacy_unreviewed'",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE webpage_findings ADD COLUMN {name} {definition}")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS source_rules (
                id INTEGER PRIMARY KEY,
                match_type TEXT NOT NULL CHECK(match_type IN ('canonical_url', 'domain')),
                match_value TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('classify', 'exclude')),
                classification TEXT CHECK(classification IN (
                    'official_publisher', 'secondary_attributed',
                    'secondary_unattributed', 'legacy_unreviewed'
                )),
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK((action = 'classify' AND classification IS NOT NULL) OR
                      (action = 'exclude' AND classification IS NULL))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS source_rule_actions (
                id INTEGER PRIMARY KEY,
                rule_id INTEGER,
                action TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                acted_at TEXT NOT NULL,
                note TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fallback_providers (
                domain TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                max_age_days INTEGER NOT NULL DEFAULT 90 CHECK(max_age_days >= 0),
                only_when_country_blank_days INTEGER NOT NULL DEFAULT 90
                    CHECK(only_when_country_blank_days >= 0),
                allow_undated_seed INTEGER NOT NULL DEFAULT 0 CHECK(allow_undated_seed IN (0, 1)),
                note TEXT
            )
        """)
        fallback_columns = {row[1] for row in conn.execute("PRAGMA table_info(fallback_providers)")}
        if "allow_undated_seed" not in fallback_columns:
            conn.execute(
                "ALTER TABLE fallback_providers ADD COLUMN allow_undated_seed INTEGER NOT NULL DEFAULT 0"
            )
        # These are deliberately seeds, rather than a migration which replaces
        # rows: an administrator's changes must survive future application
        # starts and deployments.
        conn.executemany(
            """INSERT OR IGNORE INTO fallback_providers
               (domain, enabled, max_age_days, only_when_country_blank_days, allow_undated_seed, note)
               VALUES (?, 1, 90, 90, 1, ?)""",
            [
                ('statista.com', 'Allowed only to fill a recent country-data gap; preserve attribution caveats.'),
                ('ourworldindata.org', 'Allowed only to fill a recent country-data gap; linked WPP series is not independent corroboration.'),
            ],
        )
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_source_rules_match
            ON source_rules(match_type, match_value)
        """)
        # This migration deliberately runs once.  Existing findings are
        # evidence collected before source ranking was introduced; do not infer
        # a new priority from incomplete historical extraction fields.
        conn.execute("""CREATE TABLE IF NOT EXISTS findings_schema_migrations
                        (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)""")
        migrated = conn.execute(
            "SELECT 1 FROM findings_schema_migrations WHERE name = 'source_classification_v1'"
        ).fetchone()
        if not migrated:
            conn.execute("UPDATE webpage_findings SET source_classification = 'legacy_unreviewed'")
            conn.execute(
                "INSERT INTO findings_schema_migrations(name, applied_at) VALUES (?, ?)",
                ('source_classification_v1', datetime.now(timezone.utc).isoformat()),
            )
        fallback_seed_migrated = conn.execute(
            "SELECT 1 FROM findings_schema_migrations WHERE name = 'fallback_undated_seed_v1'"
        ).fetchone()
        if not fallback_seed_migrated:
            conn.execute(
                """UPDATE fallback_providers SET allow_undated_seed = 1
                   WHERE domain IN ('statista.com', 'ourworldindata.org')"""
            )
            conn.execute(
                "INSERT INTO findings_schema_migrations(name, applied_at) VALUES (?, ?)",
                ('fallback_undated_seed_v1', datetime.now(timezone.utc).isoformat()),
            )
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_webpage_findings_report
            ON webpage_findings (effective_date, population_value)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS blocked_sources (
                canonical_url TEXT PRIMARY KEY,
                original_url TEXT NOT NULL,
                blocked_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS finding_legacy_duplicates (
                id INTEGER PRIMARY KEY,
                original_finding_id INTEGER NOT NULL UNIQUE,
                canonical_url TEXT NOT NULL,
                source_url TEXT NOT NULL,
                effective_date TEXT,
                extracted_at TEXT NOT NULL,
                finding_json TEXT NOT NULL,
                archived_at TEXT NOT NULL,
                reason TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS finding_actions (
                id INTEGER PRIMARY KEY,
                finding_id INTEGER,
                canonical_url TEXT NOT NULL,
                action TEXT NOT NULL,
                acted_at TEXT NOT NULL,
                note TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS automatic_rechecks (
                canonical_url TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK(state IN ('requested', 'consumed', 'loaded', 'cancelled')),
                requested_at TEXT NOT NULL,
                requested_finding_id INTEGER,
                consumed_at TEXT,
                consumed_run_id TEXT,
                consumed_candidate_id INTEGER,
                completed_at TEXT,
                completed_candidate_id INTEGER
            )
        """)
        _backfill_canonical_urls(conn)
        _archive_legacy_url_collisions(conn)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_webpage_findings_canonical_url
            ON webpage_findings(canonical_url)
            WHERE canonical_url IS NOT NULL
        """)
        _purge_legacy_partial_period_metrics(conn)


def _backfill_canonical_urls(conn: sqlite3.Connection) -> None:
    """Populate canonical keys for legacy findings before enforcing uniqueness."""
    rows = conn.execute("SELECT id, source_url, canonical_url FROM webpage_findings").fetchall()
    for finding_id, source_url, current in rows:
        try:
            canonical_url = canonicalise_source_url(source_url)
        except ValueError:
            # Preserve malformed legacy evidence for manual repair. The partial
            # unique index below still protects every valid URL.
            continue
        if current != canonical_url:
            conn.execute(
                "UPDATE webpage_findings SET canonical_url = ? WHERE id = ?",
                (canonical_url, finding_id),
            )


def _archive_legacy_url_collisions(conn: sqlite3.Connection) -> None:
    """Keep the newest legacy finding and archive older canonical collisions."""
    collisions = conn.execute("""
        SELECT canonical_url
        FROM webpage_findings
        WHERE canonical_url IS NOT NULL
        GROUP BY canonical_url
        HAVING COUNT(*) > 1
    """).fetchall()
    archived_at = datetime.now(timezone.utc).isoformat()
    for (canonical_url,) in collisions:
        rows = conn.execute("""
            SELECT id, source_url, effective_date, extracted_at, finding_json
            FROM webpage_findings
            WHERE canonical_url = ?
            ORDER BY extracted_at DESC, id DESC
        """, (canonical_url,)).fetchall()
        for finding_id, source_url, effective_date, extracted_at, finding_json in rows[1:]:
            conn.execute("""
                INSERT OR IGNORE INTO finding_legacy_duplicates (
                    original_finding_id, canonical_url, source_url, effective_date,
                    extracted_at, finding_json, archived_at, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                finding_id, canonical_url, source_url, effective_date, extracted_at,
                finding_json, archived_at, "Older record for the same canonical URL.",
            ))
            conn.execute(
                "DELETE FROM webpage_findings WHERE id = ?", (finding_id,)
            )


def _record_finding_action(
    conn: sqlite3.Connection,
    finding_id: int | None,
    canonical_url: str,
    action: str,
    note: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO finding_actions
           (finding_id, canonical_url, action, acted_at, note)
           VALUES (?, ?, ?, ?, ?)""",
        (finding_id, canonical_url, action, datetime.now(timezone.utc).isoformat(), note),
    )


def _request_automatic_recheck(
    conn: sqlite3.Connection, canonical_url: str, finding_id: int | None,
) -> None:
    """Record one explicit override of historical-loaded URL suppression."""
    requested_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO automatic_rechecks
               (canonical_url, state, requested_at, requested_finding_id)
           VALUES (?, 'requested', ?, ?)
           ON CONFLICT(canonical_url) DO UPDATE SET
               state = 'requested', requested_at = excluded.requested_at,
               requested_finding_id = excluded.requested_finding_id,
               consumed_at = NULL, consumed_run_id = NULL,
               consumed_candidate_id = NULL, completed_at = NULL,
               completed_candidate_id = NULL""",
        (canonical_url, requested_at, finding_id),
    )


def pending_automatic_rechecks() -> dict[str, dict[str, Any]]:
    """Return URL rechecks that still override an old successful page load."""
    initialise_findings_table()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM automatic_rechecks
               WHERE state IN ('requested', 'consumed')"""
        ).fetchall()
    return {row['canonical_url']: dict(row) for row in rows}


def list_automatic_rechecks() -> list[dict[str, Any]]:
    """Return the compact audit trail for reviewer visibility."""
    initialise_findings_table()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM automatic_rechecks ORDER BY requested_at DESC, canonical_url"
        ).fetchall()
    return [dict(row) for row in rows]


def list_finding_actions(finding_id: int | None = None) -> list[dict[str, Any]]:
    """Return the durable finding administration audit trail."""
    initialise_findings_table()
    sql = "SELECT * FROM finding_actions"
    params: tuple[Any, ...] = ()
    if finding_id is not None:
        sql += " WHERE finding_id = ?"
        params = (int(finding_id),)
    sql += " ORDER BY acted_at DESC, id DESC"
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def consume_automatic_recheck(canonical_url: str, run_id: str, candidate_id: int) -> None:
    """Mark the one historical-load override as used by an automatic candidate."""
    canonical_url = canonicalise_source_url(canonical_url)
    initialise_findings_table()
    with get_connection() as conn:
        conn.execute(
            """UPDATE automatic_rechecks
               SET state = 'consumed', consumed_at = COALESCE(consumed_at, ?),
                   consumed_run_id = COALESCE(consumed_run_id, ?),
                   consumed_candidate_id = COALESCE(consumed_candidate_id, ?)
               WHERE canonical_url = ? AND state = 'requested'""",
            (datetime.now(timezone.utc).isoformat(), run_id, candidate_id, canonical_url),
        )


def complete_automatic_recheck(canonical_url: str, candidate_id: int) -> None:
    """Close an override once that URL itself has successfully loaded again."""
    canonical_url = canonicalise_source_url(canonical_url)
    initialise_findings_table()
    with get_connection() as conn:
        conn.execute(
            """UPDATE automatic_rechecks
               SET state = 'loaded', completed_at = ?, completed_candidate_id = ?
               WHERE canonical_url = ? AND state IN ('requested', 'consumed')""",
            (datetime.now(timezone.utc).isoformat(), candidate_id, canonical_url),
        )


def blocked_source_urls() -> set[str]:
    """Return canonical URLs intentionally excluded by the reviewer."""
    initialise_findings_table()
    with get_connection() as conn:
        return {row[0] for row in conn.execute("SELECT canonical_url FROM blocked_sources")}


def block_source_url(url: str) -> str:
    """Suppress a URL before it has a stored finding, with an audit entry."""
    canonical_url = canonicalise_source_url(url)
    initialise_findings_table()
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO blocked_sources (canonical_url, original_url, blocked_at) VALUES (?, ?, ?)",
            (canonical_url, str(url), datetime.now(timezone.utc).isoformat()),
        )
        _record_finding_action(conn, None, canonical_url, "source_suppressed")
    return canonical_url


def list_blocked_sources() -> list[dict[str, Any]]:
    """Return suppressed URLs for the small local reviewer control."""
    initialise_findings_table()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT canonical_url, original_url, blocked_at
               FROM blocked_sources ORDER BY blocked_at DESC, canonical_url"""
        ).fetchall()
    return [dict(row) for row in rows]


def list_source_rules() -> list[dict[str, Any]]:
    """Return configured source rules for administration clients."""
    initialise_findings_table()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM source_rules ORDER BY match_type, match_value"
        ).fetchall()
    return [dict(row) for row in rows]


def list_source_rule_actions(rule_id: int | None = None) -> list[dict[str, Any]]:
    """Return the source-rule audit trail, newest changes first."""
    initialise_findings_table()
    sql = "SELECT * FROM source_rule_actions"
    params: tuple[Any, ...] = ()
    if rule_id is not None:
        sql += " WHERE rule_id = ?"
        params = (int(rule_id),)
    sql += " ORDER BY acted_at DESC, id DESC"
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _source_rule_snapshot(conn: sqlite3.Connection, rule_id: int) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM source_rules WHERE id = ?", (int(rule_id),)).fetchone()
    return dict(row) if row is not None else None


def _record_source_rule_action(conn: sqlite3.Connection, rule_id: int | None,
                               action: str, before: dict[str, Any] | None,
                               after: dict[str, Any] | None, note: str | None = None) -> None:
    conn.execute(
        """INSERT INTO source_rule_actions
           (rule_id, action, before_json, after_json, acted_at, note)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (rule_id, action, json.dumps(before, default=str) if before is not None else None,
         json.dumps(after, default=str) if after is not None else None,
         datetime.now(timezone.utc).isoformat(), note),
    )


def list_fallback_providers() -> list[dict[str, Any]]:
    """Return configured fallback providers for administration clients."""
    initialise_findings_table()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM fallback_providers ORDER BY domain"
        ).fetchall()
    return [dict(row) for row in rows]


def update_fallback_provider(domain: str, *, enabled: bool, max_age_days: int,
                             only_when_country_blank_days: int,
                             allow_undated_seed: bool, note: str | None = None) -> None:
    """Validate and update one configured fallback provider."""
    normalized = str(domain or '').strip().lower().removeprefix('www.')
    if not normalized or '/' in normalized:
        raise ValueError('A fallback provider must contain a normalized hostname only.')
    if max_age_days < 0 or only_when_country_blank_days < 0:
        raise ValueError('Provider age limits must be non-negative.')
    initialise_findings_table()
    with get_connection() as conn:
        exists = conn.execute(
            'SELECT 1 FROM fallback_providers WHERE domain = ?', (normalized,)
        ).fetchone()
        if exists is None:
            raise ValueError(f'No fallback provider is configured for {normalized}.')
        conn.execute(
            """UPDATE fallback_providers
               SET enabled = ?, max_age_days = ?, only_when_country_blank_days = ?,
                   allow_undated_seed = ?, note = ?
               WHERE domain = ?""",
            (int(enabled), max_age_days, only_when_country_blank_days,
             int(allow_undated_seed), note, normalized),
        )


def delete_and_block_webpage_finding(finding_id: int) -> str:
    """Delete a finding and prevent the same canonical article from returning."""
    initialise_findings_table()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT source_url, canonical_url FROM webpage_findings WHERE id = ?", (finding_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"No stored finding exists with ID {finding_id}.")
        canonical_url = row[1] or canonicalise_source_url(row[0])
        conn.execute(
            "INSERT OR REPLACE INTO blocked_sources (canonical_url, original_url, blocked_at) VALUES (?, ?, ?)",
            (canonical_url, row[0], datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            """UPDATE automatic_rechecks SET state = 'cancelled'
               WHERE canonical_url = ? AND state IN ('requested', 'consumed')""",
            (canonical_url,),
        )
        conn.execute("DELETE FROM webpage_findings WHERE id = ?", (finding_id,))
        _record_finding_action(conn, finding_id, canonical_url, "removed_and_suppressed")
    return canonical_url


def unblock_source_url(url: str) -> str:
    """Remove a canonical URL from the suppression list and make it eligible."""
    canonical_url = canonicalise_source_url(url)
    initialise_findings_table()
    with get_connection() as conn:
        conn.execute("DELETE FROM blocked_sources WHERE canonical_url = ?", (canonical_url,))
        _request_automatic_recheck(conn, canonical_url, None)
        _record_finding_action(conn, None, canonical_url, "unblocked")
    return canonical_url


def _purge_legacy_partial_period_metrics(conn: sqlite3.Connection) -> None:
    """Remove pre-normalisation monthly/quarterly flow values from old findings.

    Current ingestion annualizes an explicitly monthly or quarterly source and
    retains its original cadence in ``source_time_period``.  Older records
    instead stored their raw partial-period value directly in ``time_period``;
    those values are neither comparable to annual UN data nor valid chart
    points, so remove them once from durable storage.
    """
    monthly_period = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
    flow_keys = {"births", "deaths", "natural_change", "net_overseas_migration"}
    rows = conn.execute("SELECT id, finding_json FROM webpage_findings").fetchall()
    for finding_id, payload in rows:
        try:
            finding = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        statistics = finding.get("statistics")
        if not isinstance(statistics, dict):
            continue
        retained = dict(statistics)
        changed = False
        for key in flow_keys:
            metric = retained.get(key)
            if not isinstance(metric, dict):
                continue
            period = str(metric.get("time_period") or "").strip()
            if monthly_period.fullmatch(period) or period.casefold() in {"daily", "monthly", "quarterly"}:
                retained.pop(key, None)
                changed = True
        if not changed:
            continue
        if any(isinstance(metric, dict) and metric.get("value") is not None for metric in retained.values()):
            finding["statistics"] = retained
            source_url, canonical_url, effective_date, population_value, official_source, quoted_source, quoted_source_url, cleaned_payload, classification = _finding_storage_fields(finding)
            conn.execute(
                """UPDATE webpage_findings SET source_url = ?, canonical_url = ?, effective_date = ?, population_value = ?, official_source = ?,
                   quoted_source = ?, quoted_source_url = ?, finding_json = ?, source_classification = ? WHERE id = ?""",
                (source_url, canonical_url, effective_date, population_value, official_source,
                 quoted_source, quoted_source_url, cleaned_payload, classification, finding_id),
            )
        else:
            # No chartable data remains after removal, so do not keep an empty
            # article record that can be mistaken for a valid estimate.
            conn.execute("DELETE FROM webpage_findings WHERE id = ?", (finding_id,))


SOURCE_CLASSES = {
    'official_publisher', 'secondary_attributed', 'secondary_unattributed',
    'legacy_unreviewed',
}


def source_rule_for_url(url: str) -> dict[str, Any] | None:
    """Resolve enabled exact-URL rules before enabled domain rules."""
    canonical_url = canonicalise_source_url(url)
    domain = urlsplit(canonical_url).hostname or ''
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT * FROM source_rules WHERE enabled = 1 AND match_type = 'canonical_url'
               AND match_value = ?""", (canonical_url,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """SELECT * FROM source_rules WHERE enabled = 1 AND match_type = 'domain'
                   AND match_value = ?""", (domain,),
            ).fetchone()
    return dict(row) if row is not None else None


def add_source_rule(match_type: str, match_value: str, action: str,
                    classification: str | None = None, enabled: bool = True,
                    note: str | None = None) -> int:
    """Add or replace a local source rule without editing application code."""
    if match_type not in {'canonical_url', 'domain'} or action not in {'classify', 'exclude'}:
        raise ValueError('Invalid source rule match type or action.')
    if match_type == 'canonical_url':
        match_value = canonicalise_source_url(match_value)
    else:
        match_value = str(match_value).strip().lower().removeprefix('www.')
        if not match_value or '/' in match_value:
            raise ValueError('A domain rule must contain a normalized hostname only.')
    if action == 'classify' and classification not in SOURCE_CLASSES - {'legacy_unreviewed'}:
        raise ValueError('A classify rule needs a current source classification.')
    if action == 'exclude':
        classification = None
    initialise_findings_table()
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        existing_row = conn.execute(
            "SELECT id FROM source_rules WHERE match_type = ? AND match_value = ?",
            (match_type, match_value),
        ).fetchone()
        before = _source_rule_snapshot(conn, existing_row[0]) if existing_row else None
        conn.execute("""INSERT INTO source_rules
            (match_type, match_value, action, classification, enabled, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_type, match_value) DO UPDATE SET action = excluded.action,
              classification = excluded.classification, enabled = excluded.enabled,
              note = excluded.note, updated_at = excluded.updated_at""",
            (match_type, match_value, action, classification, int(enabled), note, timestamp, timestamp))
        rule_id = conn.execute("SELECT id FROM source_rules WHERE match_type = ? AND match_value = ?",
                               (match_type, match_value)).fetchone()[0]
        after = _source_rule_snapshot(conn, rule_id)
        _record_source_rule_action(conn, rule_id, 'updated' if before else 'created', before, after, note)
        return rule_id


def disable_source_rule(rule_id: int) -> dict[str, Any]:
    """Disable a rule while retaining its row and audit history for undo."""
    initialise_findings_table()
    with get_connection() as conn:
        before = _source_rule_snapshot(conn, rule_id)
        if before is None:
            raise ValueError(f"No source rule exists with ID {rule_id}.")
        if not before['enabled']:
            return before
        conn.execute("UPDATE source_rules SET enabled = 0, updated_at = ? WHERE id = ?",
                     (datetime.now(timezone.utc).isoformat(), int(rule_id)))
        after = _source_rule_snapshot(conn, rule_id)
        _record_source_rule_action(conn, rule_id, 'disabled', before, after)
        return after


def undo_source_rule(rule_id: int) -> dict[str, Any]:
    """Undo the latest source-rule mutation, preserving an audit entry."""
    initialise_findings_table()
    with get_connection() as conn:
        latest = conn.execute(
            "SELECT * FROM source_rule_actions WHERE rule_id = ? ORDER BY id DESC LIMIT 1",
            (int(rule_id),),
        ).fetchone()
        if latest is None:
            raise ValueError(f"No auditable change exists for source rule {rule_id}.")
        before = json.loads(latest[3]) if latest[3] else None
        current = _source_rule_snapshot(conn, rule_id)
        if current is None:
            raise ValueError(f"No source rule exists with ID {rule_id}.")
        if before is None:
            conn.execute("DELETE FROM source_rules WHERE id = ?", (int(rule_id),))
            after = None
        else:
            conn.execute(
                """UPDATE source_rules SET match_type=?, match_value=?, action=?, classification=?,
                   enabled=?, note=?, updated_at=? WHERE id=?""",
                (before['match_type'], before['match_value'], before['action'], before['classification'],
                 before['enabled'], before['note'], datetime.now(timezone.utc).isoformat(), int(rule_id)),
            )
            after = _source_rule_snapshot(conn, rule_id)
        _record_source_rule_action(conn, rule_id, 'undone', current, after,
                                   f"Undid source-rule action {latest[0]}.")
        return after or {'id': int(rule_id), 'status': 'deleted'}


def source_classification(finding: dict[str, Any]) -> str:
    """Classify a new finding, respecting configurable source rules."""
    rule = source_rule_for_url(finding['url'])
    if rule and rule['action'] == 'classify':
        return rule['classification']
    # A persisted/manual classification remains useful when no configured rule
    # applies, but a reviewer rule is always the higher-priority instruction.
    explicit = finding.get('source_classification')
    if explicit in SOURCE_CLASSES:
        return explicit
    if finding.get("official_source"):
        return "official_publisher"
    if str(finding.get("quoted_source") or "").strip():
        return "secondary_attributed"
    return "secondary_unattributed"


def fallback_provider_for_url(url: str) -> dict[str, Any] | None:
    """Return the enabled configured fallback provider matching an article URL."""
    hostname = (urlsplit(canonicalise_source_url(url)).hostname or '').casefold()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM fallback_providers WHERE enabled = 1').fetchall()
    for row in rows:
        provider = dict(row)
        domain = str(provider['domain']).casefold().removeprefix('www.')
        if hostname == domain or hostname.endswith('.' + domain):
            return provider
    return None


def _parse_publication_date(value: Any) -> datetime | None:
    """Parse the provider publication date without guessing an absent date."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _has_recent_article_datapoint(conn: sqlite3.Connection, country_iso3: str, days: int) -> bool:
    """Whether a country has an article finding acquired in the supplied window.

    WPP is queried from its own reference tables, not stored as a webpage
    finding.  The explicit submission-type guard also keeps any future
    imported WPP baseline rows out of this test.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT finding_json FROM webpage_findings
           WHERE extracted_at >= ?
             AND COALESCE(submission_type, '') != 'wpp_baseline'""",
        (cutoff,),
    ).fetchall()
    for (payload,) in rows:
        try:
            prior = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        if prior.get('geography_iso3') != country_iso3:
            continue
        if any(isinstance(metric, dict) and metric.get('value') is not None
               for metric in (prior.get('statistics') or {}).values()):
            return True
    return False


def _fallback_exclusion(finding: dict[str, Any], provenance: dict[str, Any], conn: sqlite3.Connection) -> dict[str, str] | None:
    """Return an auditable fallback rejection for automatic configured providers."""
    if provenance.get('submission_type') != 'automatic':
        return None
    provider = fallback_provider_for_url(finding['url'])
    if provider is None:
        return None
    country_iso3 = finding.get('geography_iso3') or ''
    country_label = finding.get('geography') or country_iso3
    if country_iso3 and _has_recent_article_datapoint(
        conn, country_iso3, int(provider['only_when_country_blank_days'])
    ):
        return {
            'reason': (f"{country_label} already has an article-derived datapoint acquired in the preceding "
                       f"{provider['only_when_country_blank_days']} days."),
        }
    published = _parse_publication_date(provenance.get('published_date'))
    if published is None:
        parsed = urlsplit(str(finding.get("url") or ""))
        is_undated_profile = (
            str(provider["domain"]).casefold() == "ourworldindata.org"
            and parsed.path.casefold().startswith("/profile/")
        )
        if not provider.get('allow_undated_seed') or not is_undated_profile:
            return {
                'reason': (
                    f"Fallback provider {provider['domain']} requires a parseable publication date "
                    "unless it is an enabled undated country profile."
                ),
            }
        # Evergreen country profiles can be a useful first seed when this
        # country has no recent article data. They remain secondary evidence,
        # not a substitute for a dated release or an official source.
        return None
    age = datetime.now(timezone.utc) - published
    if age > timedelta(days=int(provider['max_age_days'])):
        return {
            'reason': (f"Fallback provider {provider['domain']} article is older than "
                       f"{provider['max_age_days']} days."),
        }
    return None


def store_webpage_finding(finding: dict[str, Any], provenance: dict | None = None) -> dict[str, str | int]:
    """Store an extracted finding unless its canonical URL already exists."""
    provenance = provenance or {"submission_type": "manual", "discovery_source": "manual"}
    initialise_findings_table()
    model_iso3 = str(finding.get("geography_iso3") or "").strip().upper()
    geography = finding.get("geography") or ""
    try:
        resolved_iso3 = resolve_country_iso3(model_iso3 or geography)
    except sqlite3.OperationalError:
        # Small isolated storage databases used by legacy callers may not
        # contain the WPP reference. Preserve their existing label behaviour.
        resolved_iso3 = None
    canonical_country = normalise_country_name(model_iso3 or geography)
    if resolved_iso3:
        canonical_country = normalise_country_name(resolved_iso3) or canonical_country
    if canonical_country:
        finding = {**finding, "geography": canonical_country}
    if resolved_iso3:
        finding = {**finding, "geography_iso3": resolved_iso3.upper()}
    source_url = finding["url"]
    population = (finding.get("statistics") or {}).get("population") or {}
    effective_date = finding.get("effective_date")
    population_value = population.get("value")

    with get_connection() as conn:
        canonical_url = canonicalise_source_url(source_url)
        rule = source_rule_for_url(source_url)
        if rule and rule['action'] == 'exclude':
            return {"status": "excluded_source_rule", "canonical_url": canonical_url,
                    "reason": rule.get('note') or 'Excluded by configured source rule.'}
        if conn.execute("SELECT 1 FROM blocked_sources WHERE canonical_url = ?", (canonical_url,)).fetchone():
            return {"status": "excluded_blocked_source", "canonical_url": canonical_url}
        fallback_exclusion = _fallback_exclusion(finding, provenance, conn)
        if fallback_exclusion:
            return {
                'status': 'excluded_fallback_not_needed',
                'canonical_url': canonical_url,
                **fallback_exclusion,
            }
        duplicate_url = conn.execute(
            "SELECT id FROM webpage_findings WHERE canonical_url = ?", (canonical_url,)
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

        classification = source_classification(finding)
        finding = {**finding, 'source_classification': classification}
        if classification == 'official_publisher':
            finding['official_source'] = True
        cursor = conn.execute(
            """INSERT INTO webpage_findings (
                   source_url, canonical_url, effective_date, population_value, official_source,
                   quoted_source, quoted_source_url, extracted_at, finding_json,
                   submission_type, discovery_source, search_run_id, search_candidate_id,
                   source_classification
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_url,
                canonical_url,
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
                classification,
            ),
        )
        return {"status": "stored", "id": cursor.lastrowid,
                "source_classification": classification}


def list_webpage_findings() -> list[dict[str, Any]]:
    """Return stored findings with the newest numeric ID first for review."""
    normalise_stored_finding_geographies()
    initialise_findings_table()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, source_url, canonical_url, effective_date, population_value,
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
            "ISO3": finding.get("geography_iso3") or "",
            "Effective date": stored["effective_date"] or "",
            "Population": stored["population_value"],
            "TFR": ((finding.get("statistics") or {}).get("total_fertility_rate") or {}).get("value"),
            "Official source": "Yes" if stored["official_source"] else "No",
            "Source classification": stored["source_classification"],
            "Source": finding.get("source") or "",
            "Quoted source": stored["quoted_source"] or "",
            "Quoted source URL": stored["quoted_source_url"] or "",
            "Webpage URL": stored["source_url"],
            "Canonical URL": stored["canonical_url"],
            "Extracted at (UTC)": stored["extracted_at"],
            "Extracted JSON": json.dumps(finding, ensure_ascii=False, sort_keys=True),
        })
    return sorted(findings, key=lambda finding: finding["ID"], reverse=True)


def get_webpage_finding(finding_id: int) -> dict[str, Any]:
    """Return one stored finding for the database editor."""
    normalise_stored_finding_geographies()
    initialise_findings_table()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT finding_json FROM webpage_findings WHERE id = ?", (finding_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"No stored finding exists with ID {finding_id}.")
    return json.loads(row[0])


def find_webpage_finding_by_url(url: str) -> dict[str, Any] | None:
    """Return the active finding for a canonical URL, if one exists."""
    normalise_stored_finding_geographies()
    canonical_url = canonicalise_source_url(url)
    initialise_findings_table()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT id, source_url, canonical_url, finding_json
               FROM webpage_findings WHERE canonical_url = ?""",
            (canonical_url,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "source_url": row["source_url"],
        "canonical_url": row["canonical_url"],
        "finding": json.loads(row["finding_json"]),
    }


def _finding_storage_fields(finding: dict[str, Any]) -> tuple:
    if not isinstance(finding, dict) or not finding.get("url"):
        raise ValueError("The finding JSON must be an object with a non-empty url.")
    model_iso3 = str(finding.get("geography_iso3") or "").strip().upper()
    geography = finding.get("geography") or ""
    try:
        resolved_iso3 = resolve_country_iso3(model_iso3 or geography)
    except sqlite3.OperationalError:
        resolved_iso3 = None
    canonical_country = normalise_country_name(model_iso3 or geography)
    if resolved_iso3:
        canonical_country = normalise_country_name(resolved_iso3) or canonical_country
    if canonical_country:
        finding["geography"] = canonical_country
    if resolved_iso3:
        finding["geography_iso3"] = resolved_iso3.upper()
    population = ((finding.get("statistics") or {}).get("population") or {})
    classification = source_classification(finding)
    if classification == 'official_publisher':
        finding['official_source'] = True
    return (
        finding["url"],
        canonicalise_source_url(finding["url"]),
        finding.get("effective_date"),
        population.get("value"),
        int(bool(finding.get("official_source"))),
        finding.get("quoted_source"),
        finding.get("quoted_source_url"),
        json.dumps(finding, sort_keys=True),
        classification,
    )


def update_webpage_finding(finding_id: int, finding_json: str) -> None:
    """Validate and save a manual edit to one stored extraction."""
    try:
        finding = json.loads(finding_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"The finding JSON is invalid: {exc.msg}.") from exc
    source_url, canonical_url, effective_date, population_value, official_source, quoted_source, quoted_source_url, payload, classification = _finding_storage_fields(finding)
    initialise_findings_table()
    with get_connection() as conn:
        exists = conn.execute("SELECT 1 FROM webpage_findings WHERE id = ?", (finding_id,)).fetchone()
        if not exists:
            raise ValueError(f"No stored finding exists with ID {finding_id}.")
        duplicate_url = conn.execute(
            "SELECT id FROM webpage_findings WHERE canonical_url = ? AND id != ?", (canonical_url, finding_id)
        ).fetchone()
        if duplicate_url:
            raise ValueError(f"This canonical webpage URL is already stored as ID {duplicate_url[0]}.")
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
                   source_url = ?, canonical_url = ?, effective_date = ?, population_value = ?,
                   official_source = ?, quoted_source = ?, quoted_source_url = ?,
                   finding_json = ?, source_classification = ?
               WHERE id = ?""",
            (source_url, canonical_url, effective_date, population_value, official_source,
             quoted_source, quoted_source_url, payload, classification, finding_id),
        )


def delete_webpage_finding(finding_id: int) -> None:
    """Delete one explicitly selected stored finding."""
    initialise_findings_table()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT source_url, canonical_url FROM webpage_findings WHERE id = ?", (finding_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"No stored finding exists with ID {finding_id}.")
        canonical_url = row[1] or canonicalise_source_url(row[0])
        conn.execute("DELETE FROM webpage_findings WHERE id = ?", (finding_id,))
        _request_automatic_recheck(conn, canonical_url, finding_id)
        _record_finding_action(conn, finding_id, canonical_url, "removed_allow_rerun")


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
    if not any(isinstance(value, dict) and value.get("value") is not None
               for value in statistics.values()):
        raise ValueError(
            f"Finding #{finding_id} has no other datapoints. Delete the full record instead."
        )
    finding['statistics'] = statistics
    update_webpage_finding(finding_id, json.dumps(finding, ensure_ascii=False))
    initialise_findings_table()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT canonical_url, source_url FROM webpage_findings WHERE id = ?", (finding_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"No stored finding exists with ID {finding_id}.")
        _record_finding_action(
            conn, finding_id, row[0] or canonicalise_source_url(row[1]), "metric_removed", metric
        )


def normalise_stored_finding_geographies() -> int:
    """Backfill ISO3 identity and canonical display labels on stored findings."""
    global _COUNTRY_MIGRATION_IN_PROGRESS
    if _COUNTRY_MIGRATION_IN_PROGRESS:
        return 0
    _COUNTRY_MIGRATION_IN_PROGRESS = True
    initialise_findings_table()
    updates = 0
    try:
        with get_connection() as conn:
            rows = conn.execute("SELECT id, finding_json FROM webpage_findings").fetchall()
            for finding_id, payload in rows:
                finding = json.loads(payload)
                source = finding.get("geography_iso3") or finding.get("geography") or ""
                try:
                    resolved_iso3 = resolve_country_iso3(source)
                except sqlite3.OperationalError:
                    resolved_iso3 = None
                canonical_country = normalise_country_name(resolved_iso3 or source)
                changed = False
                if canonical_country and canonical_country != finding.get("geography"):
                    finding["geography"] = canonical_country
                    changed = True
                if resolved_iso3 and resolved_iso3.upper() != finding.get("geography_iso3"):
                    finding["geography_iso3"] = resolved_iso3.upper()
                    changed = True
                if changed:
                    conn.execute(
                        "UPDATE webpage_findings SET finding_json = ? WHERE id = ?",
                        (json.dumps(finding, sort_keys=True), finding_id),
                    )
                    updates += 1
    finally:
        _COUNTRY_MIGRATION_IN_PROGRESS = False
    return updates


def run_query(sql: str, params: tuple = ()) -> list[dict]:
    """Run an operational-database query and return rows as dictionaries."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def run_wpp_query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a parameterized read against the generated WPP serving database."""
    with get_wpp_connection() as conn:
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
    rows = run_wpp_query(sql, (canonical,))
    return rows[0]["iso3"] if len(rows) == 1 else None


@lru_cache(maxsize=1)
def _country_reference() -> tuple[tuple[str, str], ...]:
    """Cache the small, static UN country/name-to-ISO reference in-process."""
    rows = run_wpp_query('''
        SELECT DISTINCT Country, "ISO3 Alpha-code" AS ISO3
        FROM medium_variant
        ORDER BY Country
    ''')
    return tuple((str(row["Country"]), str(row["ISO3"] or "")) for row in rows)


def normalise_country_name(country_name: str | None) -> str | None:
    """Return the canonical database country name from a name or ISO3 code."""
    candidate = str(country_name or '').strip().casefold()
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
    if len(matches) == 1:
        return matches[0]
    # Let the WPP country reference resolve harmless presentation differences
    # (for example ``Vietnam`` vs ``Viet Nam``) rather than growing a list of
    # one-off spelling aliases. A match must remain unique to be accepted.
    def reference_key(value: str) -> str:
        decomposed = unicodedata.normalize("NFKD", value.casefold())
        return "".join(character for character in decomposed
                       if character.isalnum() and not unicodedata.combining(character))

    candidate_key = reference_key(candidate)
    normalized_matches = [name for name, _iso3 in rows if reference_key(name) == candidate_key]
    return normalized_matches[0] if len(normalized_matches) == 1 else None


def list_country_names() -> list[str]:
    """List canonical country names for user-interface selectors."""
    return [row["Country"] for row in run_wpp_query(
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
    return run_wpp_query(sql, (country_iso3, *years))


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
    return run_wpp_query(sql)


tools = [get_population_forecast, get_list_of_countries]
WEB_TOOLS = [get_page_text, get_pdf_text]
SQL_TOOLS = tools
