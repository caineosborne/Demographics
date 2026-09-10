import json
from html import escape
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


def load_database_table():
    """Return a fresh, readable dump of the persistent webpage findings."""
    return pd.DataFrame(list_webpage_findings())


def refresh_database(revision: int):
    return load_database_table(), revision + 1


def load_database_record(finding_id: int):
    try:
        return str(finding_id), json.dumps(get_webpage_finding(finding_id), ensure_ascii=False, indent=2), "Record loaded."
    except ValueError as exc:
        return "", "", str(exc)


def save_database_record(finding_id: str | None, finding_json: str, revision: int):
    if not finding_id:
        return load_database_table(), revision, "Select Edit beside a record first."
    try:
        update_webpage_finding(int(finding_id), finding_json)
    except ValueError as exc:
        return load_database_table(), revision, f"Could not save: {exc}"
    return load_database_table(), revision + 1, "Record saved."


def remove_database_record(finding_id: int, revision: int):
    try:
        delete_webpage_finding(finding_id)
    except ValueError as exc:
        return load_database_table(), revision, gr.skip(), gr.skip(), f"Could not delete: {exc}"
    return load_database_table(), revision + 1, "", "", "Record deleted."


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
                    label="Metrics", value=list(["population", "births", "deaths", "natural_change", "net_migration"]),
                    choices=[
                        ("Population", "population"), ("Births", "births"),
                        ("Deaths", "deaths"), ("Natural change", "natural_change"),
                        ("Net migration", "net_migration"),
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
                gr.Markdown("### Stored webpage findings")
                refresh_database_button = gr.Button("Refresh database")
                database_table = gr.Dataframe(
                    label="Sorted by country, then effective date",
                    interactive=False,
                    wrap=True,
                )
                record_json = gr.Textbox(label="Record JSON", lines=18)
                save_record = gr.Button("Save edits", variant="primary")
                database_status = gr.Markdown()
                database_tab.select(refresh_database, inputs=database_revision, outputs=[database_table, database_revision])
                refresh_database_button.click(refresh_database, inputs=database_revision, outputs=[database_table, database_revision])
                save_record.click(save_database_record, inputs=[selected_record_id, record_json, database_revision], outputs=[database_table, database_revision, database_status])

                @gr.render(inputs=database_revision)
                def render_database_actions(_revision):
                    for finding in list_webpage_findings():
                        with gr.Row():
                            with gr.Column(scale=1):
                                edit_button = gr.Button("Edit", size="sm")
                                delete_button = gr.Button("Delete", variant="stop", size="sm")
                            with gr.Column(scale=8):
                                country = escape(str(finding["Country"]))
                                effective_date = escape(str(finding["Effective date"] or "No effective date"))
                                source = escape(str(finding["Source"] or "Unknown source"))
                                url = escape(str(finding["Webpage URL"] or "No webpage URL"))
                                gr.Markdown(f"**{country} — {effective_date}**  \n{source}  \n`ID {finding['ID']}` · {url}")
                        finding_id = finding["ID"]
                        edit_button.click(
                            lambda record_id=finding_id: load_database_record(record_id),
                            outputs=[selected_record_id, record_json, database_status],
                        )
                        delete_button.click(
                            lambda revision, record_id=finding_id: remove_database_record(record_id, revision),
                            inputs=database_revision,
                            outputs=[database_table, database_revision, selected_record_id, record_json, database_status],
                        )

    demo.launch()
