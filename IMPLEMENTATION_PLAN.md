# Demographics Agent Implementation Plan

## Executive checklist

- [x] **Phase 1 — Stabilize and improve the current research pipeline**
  - [x] **Step 1.1 — Preserve and inventory the current database**
  - [x] **Step 1.2 — Fix canonical duplicates and deletion behaviour**
  - [x] **Step 1.3 — Include and rank every source type**
  - [x] **Step 1.4 — Add fallback providers and manual-only comparison**
  - [x] **Step 1.7 — Verify record and metric deletion**
  - [x] **Step 1.5 — Reduce the candidate audit**
  - [x] **Step 1.6 — Prepare the full and filtered WPP databases**
- [x] **Phase 2 — Establish the backend alongside the retained Gradio client**
  - [x] **Step 2.1 — Establish the FastAPI application boundary**
  - [x] **Step 2.2 — Extract read-only query and graph services**
  - [x] **Step 2.3 — Extract finding administration services**
  - [x] **Step 2.4 — Extract manual webpage analysis**
  - [x] **Step 2.5 — Extract research and country-hunt services**
  - [x] **Step 2.5a — Audit and complete ISO3-only country identity**
  - [x] **Step 2.6 — Add durable worker commands and job state**
  - [x] **Step 2.7 — Add versioned JSON endpoints and fixtures**
  - [x] **Step 2.8 — Build a basic API testing frontend**
- [ ] **Phase 3 — Build the admin interface and retire Gradio**
  - [x] **Step 3.1 — Build the admin application shell**
  - [x] **Step 3.2 — Benchmark service functionality against Gradio**
  - [x] **Step 3.3 — Complete backend durability and frontend contracts**
  - [x] **Step 3.4 — Tighten extraction scope and validation**
  - [x] **Step 3.5 — Rebuild manual webpage analysis**
  - [x] **Step 3.6 — Rebuild research and country-hunt controls**
  - [x] **Step 3.7 — Rebuild findings and administration**
  - [x] **Step 3.8 — Rebuild the graphs and reporting**
  - [ ] **Step 3.9 — Cut over and retire Gradio**
  - [ ] **Step 3.10 — Simplify the post-Gradio codebase**
- [ ] **Phase 4 — Add the claims and conflict framework**
  - [ ] **Step 4.1 — Introduce source documents, claims, and observation groups**
  - [ ] **Step 4.2 — Migrate legacy records and extend backend contracts**
  - [ ] **Step 4.3 — Add conflict review and provenance views**
  - [ ] **Step 4.4 — Verify claim and graph semantics**
- [ ] **Phase 5 — Deploy the private admin application to Railway**
  - [ ] **Step 5.1 — Provision and migrate the Postgres application database**
  - [ ] **Step 5.2 — Package the filtered WPP database**
  - [ ] **Step 5.3 — Deploy the API, admin frontend, and worker**
  - [ ] **Step 5.4 — Add authentication, backups, and operational safeguards**
  - [ ] **Step 5.5 — Verify all manual workflows online**
- [ ] **Phase 6 — Automate discovery and country coverage**
  - [ ] **Step 6.1 — Automate the daily news search**
  - [ ] **Step 6.2 — Run the initial 20-country-per-day coverage sweep**
  - [ ] **Step 6.3 — Introduce the steady-state country schedule**
  - [ ] **Step 6.4 — Apply the tested fallback-provider rule to scheduled hunts**
  - [ ] **Step 6.5 — Add spend, run, and failure dashboards**
- [ ] **Phase 7 — Publish the database-free public site**
  - [ ] **Step 7.1 — Build the static JSON exporter**
  - [ ] **Step 7.2 — Build the read-only public frontend**
  - [ ] **Step 7.3 — Automate the weekly Vercel release**
  - [ ] **Step 7.4 — Verify publication, attribution, and rollback**
- [ ] **Phase 8 — Add public URL submissions**
  - [ ] **Step 8.1 — Build the submission form**
  - [ ] **Step 8.2 — Build the admin moderation queue**
  - [ ] **Step 8.3 — Add submission security and abuse controls**

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
current tool first; Phase 2 then ports those proven rules into the new backend
architecture.

Phase 1 does not change the existing effective-date plus population duplicate
safeguard, and does not group different webpages that report the same statistic
into a single graph observation. It adds canonical-URL handling alongside that
existing safeguard. Retaining different URLs as corroborating sources, and
source-to-source conflict grouping, are deliberate Phase 4 work.

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
  SQLite finding model. Phase 4 replaces this safeguard with claims grouped
  under a shared observation.
- At discovery time, check the canonical URL before summary review, fetching,
  model extraction, or Tavily extraction. A duplicate remains visible in the
  candidate audit with the existing finding ID and a `duplicate` status.
- For automatic discovery, also treat a URL whose page content was successfully
  loaded in an earlier run as a duplicate. A URL that was only discovered,
  summary-reviewed, or failed to load remains eligible for a later retry.
- An administrator can explicitly request a **recheck** for a previously loaded
  URL. That URL bypasses the historical-loaded duplicate check once in the next
  automatic run. If it loads again, it returns to the normal duplicate set; if
  it does not load, it remains eligible under the ordinary failed-load rule.
- At manual submission time, show the existing record and do not re-run it
  unless the administrator first chooses the explicit rerun path below.

#### Required deletion behaviour

| Admin action | Stored finding | Canonical URL next time | Block list |
|---|---|---|---|
| Remove and allow rerun | Delete the active finding and request one automatic recheck | Eligible for manual analysis and one future automatic recheck | Unchanged/absent |
| Remove and suppress source | Delete the active finding | Rejected before fetch | Insert canonical URL |
| Unblock source | No finding is recreated | Eligible again | Remove canonical URL |

Create a compact `finding_actions` audit table with the finding ID, canonical
URL, action, timestamp, and optional note. A normal removal therefore permits a
future re-run without pretending the original review never occurred.
Store outstanding automatic rechecks as canonical URLs, with an auditable
requested/consumed state; do not infer them from a deleted finding row.

#### Required tests

- URL variants with `www`, `http`, tracking parameters, fragments, trailing
  slashes, and query-pair ordering resolve to one canonical URL.
