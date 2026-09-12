# Demographics Agent Implementation Plan

## Executive checklist

- [ ] **Phase 1 — Stabilize and improve the current research pipeline**
  - [x] **Step 1.1 — Preserve and inventory the current database**
  - [x] **Step 1.2 — Fix canonical duplicates and deletion behaviour**
  - [x] **Step 1.3 — Include and rank every source type**
  - [x] **Step 1.4 — Add fallback providers and manual-only comparison**
  - [x] **Step 1.7 — Verify record and metric deletion**
  - [ ] **Step 1.5 — Reduce the candidate audit**
  - [ ] **Step 1.6 — Prepare the full and filtered WPP databases**
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

### Phase 1 boundary

Phase 1 changes and tests the existing Python/Gradio application. It does **not**
introduce FastAPI, Postgres, a new frontend, or the full source/claim/conflict
model. The objective is to make the research rules correct and observable in the
current tool first; Phase 2 then ports those proven rules into the new
application architecture.

Phase 1 does not change the existing effective-date plus population duplicate
safeguard, and does not group different webpages that report the same statistic
into a single graph observation. It adds canonical-URL handling alongside that
existing safeguard. Retaining different URLs as corroborating sources, and
source-to-source conflict grouping, are deliberate Phase 2 work.

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

#### Required URL canonicalisation

Use one shared `canonicalise_source_url()` function for manual analysis, news
search, country hunts, storage, deletion, blocking, and tests. Given a valid
HTTP(S) URL it must:

1. Reject URLs with credentials, a missing host, or a non-HTTP(S) scheme.
2. Lowercase the host and remove a leading `www.`.
3. Normalize the scheme to `https`.
4. Remove the fragment (`#...`).
5. Remove a trailing slash except at the root path.
6. Remove known tracking parameters: `utm_*`, `fbclid`, and `gclid`.
7. Preserve meaningful query parameters, but sort retained query pairs so
   parameter ordering cannot create a second finding.

Do not follow redirects or try to infer that two different publisher URLs have
the same article in Phase 1. That is deliberately out of scope; only the same
canonical URL is a duplicate.

#### Required storage and lookup behaviour

- Add `canonical_url TEXT` to `webpage_findings` and backfill it for every
  stored record using the shared function.
- Before creating the unique index, generate a collision report. For a legacy
  collision, retain the newest extracted record as the active finding and move
  older rows into a small legacy-duplicate archive table; do not silently
  discard them. The dated full-database backup remains the recovery source.
- Add `UNIQUE(canonical_url)` after collisions have been handled.
- Keep the original, user-visible URL in `source_url`; it is evidence, while
  `canonical_url` is only the deduplication key.
- Retain the current “same effective date + same population” duplicate rule
  unchanged. The canonical URL check is an earlier, simpler duplicate gate;
  this existing value/date safeguard remains the later storage-time check.
  Do not try to attach a second URL as corroborating evidence in the current
  SQLite finding model. Phase 2 replaces this safeguard with claims grouped
  under a shared observation.
- At discovery time, check the canonical URL before summary review, fetching,
  model extraction, or Tavily extraction. A duplicate remains visible in the
  candidate audit with the existing finding ID and a `duplicate` status.
- For automatic discovery, also treat a URL whose page content was successfully
  loaded in an earlier run as a duplicate. A URL that was only discovered,
  summary-reviewed, or failed to load remains eligible for a later retry.
- At manual submission time, show the existing record and do not re-run it
  unless the administrator first chooses the explicit rerun path below.

#### Required deletion behaviour

| Admin action | Stored finding | Canonical URL next time | Block list |
|---|---|---|---|
| Remove and allow rerun | Delete the active finding | Eligible for future discovery/manual analysis | Unchanged/absent |
| Remove and suppress source | Delete the active finding | Rejected before fetch | Insert canonical URL |
| Unblock source | No finding is recreated | Eligible again | Remove canonical URL |

Create a compact `finding_actions` audit table with the finding ID, canonical
URL, action, timestamp, and optional note. A normal removal therefore permits a
future re-run without pretending the original review never occurred.

#### Required tests

- URL variants with `www`, `http`, tracking parameters, fragments, trailing
  slashes, and query-pair ordering resolve to one canonical URL.
- Meaningful query parameters remain distinct.
- A duplicate is skipped before retrieval and model work.
- “Remove and allow rerun” allows a later run; “remove and suppress” does not;
  unblocking restores eligibility.
- The existing same-date plus same-population behavior remains unchanged for
  different source URLs.

**Complete when:** the current Gradio tool prevents URL variants from creating duplicate findings while preserving the existing date/population safeguard.

