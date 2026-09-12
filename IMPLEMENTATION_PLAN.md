# Demographics Agent Implementation Plan

## Executive checklist

- [ ] **Phase 1 — Stabilize and improve the current research pipeline**
  - [x] **Step 1.1 — Preserve and inventory the current database**
  - [ ] **Step 1.2 — Fix canonical duplicates and deletion behaviour**
  - [ ] **Step 1.3 — Include and rank every source type**
  - [ ] **Step 1.4 — Add fallback providers and manual-only comparison**
  - [ ] **Step 1.5 — Reduce the candidate audit**
  - [ ] **Step 1.6 — Prepare the full and filtered WPP databases**
  - [ ] **Step 1.7 - ensure that the delete datapoint/dete record is working**
- [ ] **Phase 2 — Build the local FastAPI application layer**
  - [ ] **Step 2.1 — Build the new data model and separate workflows from Gradio**
  - [ ] **Step 2.2 — Add FastAPI endpoints and Jinja page shells**
  - [ ] **Step 2.3 — Add durable worker commands**
  - [ ] **Step 2.4 — Define the frontend and export JSON contracts**
  - [ ] **Step 2.5 — Prove local behavioural parity**
- [ ] **Phase 3 — Replace the local Gradio frontend**
  - [ ] **Step 3.1 — Build the admin application shell**
  - [ ] **Step 3.2 — Rebuild manual webpage analysis**
  - [ ] **Step 3.3 — Rebuild research and country-hunt controls**
  - [ ] **Step 3.4 — Rebuild findings, review, and conflict management**
  - [ ] **Step 3.5 — Rebuild the graphs and reporting**
- [ ] **Phase 4 — Deploy the private admin application to Railway**
  - [ ] **Step 4.1 — Provision and migrate the Postgres application database**
  - [ ] **Step 4.2 — Package the filtered WPP database**
  - [ ] **Step 4.3 — Deploy the API, admin frontend, and worker**
  - [ ] **Step 4.4 — Add authentication, backups, and operational safeguards**
  - [ ] **Step 4.5 — Verify all manual workflows online**
- [ ] **Phase 5 — Automate discovery and country coverage**
  - [ ] **Step 5.1 — Automate the daily news search**
  - [ ] **Step 5.2 — Run the initial 20-country-per-day coverage sweep**
  - [ ] **Step 5.3 — Introduce the steady-state country schedule**
  - [ ] **Step 5.4 — Apply the tested fallback-provider rule to scheduled hunts**
  - [ ] **Step 5.5 — Add spend, run, and failure dashboards**
- [ ] **Phase 6 — Publish the database-free public site**
  - [ ] **Step 6.1 — Build the static JSON exporter**
  - [ ] **Step 6.2 — Build the read-only public frontend**
  - [ ] **Step 6.3 — Automate the weekly Vercel release**
  - [ ] **Step 6.4 — Verify publication, attribution, and rollback**
- [ ] **Phase 7 — Add public URL submissions**
  - [ ] **Step 7.1 — Build the submission form**
  - [ ] **Step 7.2 — Build the admin moderation queue**
  - [ ] **Step 7.3 — Add submission security and abuse controls**

---

## Target architecture

```text
                                     +------------------------+
                                     | Full WPP archive       |
                                     | Offline, unchanged     |
                                     +-----------+------------+
                                                 |
                                      reproducible filter/build
                                                 |
                                                 v
+------------------+                 +------------------------+
| Admin browser    |<--------------->| FastAPI on Railway     |
+------------------+                 | + filtered WPP SQLite  |
                                     +----------+-------------+
                                                |
                                                v
                                     +------------------------+
                                     | Postgres               |
                                     | application data       |
                                     +-----+--------------+---+
                                           |              |
                                    scheduled workers     | pending URLs
                                           |              |
                                           v              v
                                     +-----------+   +-----------+
                                     | Research  |   | Submission|
                                     | jobs      |   | queue     |
                                     +-----+-----+   +-----------+
                                           |
                                      weekly export
                                           |
                                           v
                                     +------------------------+
                                     | Static JSON + frontend |
                                     | Public on Vercel       |
                                     +------------------------+
```

### Storage boundaries

| Store | Location | Contents | Mutable? |
|---|---|---|---|
| Full WPP archive | Offline | Original WPP columns, releases, and provenance | No |
| Filtered WPP serving database | Railway application image | Only the WPP fields required by comparisons and graphs | No |
| Application database | Railway Postgres | Findings, metric claims, sources, conflicts, jobs, submissions, settings, and compact audit records | Yes |
| Public release | Vercel | Versioned JSON and static frontend assets | Replaced only by a successful release |