- Meaningful query parameters remain distinct.
- A duplicate is skipped before retrieval and model work.
- “Remove and allow rerun” allows a later run; “remove and suppress” does not;
  unblocking restores eligibility.
- An explicit recheck permits one automatic retry of a previously loaded URL.
- The existing same-date plus same-population behavior remains unchanged for
  different source URLs.

**Complete when:** the current Gradio tool prevents URL variants from creating duplicate findings while preserving the existing date/population safeguard.

**Completed 2026-09-12:** canonical URL keys are stored and uniquely indexed;
the live migration archived one older canonical collision while retaining the
newer active record. Duplicate candidates are excluded before review, fetch,
extraction, or comparison. Normal removal permits manual rerun and creates an
auditable automatic recheck; a successful reload closes that override, while a
failed reload remains eligible. Suppression blocks a canonical URL, and
unblocking creates the same automatic recheck. The existing effective-date
plus population duplicate rule remains unchanged.

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
Phase 4 introduces observation groups, conflicts, and corroborating sources.

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
  the Phase 4 claim model.

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

An administrator may nevertheless deliberately enter a fallback domain in a
manual search category or include-domain control. That is an explicit operator
choice, not a system-created provider hunt; the same enabled/date/country-gap
fallback checks still apply to any resulting automatic candidate.

Fallback providers are not official merely because they are configured. For
example, Statista can be a named-secondary circle when it names IMF; it remains
an unnamed-secondary cross if it does not. OWID material that reproduces the
same WPP series is a linked source, not independent corroboration.

#### Data-retention and annualisation policy

Store a useful numeric, national demographic datapoint whether or not it can
be directly compared with WPP. Exclude an extraction only when it has no useful
numeric demographic metric, or—on automatic discovery—does not resolve to one
unique national geography. Relevance screening still rejects clearly
irrelevant pages before extraction.

When a source gives an explicit time period, retain its original value and
cadence and calculate a deterministic annualised value for count flows:

- daily count × days in its documented reporting year;
- monthly count × 12;
- quarterly count × 4;
- an explicit multi-month or dated partial period scaled by its documented
  duration.

Do not prorate population stock figures, rates such as TFR, or a period whose
duration is unknown. Those claims remain stored as source evidence with their
period/caveat; they are not presented as directly comparable annual WPP values.

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
- Alternative-source recovery applies the publication date of the page actually
  loaded when evaluating fallback eligibility.
- A useful partial-period metric is stored; count flows with an explicit cadence
  receive the documented annualisation while population stocks and rates do not.

**Complete when:** normal automatic discovery can fill a country that has had
no datapoint in the preceding 90 days with a recent Statista/OWID result,
without ever targeting those providers; automatic jobs never call the
comparison agent, and manual URL analysis still does.

**Completed 2026-09-12:** automatic discovery stores extraction results without
calling the UN comparison agent or applying an outlier-deletion branch. The
durable `fallback_providers` table seeds Statista and OWID with the configured
90-day limits; enabled-provider, publication-date, and country-gap checks run
at finding storage and record `excluded_fallback_not_needed` in the audit.
Explicitly configured Include domains remain allowed for those fallback
domains; the system itself never creates a fallback-provider-only hunt. Useful
non-comparable numeric national evidence is stored with its comparison caveat,
and documented partial count periods are annualised while retaining their
source value, cadence, factor, and note.

An undated Statista/OWID country profile may be retained as a secondary seed
only when the country has no article-derived datapoint in the configured gap
window. A dated fallback result still has to meet the configured publication
age limit. This permits a blank-country OWID profile without turning fallback
providers into a general evergreen search target.

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
- `page_loaded`, the actual loaded URL, and compact alternative-attempt status
  metadata. These are identity/audit markers, not retained article content.

Remove when a candidate finishes, fails, or is deferred:

- `full_text`;
- raw provider payloads;
- alternative-page content and large retrieval attempt payloads;
- embedded/binary/image data.

The accepted finding continues to keep the structured extracted metrics and its
original article URL. The audit records *why* a candidate was accepted,
rejected, duplicate, deferred, or blocked, not a copy of the article.

Run a one-time sanitisation over existing candidate JSON, preserving a
`page_loaded` marker for legacy rows whose `full_text` proves a successful
load. Record the number of bytes removed, then run SQLite `VACUUM` on the
working database after the verified backup exists.

**Complete when:** the current audit remains useful for outcomes and debugging without accumulating full article text, and no accepted finding or source URL has been removed.

**Completed 2026-09-12:** candidates now retain page bodies only during active
review and extraction. Every finished, failed, or deferred outcome is reduced
to compact decision, extraction, timing, identity, and alternative-attempt
metadata; successful loads retain only the durable `page_loaded` marker and
actual loaded URL. The maintenance command performs the one-time legacy JSON
sanitisation, logs removed bytes in `audit_cleanup_log`, and vacuums only after
creating its rollback backup.

### Step 1.6 — Prepare the full and filtered WPP databases

- Retain the complete WPP database offline as the authoritative source archive.
- Keep the current working database under `databases/` and retain only offline
  source material under `Data_Files/`.
- Create a repeatable build command that generates a small `wpp_serving.sqlite` from that archive.
- Keep `estimates` and `medium_variant` as separate tables inside the same
  serving file. They are the two logical WPP datasets; splitting the existing
  archive into duplicate full SQLite files would add operational cost without
  improving the Railway or static-export architecture.
- Convert years and measures to appropriate numeric types during generation.
- Add indexes for ISO3 and year.
- Attach a manifest containing the WPP vintage, generation time, source checksum, included columns, and schema version.
- Confirm that every existing comparison and graph produces the same values from the serving copy.

**Complete when:** the filtered database can be rebuilt from scratch and passes comparison tests against the full archive, without changing the running Gradio application.

