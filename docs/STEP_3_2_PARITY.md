# Step 3.2 service parity record

Date: 2026-09-13

This record compares the retained Gradio callbacks with the extracted service
and versioned API workflows. The comparisons are functional treatments using
local doubles and the temporary SQLite database; they do not call Tavily,
OpenRouter, or a live webpage. The test treatment is
`tests/test_step_3_2_parity.py`.

## Disposition key

- **Equivalent implementation**: the same business outcome is produced even
  if the transport or presentation differs.
- **Intentional improvement**: the API deliberately tightens or makes a
  boundary more durable/explicit; this is not a lost capability.
- **Regression to investigate**: a required business outcome is missing or
  incorrect. There are no open regressions in the cases below.

## Comparable workflows

| Workflow | Gradio reference | Service/API treatment | Disposition and meaningful difference |
| --- | --- | --- | --- |
| Manual webpage analysis | `main.run_pipeline` executes the compiled graph, streams fetch status, and renders extraction and UN comparison objects. | `research_services.start_manual_analysis` creates a durable job; its worker calls extraction and comparison services and exposes a compact JSON result. | **Equivalent implementation** for extraction, comparison, and storage outcomes. **Intentional improvement**: the API is asynchronous/restart-auditable and supports `compare=false`; it does not expose LangChain messages, page text, or Plotly/Gradio objects. |
| Automatic search | `research_ui.run_search` validates controls and starts the shared background `BossAgent` worker. Automatic discovery follows the no-comparison-agent policy. | `research_services.start_research` validates the same `SearchSettings` shape and starts the durable worker job. | **Equivalent implementation** for settings, discovery routing, and the automatic no-comparison policy. **Intentional improvement**: API job IDs/status are durable and JSON-only. |
| Single country hunt | `research_ui.country_hunt_settings` builds a one-country, one-year news search from a display country name. | `research_services.country_hunt_settings` builds the same bounded search from an ISO3 context. | **Equivalent implementation** for query intent, one-year window, result limits, and isolated settings. **Intentional improvement**: ISO3 is validated and retained as machine identity; labels remain display data. |
| Bulk country hunt | Gradio filters stale countries by display-name prefix and creates one category per selected country. | API accepts a bounded list of validated ISO3 codes and creates one category per country. | **Equivalent implementation** for bounded per-country search and run-wide budget. **Intentional improvement**: server-side ISO3 identity avoids ambiguous labels. Prefix selection remains a UI/API contract item for later Phase 3 work. |
| Finding edit | Gradio saves JSON through `tools.update_webpage_finding`, then refreshes tables. | API validates the request and geography ISO3, delegates to the same tool, and returns the stored finding. | **Equivalent implementation** for canonical URL, duplicate, and metric persistence checks. **Intentional improvement**: malformed/ambiguous geography is rejected at the API boundary; Gradio’s legacy free-form editor can reach the shared tool with less pre-validation. |
| Metric deletion | Gradio calls `delete_finding_metric`; the selected metric is removed while other metrics/source remain. | `admin_services.delete_metric` calls the same operation and returns a JSON mutation receipt. | **Equivalent implementation**; only status/presentation differs. |
| Remove and allow rerun | Gradio deletes the finding and the shared tool requests an automatic recheck. | API delete delegates to the same tool and exposes the auditable recheck/action records. | **Equivalent implementation**: finding is gone, URL is not blocked, and a later store is eligible. **Intentional improvement**: requested/consumed rechecks are explicit durable state rather than inferred from a missing row. |
| Delete and block / unblock | Gradio deletes, inserts the canonical URL in the block list, and can later unblock it. | API administration endpoints delegate to those same operations. | **Equivalent implementation** for suppression and restored eligibility; API returns canonical URL/status JSON. |
| Graph values | `visualisation.build_visualisation` renders Plotly traces, scaling WPP count columns from thousands to people and adding article points/hover metadata. | `read_services.graph_series` returns raw WPP rows and finding payloads for a renderer. | **Equivalent implementation after rendering**: the API supplies the same source rows and metric selection. **Intentional improvement**: raw, framework-independent values avoid coupling the service to Plotly. The API does not promise Gradio trace names, hover text, marker symbols, or scaled Plotly y-values. |

No treatment produced a required business-outcome regression. Exact wording,
stream timing, model prose, Plotly object shape, and HTTP status envelopes are
not parity criteria for this step; they are presentation/transport concerns.
