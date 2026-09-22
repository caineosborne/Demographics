import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import AuthContext, Settings, create_app, get_settings
from services import read_services
from services.read_services import graph_series


class ApiBoundaryTests(unittest.TestCase):
    def test_admin_shell_is_jinja_rendered_and_exposes_required_navigation(self):
        client = TestClient(create_app(Settings(environment="test")))

        response = client.get("/admin/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('<meta name="api-version" content="v1"', response.text)
        for label in (
            "Overview", "Analyse webpage", "Research", "Country coverage",
            "Findings", "Conflicts", "Submissions", "Exports", "Settings",
        ):
            self.assertIn(f'>{label}</a>', response.text)
        self.assertIn("data-state=\"loading\"", response.text)
        self.assertIn("data-state=\"empty\"", response.text)

    def test_admin_shell_assets_and_fixtures_are_served(self):
        client = TestClient(create_app())

        script = client.get("/admin-assets/api-client.js")
        styles = client.get("/admin-assets/admin.css")
        countries = client.get("/fixtures/api/countries.json")
        health_fixture = client.get("/fixtures/api/health.json")

        self.assertEqual(script.status_code, 200)
        self.assertIn("createApiClient", script.text)
        self.assertIn("pollJob", script.text)
        self.assertEqual(styles.status_code, 200)
        self.assertIn("data-state=", styles.text)
        self.assertEqual(countries.status_code, 200)
        self.assertEqual(countries.json()["items"][0]["iso3"], "JPN")
        self.assertEqual(health_fixture.json()["environment"], "fixture")

    def test_admin_fixture_client_routes_analysis_and_research_job_polls(self):
        script = TestClient(create_app()).get("/admin-assets/admin.js").text

        self.assertIn("analysis\\/jobs|research\\/jobs", script)
        self.assertIn("/fixtures/api/worker-job.json", script)
        self.assertIn("No fixture registered for", script)

    def test_local_frontend_is_served_without_an_authentication_flow(self):
        client = TestClient(create_app())

        page = client.get("/")
        script = client.get("/assets/app.js")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Route console", page.text)
        self.assertNotIn("login", page.text.casefold())
        self.assertEqual(script.status_code, 200)

    def test_health_endpoint_returns_typed_boundary_response(self):
        client = TestClient(create_app(Settings(environment="test")))

        response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "status": "ok",
            "service": "demographics-agent",
            "version": "v1",
            "environment": "test",
        })

    def test_authentication_hook_is_injectable(self):
        calls = []

        def auth_hook():
            calls.append(True)
            return AuthContext(subject="test-user")

        response = TestClient(create_app(auth_dependency=auth_hook)).get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [True])

    def test_settings_can_be_loaded_from_environment(self):
        get_settings.cache_clear()
        with patch.dict(os.environ, {
            "DEMOGRAPHICS_APP_NAME": "test-api",
            "DEMOGRAPHICS_API_VERSION": "v9",
            "DEMOGRAPHICS_ENVIRONMENT": "ci",
        }):
            settings = get_settings()

        self.assertEqual(settings, Settings(app_name="test-api", api_version="v9", environment="ci"))
        get_settings.cache_clear()

    def test_read_endpoints_serialize_service_results(self):
        with patch("api.read_services.list_country_choices", return_value=[{"name": "Japan", "iso3": "JPN"}]), \
             patch("api.read_services.list_findings", return_value=[{"ID": 7, "Country": "Japan"}]), \
             patch("api.read_services.list_run_history", return_value=[{"id": "run-1", "status": "complete"}]), \
             patch("api.read_services.list_candidate_history", return_value=[{"id": 3, "run_id": "run-1", "status": "complete"}]):
            client = TestClient(create_app())

            self.assertEqual(client.get("/api/v1/countries").json(), {
                "items": [{"name": "Japan", "iso3": "JPN"}],
            })
            self.assertEqual(client.get("/api/v1/findings?iso3=JPN").json(), {
                "items": [{"ID": 7, "Country": "Japan"}],
            })
            self.assertEqual(client.get("/api/v1/research/runs").json(), {
                "items": [{"id": "run-1", "status": "complete"}],
            })
            self.assertEqual(client.get("/api/v1/research/candidates?run_id=run-1").json(), {
                "items": [{"id": 3, "run_id": "run-1", "status": "complete"}],
            })

    def test_graph_endpoint_preserves_raw_series_and_query_selection(self):
        series = {
            "country": "Japan",
            "iso3": "JPN",
            "metrics": ["population"],
            "historic": [{"Year": 2023, "Population 1 Jul": 123.0}],
            "forecast": [{"Year": 2024, "Population 1 Jul": 124.0}],
            "alternate_releases": {"2022": [{"Year": 2023, "Population 1 Jul": 120.0}]},
            "findings": [{"id": 7, "effective_date": "2023-07-01", "finding": {}}],
        }
        with patch("api.read_services.graph_series", return_value=series) as service:
            response = TestClient(create_app()).get(
                "/api/v1/graph-series/JPN?metrics=population&revisions=2022"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["historic"], series["historic"])
        service.assert_called_once_with("JPN", ["population"], [2022])

    def test_graph_service_returns_a_framework_independent_contract(self):
        with patch("services.read_services.normalise_country_name", return_value="Japan"), \
             patch("services.read_services.resolve_country_iso3", return_value="JPN"), \
             patch("services.read_services._un_series", return_value=([{"Year": 2023}], [{"Year": 2024}])), \
             patch("services.read_services._release_series", return_value={}), \
             patch("services.read_services._finding_graph_rows", return_value=[]):
            result = graph_series("JPN", ["population"])

        self.assertEqual(result["country"], "Japan")
        self.assertEqual(result["metrics"], ["population"])
        self.assertEqual(result["historic"], [{"Year": 2023}])
        self.assertNotIn("gradio", read_services.__dict__)

    def test_administration_routes_delegate_validated_mutations(self):
        with patch("api.admin_services.update_finding", return_value={"url": "https://example.test"}) as update, \
             patch("api.admin_services.delete_metric", return_value={"status": "deleted"}) as metric, \
             patch("api.admin_services.delete_and_block", return_value={"status": "deleted_and_blocked"}) as block, \
             patch("api.admin_services.unblock_source", return_value={"status": "unblocked"}) as unblock:
            client = TestClient(create_app())
            updated = client.put("/api/v1/admin/findings/7", json={"finding": {"url": "https://example.test"}})
            removed_metric = client.post("/api/v1/admin/findings/7/delete-metric", json={"metric": "births"})
            blocked = client.post("/api/v1/admin/findings/7/delete-and-block")
            restored = client.post("/api/v1/admin/blocked-sources/unblock", json={"url": "https://example.test"})

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(removed_metric.status_code, 200)
        self.assertEqual(blocked.status_code, 200)
        self.assertEqual(restored.status_code, 200)
        update.assert_called_once_with(7, {"url": "https://example.test"})
        metric.assert_called_once_with(7, "births")
        block.assert_called_once_with(7)
        unblock.assert_called_once_with("https://example.test")

    def test_administration_validation_returns_bad_request(self):
        with patch("api.admin_services.delete_metric", side_effect=ValueError("Unknown finding metric: nope.")):
            response = TestClient(create_app()).post(
                "/api/v1/admin/findings/7/delete-metric", json={"metric": "nope"}
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown finding metric", response.json()["detail"])

    def test_analysis_and_research_job_routes_delegate_to_services(self):
        with patch("api.research_services.start_manual_analysis", return_value={"id": "job-1", "status": "queued"}) as analysis, \
             patch("api.research_services.get_manual_analysis", return_value={"id": "job-1", "status": "complete"}), \
             patch("api.research_services.start_research", return_value={"run_id": "run-1", "status": "running"}) as research, \
             patch("api.research_services.start_country_hunt", return_value={"run_id": "run-2", "status": "running"}) as hunt, \
             patch("api.research_services.start_bulk_country_hunt", return_value={"run_id": "run-3", "status": "running"}) as bulk, \
             patch("api.research_services.get_research_run", return_value={"id": "run-1", "status": "complete"}), \
             patch("api.research_services.stop_research", return_value={"run_id": "run-1", "status": "stopping"}) as stop:
            client = TestClient(create_app())
            self.assertEqual(client.post("/api/v1/analysis/jobs", json={"url": "https://example.test"}).status_code, 202)
            self.assertEqual(client.get("/api/v1/analysis/jobs/job-1").status_code, 200)
            self.assertEqual(client.post("/api/v1/research/jobs", json={"settings": {}}).status_code, 202)
            self.assertEqual(client.get("/api/v1/research/jobs/run-1").status_code, 200)
            self.assertEqual(client.post("/api/v1/research/jobs/run-1/stop").status_code, 200)
            self.assertEqual(client.post("/api/v1/research/country-hunts", json={"country_iso3": "JPN"}).status_code, 202)
            self.assertEqual(client.post("/api/v1/research/bulk-country-hunts", json={"country_iso3s": ["JPN", "AUS"]}).status_code, 202)

        analysis.assert_called_once_with(
            "https://example.test", country_iso3=None, compare=True, review_before_store=False
        )
        research.assert_called_once_with({})
        hunt.assert_called_once_with("JPN", max_results=15, topic="general")
        bulk.assert_called_once_with(["JPN", "AUS"], max_results=15, topic="general")
        stop.assert_called_once_with("run-1")


if __name__ == "__main__":
    unittest.main()