**Completed 2026-09-12:** the complete 65-column `estimates` and
`medium_variant` archive, with the WPP revision-history overlay, is now held
offline in `Data_Files/WPP2024_GEN_F01_DEMOGRAPHIC_INDICATORS_COMPACT.sqlite`.
The repeatable maintenance build creates the read-only
`databases/wpp_serving.sqlite`, retaining the two logical WPP tables and only
the ten columns used by comparisons and graphs. Numeric measures and years are
converted during generation; source placeholders become SQL `NULL`, and ISO3/
year lookup indexes plus a checksum manifest are included. The writable
research database remains under `databases/`, while WPP reads use the serving
copy, preserving application behaviour without a fallback to `Data_Files`.

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
deletion now presents as “Remove and allow rerun”, records an auditable
automatic recheck, and has regression coverage for successful and failed
rechecks. Suppress/unblock and source/metric deletion likewise have regression
coverage. Search categories remain directly editable, including arbitrary
terms such as `Russia population` when the WPP country label is `Russian
Federation`.

### Phase 1 SQLite database shape after completion

Phase 1 keeps the existing SQLite application database. It adds small tables
and fields; it does not yet introduce Postgres or the Phase 4 claim tables.

| Table | Phase 1 state |
|---|---|
| `webpage_findings` | Existing finding storage plus `canonical_url`, source-class values including `legacy_unreviewed`, and an optional matching source-rule ID. `source_url` remains the original visible link. |
| `blocked_sources` | Existing exact canonical-URL suppression list, extended with an optional reason/note and audit metadata if needed. |
| `source_rules` | New configurable domain/exact-URL classification or exclusion rules. |
| `fallback_providers` | New configured domains, enabled flag, max age, and “only when the country has no datapoint acquired in the previous 90 days” rule. |
| `finding_actions` | New compact log of remove, suppress, unblock, and metric-delete actions. |
| `automatic_rechecks` | Compact requested/consumed/loaded/cancelled state for explicit automatic retries of historically loaded canonical URLs. |
| `search_candidates` | Existing rows, but large webpage/provider content removed from `details_json` after processing. |
| `search_runs` / `research_settings` | Existing job and search controls, with links to source/fallback configuration where needed. |
| WPP SQLite files | Full offline archive plus a generated read-only serving copy; neither stores article findings. |

The proposed claims tables (`source_documents`, `metric_claims`, and
`observation_groups`) are intentionally deferred to Phase 4. Phase 2 keeps the
current finding model and business rules stable while replacing its backend.

## Phase 2 — Replace the backend behind the retained Gradio client

### Phase 2 country-identity rule

From Step 2.2 onward, ISO3 is the sole country identifier used by service
calls, database lookups, job state, API paths and query parameters, graph
series, and finding/research records. A country name is display text or
untrusted input only: resolve it at the boundary to one canonical ISO3 code
before it enters a workflow. Services may carry the canonical WPP label beside
the ISO3 code for prompts and display, but must not use that label as an
identity or query key.

Every LLM call that concerns a country must receive the resolved ISO3 code and
canonical WPP label in its structured context. If an LLM returns a geography,
the service must resolve that text to exactly one ISO3 code before comparison,
storage, or any downstream call; ambiguous, subnational, and unresolvable
geographies are not allowed onto a country-specific path.

### Step 2.1 — Establish the FastAPI application boundary

Add the FastAPI application, configuration, health check, dependency wiring,
and a test client without changing any current workflow. Keep Gradio as the
only user interface. FastAPI owns HTTP validation, authentication hooks, and
response serialization; it contains no research business logic.

**Complete when:** the API starts locally, its health endpoint is tested, and
the existing Gradio application remains unchanged.

**Completed 2026-09-12:** `api.py` provides the FastAPI application factory,
runtime settings, injectable authentication hook, lifespan wiring, static test
frontend, and tested health endpoint. The original Gradio entry point remains
available during the Phase 3 transition.

### Step 2.2 — Extract read-only query and graph services

Move country lookup, WPP queries, stored-finding reads, and graph-data assembly
into framework-independent Python services. Resolve any supplied country name
to ISO3 at the service boundary, then use ISO3 for every WPP, finding, and graph
lookup. Add read-only endpoints for country choices, findings, graph series,
run history, and candidate history; country choices may include display labels,
but country-specific requests use ISO3.

**Complete when:** service and endpoint tests return the same values as the
current graphs and selectors, with no Gradio imports in the service code.

**Completed 2026-09-12:** `read_services.py` now owns plain-data country,
finding, WPP graph-series, run-history, and candidate-history reads. The
versioned API endpoints expose those structures while Gradio remains the
unchanged renderer; Plotly/Gradio rendering stays in place until the Phase 3
replacement UI is ready.

### Step 2.3 — Extract finding administration services

Move finding edits, metric deletion, record deletion, source suppression,
unblock, source rules, provider settings, and existing audit actions behind
services. Add validated mutation endpoints while retaining the current SQLite
finding schema and Phase 1 rules. Resolve and retain ISO3 for every new or
edited national finding, and use ISO3—not a stored country label—for all
country-scoped administration operations.

**Complete when:** every existing administration action has service and API
coverage and preserves the current behaviour.

**Completed 2026-09-12:** finding reads and edits, metric and record deletion,
source suppression and unblocking, recheck/action history, source rules, and
fallback-provider settings are exposed through framework-independent services
and versioned administration routes while retaining the Phase 1 SQLite rules.

### Step 2.4 — Extract manual webpage analysis

Move the manual URL analysis workflow, fetching progress, extraction, optional
manual-only comparison, and storage decision into a service. Expose it through
a single asynchronous API operation and a status/result endpoint. Resolve the
requested country context and any extracted LLM geography to ISO3 before UN
comparison or storage; include the ISO3 and canonical WPP label in every
country-specific LLM prompt/context.

**Complete when:** a representative manual URL can be analysed through Python
and FastAPI with the same stored result as the current Gradio flow.

**Completed 2026-09-12:** manual analysis is available as an asynchronous API
job with durable status/progress, optional ISO3 context, structured extraction,
manual-only WPP comparison, storage outcome, and compact JSON result polling.
The Phase 3 frontend will make the approval/storage semantics explicit.

### Step 2.5 — Extract research and country-hunt services

