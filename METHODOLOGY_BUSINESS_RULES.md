# Research Methodology and Business Rules

This document describes the Phase 1 research pipeline as implemented in the
current Gradio application. It is the operational rulebook for deciding when a
URL is processed, stored, skipped, suppressed, or allowed to run again.

## 1. Two research entry points

The pipeline has two distinct entry points:

| Entry point | Purpose | Comparison agent | Duplicate check |
|---|---|---:|---|
| Manual URL submission | An administrator asks the tool to analyse a specific webpage | Yes, for an eligible new URL | Yes, before retrieval when the URL is explicitly present in the submission |
| Bulk/automatic search | News, Reddit, country-hunt, or other provider discovery | No separate comparison run | Yes, after discovery is recorded but before review, retrieval, extraction, or comparison |

The duplicate rule is therefore not limited to bulk search. It also applies to
manually submitted URLs.

## 2. URL identity

The system keeps two URL values:

- `source_url`: the original URL supplied by the user or provider. This is
  retained as evidence and is the URL shown to the reviewer.
- `canonical_url`: the normalized URL used only for identity and deduplication.

Canonicalization applies the following deterministic rules:

1. Only HTTP and HTTPS URLs are accepted; credentials, missing hosts, and other
   schemes are rejected.
2. The host is lowercased and a leading `www.` is removed.
3. The scheme is stored as `https`.
4. Fragments are removed.
5. A trailing slash is removed, except for a root path.
6. `utm_*`, `fbclid`, and `gclid` tracking parameters are removed.
7. Remaining query parameters are retained and sorted, so their order does not
   create a second identity.

The system does not follow redirects or decide that two different publisher
URLs describe the same article. Different canonical URLs remain different
URLs.

Examples:

```text
HTTP://www.example.com/report/?b=2&utm_source=news&a=1#chart
https://example.com/report?a=1&b=2
```

These are the same canonical URL.

```text
https://example.com/report?edition=mobile
https://example.com/report?edition=print
```

These remain different canonical URLs.

## 3. Manual URL methodology

When an administrator submits a message containing an explicit HTTP(S) URL:

1. The URL is canonicalized without asking a model to identify it.
2. The suppression list is checked.
3. If the canonical URL is suppressed, the submission is excluded before page
   retrieval.
4. If the canonical URL already belongs to an active finding, the existing
   finding is shown and the URL is excluded before retrieval and extraction.
5. The comparison agent is not rerun for an excluded duplicate or suppressed
   URL.
6. A new, eligible URL proceeds through page retrieval, structured extraction,
   storage, and the manual UN comparison flow.

Therefore, duplicate detection does work for manually submitted links. A
manual submission of an HTTP/HTTPS URL variant of an existing finding does not
download or reanalyse the page.

Removing a finding is the **Remove and allow rerun** control. It permits a
manual resubmission immediately and records an automatic recheck for the
canonical URL. The next automatic discovery of a URL that was previously
loaded may pass the historical-loaded check. If that page loads, the ordinary
duplicate rule resumes; if it does not load, later discovery remains eligible
under the ordinary failed-load rule. Suppression must be removed first; a
recheck never overrides the block list.

If a message does not contain an explicit URL, the system cannot perform this
deterministic pre-check at submission time. In that case the normal model-led
manual flow applies, and the storage layer still enforces the duplicate rules
before saving.

## 4. Bulk and automatic search methodology

Bulk search deliberately records discoveries in the candidate audit so the
administrator can see what was found, including duplicates. Recording a
candidate is not the same as processing its webpage.

For each discovered candidate:

1. The candidate is recorded with its provider and discovery metadata.
2. Its URL is canonicalized.
3. A suppressed canonical URL is marked `excluded_blocked_source` and is not
   fetched.
4. A URL already present in the active findings database is marked `duplicate`
   with the existing finding ID and is not sent to summary review, retrieval,
   extraction, Tavily extraction, or comparison.
5. A URL whose page content successfully loaded in an earlier search-result
   batch is marked `duplicate`, even if the loaded page was later marked
   irrelevant or was never saved as a finding. It points to the earlier
   candidate and is not sent to summary review, retrieval, extraction, Tavily
   extraction, or comparison, unless an administrator has recorded an active
   automatic recheck for that canonical URL. A summary-only,
   excluded-before-load, or failed candidate remains eligible for a later
   retry.
6. A URL already seen earlier in the same run is marked `duplicate` and points
   to the earlier candidate. It is not processed twice.
7. Only a new, eligible URL can proceed to discovery screening, publisher
   limits, budget limits, summary review, retrieval, extraction, and storage.

