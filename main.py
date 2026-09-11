import json
from html import escape
from urllib.parse import urlsplit
from uuid import uuid4

import gradio as gr
import pandas as pd

from agents import graph
from research_ui import build_search_tabs
from tools import (
    PageAccessError, delete_webpage_finding, get_webpage_finding,
    list_country_names, list_webpage_findings, update_webpage_finding,
)
from visualisation import build_visualisation_for_latest_analysis


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
    yield None, "", None, "", "Starting analysis", gr.skip(), "\n".join(log_lines)
    result = None
    try:
        for mode, event in graph.stream(
            {"messages": [{"role": "user", "content": user_input}]},
            config={"configurable": {"thread_id": str(uuid4())}},
            stream_mode=["values", "custom"],
        ):
            if mode == "custom":
                if "log" in event:
                    log_lines.append(event["log"])
                if "fetch_status" in event:
                    yield gr.skip(), gr.skip(), gr.skip(), gr.skip(), event["fetch_status"], gr.skip(), "\n".join(log_lines)
                elif "log" in event:
                    yield gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), "\n".join(log_lines)
            elif mode == "values":
                result = event
    except PageAccessError as exc:
        log_lines.append(f"Failed: {exc}")
        yield None, str(exc), None, "", "Failed — Requests and Playwright could not access the page", gr.skip(), "\n".join(log_lines)
        return
    except Exception:
        log_lines.append("Analysis failed. See the terminal for the traceback.")
        yield None, "Analysis failed. Please try again.", None, "", "Analysis failed", gr.skip(), "\n".join(log_lines)
        raise

    if result is None:
        log_lines.append("No result was produced.")
        yield None, "No result was produced.", None, "", "Analysis failed", gr.skip(), "\n".join(log_lines)
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
    )


DATABASE_COLUMNS = [
    "ID", "Country", "Effective date", "Population", "TFR", "Source",
    "Official", "Added by",
]


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
            "TFR": finding.get("TFR", ""),
            "Source": finding.get("Source", ""),
            "Official": finding.get("Official source", ""),
            "Added by": added_by,
        })
    return rows


def load_database_table(findings=None):
    """Return a compact newest-first finding list; JSON stays in the detail pane."""
    findings = findings if findings is not None else list_webpage_findings()
    return pd.DataFrame(_database_display_rows(findings), columns=DATABASE_COLUMNS)


def database_choices(findings=None):
    findings = findings if findings is not None else list_webpage_findings()
    return [
        (f"#{finding['ID']} · {finding['Country'] or 'Unknown country'} · "
         f"{finding['Effective date'] or 'No date'} · {finding['Source'] or 'Unknown source'}", str(finding["ID"]))
        for finding in list_webpage_findings()
    ]


def refresh_database(revision: int):
    findings = list_webpage_findings()
    choices = database_choices(findings)
    return load_database_table(findings), revision + 1, gr.update(choices=choices, value=choices[0][1] if choices else None)


def load_database_record(finding_id: int):
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
        return load_database_table(), revision, "Select a record first.", gr.skip()
    try:
        update_webpage_finding(int(finding_id), finding_json)
    except ValueError as exc:
        return load_database_table(), revision, f"Could not save: {exc}", gr.skip()
    return load_database_table(), revision + 1, "Record saved.", gr.skip()


def remove_database_record(finding_id: int, revision: int):
    try:
        delete_webpage_finding(finding_id)
    except ValueError as exc:
        return load_database_table(), revision, gr.skip(), gr.skip(), f"Could not delete: {exc}", gr.skip()
    return load_database_table(), revision + 1, "", "", "Record deleted.", ""


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
                ]
                run_button.click(run_pipeline, inputs=prompt, outputs=outputs)
                prompt.submit(run_pipeline, inputs=prompt, outputs=outputs)

            build_search_tabs()

            with gr.Tab("Visualise figures"):
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
                draw_button = gr.Button("Draw charts", variant="primary")
                chart_status = gr.Markdown()
                population_chart = gr.Plot(label="Population")
                flows_chart = gr.Plot(label="Births, deaths, natural change and migration")
                draw_button.click(
                    build_visualisation_for_latest_analysis,
                    inputs=[visual_country, latest_analysis_country, selected_metrics],
                    outputs=[population_chart, flows_chart, chart_status],
                )

            with gr.Tab("Database") as database_tab:
                gr.Markdown("### Stored findings\nNewest first. Select any row to inspect or edit the complete record below.")
                with gr.Row(elem_classes="database-toolbar"):
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
                    delete_record = gr.Button("Delete selected record", variant="stop")
                database_status = gr.Markdown()
                database_tab.select(refresh_database, inputs=database_revision, outputs=[database_table, database_revision, record_picker])
                refresh_database_button.click(refresh_database, inputs=database_revision, outputs=[database_table, database_revision, record_picker])
                database_table.select(load_database_table_row, outputs=[selected_record_id, record_json, database_status, record_link, record_picker])
                record_picker.change(load_database_record, inputs=record_picker, outputs=[selected_record_id, record_json, database_status, record_link])
                save_record.click(save_database_record, inputs=[selected_record_id, record_json, database_revision], outputs=[database_table, database_revision, database_status, record_link])
                delete_record.click(remove_database_record, inputs=[selected_record_id, database_revision], outputs=[database_table, database_revision, selected_record_id, record_json, database_status, record_link])

    demo.launch(css=APP_CSS)