Move news discovery, country hunts, stop handling, bounded processing,
duplicate checks, domain limits, fallback-provider eligibility, and access
recovery into services. Preserve the rule that automatic and bulk routes do
not invoke the comparison agent. Start country hunts from ISO3, retain ISO3 in
candidate and run state, and use a canonical WPP label only to construct
human-readable search text. Any LLM extraction or review in these paths must
resolve its geography to the same ISO3 before it can continue.

**Complete when:** research and country-hunt regression cases run through the
service layer with unchanged outcomes.

**Completed 2026-09-12:** automatic discovery, direct country hunts, bulk ISO3
hunts, settings, progress, stop handling, candidate history, duplicate/source
rules, fallback eligibility, and alternative-source recovery run through the
shared research services. Automatic and bulk discovery do not invoke the LLM
comparison step.

### Step 2.5a — Audit and complete ISO3-only country identity

Mop up every Phase 2 path before worker and public-API work proceeds. Backfill
or map legacy finding, candidate, run, and WPP-release-overlay rows to ISO3;
the historical WPP release build must populate ISO3 even where the source
release supplies only a country label or numeric location code. Replace all
remaining name-based country joins, filters, and fallback queries with ISO3
lookups. Keep names only in explicit input-to-ISO3 mapping, prompt/display
metadata, and the canonical country-reference table.

Add regression coverage for aliases, legacy stored labels, historic WPP
releases, manual analysis, and country hunts. The tests must prove that each
path resolves one ISO3 before a country-specific LLM call, query, comparison,
or write, and that no country-specific service query depends on a free-form
country name.

**Complete when:** an implementation audit and regression suite demonstrate
ISO3-only identity across every extracted Phase 2 service, with no name-based
country fallback remaining.

**API audit completed:** all country-scoped API inputs now use ISO3: finding
filters use `?iso3=`, graph routes use `/graph-series/{iso3}`, manual analysis
accepts `country_iso3`, and direct/bulk country hunts accept only ISO3 lists.
Canonical WPP labels are returned as display metadata or used to construct
human-readable hunt queries; they are not used as query keys. Manual and
country-hunt extraction paths resolve the model geography to ISO3 before
comparison or storage, and scoped hunts reject a result whose ISO3 does not
match the requested country. Candidate extraction history now exposes
`extracted_iso3`, and country-hunt run settings retain `country_iso3`.

Legacy finding rows are backfilled on API reads through the existing country
reference, while historical WPP release imports backfill ISO3 from canonical
country labels or numeric location codes before building the serving overlay.
The API audit does not include the retained Gradio adapters; those remain
label-oriented through Phase 2. The Phase 3 UI converts its controls to ISO3
before calling services. Durable job storage and cross-process worker
coordination remain Step 2.6 work, not an ISO3 identity gap.

### Step 2.6 — Add durable worker commands and job state

- Add explicit commands for manual analysis, news search, country search,
  maintenance, and later export work.
- Store structured job state and progress in the application database.
- Make jobs idempotent within the current finding model.
- Add a database lock preventing overlapping discovery runs.
- Mark interrupted jobs clearly and allow a safe retry.
- Store country scope and country-specific progress using ISO3; preserve a
  canonical label only as job-display metadata.

**Complete when:** stopping the API does not corrupt job state and rerunning a
failed command is safe.

**Completed 2026-09-12 for the Phase 2 boundary:** durable job records,
attempts, progress, discovery locking, interruption recovery, safe retry, and
CLI commands exist for manual analysis, news search, country search,
maintenance, recovery, and the provisional export path. Cross-process owner
leases, status-preserving stop semantics, and removal of the provisional export
behavior are explicitly carried into Phase 3 Step 3.3 before production use.

### Step 2.7 — Add versioned JSON endpoints and fixtures

- Use typed, versioned response models for every extracted service.
- Include stable IDs and ISO3 codes, with internal audit fields excluded from
  externally consumable response models.
- Make ISO3 the required country identifier for country-specific endpoints;
  labels are returned only for display and may be accepted only by an explicit
  boundary mapping route where needed.
- Save representative JSON fixtures for the Phase 3 frontend.
- Add endpoint validation and error tests.

**Complete when:** a frontend can be developed from fixtures without direct
database access.

**Completed 2026-09-12 for frontend development:** the versioned read,
administration, analysis, research, country-hunt, settings, and worker routes
are exposed with representative fixtures and validation/route tests. The full
suite passes 143 tests. Final explicit response models, consistent missing-item
responses, redaction review, and HTTP-to-temporary-database integration tests
are recorded as Phase 3 Step 3.3 contract hardening.

### Step 2.8 — Build a basic API testing frontend

Build a deliberately small, local-only frontend against the available API. It
is a testing surface, not the Phase 3 admin application and not a Gradio
rewrite. It should support country selection, raw findings and graph-series
inspection, starting and polling manual-analysis jobs, and starting/observing
research jobs. It may use simple server-rendered HTML and small JavaScript
modules; avoid styling, comprehensive administration, duplicated chart logic,
or a separate frontend framework.

Keep Gradio unchanged as the operational reference while this surface proves
that the API is usable from a browser.

**Complete when:** a developer can exercise the principal read and job
contracts through a browser without direct database access. The local testing
surface also includes an open route console so every versioned API route can be
called without introducing a user-admin or authentication workflow at this
stage.

**Completed 2026-09-12:** the local API desk loads countries, findings, graph
series, and saved settings; starts and polls manual-analysis and research jobs;
and provides an open route console for the remaining versioned endpoints. It is
intentionally a test surface rather than the Phase 3 admin interface.

## Phase 3 — Build the admin interface and retire Gradio

### Phase 3 decisions required

Resolve and record these decisions at the indicated step; do not let the
frontend accidentally decide them through implementation details.

1. **Worker ownership — required before Step 3.3:** choose the stale-worker
   detection mechanism used before recovering jobs or releasing locks.
   Recommended: persisted owner IDs with renewable leases/heartbeats and a
   conservative expiry period.
2. **Placeholder export — required before Step 3.3 completes:** either disable
   the current placeholder command until Phase 7 or specify a real, typed
   internal export contract. Recommended: disable it so run history cannot be
   mistaken for exported findings.
