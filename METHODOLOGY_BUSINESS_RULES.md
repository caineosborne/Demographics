# Research Methodology and Business Rules

This is a permission-first evidence collection system. Its purpose is a broad
scan of news and web information that can crowdsource population and demographic
observations. Findings are expected to be imperfect; quality signals are kept
for review instead of becoming unnecessary rejection gates.

## 1. Two research entry points

The pipeline has two distinct entry points:

| Entry point | Purpose | Comparison agent | Duplicate check |
|---|---|---:|---|
| Manual URL submission | An administrator asks the tool to analyse a specific webpage | Yes when requested | Exact URL only |
| Bulk/automatic search | News, Reddit, country-hunt, or other provider discovery | Yes, against local WPP data | Exact URL only |

The duplicate rule is therefore not limited to bulk search. It also applies to
manually submitted URLs.

## 2. URL identity

The system keeps two URL values:

- `source_url`: the original URL supplied by the user or provider. This is
  retained as evidence and is the URL shown to the reviewer.
- `canonical_url`: the normalized URL used for audit and explicit suppression.

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

Duplicate identity uses the exact trimmed `source_url`. Canonical variants,
mirrors, syndications, and different publisher URLs are separate evidence.

Examples:

```text
HTTP://www.example.com/report/?b=2&utm_source=news&a=1#chart
https://example.com/report?a=1&b=2
```

These have the same canonical form but are not storage duplicates.

```text
https://example.com/report?edition=mobile
https://example.com/report?edition=print
```

These remain different canonical URLs.

## 3. Manual URL methodology

When an administrator submits a message containing an explicit HTTP(S) URL:

1. The URL is canonicalized without asking a model to identify it.
2. The suppression list is checked.
3. If the URL is explicitly suppressed, the submission is excluded before retrieval.
4. If the exact submitted URL already belongs to an active finding, the existing
   finding is shown and the URL is excluded before retrieval and extraction.
5. The comparison agent is not rerun for an excluded duplicate or suppressed
   URL.
6. A new, eligible URL proceeds through page retrieval, structured extraction,
   storage, and the manual UN comparison flow.

Manual fields such as `unit`, `observation_status`, and
`national_scope_status` are optional. A unit such as `people` may be inferred
when obvious, but it is not required for approval. Deterministic validation
issues are warnings when at least one numeric demographic metric is present.

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
4. An exact URL already present in the active findings database is marked `duplicate`
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
7. Discovery heuristics, publisher mix, summary review, and full-text review are
   advisory. They are recorded as warnings and do not prevent extraction.
8. A comparable numeric metric more than 25% above or below its UN WPP value
   excludes the bulk finding. Exactly 25% is allowed.
9. If no UN comparison is available, the article is admitted by default and
   the missing comparison is retained as an audit caveat.

The URL is reserved as soon as it is seen in the run. Earlier search runs are
treated as prior sightings only after a page was successfully loaded. This
means the exact same URL is processed once, while URL variants and a prior
failed or never-loaded result remain eligible.

## 5. Fallback providers and explicit search configuration

Statista, Our World in Data, DataReportal, and other secondary providers may be
stored when returned by normal discovery. Source class, attribution, scope,
publication date, and staleness remain visible metadata; none is a default
storage veto.

The Include domains control remains an administrator choice. It is not blocked
for fallback domains. Configuring such a domain does not make it official or
independent corroboration.

When an inaccessible automatic result is recovered through an alternative
page, that alternative passes the same blocked-URL, source-rule, duplicate,
and basic article-quality checks before it is fetched.

## 6. Storage-time duplicate rules

Only an exact `source_url` match is a duplicate, enforced by a unique index on
`webpage_findings.source_url`. The same date and population value at a different
URL is allowed because independent and syndicated reports are useful evidence.

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

- `duplicate`: active finding or earlier candidate uses the same exact source URL.
- `excluded_blocked_source`: the canonical URL is suppressed.
- `excluded_duplicate_url`: storage or manual handling found an existing URL.
- `stored`: a new finding was saved.

The audit keeps the original URL, canonical URL, reason, and related finding or
candidate ID so the reviewer can distinguish a duplicate from a retrieval or
model failure.