The filtered WPP database is generated from the complete offline archive. Adding another WPP indicator later means changing the filter/build specification and producing a new serving database; it does not require reacquiring the original data. The complete WPP tables can also be imported into the core application later if that becomes useful.

The current `estimates` and `medium_variant` tables each contain 65 columns. The serving copy initially retains:

- Country
- ISO3 code
- Year
- Population on 1 January
- Population on 1 July
- Total births
- Total deaths
- Natural change
- Net migration
- Total fertility rate

Historic WPP release overlays retain the same chart metrics plus revision and cadence metadata. Source URLs remain in the offline archive and should also be carried into generated attribution metadata.

## Phase 1 — Stabilize and improve the current research pipeline

### Step 1.1 — Preserve and inventory the current database

- Create a dated, read-only backup of the current SQLite file before changing any table or record.
- Record table counts, database size, accepted finding count, candidate count, and checksums.
- Treat the backup as the migration rollback point.
- Ensure tests use temporary databases and cannot add test runs or candidates to the real database.

**Complete when:** the existing application can still run from the untouched backup and the pre-migration counts are documented.

**Completed 2026-09-12:** the runtime database now lives under `databases/`,
with a read-only rollback copy and inventory in
`databases/backups/`. The pre-migration inventory is also committed at
`databases/inventory/2026-09-12-pre-migration.json`. `Data_Files/` is retained
only for offline source material and is not a production database location.

### Step 1.2 — Fix canonical duplicates and deletion behaviour

- Normalize HTTP/HTTPS, `www`, trailing slashes, fragments, and known tracking parameters.
- Store a canonical URL alongside the original URL and enforce canonical URL uniqueness for stored findings.
- Continue to record duplicate discoveries in the research audit, but do not fetch or extract them again.
- Keep the two explicit actions:
  - **Remove and allow rerun:** delete the accepted finding while leaving its canonical URL eligible for a future search.
  - **Remove and suppress:** delete the accepted finding and add its canonical URL to the block list.
- Allow an administrator to remove a URL from the block list later.

**Complete when:** the current Gradio tool prevents URL variants from creating duplicate findings, while preserving its existing workflows and data display.

### Step 1.3 — Include and rank every source type

- Remove the automatic `excluded_unattributed_source` outcome from the current research pipeline.
- Store unattributed secondary sources as low-priority findings rather than discarding them.
- Retain the three source classes: official publisher, named secondary, and unnamed secondary.
- Use source class to control marker shape and the default preferred value if sources disagree; it is not an acceptance gate.
- Draw unnamed secondary sources as crosses in the current graph, including when no formal source is named.
- Continue to store the extracted source-related fields even when they are empty.
- Preserve all existing findings exactly as they are; do not re-fetch or discard them.
- Mark existing records as `legacy_unreviewed` where the old data cannot support a reliable source class.
- Draw legacy records as neutral standard stars, rather than implying that they are official, named secondary, or unnamed secondary.
- Allow later manual classification, editing, or replacement of an individual legacy record.

The initial priority order is official publisher, then named secondary, then unnamed secondary. A legacy record has no automatic priority until reviewed.

Legacy findings will be brought into the richer source/claim model only when the FastAPI application is built. Until then, they remain visible in the current tool as neutral historic datapoints.

**Complete when:** the current Gradio tool stores all three source types and existing charts retain historic points without making unsupported claims about source quality.

### Step 1.4 — Add fallback providers and manual-only comparison

- Make the LLM comparison agent an explicit manual-analysis feature only.
- Manual URLs entered by an administrator, including an approved public submission, run the full current comparison flow.
- Automatic news, country, and bulk scans extract and store eligible datapoints but do not run the comparison agent and are never excluded as outliers by it.
- The existing WPP series remains visible as the graph reference line; a bulk source need not have a per-article comparison record to be plotted.
- Add a configured fallback-provider list, initially including Statista and Our World in Data.
- During a country hunt, allow a configured fallback provider only if that country currently has no stored article-derived datapoint and the page was published in the previous three months.
- If the country already has a datapoint, do not use fallback providers through the country-hunt route.
- Classify fallback results by the same three source classes; they do not receive special official status.
- Store the fallback-provider list and source-rules in SQLite initially so the behaviour is testable before the frontend is replaced. Phase 3 adds the administration screen for editing them.