3. **Extraction acceptance policy — required before Step 3.4:** decide whether
   projections and subset statistics need a separate stored claim type or are
   summary-only in the current finding model. Recommended for Phase 3: keep
   them in the summary/audit but do not populate observed demographic metrics;
   revisit typed projection claims in Phase 4.
4. **Manual approval semantics — required before Step 3.5:** decide whether
   analysis continues to save immediately, with rejection implemented as a
   subsequent deletion, or whether analysis produces a durable draft that is
   stored only after explicit approval. Recommended: use a durable draft and
   explicit approval so `approve` and `reject` describe the real behavior.
5. **LangGraph boundary — required before Step 3.5:** decide whether the
   supported FastAPI manual-analysis pipeline should execute LangGraph or use
   ordinary service functions. The current FastAPI path calls the extraction
   and comparison functions directly; only Gradio executes the compiled graph.
   Recommended: keep the direct service pipeline unless LangGraph will provide
   a concrete required capability such as branching, resumable checkpoints, or
   human-in-the-loop continuation. Reassess dependency removal in Step 3.10.
6. **Gradio retirement gate — required before Step 3.9:** agree the parity/UAT,
   rollback, and data-integrity checks that permit the old entry point to be
   switched off. After the gate passes, Gradio is removed rather than retained
   as a second supported application.

### Step 3.1 — Build the admin application shell

- Use **FastAPI + Jinja** for responsive, non-Gradio page shells backed only
  by the FastAPI JSON API. Small browser-side JavaScript modules fetch JSON,
  poll jobs, and draw Plotly charts; no separate frontend framework is needed.
- Establish navigation for Overview, Analyse webpage, Research, Country coverage, Findings, Conflicts, Submissions, Exports, and Settings.
- Add clear loading, running, completed, empty, and failed states.
- Build one small browser API client for authentication, typed JSON requests,
  error display, and durable-job polling; do not reuse Gradio callbacks.
- Populate country controls from API country choices, storing ISO3 values in
  browser state and using labels only for display.

**Complete when:** the application shell runs locally and can use either the live API or fixtures.

**Completed 2026-09-13:** a responsive FastAPI/Jinja admin shell is available
at `/admin/` with the planned navigation, shared JSON client, durable-job
polling, explicit workflow states, API-populated ISO3 country controls, and a
fixture mode for local frontend development. The Phase 2.8 API desk remains at
the root route as a temporary testing surface.

### Step 3.2 — Benchmark service functionality against Gradio

Use the retained Gradio workflow as the reference baseline. Run comparable
cases through the extracted services and API for manual analysis, automatic
search, country hunts, edits, deletion, blocking, rerun behaviour, and graph
values. Record every material difference, classifying it as an equivalent
implementation, an intentional improvement, or a regression to investigate.
The goal is functional comparison and an explicit record of changed behaviour,
not exact output parity. Do not make Gradio a permanent HTTP client; use
narrowly scoped, disposable adapters only where a treatment test requires one.

**Complete when:** comparable workflows have been evaluated, their material
differences are documented and dispositioned, and no unexplained regressions
remain. Gradio is retained only as a temporary fallback/reference until
cutover.

**Completed 2026-09-13:** comparable treatments for manual analysis,
automatic search, single/bulk country hunts, edits, metric deletion, removal,
blocking, unblocking, rerun state, and graph values are recorded in
`STEP_3_2_PARITY.md` and covered by `tests/test_step_3_2_parity.py`. All
material differences are classified as equivalent implementation or
intentional improvement; no unexplained regression remains.

### Step 3.3 — Complete backend durability and frontend contracts

**Decisions required:** Phase 3 decisions 1 and 2 must be recorded before this
step is considered complete.

- Maintain durable manual and discovery jobs, progress, attempts, recovery,
  locking, and safe retry behavior; a startup timeout must never permit
  overlapping discovery work.
- Give every active worker an explicit owner and renewable lease/heartbeat.
  Recover a `running` job and release its discovery lock only after that owner
  is demonstrably stale; starting or reloading FastAPI must not interrupt a
  valid CLI worker or another application process using the same database.
- Make stop operations idempotent and status-preserving. Stopping an active
  run may move it to `stopping`/`interrupted`; stopping a completed, failed, or
  already interrupted run must report its existing terminal status rather than
  claiming that it was newly interrupted.
- Finalize typed, versioned public responses, structured run/candidate detail,
  correct missing-resource errors, and redaction of raw storage/internal audit
  fields from browser list responses.
- Replace remaining arbitrary `dict` response bodies with explicit response
  models, including administration, analysis, research, settings, run-detail,
  candidate-detail, and country-hunt routes. Return consistent `404` responses
  for missing findings, jobs, runs, candidates, providers, and rules.
- Add server-side ISO3 country-gap preview and bounded batch selection. Keep
  labels as display data and show scope-mismatch exclusions explicitly.
- Expand fixtures and integration tests for jobs, progress, candidate detail,
  mutations, validation failures, retries, and recovery without live-provider
  or local-credential dependencies. These tests must exercise the complete
  HTTP -> service -> temporary database path rather than only mocking the
  service called by each route.
- Remove or clearly disable the Phase 2 placeholder export command, which must
  not label research-run history as findings. Keep the validated public-release
  exporter as Phase 7 work unless an earlier internal export contract is
  deliberately specified and tested.

**Complete when:** the Phase 3 UI can rely solely on stable, durable API
contracts and fixtures.

**Completed 2026-09-13:** worker ownership uses persisted owner IDs with a
30-second heartbeat and conservative 180-second lease expiry. Startup and
recovery reclaim only demonstrably stale leases, so a live CLI or other API
process is not interrupted. Stop requests preserve terminal run status and
are idempotent. The Phase 2 placeholder export command is disabled until
Phase 7. Public route families now use versioned response models with
redacted list payloads, structured run/candidate details, consistent missing
resource errors, and server-side ISO3 gap previews with bounded batches and
explicit scope exclusions. Durable jobs, progress, attempts, retries, and
temporary-database HTTP integration coverage are included for the Phase 3
frontend contract.

### Step 3.4 — Tighten extraction scope and validation

**Decision required:** Phase 3 decision 3 fixes what the current finding model
may store before prompt, schema, and validation changes are implemented.

