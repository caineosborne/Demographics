"""Functional treatment comparisons for Step 3.2.

The Gradio callbacks remain the reference treatment. These tests deliberately
use local doubles for model/provider work and exercise the extracted service
boundaries without making Gradio an HTTP client.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import admin_services
import main
from services import read_services, research_services
import research_ui
from data import tools
import visualisation


class _Model:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, **_kwargs):
        return self.payload


class Step32ParityTests(unittest.TestCase):
    def test_manual_gradio_and_service_both_produce_extraction_and_comparison(self):
        extraction = {"title": "Japan population release", "geography": "Japan"}
        comparison = {"overall_assessment": "Comparable", "notes": None}
        gradio_result = {
            "result": _Model({**extraction, "summary": "A release."}),
            "comparison": _Model(comparison), "un_data": [{"year": 2025}],
            "storage": {"status": "stored", "id": 7},
        }
        with patch.object(main.graph, "stream", return_value=[("values", gradio_result)]):
            gradio_final = list(main.run_pipeline("Analyse https://example.test/release"))[-1]

        job_id = "manual-parity-job"
        research_services._jobs[job_id] = {
            "id": job_id, "kind": "manual_analysis", "status": "queued",
            "logs": [], "created_at": "now", "updated_at": "now",
        }
        try:
            with patch.object(research_services.tools, "set_progress_callback", return_value="token"), \
                 patch.object(research_services.tools, "reset_progress_callback"), \
                 patch.object(research_services.research_store, "update_job"), \
                 patch.object(research_services.agents, "research_agent", return_value={
                     "result": _Model({**extraction, "summary": "A release."}),
                     "storage": {"status": "stored", "id": 7},
                 }), \
                 patch.object(research_services.agents, "compare_to_un", return_value={
                     "comparison": _Model(comparison), "un_data": [{"year": 2025}],
                 }) as compare:
                research_services._run_manual(job_id, "https://example.test/release", None, True)
            service_result = research_services._jobs[job_id]["result"]
        finally:
            research_services._jobs.pop(job_id, None)

        self.assertEqual(gradio_final[0]["title"], service_result["result"]["title"])
        self.assertIn(service_result["comparison"]["overall_assessment"], gradio_final[3])
        self.assertEqual(service_result["storage"]["status"], "stored")
        compare.assert_called_once()

    def test_manual_service_can_skip_comparison_while_gradio_reference_always_compares(self):
        job_id = "manual-no-compare-job"
        research_services._jobs[job_id] = {
            "id": job_id, "kind": "manual_analysis", "status": "queued",
            "logs": [], "created_at": "now", "updated_at": "now",
        }
        try:
            with patch.object(research_services.tools, "set_progress_callback", return_value="token"), \
                 patch.object(research_services.tools, "reset_progress_callback"), \
                 patch.object(research_services.research_store, "update_job"), \
                 patch.object(research_services.agents, "research_agent", return_value={"result": _Model({})}), \
                 patch.object(research_services.agents, "compare_to_un") as compare:
                research_services._run_manual(job_id, "https://example.test/release", None, False)
            result = research_services._jobs[job_id]["result"]
        finally:
            research_services._jobs.pop(job_id, None)
        self.assertIsNone(result["comparison"])
        self.assertEqual(result["un_data"], [])
        compare.assert_not_called()

    def test_automatic_gradio_and_api_accept_the_same_search_settings_without_comparison(self):
        settings = research_ui.parse_settings(
            [[True, "Population", "national population", 3, "basic", "", ""]],
            "news", "day", False, 1, 10, 2, research_ui.CRITERIA,
        )
        with patch.object(research_ui, "start_search_settings", return_value=("", [], "run-1", "done")) as start:
            research_ui.run_search(
                [[True, "Population", "national population", 3, "basic", "", ""]],
                "news", "day", False, 1, 10, 2, research_ui.CRITERIA,
            )
        start.assert_called_once_with(settings)

        with patch.object(research_services, "_start_research", return_value={"run_id": "run-1"}) as api_start:
            result = research_services.start_research(settings)
        self.assertEqual(result["run_id"], "run-1")
        api_start.assert_called_once_with(settings)

    def test_country_hunt_settings_preserve_gradio_intent_and_add_iso3_identity(self):
        gradio = research_ui.country_hunt_settings("Japan")
        api = research_services.country_hunt_settings({"iso3": "JPN", "label": "Japan"})
        # The API country hunt deliberately defaults to general web search.
        # The legacy Gradio route remains untouched.
        self.assertEqual(api.categories[0].topic, "general")
        self.assertEqual(gradio["categories"][0]["time_range"], api.categories[0].time_range)
        self.assertEqual(gradio["categories"][0]["max_results"], api.categories[0].max_results)
        self.assertIn("Japan", api.categories[0].query)
        self.assertEqual(api.categories[0].country_iso3, "JPN")

    def test_country_hunt_uses_a_small_known_alias_set_for_canonical_labels(self):
        api = research_services.country_hunt_settings({"iso3": "RUS", "label": "Russian Federation"})
        self.assertIn('"Russian Federation" OR "Russia"', api.categories[0].query)
        self.assertNotIn('Soviet', api.categories[0].query)

    def test_bulk_hunt_is_bounded_per_country_in_both_treatments(self):
        with patch.object(research_ui, "countries_missing_recent_data", return_value=["Japan", "Australia"]):
            gradio, countries = research_ui.bulk_country_hunt_settings("J", 31, 2, 1)
        api = research_services.start_bulk_country_hunt
        with patch.object(research_services, "_start_research", return_value={"run_id": "run-2"}):
            result = api(["JPN", "AUS"], max_results=5)
        self.assertEqual(countries, ["Japan", "Australia"])
        self.assertEqual(len(gradio["categories"]), 2)
        self.assertEqual(result["country_iso3s"], ["JPN", "AUS"])

    def test_edit_delete_metric_block_and_rerun_have_the_same_storage_outcomes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, "DB_PATH", Path(directory) / "db.sqlite"):
            finding = {
                "url": "http://www.example.test/report?utm_source=parity",
                "geography": "Japan", "effective_date": "2026-06-30",
                "statistics": {"population": {"value": 123}, "births": {"value": 4}},
            }
            stored = tools.store_webpage_finding(finding)
            edited = admin_services.update_finding(stored["id"], {
                **finding, "geography_iso3": "JPN", "quoted_source": "Edited"
            })
            self.assertEqual(edited["quoted_source"], "Edited")
            admin_services.delete_metric(stored["id"], "births")
            self.assertNotIn("births", tools.get_webpage_finding(stored["id"])["statistics"])

            # The Gradio remove action and API action both use the same
            # canonical storage primitive and therefore the same rerun state.
            main.remove_database_record(stored["id"], 0)
            self.assertEqual(tools.store_webpage_finding(finding)["status"], "stored")
            blocked_finding = {**finding, "url": "https://example.test/blocked", "effective_date": "2026-07-01"}
            second = tools.store_webpage_finding(blocked_finding)
            blocked = admin_services.delete_and_block(second["id"])
            self.assertEqual(tools.store_webpage_finding(blocked_finding)["status"], "excluded_blocked_source")
            admin_services.unblock_source(blocked["canonical_url"])
            self.assertEqual(tools.store_webpage_finding(blocked_finding)["status"], "stored")

    def test_api_graph_rows_match_gradio_values_after_renderer_scaling(self):
        rows = [{"Year": 2023, "Population 1 Jul": 100.0}]
        with patch.object(visualisation, "_un_rows", return_value=(rows, [])), \
             patch.object(visualisation, "_release_rows", return_value={}), \
             patch.object(visualisation, "_stored_findings", return_value=[]), \
             patch.object(visualisation, "normalise_country_name", return_value="Japan"):
            population, _, _ = visualisation.build_visualisation("Japan", ["population"])
        gradio_y = next(trace for trace in population.data if trace.name == "UN historic").y[0]

        with patch.object(read_services, "resolve_country_iso3", return_value="JPN"), \
             patch.object(read_services, "normalise_country_name", return_value="Japan"), \
             patch.object(read_services, "_un_series", return_value=(rows, [])), \
             patch.object(read_services, "_release_series", return_value={}), \
             patch.object(read_services, "_finding_graph_rows", return_value=[]):
            api_rows = read_services.graph_series("JPN", ["population"])["historic"]
        self.assertEqual(api_rows[0]["Population 1 Jul"] * 1000, gradio_y)


if __name__ == "__main__":
    unittest.main()