**Complete when:** country hunts can populate an empty country with a recent Statista/OWID result, automatic jobs no longer use the comparison agent, and manual URL analysis still does.

### Step 1.5 — Reduce the candidate audit

Retain compact operational and decision data:

- Run and candidate IDs, original/canonical URL, title/snippet, provider/category, timestamps, status/reasons, source relationships, duplicate relationship, concise errors, and usage measurements.

Remove large candidate detail after processing:

- Full webpage text, raw provider payloads, embedded/binary material, and repeated alternative-page contents.

Keep accepted structured extraction with the stored finding. This is a storage change only; it must not remove any accepted datapoint or change current search decisions.

**Complete when:** the current audit remains useful for outcomes and debugging without accumulating full article text.

### Step 1.6 — Prepare the full and filtered WPP databases

- Retain the complete WPP database offline as the authoritative source archive.
- Create a repeatable build command that generates a small `wpp_serving.sqlite` from that archive.
- Keep `estimates` and `medium_variant` as separate tables inside the same serving file rather than separate SQLite files.
- Convert years and measures to appropriate numeric types during generation.
- Add indexes for ISO3 and year.
- Attach a manifest containing the WPP vintage, generation time, source checksum, included columns, and schema version.
- Confirm that every existing comparison and graph produces the same values from the serving copy.

**Complete when:** the filtered database can be rebuilt from scratch and passes comparison tests against the full archive, without changing the running Gradio application.


### Step 1.7 - ensure that the delete datapoint/dete record is working

Ensure that the user can delet a specific datapoint (births etc) from the recod, alongisde the full record. 

## Phase 2 — Build the local FastAPI application layer

### Step 2.1 — Build the new data model and separate workflows from Gradio

Build this alongside the retained legacy records rather than changing their meaning in place.

Replace the current assumption that one webpage finding is one graph point with three related concepts:

- **Source document:** the article or webpage, identified by a unique canonical URL.
- **Metric claim:** one source's reported country, metric, period, value, unit, and definition.
- **Observation group:** claims which refer to the same country, metric, period, unit, and definition.

Same statistic from a different source:

- Keep the new source document.
- Attach its claim to the existing observation group.
- Draw one graph point when the normalized value is the same, with every supporting source listed in its details.

Different value from a different source:

- Retain both claims.
- Mark the observation group as conflicting when the values materially disagree.
- Do not treat harmless unit conversion or obvious rounding as a conflict.
- Allow the administrator to select a preferred claim, mark claims as equivalent/rounded, separate claims with different definitions, or reject a claim.
- Keep divergence from the WPP reference separate from disagreement between article claims.

Carry the Phase 1 source priorities, fallback-provider rule, compact audit policy, and manual-only comparison policy into the new service layer without changing their behaviour.

- Move manual webpage analysis, news discovery, country discovery, extraction, optional deterministic UN lookup, and storage behind framework-independent Python services.
- Keep the comparison mode explicit: manual/admin-approved URLs use the comparison agent; scheduled/bulk URLs do not.
- Preserve the current bounded processing, domain limits, duplicate checks, access recovery, and model timeouts.
- Replace Gradio-specific progress events with structured job events.
- Make the storage layer selectable so local development can use a temporary application database while production uses Postgres.

**Complete when:** all three source classes can be stored, existing legacy records remain neutral, and every workflow can be invoked from Python without importing Gradio.

### Step 2.2 — Add FastAPI endpoints and Jinja page shells

Use **FastAPI + Jinja** for the first version. FastAPI supplies the JSON API and Jinja renders the initial admin page shells. Small browser-side JavaScript fetches JSON and draws Plotly charts; no React or separate frontend framework is required initially.

Provide private/admin endpoints for:

- Manual webpage analysis
- Starting and stopping research jobs
- Direct country hunts and bulk country hunts
- Search settings
- Run and candidate history
- Findings, claims, and source documents
- Conflict review and preferred-claim selection
- Graph data
- URL block-list management
- Submission moderation
- Export status
- Source-rule settings

Long-running calls create jobs and return job IDs. Request handlers do not keep research work alive inside the web process.

**Complete when:** API tests cover authentication boundaries, validation, errors, and all existing admin operations; Jinja pages can use the same JSON endpoints as the future static site.

### Step 2.3 — Add durable worker commands