Treat extraction quality as an ongoing evaluated contract, not a one-off prompt
edit. Tighten the extraction prompt and add deterministic post-extraction guards
so unsupported values cannot be stored merely because the model placed a number
in a metric field.

- Store only observed national demographic measurements in the current finding
  model. Do not populate a metric from a forecast, projection, scenario,
  conditional estimate, or statement about a future year. Such figures may be
  retained in the prose summary with an explicit `projection` label, but they
  are not current observed findings. For example, a conditional forecast that
  Germany's population could shrink must not create an observed population
  datapoint.
- A population value must describe the total national resident population.
  Reject subsets and administrative categories, including people with a
  migration background, refugees, asylum seekers, visa holders, foreign-born
  residents, age groups, religious groups, and residents from a named origin.
  For example, a count of people with a migration background is not Germany's
  total national population.
- Reject currency amounts, budgets, costs, spending, percentages, percentage
  changes, and differences from the demographic metric value fields. For
  example, 24.8 billion euros of migration-related spending and its 3.2 billion
  euro decline must create no population or migration-count datapoint.
- Require each non-null metric to carry a short evidence excerpt, metric type,
  unit, observation/projection status, national-scope status, and measured
  period. Validate the numeric value against that evidence before storage.
- Add deterministic unit and context checks after model extraction: currency
  markers invalidate demographic counts; percent/rate language cannot populate
  absolute counts; future/scenario language invalidates observed metrics; and
  subset language invalidates total-population metrics. Ambiguous cases become
  `needs_review` rather than being stored automatically.
- Maintain a version set of positive and negative extraction examples and run
  it whenever the prompt, model, schema, or post-processing changes. Include the
  three Germany examples above as permanent negative regression cases, together
  with valid national population, births, deaths, fertility, natural-change,
  and net-migration examples.
- Record extraction-rule and prompt versions with each run/candidate so later
  prompt changes can be evaluated against earlier outcomes.

**Complete when:** the negative regression set cannot populate demographic
metrics, valid national observations still extract correctly, and ambiguous
claims are reviewable without being silently stored.

**Completed 2026-09-13:** the current finding model accepts observed national
measurements only. Projections and subset/category claims remain summary/audit
material, deterministic evidence and context guards prevent unsupported metric
storage, and ambiguous evidence is retained as `needs_review`. Extraction
prompt/rule version 3.4.0 is recorded with findings, candidates, runs, and
manual jobs. The versioned regression set includes the permanent Germany
negative cases and valid coverage for all supported demographic metrics.

### Step 3.5 — Rebuild manual webpage analysis

**Decisions required:** Phase 3 decisions 4 and 5 determine the workflow state
model and orchestration boundary before this screen is implemented.

- Accept and validate a URL. If the current free-form Gradio prompt is kept,
  extract exactly one HTTP(S) URL deterministically at the browser boundary;
  do not make the backend infer an unbounded natural-language request.
- Display fetch and extraction progress.
- Show structured metrics, source attribution, effective period, comments, and UN comparison.
- Allow the administrator to approve, edit, reject, rerun, remove, or suppress the source.

**Complete when:** the current Add Webpage workflow is fully available without Gradio.

**Completed 2026-09-13:** manual analysis now uses a direct FastAPI service
pipeline: one deterministic HTTP(S) URL is fetched, extracted, and optionally
compared with WPP, with durable progress and a reviewable analysis draft. A
finding is stored only after explicit approval; edits use optimistic revisions,
and reject/remove/suppress/rerun actions are durable and audited. The Jinja
review desk renders evidence, attribution, period, comments, and comparison
details in fixture or live API mode. The supported FastAPI path does not call
the LangGraph manual workflow.

### Step 3.6 — Rebuild research and country-hunt controls

- Preserve editable news categories, search depth, search window, candidate limits, and domain limits.
- Provide direct country search which always bypasses automatic recency/eligibility rules.
- Show the bulk-country queue, last attempt, last successful finding, next eligible date, and search outcome.
- Make it clear that scheduled/bulk discovery does not run the LLM comparison agent; it performs only the deterministic UN lookup.
- Show live job progress by polling durable job state.
- Rebuild gap preview and bounded country-batch selection from the Step 3.3
  API; show country labels beside ISO3 and make scope-mismatch exclusions
  visible in the candidate audit.
- Provide compact run history plus explicit run and candidate detail views,
  including progress events, decisions, recovery attempts, and errors.

**Complete when:** the current research controls and the new country scheduling state are operable locally.

**Completed 2026-09-13:** the FastAPI/Jinja research desk now saves and runs
editable discovery controls, supports direct and bounded bulk country hunts,
persists queue eligibility/outcomes, polls durable run state, and exposes gap,
run, and candidate audit details including scope mismatches and recovery errors.
Scheduled and bulk hunts retain the deterministic UN-only comparison behavior.

### Step 3.7 — Rebuild findings and administration

- Provide country and metric filters.
- Display the existing findings, source classifications, comparisons, and
  source URLs without changing their meaning.
- Support all current edit, metric-delete, record-delete, block, unblock, and
  rerun actions.
- Provide source-rule management: exact canonical URL blocks, domain blocks, and source classifications/official-publisher overrides.
- Keep every source-rule change auditable and reversible.
- Recreate the current database table, country-coverage summary, record
  picker, editable JSON review, and safe source link presentation from API
  responses rather than direct SQLite reads.

**Complete when:** every current finding and its administration controls work
through the new UI without Gradio.

**Completed 2026-09-13:** the API-backed findings workspace now provides
country/metric filtering, coverage counts, safe source links, editable record
JSON, metric and record removal, source blocking/unblocking, rerun eligibility,
and reversible source-rule administration with a durable audit trail.

### Step 3.8 — Rebuild the graphs and reporting

Graph markers:

- Diamond: official publisher
- Outlined circle: secondary publisher with a named source, including accepted fallback datapoints
- Cross: secondary publisher without a named source

Graph behaviour:

- Reproduce the current graph values, existing source classifications, and
  WPP reference series exactly.
- Refactor chart construction into a browser-side renderer that consumes the
  versioned graph-series response. Keep per-view hidden-finding choices as
  browser state; use stable finding IDs for mutations and reload the series
  after a durable change.
