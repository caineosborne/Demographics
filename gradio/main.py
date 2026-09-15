import json
import sys
from html import escape
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

# Allow the legacy entry point to be run directly from this subdirectory while
# retaining the repository root as the import location for shared code.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gradio as gr
import pandas as pd

from core.agents import graph
from research_ui import build_search_tabs
from data import tools
from data.tools import (
    PageAccessError, delete_and_block_webpage_finding, delete_finding_metric, delete_webpage_finding, get_webpage_finding,
    list_blocked_sources, list_country_names, list_webpage_findings, unblock_source_url, update_webpage_finding,
)
from visualisation import (
    build_visualisation_for_latest_analysis, all_finding_choices, finding_choices, finding_link,
    refresh_visualisation_controls,
)


APP_CSS = """
html, body {
    overscroll-behavior-x: none;
}
.gradio-container, .gradio-container * {
    overscroll-behavior-x: contain;
}
.database-toolbar {
    align-items: end;
}
.database-table {
    margin-bottom: 0.35rem;
}
.database-table table td,
.database-table table th {
    padding-top: 0.4rem !important;
    padding-bottom: 0.4rem !important;
}
.database-record textarea {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace !important;
    font-size: 0.82rem !important;
    line-height: 1.35 !important;
}
.database-link a {
    display: inline-block;
    margin: 0.15rem 0 0.35rem;
    font-weight: 600;
}
.research-categories {
    border: 1px solid #e7e1d8;
    border-radius: 10px;
    overflow: hidden;
}
.research-categories table td,
.research-categories table th {
    padding: 0.35rem 0.45rem !important;
    vertical-align: top !important;
}
.research-candidates table td,
.research-candidates table th,
.research-runs table td,
.research-runs table th {
    padding: 0.35rem 0.45rem !important;
}
.record-actions {
    margin-top: 0.25rem;
}
"""


def run_pipeline(user_input: str):
    log_lines = ["Starting analysis"]
    full_llm_lines = []
    yield None, "", None, "", "Starting analysis", gr.skip(), "\n".join(log_lines), "\n".join(full_llm_lines)
    result = None
    def progress_callback(event):
        message = event.get("message") if isinstance(event, dict) else None
        if not message:
            return
        if event.get("type") == "llm_full":
            full_llm_lines.append(message)
        elif not log_lines or log_lines[-1] != message:
            log_lines.append(message)
    progress_token = tools.set_progress_callback(progress_callback)
    try:
        for mode, event in graph.stream(
            {"messages": [{"role": "user", "content": user_input}]},
            config={"configurable": {"thread_id": str(uuid4())}},
            stream_mode=["values", "custom"],
        ):
            if mode == "custom":
                if "log" in event:
                    if not log_lines or log_lines[-1] != event["log"]:
                        log_lines.append(event["log"])
                if "llm_full" in event:
                    full_llm_lines.append(event["llm_full"])
                if "fetch_status" in event:
                    yield gr.skip(), gr.skip(), gr.skip(), gr.skip(), event["fetch_status"], gr.skip(), "\n".join(log_lines), "\n".join(full_llm_lines)
                elif "log" in event:
                    yield gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), "\n".join(log_lines), "\n".join(full_llm_lines)
            elif mode == "values":
                result = event
    except PageAccessError as exc:
        log_lines.append(f"Failed: {exc}")
        yield None, str(exc), None, "", "Failed — Requests and Playwright could not access the page", gr.skip(), "\n".join(log_lines), "\n".join(full_llm_lines)
        return
    except Exception:
        log_lines.append("Analysis failed. See the terminal for the traceback.")
        yield None, "Analysis failed. Please try again.", None, "", "Analysis failed", gr.skip(), "\n".join(log_lines), "\n".join(full_llm_lines)
        raise
    finally:
        tools.reset_progress_callback(progress_token)

    if result is None:
        log_lines.append("No result was produced.")
        yield None, "No result was produced.", None, "", "Analysis failed", gr.skip(), "\n".join(log_lines), "\n".join(full_llm_lines)
        return
    research = result["result"].model_dump()
    comparison = result["comparison"].model_dump()

    research_structured = {
        key: value
        for key, value in research.items()
        if key not in {"summary", "comments"}
    }
    research_structured["storage"] = result.get("storage", {})
    research_commentary = research.get("summary") or "No summary was produced."
    if research.get("comments"):
        research_commentary += f"\n\nCaveats: {research['comments']}"

    comparison_structured = {
        key: value
        for key, value in comparison.items()
        if key not in {"overall_assessment", "notes"}
    }
    comparison_structured["sql_tool_data"] = result.get("un_data", [])
    comparison_commentary = comparison.get("overall_assessment") or "No comparison assessment was produced."
    if comparison.get("notes"):
        comparison_commentary += f"\n\nNotes: {comparison['notes']}"

    yield (
        research_structured,
        research_commentary,
        comparison_structured,
        comparison_commentary,
        "Complete",
        research.get("geography"),
        "\n".join([*log_lines, "Complete"]),
        "\n".join(full_llm_lines),
    )


