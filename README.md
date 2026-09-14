# Demographics Research Agent

Install dependencies and the browser used for fallback fetching:

```sh
uv sync
uv run playwright install chromium
uv run python main.py
```

Pages are fetched with Requests first. Request errors, empty pages, and common
access challenges trigger a headless Playwright/Chromium retry. Both paths extract
text from the retrieved HTML for the existing summarisation and UN comparison.
PDF URLs are read directly with Requests and `pypdf`, so their extracted document
text follows the same summarisation and UN comparison flow.
If both fail, the analysis stops and displays the error. The Debug status field
shows the current fetch method while the analysis runs.

Run the regression checks with `uv run python -m unittest discover -s tests`.

## FastAPI application

Start the API and non-Gradio admin frontend locally with:

```sh
uv run uvicorn api:app --reload
```

Open <http://127.0.0.1:8000/admin/> for the research administration workspace.
Use <http://127.0.0.1:8000/admin/?fixtures=1> for a non-mutating fixture tour,
or <http://127.0.0.1:8000/docs> for the API documentation. The application is
local-only at this stage and has no user-admin or login flow.

The admin frontend supports manual URL analysis and approval, editable Tavily
discovery settings, direct and bounded bulk country hunts, durable run and
candidate history, finding/source administration, coverage, and WPP graphs.
These workflows call the FastAPI JSON boundary and do not require Gradio.

The original API desk remains available at <http://127.0.0.1:8000/>.

The initial boundary exposes `GET /health`. Runtime settings can be adjusted
with `DEMOGRAPHICS_APP_NAME`, `DEMOGRAPHICS_API_VERSION`, and
`DEMOGRAPHICS_ENVIRONMENT`.

Durable worker commands create job state in the writable application database
before processing. Run them directly or hand an existing job ID to a worker:

```sh
uv run python worker.py manual-analysis https://example.test/release --country-iso3 JPN
uv run python worker.py news-search settings.json
uv run python worker.py country-search JPN
uv run python worker.py maintenance
uv run python worker.py export export.json
uv run python worker.py run JOB_ID
uv run python worker.py recover
```

`recover` marks work interrupted by a terminated local API/worker process as
retryable and releases its discovery lock. Retrying the same job ID records a
new attempt; completed jobs remain idempotent.

The versioned JSON API includes the read, research, analysis, administration,
and `GET /api/v1/worker/jobs/{job_id}` contracts. Representative frontend
fixtures are under `fixtures/api/`; country-specific routes require ISO3.

## Database storage and Phase 1.1 rollback

The writable research SQLite database is stored under `databases/`; WPP
comparisons use the generated read-only `databases/wpp_serving.sqlite` file.
`Data_Files/` is an offline source/archive location and is excluded from the
production path. Override these locations explicitly with
`DEMOGRAPHICS_DB_PATH` and `DEMOGRAPHICS_WPP_DB_PATH`; neither has a fallback
to `Data_Files`.

Before changing any table or record, create a dated read-only backup and
inventory with:

```sh
.venv/bin/python database_maintenance.py
```

The pre-migration inventory is recorded at
`databases/inventory/2026-09-12-pre-migration.json`. It records 100 accepted
findings, 528 candidates, 51,712,000 bytes, and SHA-256
`d2d9064656cdb1c19b8c755ecec79964b4885e6aeb7c04af9e547b73c07d19e6`.

See [METHODOLOGY_BUSINESS_RULES.md](METHODOLOGY_BUSINESS_RULES.md) for the
manual-versus-bulk research rules, URL identity rules, duplicate handling,
suppression, unblocking, and rerun behavior.

## Automatic research

