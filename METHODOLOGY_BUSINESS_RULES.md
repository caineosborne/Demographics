# Research Methodology and Business Rules

## Process at a glance

Country hunt and manual URL analysis use the same evidence principles. The automatic hunt follows this order:

1. **Choose scope and search type.** Select a country for search context and use **General web** by default (or News when the user selects it).
2. **Search Tavily.** Request up to the chosen number of recent, advanced-search results using a demographic query for the selected country.
3. **Create an audit candidate for every result.** Record the title, URL, snippet, source, search context, and current status before deciding whether to process it.
4. **Apply deterministic early exclusions.** Do not fetch invalid, blocked, exact-duplicate, or over-budget candidates.
5. **Batch-review search results.** The review model returns **relevant**, **irrelevant**, or **unclear** for each snippet. Irrelevant results stop here; relevant and unclear results continue.
6. **Retrieve the page.** Prefer Tavily's extracted page text when available; otherwise fetch the source directly. For an inaccessible source, look once for up to three eligible alternative sources.
7. **Review the retrieved page.** Record a full-text relevant/irrelevant/unclear assessment. This is an audit signal, not a second automatic exclusion gate.
8. **Extract structured demographic facts.** The extraction model identifies the country from page evidence, then extracts the newest observed national value for each supported metric.
9. **Retry once only when needed.** A partial, unclear, or internally inconsistent extraction gets one corrective extraction retry. A useful first answer is retained if the retry returns nothing useful.
10. **Validate, normalise, and compare.** Check the evidence, remove invalid metrics, make eligible time-flow conversions, and compare comparable bulk metrics with UN WPP data.
11. **Store only confirmed findings.** A candidate receives a tick / `complete` status only after its finding has been stored in the database. No data, exclusions, errors, duplicates, and timeouts are not ticks.
12. **Keep a durable audit trail.** Log every material stage, decision, duration, stop, error, timeout, source URL, extraction payload, and storage outcome so the run can be reviewed afterwards.

## 1. Entry points

| Entry point | Starting input | Main use | UN comparison |
|---|---|---|---|
| Country hunt | Country + General/News search choice | Find candidate webpages | Yes, for comparable automatic findings |
| Bulk country hunt | Multiple countries + General/News choice | Queue several country hunts | Yes, for comparable automatic findings |
| Manual URL analysis | A specific HTTP(S) URL | Check a known page | Optional/manual flow |
| Other automatic search | Saved search categories, Reddit, or provider results | Broad discovery | Yes, for comparable automatic findings |

- The country selected for a hunt is **search and audit context only**.
- The extraction model assigns `geography` and `geography_iso3` from the page's own evidence. A UK result found during an Iceland hunt is therefore allowed to be allocated to the UK.
- A country hunt defaults to **General web**, because it is intended to find strong web results, including official statistical releases. News remains an explicit user choice.
- Default country-hunt settings use an advanced Tavily search, a one-year time range, and up to 12 results (within the configured 1–20 limit).

## 2. Candidate creation and status meaning

- Every discovery result becomes a candidate before review or retrieval.
- Candidate cards show a question mark while they are in progress.
- A tick means `complete`: the finding was successfully stored and has a database finding ID.
- A cross means the candidate did not become a stored finding, including: `excluded_summary`, `excluded_no_data`, `excluded_un_bounds`, `excluded_blocked_source`, source-rule exclusions, invalid geography, duplicates, and errors.
- Access-blocked candidates remain visible for review rather than being treated as a successful finding.
- The audit stores the outcome even when the page is not fetched. This makes it possible to distinguish “not searched”, “rejected at summary”, “could not be accessed”, “extracted no data”, and “stored”.

## 3. URL identity, blocking, and duplicates

### 3.1 URL values

- `source_url` is the original URL supplied by the search provider or user. It is retained as evidence and shown to the reviewer.
- `canonical_url` is a normalised URL used for blocking, audit, and recheck decisions.
- Canonicalisation:
  - accepts HTTP(S) URLs only;
  - rejects credentials, missing hosts, and other schemes;
  - lowercases the host and removes a leading `www.`;
  - stores HTTPS;
  - removes fragments and non-root trailing slashes;
  - removes `utm_*`, `fbclid`, and `gclid` parameters;
  - retains and sorts other query parameters.

### 3.2 Duplicate rules

- The active-findings database has a unique constraint on the exact trimmed `source_url`.
- A canonical URL is not, by itself, a storage duplicate. URL variants, mirrors, and syndications are separate evidence unless their exact source URL matches.
- In a single run, an exact source URL is reserved when first seen. A later identical result becomes `duplicate` and is not processed twice.
- Across earlier automatic runs, an exact source URL is a duplicate only after its page was successfully loaded. A result rejected before loading or one that failed to load remains eligible for a later attempt.
- Exact duplicates are not sent to summary review, page retrieval, extraction, or UN comparison.