**Completed 2026-09-12:** canonical URL keys are stored and uniquely indexed;
the live migration archived one older canonical collision while retaining the
newer active record. Duplicate candidates are excluded before review, fetch,
extraction, or comparison. Normal removal permits rerun, suppression blocks a
canonical URL, and unblocking restores eligibility. The existing
effective-date plus population duplicate rule remains unchanged.

### Step 1.3 — Include and rank every source type

#### Source classes and priority

Every extracted finding with a valid country, period, and metric that passes
the unchanged Phase 1.2 duplicate safeguards is stored. A source class is a
display and future conflict-resolution priority; it is never an automatic
acceptance gate. In particular, an unnamed secondary source is not rejected
merely because it is unnamed.

| Rank | Stored class | How it is assigned in Phase 1 | Current graph marker |
|---:|---|---|---|
| 1 | `official_publisher` | A matching explicit source rule, or a direct publisher verified by the extraction | Diamond |
| 2 | `secondary_attributed` | Non-official publisher with a named/cited underlying source | Outlined circle |
| 3 | `secondary_unattributed` | No direct official publisher or usable named underlying source | Cross |
| — | `legacy_unreviewed` | A finding that existed before this migration | Neutral star |

Remove the `excluded_unattributed_source` storage outcome and corresponding
automatic-research branch. Unattributed sources are stored at rank 3 and are
visible in the graph and database. The existing fields (`official_source`,
`quoted_source`, and `quoted_source_url`) remain useful evidence even when they
are blank.

Phase 1 does not automatically choose a winner when separately stored rank-1
and rank-3 sources report different values. It displays those separate current
findings. The existing Phase 1.2 date-plus-population safeguard is unchanged;
Phase 2 introduces observation groups, conflicts, and corroborating sources.

#### Search, database, and graph output

This step deliberately updates both automatic-research output and the current
graphs; it is not just a database classification change.

- When extraction finishes, determine the source class before storage and add
  it to the candidate audit record. The search-results table must show a
  `Source class` column for stored results (and the reason when a configured
  exclusion rule applied), so a reviewer can see what will be plotted without
  opening JSON.
- Show the same class in the stored-findings database table and in the record
  detail/JSON. Preserve the original publisher, quoted source, and quoted
  source URL in the detail view.
- Use `source_classification` directly in the chart query rather than
  re-deriving the class independently from `official_source` and
  `quoted_source`.
- Render markers consistently: filled teal diamond for
  `official_publisher`; amber `circle-open` for `secondary_attributed`; grey
  `x` for `secondary_unattributed`; neutral-grey star for
  `legacy_unreviewed`.
- Remove the current red “latest stored estimate” star overlay. A star is
  reserved for legacy evidence under this rule; “latest” can remain a hover or
  detail label instead.
- Keep conflict halos and a preferred claim out of this step: those require
  the Phase 2 claim model.

#### Configurable source rules

Create a `source_rules` SQLite table, initially managed directly in the
database or a small local configuration command; Phase 3 provides the admin
screen.

| Field | Purpose |
|---|---|
| `id` | Stable rule ID |
| `match_type` | `canonical_url` or `domain` |
| `match_value` | The canonical URL or normalized domain to match |
| `action` | `classify` or `exclude` |
| `classification` | The source class for a `classify` rule; null for `exclude` |
| `enabled` | Allows a rule to be disabled without deleting its history |
| `note` | Reason and evidence for the rule |
| `created_at`, `updated_at` | Audit timestamps |

Rule resolution order is: exact canonical-URL rule, then domain rule, then
extraction-derived classification. A rule can mark a verified statistical
office as official or explicitly exclude a domain/URL. `.gov` is only a signal;
it never makes a source official without a rule or supporting extraction.

The existing `blocked_sources` table remains the fast exact-URL suppression
list created by “Remove and suppress source.” A broad publisher decision belongs
in `source_rules`, not in `blocked_sources`.

#### Legacy migration

- Preserve all legacy `finding_json`, URLs, dates, and values unchanged.
- In a one-time migration, set findings that existed before Step 1.3 to
  `legacy_unreviewed`; retain historical source fields rather than trying to
  infer a new class. Do not overwrite those values on every database
  initialization. Findings created after the migration receive their class from
  the current source rules/extraction.
- Render them as neutral stars until a later manual reclassification.
- Do not re-fetch legacy sources as part of this migration.

#### Required tests

- A result without `quoted_source` is stored as `secondary_unattributed`, not
  excluded.
- Exact URL and domain rules override model/extraction classification.
- An excluded rule stops processing before fetch.
- Legacy records remain present and render as stars.
- Marker selection matches the four classes above.
- Search candidate output exposes the class assigned to every stored result.
- The newest stored finding is not given a star merely because it is newest.