Open **Automatic research**, edit the category table, then click **Search and
analyse**. The defaults request up to 30 Tavily results across population,
births/deaths and migration, plus external links from the newest 30 r/Natalism
submissions. These are retrieval limits, not guaranteed numbers of relevant
articles. Add/delete category rows and change query, topic, count, search depth
and domain filters. The search-window dropdown applies one window
to every Tavily category and defaults to the previous 24 hours. The relevance
criteria are editable too.
The **Maximum unique articles to process** setting bounds expensive page fetches
and model calls. Every discovered link still appears in Search results; links
beyond the run limit are marked `deferred_budget` and can be reviewed later.
**Save search settings** persists changes without running a search; running also
saves them. Manual URLs still use **Analyse webpage**.

### Finding data gaps

The **Find missing data** panel in Automatic research has two on-demand modes.
**Hunt for country data** searches one selected canonical country across the
previous year, using release-oriented population, vital-statistics and migration
terms. **Hunt all listed gaps** first selects countries whose names start with a
prefix (for example `A` or `Viet`) and that have not had a finding *added to the
local database* in the selected number of days (31 by default), then retrieves
up to five one-year **news** results per selected country. The bulk controls
also let you choose the batch size and one-based starting position in the
eligible list—for example, count `5`, start `1` searches #1–5; start `6`
searches #6–10. This freshness test is based on the
time the finding was acquired, not the historical date of the statistic, so a
newly found older release is not hunted again immediately. Preview the matching
country list before running the bulk hunt. These modes use an isolated settings
snapshot and never alter the saved automatic-discovery controls.

A country hunt describes how a link was found; it does not force the extracted
finding to be for that country. If a Thailand hunt naturally returns a valid
China article, it is retained as China evidence unless the normal processing
limit has already been reached. The results table shows the extracted country
and article summary; selecting a row opens its compact audit record. An
undated enabled OWID/Statista country profile can seed a country with no recent
article datapoint, but it remains secondary evidence and is excluded once that
country has recent data.

Configure `TAVILY_API_KEY` and `OPENROUTER_API_KEY` in `.env`. The default
extraction model is `deepseek/deepseek-v4-flash-0731`: low reasoning performs
the first article extraction. An empty low pass ends the workflow; medium
reasoning retries only when low found numeric data but its validation is partial
or unclear. Set `LLM_MODEL`,
`LLM_MEDIUM_MODEL`, `LLM_LOW_REASONING_EFFORT`, or
`LLM_MEDIUM_REASONING_EFFORT` to override those stages without editing the
application. UN comparison is deterministic from the local WPP data and does
not call a model. The notebook now
reads the same Tavily environment variable. Search parameters follow the
[Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search):
1–20 results per category in this UI; advanced depth costs more than basic.
Reddit uses its newest-submission JSON listing independently of Tavily, with a
public Atom/RSS feed fallback when JSON is blocked. The transport and fallback
reason are saved with candidates. It inspects
external submission URLs and links in text posts, preserving the Reddit
permalink. It checks only the configured newest posts (maximum 100), not the
whole subreddit. Blocked/rate-limited Reddit requests are recorded as provider
errors while Tavily processing continues.

The bounded boss coordinator in `research.py` delegates to reusable research
skills: extract links → review summary → fetch and review full text → extract
facts → existing UN comparison agent. Summary decisions are relevant,
irrelevant, or unclear. `unclear` is reserved for a source that plausibly has
national demographic figures but cannot be decided from its snippet; clearly
off-topic results stop before download. Eligible search summaries are reviewed
in batches of up to 20 and returned with their individual candidate IDs,
reducing model round trips while retaining one decision and reason per article.
Eligible Tavily links are extracted in batches before direct retrieval, then
fall back to Requests/Playwright where Tavily cannot provide usable text.
Publisher bot blocks are retained as `relevant_access_blocked` or
`unclear_access_blocked`, distinct from a processing error. Full-text review
remains one article at a time.
When an article cannot be accessed, the pipeline performs one bounded Tavily
recovery search using its title and tries up to three distinct replacement URLs.
The original access error and each attempted replacement are retained on the
candidate audit record; if none is accessible, the original access-blocked
outcome remains.
Full-text uncertainty is retained as `needs_review`, without extracting or
comparing unsupported facts. You can inspect it and manually submit the URL for
analysis. Downloaded text is reused for extraction rather than fetched again.
Long articles are held only while the run needs them; the durable audit keeps
the outcome, reasons, timings, loaded marker and structured extraction rather
than an article copy. A bounded, evidence-focused extract is sent to the model
for full-text decision and extraction. The UN lookup is
deterministic from the normalised country and effective date. It retrieves the
reported and following year, then identifies the closest available 1 January or
1 July population observation before the comparison call.
The boss controls routing and budgets in code; model decisions are limited to
relevance, extraction and comparison. No scheduling or unbounded tool loop is
introduced.