- Defer corroboration grouping, preferred claims, and conflict overlays to
  Phase 4.

**Complete when:** the new graphs reproduce current values and provenance
without relying on Gradio.

**Completed 2026-09-13:** browser-side responsive SVG graphs now consume the
versioned graph-series endpoint, preserve WPP historic/forecast and alternate
release values, expose the three required source markers, retain stable finding
IDs, and support browser-only per-country hiding with durable-change reloads.

### Step 3.9 — Cut over and retire Gradio

**Decision required:** Phase 3 decision 6 defines the cutover gate. Do not
remove the fallback until it passes, and do not retain Gradio after it passes.

- Keep Gradio available only as a temporary fallback and parity reference while
  Phase 3 is under construction; it is not an API implementation target and
  must not be used for concurrent discovery work alongside the new application.
- Use the Phase 2 fixtures and parity checks to verify each migrated workflow.
- Make the Jinja admin the default local application only after the full
  current workflow is available.
- After a rollback-capable cutover check, stop the Gradio entry point and remove
  its dependencies, callbacks, process-local worker state, and UI-specific
  adapters. From that point onward, all operational workflows use the FastAPI
  services, durable workers, and Jinja/browser interface.

**Complete when:** the local admin application runs entirely through the
FastAPI/Jinja interface, the old `main.py` Gradio entry point no longer runs,
and Gradio has been removed from the supported runtime.

### Step 3.10 — Simplify the post-Gradio codebase

**Decision checkpoint:** confirm the Step 3.5 LangGraph decision against the
implemented workflow and remove the dependency if no supported path uses it.

- Remove duplicated Gradio-era adapters and orchestration paths after parity is
  proven and the rollback window has closed.
- Review whether LangGraph still provides useful state/checkpoint behavior for
  the remaining manual-analysis workflow. If the durable service pipeline is
  clearer without it, replace the small fixed graph with ordinary service
  functions while preserving the evaluated extraction and comparison behavior.
- Replace the manual-analysis model tool loop with deterministic URL parsing and
  retrieval: select PDF/HTML handling in code, try Requests first, and use
  Playwright only when the direct request cannot provide usable content. The
  model receives the retrieved bounded text; it does not decide how to fetch it.
- Remove the unused model-bound SQL tools. Keep ISO3/year selection and WPP
  queries deterministic, and use the comparison model only to interpret the
  already retrieved research and WPP values.
- Rename `BossAgent` to a non-agentic name such as `DiscoveryCoordinator` or
  `ResearchPipeline`, unless it has acquired genuine dynamic planning behavior.
  Preserve its explicit budgets, stop checks, provider routing, fallback order,
  and audit trail as ordinary application control flow.
- Restrict model responsibilities to the tasks that benefit from semantic
  judgment: summary relevance, full-text relevance, structured evidence
  extraction, and comparison explanation. URL parsing, fetching, provider
  fallback, duplicate detection, country resolution, SQL selection, validation,
  storage, retries, limits, and job transitions remain deterministic code.
- Consolidate country-hunt settings, prompts, job status definitions, and
  response serialization so there is one implementation of each rule.
- Run the full regression and extraction-evaluation suites after simplification.

**Complete when:** the supported application has one frontend path, one durable
job path, one implementation of each business rule, no unused Gradio/LangGraph/
SQL-tool bindings, and no model-controlled loop where deterministic application
control flow is sufficient.


## Phase 4 — Add the claims and conflict framework

### Step 4.1 — Introduce source documents, claims, and observation groups

Build this alongside retained legacy findings rather than changing their
meaning in place:

- **Source document:** an article or webpage identified by canonical URL.
- **Metric claim:** a source's country, metric, period, value, unit, and
  definition.
- **Observation group:** claims for the same country, metric, period, unit,
  and definition.

Canonical URL uniqueness remains the only rule that rejects a source document
outright. Different URLs create additional documents and claims. Do not invent
missing legacy corroborating sources.

### Step 4.2 — Migrate legacy records and extend backend contracts

- Migrate retained Phase 1 findings without changing their source evidence.
- Associate new claims with observation groups.
- Retain materially different values as conflicts; harmless rounding and unit
  conversion are not conflicts.
- Keep WPP divergence separate from disagreement between article claims.
- Extend the FastAPI contracts without breaking the existing finding views.

### Step 4.3 — Add conflict review and provenance views

- Display corroborating sources under one observation.
- Provide a conflict queue with values, periods, definitions, and source
  classifications side by side.
- Allow preferred-claim selection, equivalent/rounded marking, separation of
  definitions, and claim rejection.
- Add graph controls for corroboration and unresolved conflicts.

### Step 4.4 — Verify claim and graph semantics

**Complete when:** every claim is traceable to a source document, legacy data
is preserved, and graphs distinguish corroboration, conflicts, and the WPP
reference series correctly.

## Phase 5 — Deploy the private admin application to Railway

### Step 5.1 — Provision and migrate the Postgres application database

- Define version-controlled database migrations.
- Create Postgres tables for users, sources, claims, observation groups, runs, candidates, country schedule state, submissions, blocks, settings, and audit events.
- Import the locally validated application data.
- Reconcile counts and sampled records after import.
- Keep WPP reference data outside Postgres initially.

**Complete when:** Postgres contains the verified application data and a repeatable migration can rebuild its schema.

### Step 5.2 — Package the filtered WPP database

- Generate `wpp_serving.sqlite` during a controlled build step.
- Package it read-only with the Railway API/worker image.
- Verify its WPP vintage and checksum at application startup.
- Fail clearly if the expected serving schema is missing or incompatible.

**Complete when:** Railway comparisons and graphs use the same immutable WPP values as local development.

### Step 5.3 — Deploy the API, admin frontend, and worker

- Deploy FastAPI and the admin frontend.
- Deploy research processing as a separate worker/service command.
- Configure health checks and safe deployment behaviour.
- Keep API keys and database credentials in Railway environment secrets.
- Restrict public network exposure to the endpoints that genuinely need it.

**Complete when:** the hosted admin application can perform every manual workflow through the Railway services.

### Step 5.4 — Add authentication, backups, and operational safeguards

