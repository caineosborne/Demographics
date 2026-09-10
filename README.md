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

## Automatic research

Open **Automatic research**, edit the category table, then click **Search and
analyse**. The defaults request up to 30 Tavily results across population,
births/deaths and migration, plus external links from the newest 30 r/Natalism
submissions. These are retrieval limits, not guaranteed numbers of relevant
articles. Add/delete category rows and change query, topic, count, time range,
search depth and domain filters. The relevance criteria are editable too.
**Save search settings** persists changes without running a search; running also
saves them. Manual URLs still use **Analyse webpage**.

Configure `TAVILY_API_KEY` and `OPENROUTER_API_KEY` in `.env`. The notebook now
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
irrelevant, or unclear; relevant and unclear both trigger full-text fetching.
Full-text uncertainty is retained as `needs_review`, without extracting or
comparing unsupported facts. You can inspect it and manually submit the URL for
analysis. Downloaded text is reused for extraction rather than fetched again.
The boss controls routing and budgets in code; model decisions are limited to
relevance, extraction and comparison. No scheduling or unbounded tool loop is
introduced.

**Search results** exposes run settings, provider counts/errors and all candidate
outcomes. Select a run, then inspect a candidate ID for the original provider
payload, full text, both decisions/reasons, extraction, storage result and UN
comparison. Empty and failed searches are visible in the run history too.
Tracking parameters/fragments are removed for deduplication. Repeated URLs are
recorded with links to their original candidate or stored finding. The existing
finding/report deduplication rules remain in effect.

SQLite tables in the existing demographics database:

- `research_settings`: saved controls.
- `search_runs`: immutable settings snapshot, timestamps, status and provider events.
- `search_candidates`: every discovery, review, fetched text, error and comparison.
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

Runs execute sequentially with one automatic run at a time per app process.
Errors are isolated per provider/article. Closing a running generator records
`interrupted`; a hard process crash may leave a run marked `running`. There is no
automatic resume. Known stored URLs are skipped on later runs; use manual URL
analysis to re-run a comparison. Search history shows the latest 100 runs and
1,000 candidates per view; the complete history remains in SQLite.
