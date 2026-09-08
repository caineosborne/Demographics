
import gradio as gr

from agents import graph


CONFIG = {"configurable": {"thread_id": "production-pipeline"}}


def run_pipeline(user_input: str):
    result = graph.invoke(
        {"messages": [{"role": "user", "content": user_input}]},
        config=CONFIG,
    )
    research = result["result"].model_dump()
    comparison = result["comparison"].model_dump()

    research_structured = {
        key: value
        for key, value in research.items()
        if key not in {"summary", "comments"}
    }
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

    return (
        research_structured,
        research_commentary,
        comparison_structured,
        comparison_commentary,
    )


if __name__ == "__main__":
    with gr.Blocks(title="Demographics Research Agent") as demo:
        gr.Markdown("# Demographics Research Agent")
        prompt = gr.Textbox(
            label="Request",
            placeholder="Please summarise a URL and compare it to expected UN figures.",
            lines=3,
        )
        run_button = gr.Button("Run analysis", variant="primary")

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
        ]
        run_button.click(run_pipeline, inputs=prompt, outputs=outputs)
        prompt.submit(run_pipeline, inputs=prompt, outputs=outputs)

    demo.launch()