**Search results** exposes run settings, provider counts/errors and all candidate
outcomes. Select a run, then select a candidate row (or enter its ID) to inspect
the provider snippet, both decisions/reasons, extraction, storage result and
fallback decision. Empty and failed searches are visible in the run history too.
Tracking parameters/fragments are removed for deduplication. Repeated URLs are
shown as `🔁 DUPLICATE` with an explanation and the ID of their original
candidate or stored finding. The existing finding/report deduplication rules
remain in effect.

SQLite tables in the existing demographics database:

- `research_settings`: saved controls.
- `search_runs`: immutable settings snapshot, timestamps, status and provider events.
- `search_candidates`: every discovery, review, compact outcome, loaded marker,
  error and structured extraction; it is not a durable article-text archive.
- `webpage_findings`: new `submission_type`, `discovery_source`, `search_run_id`
  and `search_candidate_id` columns link automatic extractions to their audit.
  Existing rows are `legacy_unknown`; new submissions are `manual` or `automatic`.
  Finding provenance describes its first stored submission; later duplicate
  discoveries remain in `search_candidates` and do not relabel that finding.

To extend discovery, register a provider callable in `BossAgent(providers=...)`.
Additional providers receive `SearchSettings` and return a list of dictionaries
with `url`, `title`, `snippet`, `source`, and `category` (plus any provenance).
The built-in Tavily provider receives a category; Reddit receives a post limit.
Pass a `ResearchSkills` instance to replace review, fetch, extraction or comparison
without changing routing or persistence. These are application-level Python
skills, independent of Codex's editor skills.

Runs execute in a background worker, sequentially with one automatic run at a
time per app process. Browser navigation, refreshes and temporary disconnects do
not stop that worker. The Automatic research table polls SQLite every two
seconds; Search results polls every five seconds and can reconnect to a running
job after a page reload. **Stop current run** signals the worker between stages
and records `interrupted`; in-flight model calls use the `LLM_TIMEOUT_SECONDS`
setting (120 seconds by default) so a provider cannot hang the run indefinitely.
On application startup, runs left as `running` or `stopping` by a previous
process are recorded as interrupted with a recovery event. There is no automatic
resume after the Python process itself stops. Known stored URLs are skipped on
later runs; use manual URL analysis to re-run a comparison. Search history shows
the latest 100 runs and 1,000 candidates per view; the complete history remains
in SQLite.

## Fertility and country labels

The extraction, comparison and charts support `total_fertility_rate`, measured
in live births per woman. It is stored as a separate statistic, compared with
the UN WPP `Total Fertility Rate (live births per woman)` field, and plotted on
its own scale rather than as people. The chart country normaliser maps `Taiwan`
to the WPP label `China, Taiwan Province of China`, so stored Taiwan articles
now appear with the matching UN series.

Stored findings also have a source-status label derived without changing the
extraction JSON: **Official publisher**, **Secondary, official source named**,
or **Secondary, source not named**. The database displays this status; charts
use diamonds for official publisher pages, circles for attributed secondary
reporting, and crosses for unattributed secondary reporting, with the detail
available in each point's hover text.