DATABASE_COLUMNS = [
    "ID", "Country", "Effective date", "Population", "Births", "Deaths",
    "Natural change", "Net migration", "TFR", "Source", "Article", "Source status", "Added by",
]
DATABASE_SUMMARY_COLUMNS = [
    "Country", "Records",
    "Population records", "Population non-zero",
    "Births records", "Births non-zero",
    "Deaths records", "Deaths non-zero",
    "Natural change records", "Natural change non-zero",
    "Net migration records", "Net migration non-zero",
    "TFR records", "TFR non-zero",
]


def _finding_metric(finding, key):
    stats = finding.get("Extracted JSON")
    if isinstance(stats, str):
        try:
            stats = json.loads(stats)
        except json.JSONDecodeError:
            stats = {}
    stats = stats or {}
    name = "net_overseas_migration" if key == "Net migration" else key.casefold().replace(" ", "_")
    return ((stats.get("statistics") or {}).get(name) or {}).get("value", "")


def _database_display_rows(findings):
    rows = []
    for finding in findings:
        submission = finding.get("Submission type") or "Unknown"
        discovery = finding.get("Discovery source") or ""
        added_by = submission.title()
        if submission == "automatic" and discovery:
            added_by = f"Automatic · {discovery.title()}"
        rows.append({
            "ID": finding.get("ID", ""),
            "Country": finding.get("Country", ""),
            "Effective date": finding.get("Effective date", ""),
            "Population": finding.get("Population", ""),
            "Births": _finding_metric(finding, "Births"),
            "Deaths": _finding_metric(finding, "Deaths"),
            "Natural change": _finding_metric(finding, "Natural change"),
            "Net migration": _finding_metric(finding, "Net migration"),
            "TFR": finding.get("TFR", ""),
            "Source": finding.get("Source", ""),
            "Article": f'[Open article ↗]({finding.get("Webpage URL", "")})' if finding.get("Webpage URL") else "",
            "Source status": {
                "official_publisher": "Official publisher",
                "secondary_attributed": "Secondary · official source named",
                "secondary_unattributed": "Secondary · source not named",
                "legacy_unreviewed": "Legacy · unreviewed",
            }.get(finding.get("Source classification"), "Unknown"),
            "Added by": added_by,
        })
    return rows