- Use Google/OpenID Connect sign-in for the private admin application rather than storing administrator passwords ourselves.
- Store users, roles, active status, and the email allowlist in Postgres. The database is the source of authorization: initially the only required role is `admin`; a `contributor` role can be added later if needed.
- Seed your email as the first administrator during deployment. Admins can grant or revoke access by changing the database-backed user record.
- Require authentication for all admin actions and data-management endpoints. Public static pages require none.
- Use secure sessions, CSRF protection where applicable, and least-privilege database credentials.
- Configure automated Postgres backups and test a restore.
- Add structured logs, error reporting, timeouts, rate limits, and job concurrency locks.
- Record administrative changes in an audit log.

**Complete when:** unauthenticated users cannot scan, modify, export, or inspect private operational data and a recovery test has succeeded.

### Step 5.5 — Verify all manual workflows online

- Analyse a representative HTML article and PDF.
- Verify direct and fallback fetching.
- Edit, remove, rerun, suppress, and unblock sources.
- Review a simulated conflict.
- Draw every graph metric for representative countries.
- Confirm a Railway restart does not lose job or application state.

**Complete when:** Railway becomes the trusted admin environment and local Gradio is no longer needed for normal use.

## Phase 6 — Automate discovery and country coverage

### Step 6.1 — Automate the daily news search

- Run the approved news-search settings once per day.
- Keep the retrieval window aligned with the schedule so coverage neither overlaps excessively nor leaves gaps.
- Preserve candidate, domain, model, and timeout budgets.
- Prevent a second run when the previous run is still active.
- Notify the administrator only for failures, conflicts requiring review, or unusually high usage.

**Complete when:** daily news discovery runs unattended and every run has a visible status and cost record.

### Step 6.2 — Run the initial 20-country-per-day coverage sweep

- Freeze a canonical country list for the sweep.
- Search up to 20 not-yet-attempted countries per day.
- Mark a country attempted even when it produces no usable result.
- Never repeat a country during the initial sweep.
- Allow direct manual country hunts at any time; these do not disturb the sweep cursor.
- Stop the bootstrap mode once every canonical country has been attempted.

**Complete when:** every country has one recorded baseline attempt, expected to take approximately ten days.

### Step 6.3 — Introduce the steady-state country schedule

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

### Step 6.4 — Apply the tested fallback-provider rule to scheduled hunts

Use the Phase 1 fallback-provider rule in unattended country hunts:

- “No datapoints” means no stored article-derived datapoints for the country; the WPP reference series does not count.
- When a candidate is processed, check whether the country currently has any stored article-derived datapoint.
- If it has a datapoint, exclude configured fallback providers such as Statista and Our World in Data from this country-scan route.
- If it has no datapoints, allow a configured fallback provider when the candidate is from the previous three months, or when it is an enabled undated country-profile seed.
- Store the result using its actual source classification: named secondary as an outlined circle; unnamed secondary as a cross.
- Do not promote the fallback publisher to official status.
- If the fallback reproduces the same WPP dataset already used as the baseline, retain the source relationship but do not treat it as independent corroboration.
- If an official source is later found, keep the fallback as an additional source and prefer the official claim.

The result is intentionally dependent on the data already stored when the candidate is considered. This accepted trade-off keeps the rule easy to understand and operate.

**Complete when:** an otherwise empty country can receive a recent fallback datapoint with a visible source-priority marker, without using those providers for countries that already have findings.

### Step 6.5 — Add spend, run, and failure dashboards

- Record Tavily search and extraction credits by job and provider request.
- Record model, token, request, and estimated-cost usage where available.
- Display daily and monthly totals against configurable budgets.
- Add hard daily limits for country count, candidate processing, provider credits, and model work.
- Show failures, retries, blocked pages, no-result countries, and unresolved jobs.
- Allow scheduled automation to be paused without disabling direct manual analysis.

**Complete when:** scheduled work cannot silently exceed configured budgets and failures are visible without reading server logs.

## Phase 7 — Publish the database-free public site

### Step 7.1 — Build the static JSON exporter

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

### Step 7.2 — Build the read-only public frontend

- Load all content through static JSON files.
- Provide country navigation, graphs, source/article details, data freshness, WPP vintage, and methodology.
- Preserve source marker meanings and conflict indicators.
- Do not expose scan, edit, delete, moderation, or admin API controls.
- Add accessible chart summaries and useful metadata for search engines and link sharing.

**Complete when:** the public site displays the graphs and published articles without any live database or research access.

### Step 7.3 — Automate the weekly Vercel release

- Schedule one weekly export after normal discovery processing.
- Validate JSON schemas, required files, source URLs, and record counts.
- Deploy the validated static output to Vercel.
- Record the deployment URL and release ID in the application database.
- Keep the previous successful release available for rollback.
- Allow an administrator to trigger an exceptional manual release.

**Complete when:** the public site updates weekly without exposing Railway or requiring manual file movement.

### Step 7.4 — Verify publication, attribution, and rollback

- Compare a sample of public points with the admin values and their original sources.
- Confirm that unpublished or rejected records never enter the release.
- Verify WPP, source, and fallback-provider attribution and applicable reuse terms.
- Test mobile display, broken-link handling, accessibility, caching, and rollback.

**Complete when:** a release can be independently verified and safely rolled back.

## Phase 8 — Add public URL submissions

### Step 8.1 — Build the submission form

- Accept an HTTP/HTTPS URL and an optional short note.
- Store the URL as a pending submission only.
- Do not fetch, scan, or add statistics at submission time.
- Return a neutral acknowledgement without exposing internal job information.

**Complete when:** a public user can propose a source without triggering research or modifying published data.

### Step 8.2 — Build the admin moderation queue

- Show pending URLs, canonical duplicates, notes, submission time, and moderation status.
- Allow the administrator to analyse, approve for processing, reject, or suppress a URL.
- Route approved URLs through the same manual analysis and duplicate rules as admin-entered URLs.
- Record the moderation decision without exposing it publicly.

**Complete when:** no submitted URL reaches the research worker or findings database without an explicit admin action.

### Step 8.3 — Add submission security and abuse controls

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