**Complete when:** the current Gradio tool stores every valid source, source priority is visible, and source rules can be changed without editing Python code.

**Completed 2026-09-12:** findings are classified once at storage as official,
attributed secondary, unattributed secondary, or legacy-unreviewed. Configurable
URL/domain rules can classify or exclude a source before automatic retrieval;
unattributed secondary findings are retained. Candidate and database tables
show the class, and charts use the stored classification for their markers.

### Step 1.4 — Add fallback providers and manual-only comparison

#### Manual-only comparison policy

- The existing full LLM comparison agent remains on the manual **Analyse
  webpage** path only. It runs after the administrator submits a URL and can
  show the narrative UN comparison and existing outlier explanation.
- Automatic news search, direct country hunt, and bulk country hunt perform
  relevance review and metric extraction, but do not call the comparison agent.
- Remove the automatic `excluded_outlier` branch. An automatically discovered
  point is stored if its extraction is valid, even when it would have differed
  substantially from WPP.
- WPP remains a reference line in charts. It is not necessary to run a
  per-article comparison in order to plot an automatic finding.

#### Fallback-provider policy

Create a `fallback_providers` SQLite table:

| Field | Initial value / purpose |
|---|---|
| `domain` | Primary key, initially `statista.com` and `ourworldindata.org` |
| `enabled` | Allows a provider to be paused without code changes |
| `max_age_days` | `90` |
| `only_when_country_blank_days` | `90` |
| `note` | Why the provider is allowed and any attribution caveat |

For every **automatic-discovery candidate**—daily news search, direct country
hunt, or bulk country hunt—evaluate the rule at the point the candidate is
stored:

1. Is the candidate from an enabled fallback-provider domain?
2. If not, use normal source processing.
3. If yes, has the country had **no stored article-derived datapoint in the
   preceding 90 days**? WPP baseline rows do not count.
4. Is the page publication date no more than 90 days old? A missing or
   unparseable publication date fails this fallback rule.
5. If both conditions pass, store it with the normal source classification and
   marker. If either fails, mark it `excluded_fallback_not_needed` in the audit
   and do not add a finding.

This is intentionally order-dependent within a run: if an earlier candidate
creates the first datapoint for a country, a later Statista/OWID candidate is
not stored. That is an accepted simplicity trade-off.

Fallback providers are **not** added as targeted searches, include-domains, or
separate provider calls. They are considered only when Statista or OWID appears
naturally in the ordinary news or country-hunt results. In other words, daily
news search may retain a suitable Statista/OWID result that it independently
found; the system never performs an “OWID hunt.”

Fallback providers are not official merely because they are configured. For
example, Statista can be a named-secondary circle when it names IMF; it remains
an unnamed-secondary cross if it does not. OWID material that reproduces the
same WPP series is a linked source, not independent corroboration.

#### Required tests

- A manual URL calls the comparison agent; an automatic URL does not.
- Automatic extraction is stored even when an old comparison would have marked
  it as an outlier.
- A recent Statista/OWID candidate discovered through either normal news search
  or a country hunt is stored when the country has no datapoint acquired in the
  preceding 90 days.
- The same candidate is excluded when that country already has a datapoint
  acquired in the preceding 90 days.
- A fallback candidate older than 90 days, or without a date, is excluded.
- Disabling a configured provider takes effect without a code change.

**Complete when:** normal automatic discovery can fill a country that has had
no datapoint in the preceding 90 days with a recent Statista/OWID result,
without ever targeting those providers; automatic jobs never call the
comparison agent, and manual URL analysis still does.

**Completed 2026-09-12:** automatic discovery stores extraction results without
calling the UN comparison agent or applying an outlier-deletion branch. The
durable `fallback_providers` table seeds Statista and OWID with the configured
90-day limits; enabled-provider, publication-date, and country-gap checks run
at finding storage and record `excluded_fallback_not_needed` in the audit.

### Post-1.4 implementation order

The checklist order is intentional: after Step 1.4, complete **Step 1.7**
(record and metric deletion) before either database-maintenance activity. Steps
1.5 and 1.6 form the final database-maintenance set for Phase 1 and follow
Step 1.7. Their existing numbers are retained so earlier decisions and notes
continue to point to the same work.

### Step 1.5 — Reduce the candidate audit

Keep in `search_candidates.details_json` only:

- title, snippet, publication date, provider, category, original/canonical URL;
- processing status, source class, decision reasons, duplicate/finding IDs, and
  concise errors;
- structured extraction/storage result and measured provider/model timings.

Remove when a candidate finishes, fails, or is deferred:

- `full_text`;
- raw provider payloads;
- alternative-page content and large retrieval attempt payloads;
- embedded/binary/image data.

The accepted finding continues to keep the structured extracted metrics and its
original article URL. The audit records *why* a candidate was accepted,
rejected, duplicate, deferred, or blocked, not a copy of the article.

Run a one-time sanitisation over existing candidate JSON, record the number of
bytes removed, then run SQLite `VACUUM` on the working database after the
verified backup exists.

**Complete when:** the current audit remains useful for outcomes and debugging without accumulating full article text, and no accepted finding or source URL has been removed.

### Step 1.6 — Prepare the full and filtered WPP databases

- Retain the complete WPP database offline as the authoritative source archive.
- Keep the current working database under `databases/` and retain only offline
  source material under `Data_Files/`.
- Create a repeatable build command that generates a small `wpp_serving.sqlite` from that archive.
- Keep `estimates` and `medium_variant` as separate tables inside the same serving file rather than separate SQLite files.
- Convert years and measures to appropriate numeric types during generation.
- Add indexes for ISO3 and year.
- Attach a manifest containing the WPP vintage, generation time, source checksum, included columns, and schema version.
- Confirm that every existing comparison and graph produces the same values from the serving copy.

**Complete when:** the filtered database can be rebuilt from scratch and passes comparison tests against the full archive, without changing the running Gradio application.

### Step 1.7 — Verify record and metric deletion

- Verify that an administrator can delete one metric, such as births, from an
  accepted record while preserving its other extracted metrics and source URL.
- Verify that deleting the full record removes all its points from every graph.
- Verify that “remove and suppress source” prevents a later automatic or manual
  re-add, while “remove and allow rerun” does not.
- Confirm the editable finding JSON, database table, and graph all refresh from
  the same saved state after each action.
- Add regression tests for metric deletion, record deletion, suppression,
  unblock, and later rerun.
- Ensure user can edit search terms (ie official country is Russia Federation -  how do we sdarch for Russia pulation)

**Complete when:** per-metric deletion and full-record deletion are both safe,
visible immediately in the graphs, and preserve the intended URL eligibility.

**Completed 2026-09-12:** individual metric deletion is available from the
database and visualisation controls, reloads the stored JSON after saving, and
records a `metric_removed` action. Deleting the final remaining metric is
rejected so the administrator must explicitly delete the full record. Full
deletion, suppress/unblock, and rerun eligibility have regression coverage.

### Phase 1 SQLite database shape after completion

Phase 1 keeps the existing SQLite application database. It adds small tables
and fields; it does not yet introduce Postgres or the Phase 2 claim tables.

| Table | Phase 1 state |
|---|---|
| `webpage_findings` | Existing finding storage plus `canonical_url`, source-class values including `legacy_unreviewed`, and an optional matching source-rule ID. `source_url` remains the original visible link. |
| `blocked_sources` | Existing exact canonical-URL suppression list, extended with an optional reason/note and audit metadata if needed. |
| `source_rules` | New configurable domain/exact-URL classification or exclusion rules. |
| `fallback_providers` | New configured domains, enabled flag, max age, and “only when the country has no datapoint acquired in the previous 90 days” rule. |
| `finding_actions` | New compact log of remove, suppress, unblock, and metric-delete actions. |
| `search_candidates` | Existing rows, but large webpage/provider content removed from `details_json` after processing. |
| `search_runs` / `research_settings` | Existing job and search controls, with links to source/fallback configuration where needed. |
| WPP SQLite files | Full offline archive plus a generated read-only serving copy; neither stores article findings. |

The proposed Phase 2 tables (`source_documents`, `metric_claims`, and
`observation_groups`) are intentionally not added in Phase 1. This keeps the
current Gradio data model stable while its logic is being corrected and tested.

## Phase 2 — Build the local FastAPI application layer

### Step 2.1 — Build the new data model and separate workflows from Gradio

Build this alongside the retained legacy records rather than changing their meaning in place.

Replace the current assumption that one webpage finding is one graph point with three related concepts:

- **Source document:** the article or webpage, identified by a unique canonical URL.
- **Metric claim:** one source's reported country, metric, period, value, unit, and definition.
- **Observation group:** claims which refer to the same country, metric, period, unit, and definition.

At the Phase 2 cutover, retire the Phase 1 effective-date plus population
exclusion for new writes. Canonical URL uniqueness remains the only rule that
rejects a source document outright. A different canonical URL instead creates a
source document and one or more claims, which are then associated with an
observation group. Do not manufacture missing legacy corroborating sources:
only migrate the documents that were retained in Phase 1, and allow later
searches or manual submissions to add further evidence.

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