def load_database_country_summary(findings=None):
    """Pivot stored findings by country and metric coverage."""
    findings = findings if findings is not None else list_webpage_findings()
    metrics = [
        ("Population", lambda f: f.get("Population")),
        ("Births", lambda f: _finding_metric(f, "Births")),
        ("Deaths", lambda f: _finding_metric(f, "Deaths")),
        ("Natural change", lambda f: _finding_metric(f, "Natural change")),
        ("Net migration", lambda f: _finding_metric(f, "Net migration")),
        ("TFR", lambda f: f.get("TFR")),
    ]
    grouped = {}
    for finding in findings:
        country = finding.get("Country") or "Unknown country"
        grouped.setdefault(country, []).append(finding)
    rows = []
    for country in sorted(grouped, key=str.casefold):
        records = grouped[country]
        row = {"Country": country, "Records": len(records)}
        for label, getter in metrics:
            values = [getter(finding) for finding in records]
            present = [value for value in values if value not in (None, "")]
            non_zero = []
            for value in present:
                try:
                    if float(value) != 0:
                        non_zero.append(value)
                except (TypeError, ValueError):
                    non_zero.append(value)
            row[f"{label} records"] = len(present)
            row[f"{label} non-zero"] = len(non_zero)
        rows.append(row)
    return pd.DataFrame(rows, columns=DATABASE_SUMMARY_COLUMNS)


def load_database_table(findings=None, country=None):
    """Return a compact newest-first finding list; JSON stays in the detail pane."""
    findings = findings if findings is not None else list_webpage_findings()
    if country and country != "All countries":
        findings = [finding for finding in findings if finding.get("Country") == country]
    return pd.DataFrame(_database_display_rows(findings), columns=DATABASE_COLUMNS)


def database_choices(findings=None):
    findings = findings if findings is not None else list_webpage_findings()
    return [
        (f"#{finding['ID']} · {finding['Country'] or 'Unknown country'} · "
         f"{finding['Effective date'] or 'No date'} · {finding['Source'] or 'Unknown source'}", str(finding["ID"]))
        for finding in findings
    ]


def refresh_database(revision: int, country=None):
    findings = list_webpage_findings()
    if country and country != "All countries":
        findings = [finding for finding in findings if finding.get("Country") == country]
    choices = database_choices(findings)
    return (load_database_table(findings), load_database_country_summary(findings), revision + 1,
            gr.update(choices=choices, value=choices[0][1] if choices else None))


def load_database_record(finding_id: int):
    if finding_id in (None, ""):
        return "", "", "Select a record to load.", ""
    try:
        finding = get_webpage_finding(int(finding_id))
        url = str(finding.get("url") or "")
        parsed = urlsplit(url)
        source_link = (
            f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener">Open source article ↗</a>'
            if parsed.scheme in {"http", "https"} and parsed.netloc else "No source URL recorded."
        )
        return str(finding_id), json.dumps(finding, ensure_ascii=False, indent=2), "Record loaded.", source_link
    except ValueError as exc:
        return "", "", str(exc), ""


def load_database_table_row(evt: gr.SelectData):
    """Open the selected compact table row in the stable detail pane."""
    try:
        finding_id = int(evt.row_value[0])
    except (AttributeError, IndexError, TypeError, ValueError):
        return "", "", "Could not determine the selected record.", "", gr.skip()
    loaded = load_database_record(finding_id)
    return (*loaded, gr.update(value=str(finding_id)))


def save_database_record(finding_id: str | None, finding_json: str, revision: int):
    if not finding_id:
        return load_database_table(), load_database_country_summary(), revision, "Select a record first.", gr.skip()
    try:
        update_webpage_finding(int(finding_id), finding_json)
    except ValueError as exc:
        return load_database_table(), load_database_country_summary(), revision, f"Could not save: {exc}", gr.skip()
    return load_database_table(), load_database_country_summary(), revision + 1, "Record saved.", gr.skip()


def remove_database_metric(finding_id: int, metric: str, revision: int):
    """Delete one selected metric and reload the editor from durable storage."""
    if finding_id in (None, ""):
        return (load_database_table(), load_database_country_summary(), revision,
                gr.skip(), gr.skip(), "Select a record first.", gr.skip())
    if not metric:
        return (load_database_table(), load_database_country_summary(), revision,
                gr.skip(), gr.skip(), "Select a metric to delete.", gr.skip())
    try:
        delete_finding_metric(int(finding_id), metric)
        loaded_id, finding_json, _loaded_status, source_link = load_database_record(int(finding_id))
    except (TypeError, ValueError) as exc:
        return (load_database_table(), load_database_country_summary(), revision,
                gr.skip(), gr.skip(), f"Could not delete datapoint: {exc}", gr.skip())
    return (
        load_database_table(), load_database_country_summary(), revision + 1,
        loaded_id, finding_json,
        f"Deleted {metric.replace('_', ' ')} from finding #{int(finding_id)}. Other metrics and the source URL were retained.",
        source_link,
    )