### 3.3 Reviewer controls

| Action | Immediate result | Later eligibility |
|---|---|---|
| Remove and allow rerun | Removes the active finding and records an automatic recheck | Can be manually resubmitted; a previously loaded automatic URL can retry once |
| Delete and block source | Removes the finding and adds canonical URL to `blocked_sources` | Excluded before retrieval |
| Unblock source | Removes the block and records an automatic recheck | Eligible again |

- Blocking is canonical-URL specific, not a domain-wide block.
- A recheck never overrides an active block.

## 4. Search and summary review

- Search discovery is deliberately broad. Publisher, title, snippet, country context, and date heuristics are advisory warnings rather than automatic exclusions.
- The batch review model receives at most 20 candidate snippets in one structured call.
- The review model is normally `deepseek/deepseek-v4.1-flash:nitro`.
- The summary decision meanings are:
  - **irrelevant** — clearly not a plausible national demographic source; candidate becomes `excluded_summary` and is not fetched.
  - **relevant** — plausible demographic evidence; candidate is fetched.
  - **unclear** — insufficient snippet evidence but plausible; candidate is fetched rather than discarded.
- If batch review fails or times out, candidates continue to retrieval with an audit warning. Discovery should not silently lose potentially useful pages.

## 5. Retrieval and source recovery

- Tavily article extraction is used first for Tavily search results when it is available.
- Otherwise the system fetches the discovered page directly.
- On a page access failure:
  - record the original error and set `searching_alternative`;
  - search once for alternatives;
  - attempt no more than three eligible alternatives;
  - apply normal URL, block, duplicate, source-rule, and basic quality checks to each alternative before fetching it;
  - record every attempted alternative and its result.
- If no page can be retrieved, preserve the candidate as `relevant_access_blocked` or `unclear_access_blocked` according to its summary decision. It is not stored and is not marked complete.
- A page that loads successfully is marked `page_loaded`, which enables reliable duplicate handling in later automatic runs.

## 6. Full-text review and structured extraction

- The retrieved text is compacted to a bounded, auditable model input.
- A full-text review records whether the loaded page appears relevant, irrelevant, or unclear.
- A non-relevant full-text decision is retained as a warning; the article still proceeds to structured extraction. This prevents an advisory review from hiding a real demographic figure.
- The extraction model is normally `deepseek/deepseek-v4-flash-0731`.
- The structured output contains one `RelevantResult` object with:
  - page identity and source details;
  - article-derived country and ISO3 code;
  - source classification and quoted authority where applicable;
  - supported demographic metrics and their evidence;
  - prompt and rule versions for audit.

### 6.1 Metric extraction rules

- Supported metrics:
  - population;
  - births;
  - deaths;
  - natural change;
  - migration arrivals;
  - migration departures;
  - net overseas migration;
  - total fertility rate.
- Extract the **most recent non-forecast historical figure per metric**.
- “Most recent” means the latest measured period, not the publication date and not the latest year merely mentioned in an article.
- A provisional or estimated historical value is valid; a forecast, projection, scenario, or future-period value is not.
- Different metrics may be retained from the same page. For example, a page can yield population, births, deaths, migration, and fertility, but it has only one selected value for each individual metric.
- Each populated metric must include:
  - `value` as a base-unit count for count metrics (for example `58.943 million` becomes `58,943,000`);
  - `source_value` as the number displayed by the publisher and `unit` retaining its stated magnitude;
  - a short evidence excerpt containing the number;
  - metric type and unit;
  - observation status;
  - whole-country scope status;
  - measured period; and
  - available publication date, period start/end, and cadence.
- Do not extract percentages, rates, qualitative claims, currency values, subnational figures, population subgroups, bilateral migration flows, or demographic subsets as national totals.
- Do not make a page official merely because it quotes an official body. `official_source=true` means that the publishing page itself is the producing authority; otherwise the authority is retained as `quoted_source`.

### 6.2 Corrective retry

- The first extraction is low effort for speed.
- Exactly one medium-effort retry is allowed when the first answer:
  - returns no structured result;
  - has a demographic summary without populated metrics;
  - has numeric-looking evidence but no usable numeric value; or
  - has useful data but validation remains partial or unclear.
- The retry is corrective, not a second independent extraction pass.
- If a retry returns no useful data, the useful first result is retained.
- Numeric validation accepts common grouping formats, including commas, spaces, and narrow no-break spaces, to avoid unnecessary retries for figures such as `11 424 031`.

## 7. Validation, country allocation, and UN comparison

### 7.1 Validation and normalisation

