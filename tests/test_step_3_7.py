import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import admin_services, read_services
from data import tools
from api import create_app
from fastapi.testclient import TestClient


class Step37AdministrationTests(unittest.TestCase):
    def test_source_rules_are_audited_and_undoable(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, "DB_PATH", Path(directory) / "db.sqlite"):
            created = admin_services.upsert_source_rule(
                match_type="domain", match_value="www.stats.example.org",
                action="classify", classification="official_publisher", note="Publisher override",
            )
            rule_id = created["id"]
            self.assertEqual(created["match_value"], "stats.example.org")
            disabled = admin_services.disable_source_rule(rule_id)
            self.assertEqual(disabled["enabled"], 0)
            self.assertEqual(admin_services.list_source_rule_actions(rule_id)[0]["action"], "disabled")
            restored = admin_services.undo_source_rule(rule_id)
            self.assertEqual(restored["enabled"], 1)
            self.assertGreaterEqual(len(admin_services.list_source_rule_actions(rule_id)), 2)

    def test_findings_metric_filter_and_coverage_are_api_safe(self):
        rows = [{
            "ID": 7, "Country": "Japan", "ISO3": "JPN", "Population": 123,
            "TFR": 1.3, "Extracted JSON": json.dumps({"statistics": {
                "population": {"value": 123}, "births": {"value": 4}}}),
        }]
        with patch.object(read_services, "list_webpage_findings", return_value=rows):
            result = read_services.list_findings(metric="births")
            coverage = read_services.finding_coverage()
        self.assertEqual(result[0]["ID"], 7)
        self.assertEqual(coverage[0]["births"], 1)
        self.assertNotIn("Extracted JSON", result[0])

    def test_zero_value_is_still_metric_coverage(self):
        rows = [{
            "ID": 8, "Country": "Example", "ISO3": "EXP",
            "Extracted JSON": json.dumps({"statistics": {
                "net_overseas_migration": {"value": 0}}}),
        }]
        with patch.object(read_services, "list_webpage_findings", return_value=rows):
            coverage = read_services.finding_coverage()
        self.assertEqual(coverage[0]["net_migration"], 1)

    def test_admin_api_exposes_filters_coverage_and_rule_undo(self):
        with patch("api.read_services.list_findings", return_value=[]), \
             patch("api.read_services.finding_coverage", return_value=[]), \
             patch("api.admin_services.undo_source_rule", return_value={"id": 3, "status": "undone"}) as undo:
            client = TestClient(create_app())
            self.assertEqual(client.get("/api/v1/findings?iso3=JPN&metric=population").status_code, 200)
            self.assertEqual(client.get("/api/v1/admin/findings/coverage?iso3=JPN").status_code, 200)
            self.assertEqual(client.post("/api/v1/admin/source-rules/3/undo").status_code, 200)
        undo.assert_called_once_with(3)

    def test_admin_surface_has_finding_and_source_policy_controls(self):
        html = TestClient(create_app()).get("/admin/").text
        self.assertIn("data-findings-body", html)
        self.assertIn("data-findings-country data-country-control", html)
        self.assertIn("data-record-delete-metric", html)
        self.assertIn("data-source-rule-form", html)
        script = TestClient(create_app()).get("/admin-assets/admin.js").text
        self.assertIn("safeSourceLink", script)
        self.assertIn("source-rules/${rule.id}/undo", script)


if __name__ == "__main__":
    unittest.main()