def remove_database_record(finding_id: int, revision: int):
    if finding_id in (None, ""):
        return load_database_table(), load_database_country_summary(), revision, gr.skip(), gr.skip(), "Select a record first.", gr.skip()
    try:
        delete_webpage_finding(finding_id)
    except ValueError as exc:
        return load_database_table(), load_database_country_summary(), revision, gr.skip(), gr.skip(), f"Could not delete: {exc}", gr.skip()
    return (load_database_table(), load_database_country_summary(), revision + 1, "", "",
            "Record removed. It can be manually resubmitted and will receive one automatic recheck if it is found again.", "")


def remove_database_record_and_block(finding_id: int, revision: int):
    if finding_id in (None, ""):
        return load_database_table(), load_database_country_summary(), revision, gr.skip(), gr.skip(), "Select a record first.", gr.skip()
    try:
        canonical_url = delete_and_block_webpage_finding(int(finding_id))
    except ValueError as exc:
        return load_database_table(), load_database_country_summary(), revision, gr.skip(), gr.skip(), f"Could not block: {exc}", gr.skip()
    return load_database_table(), load_database_country_summary(), revision + 1, "", "", f"Record deleted and source blocked: {canonical_url}", ""


def refresh_blocked_sources():
    choices = [
        (f"{row['canonical_url']} · blocked {row['blocked_at']}", row['canonical_url'])
        for row in list_blocked_sources()
    ]
    return gr.update(choices=choices, value=choices[0][1] if choices else None)


def unblock_selected_source(canonical_url: str | None):
    if not canonical_url:
        return refresh_blocked_sources(), "Select a blocked source first."
    try:
        unblocked = unblock_source_url(canonical_url)
    except ValueError as exc:
        return refresh_blocked_sources(), f"Could not unblock source: {exc}"
    return refresh_blocked_sources(), f"Source unblocked: {unblocked}"


def _visual_target(country, latest):
    return (country or latest or "").strip()


def select_visual_article(country, latest, finding_id):
    return finding_link(_visual_target(country, latest), finding_id)


def select_visual_point(evt: gr.SelectData):
    """Use a clicked Plotly point to select its stored finding."""
    value = getattr(evt, "value", None)
    if isinstance(value, dict):
        customdata = value.get("customdata")
    else:
        customdata = None
    if isinstance(customdata, (list, tuple)) and len(customdata) >= 5:
        return str(customdata[4])
    return gr.skip()


GRAPH_CHOICES = [(label, key) for key, (label, *_rest) in __import__("visualisation").METRICS.items()]


def refresh_simple_visualisation(country, latest, metrics, alternate_revisions=None):
    target = _visual_target(country, latest)
    charts = build_visualisation_for_latest_analysis(country, latest, metrics or [], hidden_by_metric={}, alternate_revisions=alternate_revisions or [])
    return charts[0], charts[1], gr.update(choices=all_finding_choices(target), value=None), gr.update(choices=[(label, key) for label, key in GRAPH_CHOICES if key in (metrics or [])], value=None), charts[2]


def hide_article_from_graphs(country, latest, metrics, finding_id, graph_targets, hidden_by_metric):
    hidden_by_metric = {key: list(values or []) for key, values in (hidden_by_metric or {}).items()}
    if finding_id:
        value = int(finding_id)
        for metric in (graph_targets or metrics or []):
            hidden_by_metric.setdefault(metric, [])
            if value not in hidden_by_metric[metric]:
                hidden_by_metric[metric].append(value)
    charts = build_visualisation_for_latest_analysis(country, latest, metrics or [], hidden_by_metric=hidden_by_metric)
    return charts[0], charts[1], hidden_by_metric, charts[2], "Article hidden from the selected graph(s)."