- Add explicit commands for manual analysis jobs, news search, country search, static export, and maintenance.
- Store job state and progress in the application database.
- Make jobs idempotent so a retry cannot create duplicate findings or claims.
- Add a database lock preventing overlapping discovery runs.
- Mark interrupted jobs clearly and allow a safe retry.
- Ensure every scheduled command terminates when its work finishes.

**Complete when:** stopping the API does not corrupt job state and rerunning a failed command is safe.

### Step 2.4 — Define the frontend and export JSON contracts

- Use typed, versioned response models.
- Return chart series, findings, source classifications, conflicts, and provenance as JSON rather than server-rendered Gradio state.
- Include stable IDs and ISO3 codes so the admin and public interfaces do not depend on display labels.
- Keep internal audit fields out of public response models.

**Complete when:** the frontend can be developed using saved JSON fixtures without direct database access.

### Step 2.5 — Prove local behavioural parity

- Run the existing tests against the service layer.
- Add regression fixtures for current representative URLs and countries.
- Compare old and new graph values for a sample of countries and every supported metric.
- Verify manual analysis, automatic search, country hunt, editing, deletion, blocking, and conflict handling.

**Complete when:** the FastAPI/service version covers the current application behaviour before Gradio is retired.

## Phase 3 — Replace the local Gradio frontend

### Step 3.1 — Build the admin application shell

- Create a responsive, non-Gradio admin interface backed only by the FastAPI JSON API.
- Establish navigation for Overview, Analyse webpage, Research, Country coverage, Findings, Conflicts, Submissions, Exports, and Settings.
- Add clear loading, running, completed, empty, and failed states.

**Complete when:** the application shell runs locally and can use either the live API or fixtures.

### Step 3.2 — Rebuild manual webpage analysis

- Accept and validate a URL.
- Display fetch and extraction progress.
- Show structured metrics, source attribution, effective period, comments, and UN comparison.
- Allow the administrator to approve, edit, reject, rerun, remove, or suppress the source.

**Complete when:** the current Add Webpage workflow is fully available without Gradio.

### Step 3.3 — Rebuild research and country-hunt controls

- Preserve editable news categories, search depth, search window, candidate limits, and domain limits.
- Provide direct country search which always bypasses automatic recency/eligibility rules.
- Show the bulk-country queue, last attempt, last successful finding, next eligible date, and search outcome.
- Make it clear that scheduled/bulk discovery does not run the LLM comparison agent; it performs only the deterministic UN lookup.
- Show live job progress by polling durable job state.

**Complete when:** the current research controls and the new country scheduling state are operable locally.

### Step 3.4 — Rebuild findings, review, and conflict management

- Provide country and metric filters.
- Display source documents separately from their metric claims.
- Show corroborating sources under one observation.
- Provide a conflict queue with side-by-side values, periods, definitions, and source classifications.
- Support preferred-claim selection and all delete/block/rerun actions.
- Provide source-rule management: exact canonical URL blocks, domain blocks, and source classifications/official-publisher overrides.
- Keep every source-rule change auditable and reversible.

**Complete when:** every stored or disputed value can be traced to its sources and resolved from the admin UI.

### Step 3.5 — Rebuild the graphs and reporting

Graph markers:

- Diamond: official publisher
- Outlined circle: secondary publisher with a named source, including accepted fallback datapoints
- Cross: secondary publisher without a named source
- Red outline/halo: an unresolved conflicting claim, independent of source type

Graph behaviour:

- Show one point for an observation with identical corroborating claims.
- List all supporting sources in the detail view.
- Show the preferred value by default for resolved conflicts.
- Provide a control to reveal every conflicting claim.
- Keep UN WPP as a labelled reference series, not as an article-source claim.

**Complete when:** the new graphs reproduce current values and communicate provenance/conflicts without relying on Gradio.

## Phase 4 — Deploy the private admin application to Railway

### Step 4.1 — Provision and migrate the Postgres application database

- Define version-controlled database migrations.
- Create Postgres tables for users, sources, claims, observation groups, runs, candidates, country schedule state, submissions, blocks, settings, and audit events.
- Import the locally validated application data.
- Reconcile counts and sampled records after import.
- Keep WPP reference data outside Postgres initially.

**Complete when:** Postgres contains the verified application data and a repeatable migration can rebuild its schema.

### Step 4.2 — Package the filtered WPP database