- Validate each metric against its own evidence excerpt.
- Remove metrics with a wrong metric type, rate/percentage in a count field, future/projection context, non-national scope, invalid observation status, currency context, or an evidence-value mismatch.
- Retain useful valid metrics when another metric on the same page fails.
- Annualise explicit daily, monthly, quarterly, multi-month, or dated flows only where deterministic rules permit it. Preserve the original value, cadence, conversion factor, and note.
- Population stocks, fertility rates, and unclear-duration partial figures remain as reported and carry a non-comparable explanation when appropriate.

### 7.2 Country allocation

- Resolve the country from the extracted page evidence, not from the country selected for the hunt.
- If the page supplies a valid, unique UN country, allocate the finding to that country and retain the hunt country as search context.
- Exclude invalid, subnational, or non-unique geography rather than guessing an ISO3 code.

### 7.3 UN WPP comparison for automatic findings

- Apply the automatic UN bound only to population, births, and deaths where the source and UN series are like-for-like national totals.
- A bounded value more than 50% above or below the UN reference produces `excluded_un_bounds`. Exactly 50% difference is allowed.
- Immigration arrivals and emigration departures remain valid extracted metrics, but are not bound-tested: the local UN WPP data provides net migration, not comparable gross arrivals/departures.
- Natural change, net migration, and fertility remain visible in the comparison audit but do not independently reject an automatic finding.
- If no comparison is available, retain that as an audit caveat; it is not an automatic exclusion.
- The comparison applies after extraction and validation. It is not the reason to wait for an extraction or to defer a no-data result.

## 8. Storage and final outcomes

- A finding is stored only when it has at least one useful numeric demographic datapoint and clears the applicable automatic checks.
- `complete` is assigned only after storage returns a finding ID.
- `excluded_no_data` means that the page may have been relevant, but it contained no usable numeric demographic datapoint. It is never a tick and is not stored.
- `excluded_un_bounds` means population, births, or deaths exceeded the 50% UN bound. It is retained in the candidate audit but not stored as an automatic finding.
- `needs_review_extraction` means extraction evidence or scope requires a human decision before storage.
- `duplicate` points to the related candidate or database finding.
- `error` means the candidate did not finish normally; it is distinct from an exclusion and is retained with its error message.

## 9. Logging, stop handling, and timeouts

- The run log records:
  - search configuration, search results, and candidate IDs;
  - summary and full-text decisions;
  - retrieval transport, alternative-source attempts, and timings;
  - structured LLM requests and outputs in a collapsible log group;
  - extraction outcome, prompt/rule versions, validation, comparison, storage result, and final candidate status;
  - run totals and final run state.
- Search-result and candidate URLs are retained as links in the log and audit so each decision can be checked against its source.
- Stops are checked between material stages. A stopped run is marked `interrupted`; already completed findings and audit rows remain saved.
- Page access timeouts are logged as `candidate_timeout` and the candidate is retained for review rather than being mistaken for a successful result.
- Each LLM stage has an application-level wall-clock limit of 120 seconds by default (`LLM_TIMEOUT_SECONDS`, minimum 10 seconds). This covers summary review, full-text review, extraction, retry, and web retrieval.
- On an LLM timeout, the log records `llm_timeout` with stage, model, and elapsed time. The provider call is detached and the research pipeline continues to the next safe outcome instead of waiting indefinitely.
- Provider-side HTTP timeouts remain configured as an additional safeguard, but the application-level guard is the final limit because provider streams can otherwise remain open beyond their nominal timeout.
- While a research run is active, the browser refreshes the durable job state every five seconds for up to 15 minutes. It displays the elapsed time and current log count even when the active LLM call has not yet produced a new event. A browser polling limit never stops the backend worker.

## 10. Source policy and legacy records

- Official publishers, secondary publishers, Statista, Our World in Data, DataReportal, and other sources can be discovered and processed unless a configured source rule excludes them.
- Fallback-provider rules match a whole enabled domain, not a fixed list of URLs. They currently cover `ourworldindata.org` and `statista.com`.
- Automatic fallback pages are stored only when the allocated country has no recent article-derived datapoint in the configured 90-day gap window and the page is recent (90 days). An undated OWID country profile may seed an empty country gap.
- An automatic OWID page attributed to United Nations / World Population Prospects is `excluded_un_derived_source`: it repeats the same UN baseline rather than adding independent evidence. Manual analysis remains available when a reviewer deliberately wants to inspect such a page.
- The fallback-domain rule is an admission rule, not a crawler. It applies to pages Tavily discovers; it does not enumerate or crawl every page on OWID or Statista.
- Source class, attribution, scope, publication date, and staleness remain visible review metadata; they are not default storage vetoes.
- An include-domains choice constrains discovery only. It does not make a source official or independently corroborated.
- New source classifications are recorded at storage time. Changing a source rule affects later findings and does not silently rewrite existing records.
- Historic canonical collisions were preserved: the newest extracted record remains active and older collisions were moved to `finding_legacy_duplicates`. No legacy finding JSON was silently discarded.