def delete_article_everywhere_simple(country, latest, metrics, finding_id, hidden_by_metric):
    if not finding_id:
        return gr.skip(), gr.skip(), gr.skip(), hidden_by_metric or {}, "Select an article first.", gr.skip()
    try:
        delete_webpage_finding(int(finding_id))
    except Exception as exc:
        return gr.skip(), gr.skip(), gr.skip(), hidden_by_metric or {}, f"Could not delete article: {exc}", gr.skip()
    charts = build_visualisation_for_latest_analysis(country, latest, metrics or [], hidden_by_metric=hidden_by_metric or {})
    target = _visual_target(country, latest)
    return charts[0], charts[1], gr.update(choices=all_finding_choices(target), value=None), hidden_by_metric or {}, charts[2], f"Article deleted from the database and graphs (finding #{int(finding_id)})."


def delete_and_block_article_everywhere_simple(country, latest, metrics, finding_id, hidden_by_metric):
    if not finding_id:
        return gr.skip(), gr.skip(), gr.skip(), hidden_by_metric or {}, "Select an article first.", gr.skip()
    try:
        canonical_url = delete_and_block_webpage_finding(int(finding_id))
    except Exception as exc:
        return gr.skip(), gr.skip(), gr.skip(), hidden_by_metric or {}, f"Could not delete and block article: {exc}", gr.skip()
    charts = build_visualisation_for_latest_analysis(country, latest, metrics or [], hidden_by_metric=hidden_by_metric or {})
    target = _visual_target(country, latest)
    return charts[0], charts[1], gr.update(choices=all_finding_choices(target), value=None), hidden_by_metric or {}, charts[2], f"Article deleted and blocked: {canonical_url}"


def delete_metric_from_database(country, latest, metrics, finding_id, metric, hidden_by_metric):
    """Delete one datapoint while retaining the article's other metrics."""
    if not finding_id:
        return gr.skip(), gr.skip(), gr.skip(), hidden_by_metric or {}, "Select an article first.", gr.skip()
    if not metric:
        return gr.skip(), gr.skip(), gr.skip(), hidden_by_metric or {}, "Select a metric to delete.", gr.skip()
    try:
        delete_finding_metric(int(finding_id), metric)
    except (TypeError, ValueError) as exc:
        return gr.skip(), gr.skip(), gr.skip(), hidden_by_metric or {}, f"Could not delete datapoint: {exc}", gr.skip()
    target = _visual_target(country, latest)
    charts = build_visualisation_for_latest_analysis(country, latest, metrics or [], hidden_by_metric=hidden_by_metric or {})
    return (charts[0], charts[1], gr.update(choices=all_finding_choices(target), value=None),
            hidden_by_metric or {}, charts[2],
            f"Deleted {metric.replace('_', ' ')} datapoint from finding #{int(finding_id)}; other metrics were retained.")


def hide_visual_article(country, latest, metrics, finding_id, hidden, other_hidden):
    target = _visual_target(country, latest)
    hidden = list(hidden or [])
    if finding_id:
        value = int(finding_id)
        if value not in hidden:
            hidden.append(value)
    charts = build_visualisation_for_latest_analysis(country, latest, metrics or [], hidden, other_hidden or [])
    pop_metrics = ["population"] if "population" in (metrics or []) else []
    flow_metrics = [m for m in (metrics or []) if m != "population"]
    return (*charts[:2], gr.update(choices=finding_choices(target, pop_metrics, hidden)), gr.update(choices=finding_choices(target, flow_metrics, other_hidden or [])), hidden, charts[2], "Article hidden in this graph. The stored finding remains available.")