- Generate `wpp_serving.sqlite` during a controlled build step.
- Package it read-only with the Railway API/worker image.
- Verify its WPP vintage and checksum at application startup.
- Fail clearly if the expected serving schema is missing or incompatible.

**Complete when:** Railway comparisons and graphs use the same immutable WPP values as local development.

### Step 4.3 — Deploy the API, admin frontend, and worker

- Deploy FastAPI and the admin frontend.
- Deploy research processing as a separate worker/service command.
- Configure health checks and safe deployment behaviour.
- Keep API keys and database credentials in Railway environment secrets.
- Restrict public network exposure to the endpoints that genuinely need it.

**Complete when:** the hosted admin application can perform every manual workflow through the Railway services.

### Step 4.4 — Add authentication, backups, and operational safeguards

- Use Google/OpenID Connect sign-in for the private admin application rather than storing administrator passwords ourselves.
- Store users, roles, active status, and the email allowlist in Postgres. The database is the source of authorization: initially the only required role is `admin`; a `contributor` role can be added later if needed.
- Seed your email as the first administrator during deployment. Admins can grant or revoke access by changing the database-backed user record.
- Require authentication for all admin actions and data-management endpoints. Public static pages require none.
- Use secure sessions, CSRF protection where applicable, and least-privilege database credentials.
- Configure automated Postgres backups and test a restore.
- Add structured logs, error reporting, timeouts, rate limits, and job concurrency locks.
- Record administrative changes in an audit log.

**Complete when:** unauthenticated users cannot scan, modify, export, or inspect private operational data and a recovery test has succeeded.

### Step 4.5 — Verify all manual workflows online

- Analyse a representative HTML article and PDF.
- Verify direct and fallback fetching.
- Edit, remove, rerun, suppress, and unblock sources.
- Review a simulated conflict.
- Draw every graph metric for representative countries.
- Confirm a Railway restart does not lose job or application state.

**Complete when:** Railway becomes the trusted admin environment and local Gradio is no longer needed for normal use.

## Phase 5 — Automate discovery and country coverage

### Step 5.1 — Automate the daily news search

- Run the approved news-search settings once per day.
- Keep the retrieval window aligned with the schedule so coverage neither overlaps excessively nor leaves gaps.
- Preserve candidate, domain, model, and timeout budgets.
- Prevent a second run when the previous run is still active.
- Notify the administrator only for failures, conflicts requiring review, or unusually high usage.

**Complete when:** daily news discovery runs unattended and every run has a visible status and cost record.

### Step 5.2 — Run the initial 20-country-per-day coverage sweep

- Freeze a canonical country list for the sweep.
- Search up to 20 not-yet-attempted countries per day.
- Mark a country attempted even when it produces no usable result.
- Never repeat a country during the initial sweep.
- Allow direct manual country hunts at any time; these do not disturb the sweep cursor.
- Stop the bootstrap mode once every canonical country has been attempted.

**Complete when:** every country has one recorded baseline attempt, expected to take approximately ten days.

### Step 5.3 — Introduce the steady-state country schedule

- After the initial sweep, run up to five eligible countries per day.
- Base eligibility on the last attempt as well as the last successful finding.
- Give repeated no-result countries a long backoff so they are not searched continually.
- Allow a configurable watchlist for countries that should be checked more often.
- Prioritize the longest-overdue eligible countries.
- Keep direct country hunts as an unconditional manual override.

Initial cadence defaults:

- Watchlist: configurable, suggested 30 days
- Countries with useful findings: suggested 90 days
- Countries with repeated no-result searches: suggested 365 days

**Complete when:** Samoa-like no-result countries do not recur continually, while high-interest countries can be checked more often.

### Step 5.4 — Apply the tested fallback-provider rule to scheduled hunts

Use the Phase 1 fallback-provider rule in unattended country hunts:

- “No datapoints” means no stored article-derived datapoints for the country; the WPP reference series does not count.
- When a candidate is processed, check whether the country currently has any stored article-derived datapoint.
- If it has a datapoint, exclude configured fallback providers such as Statista and Our World in Data from this country-scan route.
- If it has no datapoints, allow a configured fallback provider only when the candidate is from the previous three months.
- Store the result using its actual source classification: named secondary as an outlined circle; unnamed secondary as a cross.
- Do not promote the fallback publisher to official status.
- If the fallback reproduces the same WPP dataset already used as the baseline, retain the source relationship but do not treat it as independent corroboration.
- If an official source is later found, keep the fallback as an additional source and prefer the official claim.