The URL is reserved as soon as it is seen in the run. Earlier search runs are
treated as prior sightings only after a page was successfully loaded. This
means same-run URL variants cannot be processed twice, while a prior failed
or never-loaded result does not permanently block a legitimate retry.

Bulk research stores eligible extracted findings but does not run the separate
manual comparison agent. WPP remains the graph reference series.

## 5. Fallback providers and explicit search configuration

Statista and Our World in Data are fallback providers, not special search
providers. The application never creates a Statista/OWID-only news or country
hunt. A result from either domain is accepted only when it is naturally
returned by normal discovery (or an administrator explicitly places the domain
in a search category's Include domains control), has a usable publication date
within the configured limit, and the country has no article datapoint acquired
in the preceding configured gap period.

An undated country profile from an enabled fallback provider may instead be
stored as a secondary seed only when the country is blank in that same gap
window. This is how an OWID country profile can establish an initial Sweden
datapoint; it does not make undated fallback pages generally eligible.

The Include domains control remains an administrator choice. It is not blocked
for fallback domains. Configuring such a domain does not make it official or
independent corroboration.

DataReportal is excluded before model review: its digital reports commonly
repeat an external population headline rather than provide a demographic source
or national release. This is a configured discovery-quality decision, not a
claim that every non-official publisher is unusable.

When an inaccessible automatic result is recovered through an alternative
page, that alternative passes the same blocked-URL, source-rule, duplicate,
low-value-domain, stale-result, and geography checks before it is fetched. Its
own publication date—not the inaccessible result's date—governs any fallback
provider eligibility. Facebook and other excluded sources are therefore never
retrieved merely because they appear as an alternative.

## 6. Storage-time duplicate rules

The canonical URL check is the primary URL identity rule and is enforced by a
unique canonical URL index on `webpage_findings`.

The existing effective-date plus population-value duplicate rule remains in
place as a separate storage-time safeguard. It is checked after the canonical
URL check. This means a different URL can still be rejected if it reports the
same effective date and population value under the existing rule.

This rule is intentionally unchanged in Phase 1.

## 7. Removal, suppression, and unblocking

| Action | Result | Future URL eligibility |
|---|---|---|
| Remove and allow rerun | Removes the active finding, records `removed_allow_rerun`, and requests an automatic recheck | Eligible for manual resubmission; a historical-loaded automatic result may retry |
| Delete and block source | Removes the finding, adds its canonical URL to `blocked_sources`, and records `removed_and_suppressed` | Rejected before retrieval |
| Unblock source | Removes the canonical URL from `blocked_sources`, records `unblocked`, and requests an automatic recheck | Eligible again; no finding is recreated automatically |

The blocked-source list is exact and canonical-URL based. It is not a broad
domain block list. The compact `automatic_rechecks` audit records requested,
consumed, loaded, and cancelled states without altering old candidate history.
Successful candidate loads are preserved by a small `page_loaded` audit marker;
legacy audit rows with retained `full_text` remain recognised until the audit
cleanup removes that content.

## 8. Source classes, period evidence, and legacy records

New findings receive their source class at storage time. Updating a source rule
only affects subsequent findings; it does not silently rewrite existing or
legacy records. A deliberate bulk reclassification tool belongs to Phase 2.

Every useful numeric national metric is retained, even when it is not directly
comparable with annual WPP values. Explicit daily, monthly, quarterly,
multi-month, or dated count flows are annualised deterministically and retain
the original value, cadence, factor, and note. Population stocks, fertility
rates, and unclear-duration partial figures remain as reported and carry a
not-comparable-to-annual-WPP explanation instead.

The metric must still represent the national total or total national flow.
Asylum/visa/refugee applications, migration or population subgroups, and
birth/death subsets by cause, age, origin, religion, or programme are not
stored as national demographic metrics. This rule is applied per metric, not
by broadly blocking institutions such as CDC that can also publish valid total
national figures.

## 9. Legacy migration behavior

Before the canonical URL unique index was created, existing findings were
backfilled. One legacy canonical collision was found in the live database:

- The newest extracted record remains active.
- The older record was moved to `finding_legacy_duplicates`.
- No legacy finding JSON was silently discarded.

The dated pre-migration backup remains the full rollback source.

## 10. Audit and review statuses

Duplicates remain visible in the research audit. Typical statuses include:

- `duplicate`: active finding or earlier candidate uses the same canonical URL.
- `excluded_blocked_source`: the canonical URL is suppressed.
- `excluded_duplicate_url`: storage or manual handling found an existing URL.
- `excluded_duplicate_report`: the unchanged date+population safeguard matched.
- `stored`: a new finding was saved.

The audit keeps the original URL, canonical URL, reason, and related finding or
candidate ID so the reviewer can distinguish a duplicate from a retrieval or
model failure.