def delete_visual_article(country, latest, metrics, finding_id, hidden, other_hidden):
    if not finding_id:
        return hide_visual_article(country, latest, metrics, finding_id, hidden, other_hidden)[:-1] + ("Select an article first.",)
    try:
        delete_webpage_finding(int(finding_id))
    except Exception as exc:
        return hide_visual_article(country, latest, metrics, None, hidden, other_hidden)[:-1] + (f"Could not delete article: {exc}",)
    charts = build_visualisation_for_latest_analysis(country, latest, metrics or [], hidden or [], other_hidden or [])
    target = _visual_target(country, latest)
    pop_metrics = ["population"] if "population" in (metrics or []) else []
    flow_metrics = [m for m in (metrics or []) if m != "population"]
    return (*charts[:2], gr.update(choices=finding_choices(target, pop_metrics, hidden or []), value=None), gr.update(choices=finding_choices(target, flow_metrics, other_hidden or []), value=None), hidden or [], charts[2], "Article deleted from the database and all graphs.")


def hide_flow_article(country, latest, metrics, finding_id, flow_hidden, population_hidden):
    result = hide_visual_article(country, latest, metrics, finding_id, flow_hidden, population_hidden)
    target = _visual_target(country, latest)
    pop_metrics = ["population"] if "population" in (metrics or []) else []
    flow_metrics = [m for m in (metrics or []) if m != "population"]
    return (*result[:2], gr.update(choices=finding_choices(target, pop_metrics, population_hidden or [])), gr.update(choices=finding_choices(target, flow_metrics, result[4])), result[4], result[5], result[6])


def delete_flow_article(country, latest, metrics, finding_id, flow_hidden, population_hidden):
    result = delete_visual_article(country, latest, metrics, finding_id, flow_hidden, population_hidden)
    target = _visual_target(country, latest)
    pop_metrics = ["population"] if "population" in (metrics or []) else []
    flow_metrics = [m for m in (metrics or []) if m != "population"]
    return (*result[:2], gr.update(choices=finding_choices(target, pop_metrics, population_hidden or [])), gr.update(choices=finding_choices(target, flow_metrics, result[4])), result[4], result[5], result[6])