The result is intentionally dependent on the data already stored when the candidate is considered. This accepted trade-off keeps the rule easy to understand and operate.

**Complete when:** an otherwise empty country can receive a recent fallback datapoint with a visible source-priority marker, without using those providers for countries that already have findings.

### Step 5.5 — Add spend, run, and failure dashboards

- Record Tavily search and extraction credits by job and provider request.
- Record model, token, request, and estimated-cost usage where available.
- Display daily and monthly totals against configurable budgets.
- Add hard daily limits for country count, candidate processing, provider credits, and model work.
- Show failures, retries, blocked pages, no-result countries, and unresolved jobs.
- Allow scheduled automation to be paused without disabling direct manual analysis.

**Complete when:** scheduled work cannot silently exceed configured budgets and failures are visible without reading server logs.

## Phase 6 — Publish the database-free public site

### Step 6.1 — Build the static JSON exporter

Export only public, published data. At minimum produce:

- `manifest.json`: schema version, generation time, WPP vintage, release ID, and checksums
- A country index with ISO3 codes and available metrics
- Country-level graph series
- Published observation groups and preferred claims
- Source summaries and article links
- Conflict and provenance labels intended for public display

Exclude admin notes, raw candidates, job events, rejected claims, user details, credentials, and moderation history.

Generate the complete release in a temporary directory and validate it before publishing. A failed export must not replace the previous public release.

**Complete when:** the entire public experience can run locally from exported files with both Postgres and FastAPI unavailable.

### Step 6.2 — Build the read-only public frontend

- Load all content through static JSON files.
- Provide country navigation, graphs, source/article details, data freshness, WPP vintage, and methodology.
- Preserve source marker meanings and conflict indicators.
- Do not expose scan, edit, delete, moderation, or admin API controls.
- Add accessible chart summaries and useful metadata for search engines and link sharing.

**Complete when:** the public site displays the graphs and published articles without any live database or research access.

### Step 6.3 — Automate the weekly Vercel release

- Schedule one weekly export after normal discovery processing.
- Validate JSON schemas, required files, source URLs, and record counts.
- Deploy the validated static output to Vercel.
- Record the deployment URL and release ID in the application database.
- Keep the previous successful release available for rollback.
- Allow an administrator to trigger an exceptional manual release.

**Complete when:** the public site updates weekly without exposing Railway or requiring manual file movement.

### Step 6.4 — Verify publication, attribution, and rollback

- Compare a sample of public points with the admin values and their original sources.
- Confirm that unpublished or rejected records never enter the release.
- Verify WPP, source, and fallback-provider attribution and applicable reuse terms.
- Test mobile display, broken-link handling, accessibility, caching, and rollback.

**Complete when:** a release can be independently verified and safely rolled back.

## Phase 7 — Add public URL submissions

### Step 7.1 — Build the submission form

- Accept an HTTP/HTTPS URL and an optional short note.
- Store the URL as a pending submission only.
- Do not fetch, scan, or add statistics at submission time.
- Return a neutral acknowledgement without exposing internal job information.

**Complete when:** a public user can propose a source without triggering research or modifying published data.

### Step 7.2 — Build the admin moderation queue

- Show pending URLs, canonical duplicates, notes, submission time, and moderation status.
- Allow the administrator to analyse, approve for processing, reject, or suppress a URL.
- Route approved URLs through the same manual analysis and duplicate rules as admin-entered URLs.
- Record the moderation decision without exposing it publicly.

**Complete when:** no submitted URL reaches the research worker or findings database without an explicit admin action.

### Step 7.3 — Add submission security and abuse controls

- Validate scheme, host, redirects, and resolved network destinations before any later fetch.
- Block private, loopback, link-local, credential-bearing, and otherwise unsafe URLs.
- Add rate limiting and bot protection.
- Minimize personally identifiable information; do not request an email unless there is a clear need.
- Add retention rules for rejected submissions.

**Complete when:** the public form cannot be used to trigger scanning, access internal services, or generate unbounded moderation volume.

## Release gates

The following gates apply across the phases:

1. **No destructive migration without a verified backup and reconciliation report.**
2. **No scheduled automation until manual FastAPI workflows are stable on Railway.**
3. **No public deployment until the exporter proves it contains only approved public fields.**
4. **No public URL processing without admin moderation and network-safety validation.**
5. **No retirement of Gradio until functional parity has been demonstrated.**