if __name__ == "__main__":
    with gr.Blocks(title="Demographics Research Agent") as demo:
        gr.Markdown("# Demographics Research Agent")
        latest_analysis_country = gr.State()
        database_revision = gr.State(0)
        selected_record_id = gr.State()
        with gr.Tabs():
            with gr.Tab("Analyse webpage"):
                prompt = gr.Textbox(
                    label="Request",
                    placeholder="Please summarise a URL and compare it to expected UN figures.",
                    lines=3,
                )
                run_button = gr.Button("Run analysis", variant="primary")
                fetch_status = gr.Textbox(label="Debug status", value="Ready", interactive=False)
                with gr.Accordion("Run log", open=False):
                    run_log = gr.Textbox(label="Activity", lines=16, interactive=False)
                with gr.Accordion("Full LLM calls (diagnostics)", open=False):
                    full_llm_log = gr.Textbox(
                        label="Complete LLM request and response payloads",
                        lines=16, interactive=False,
                    )

                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### Data extraction — structured")
                        research_structured = gr.JSON(label="Extracted data")
                    with gr.Column():
                        gr.Markdown("### Data extraction — commentary")
                        research_commentary = gr.Markdown()

                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### UN comparison — structured")
                        comparison_structured = gr.JSON(label="Comparison values")
                    with gr.Column():
                        gr.Markdown("### UN comparison — commentary")
                        comparison_commentary = gr.Markdown()

                outputs = [
                    research_structured,
                    research_commentary,
                    comparison_structured,
                    comparison_commentary,
                    fetch_status,
                    latest_analysis_country,
                    run_log,
                    full_llm_log,
                ]
                run_button.click(run_pipeline, inputs=prompt, outputs=outputs)
                prompt.submit(run_pipeline, inputs=prompt, outputs=outputs)

            build_search_tabs()

            with gr.Tab("Visualise figures"):
                visual_hidden = gr.State({})
                visual_country = gr.Dropdown(
                    label="Country",
                    choices=list_country_names(),
                    filterable=True,
                    allow_custom_value=True,
                    info="Search the canonical UN country list. Leave blank to use the latest analysis.",
                )
                selected_metrics = gr.CheckboxGroup(
                    label="Metrics", value=["population", "births", "deaths", "natural_change", "net_migration", "total_fertility_rate"],
                    choices=[
                        ("Population", "population"), ("Births", "births"),
                        ("Deaths", "deaths"), ("Natural change", "natural_change"),
                        ("Net migration", "net_migration"),
                        ("Total fertility rate", "total_fertility_rate"),
                    ],
                )
                alternate_revisions = gr.CheckboxGroup(
                    label="Add alternate UN database histories",
                    choices=[("WPP 2022 — annual", 2022), ("WPP 2017 — five-year", 2017), ("WPP 2012 — five-year", 2012)],
                    value=[],
                    info="Optional dotted overlays. WPP 2024 remains the primary solid/dashed series and source for the rest of the app.",
                )
                draw_button = gr.Button("Draw charts", variant="primary")
                chart_status = gr.Markdown()
                population_chart = gr.Plot(label="Population")
                flows_chart = gr.Plot(label="Births, deaths, natural change and migration")
                with gr.Row():
                    article_picker = gr.Dropdown(label="Article", choices=[], allow_custom_value=False, scale=2)
                    graph_targets = gr.CheckboxGroup(label="Delete from graphs", choices=GRAPH_CHOICES, value=[key for _label, key in GRAPH_CHOICES], scale=3)
                    hide_article = gr.Button("Hide for selected graphs (this view)")
                    delete_everywhere = gr.Button("Remove and allow rerun")
                    delete_and_block_everywhere = gr.Button("Delete and block source", variant="stop")
                with gr.Row():
                    metric_to_delete = gr.Dropdown(
                        label="Delete one stored metric", choices=GRAPH_CHOICES,
                        info="This removes only the selected datapoint and redraws the charts.", scale=2,
                    )
                    delete_metric_everywhere = gr.Button("Delete selected metric")
                article_link = gr.HTML(value="Select an article to open its source.")
                draw_button.click(
                    refresh_simple_visualisation,
                    inputs=[visual_country, latest_analysis_country, selected_metrics, alternate_revisions],
                    outputs=[population_chart, flows_chart, article_picker, graph_targets, chart_status],
                )
                article_picker.change(select_visual_article, inputs=[visual_country, latest_analysis_country, article_picker], outputs=article_link)
                hide_article.click(hide_article_from_graphs, inputs=[visual_country, latest_analysis_country, selected_metrics, article_picker, graph_targets, visual_hidden], outputs=[population_chart, flows_chart, visual_hidden, chart_status, article_link])
                delete_everywhere.click(delete_article_everywhere_simple, inputs=[visual_country, latest_analysis_country, selected_metrics, article_picker, visual_hidden], outputs=[population_chart, flows_chart, article_picker, visual_hidden, chart_status, article_link])
                delete_and_block_everywhere.click(delete_and_block_article_everywhere_simple, inputs=[visual_country, latest_analysis_country, selected_metrics, article_picker, visual_hidden], outputs=[population_chart, flows_chart, article_picker, visual_hidden, chart_status, article_link])
                delete_metric_everywhere.click(delete_metric_from_database, inputs=[visual_country, latest_analysis_country, selected_metrics, article_picker, metric_to_delete, visual_hidden], outputs=[population_chart, flows_chart, article_picker, visual_hidden, chart_status, article_link])

            with gr.Tab("Database") as database_tab:
                gr.Markdown("### Stored findings\nNewest first. Select any row to inspect or edit the complete record below.")
                with gr.Row(elem_classes="database-toolbar"):
                    database_country = gr.Dropdown(label="Filter by country", choices=["All countries", *list_country_names()], value="All countries", scale=3)
                    record_picker = gr.Dropdown(label="Open a record", choices=[], scale=5)
                    refresh_database_button = gr.Button("Refresh", scale=1, min_width=100)
                database_table = gr.Dataframe(
                    headers=DATABASE_COLUMNS,
                    show_label=False,
                    interactive=False,
                    wrap=False,
                    line_breaks=False,
                    max_height=280,
                    max_chars=80,
                    column_widths=[60, 170, 120, 130, 75, 190, 80, 150],
                    pinned_columns=1,
                    show_search="filter",
                    buttons=[],
                    datatype=["number", "str", "str", "number", "number", "number", "number", "number", "number", "str", "markdown", "str", "str"],
                    elem_classes="database-table",
                )
                gr.Markdown("### Country coverage\nCounts show records containing a value; non-zero counts exclude blank and zero values. A country filter above also filters this summary.")
                database_summary = gr.Dataframe(
                    headers=DATABASE_SUMMARY_COLUMNS,
                    value=load_database_country_summary(),
                    show_label=False, interactive=False, wrap=False, line_breaks=False,
                    max_height=260, pinned_columns=1, show_search="filter", buttons=[],
                    elem_classes="database-table",
                )
                record_link = gr.HTML(elem_classes="database-link")
                with gr.Accordion("Advanced: editable JSON", open=False):
                    record_json = gr.Textbox(
                        label="Selected record — editable JSON", lines=10, max_lines=16,
                        elem_classes="database-record",
                    )
                with gr.Row(elem_classes="record-actions"):
                    save_record = gr.Button("Save changes", variant="primary")
                    metric_to_delete_record = gr.Dropdown(label="Metric", choices=GRAPH_CHOICES, scale=2)
                    delete_metric_record = gr.Button("Delete selected metric")
                    delete_record = gr.Button("Remove and allow rerun")
                    delete_and_block_record = gr.Button("Delete and block source", variant="stop")
                database_status = gr.Markdown()
                gr.Markdown("### Blocked sources")
                with gr.Row(elem_classes="record-actions"):
                    blocked_source_picker = gr.Dropdown(label="Suppressed canonical URL", choices=[], scale=5)
                    unblock_source = gr.Button("Unblock source")
                blocked_source_status = gr.Markdown()
                database_tab.select(refresh_database, inputs=[database_revision, database_country], outputs=[database_table, database_summary, database_revision, record_picker])
                database_tab.select(refresh_blocked_sources, outputs=blocked_source_picker)
                refresh_database_button.click(refresh_database, inputs=[database_revision, database_country], outputs=[database_table, database_summary, database_revision, record_picker])
                database_country.change(refresh_database, inputs=[database_revision, database_country], outputs=[database_table, database_summary, database_revision, record_picker])
                database_table.select(load_database_table_row, outputs=[selected_record_id, record_json, database_status, record_link, record_picker])
                record_picker.change(load_database_record, inputs=record_picker, outputs=[selected_record_id, record_json, database_status, record_link])
                save_record.click(save_database_record, inputs=[selected_record_id, record_json, database_revision], outputs=[database_table, database_summary, database_revision, database_status, record_link])
                delete_metric_record.click(remove_database_metric, inputs=[selected_record_id, metric_to_delete_record, database_revision], outputs=[database_table, database_summary, database_revision, selected_record_id, record_json, database_status, record_link])
                delete_record.click(remove_database_record, inputs=[selected_record_id, database_revision], outputs=[database_table, database_summary, database_revision, selected_record_id, record_json, database_status, record_link])
                delete_and_block_record.click(remove_database_record_and_block, inputs=[selected_record_id, database_revision], outputs=[database_table, database_summary, database_revision, selected_record_id, record_json, database_status, record_link])
                unblock_source.click(unblock_selected_source, inputs=blocked_source_picker, outputs=[blocked_source_picker, blocked_source_status])

    demo.launch(css=APP_CSS)
