import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import create_app
import read_services


ROOT = Path(__file__).resolve().parents[1]


def test_admin_uat_controls_keep_country_hunt_explicit_and_coverage_independent():
    html = TestClient(create_app()).get("/admin/").text
    assert 'data-country-hunt-country required' in html
    assert 'data-coverage-country data-country-control' in html
    assert 'Country context' not in html
    assert 'country and ISO3 code are derived from the extracted evidence' in html
    assert "non-null value" in html
    assert "containing a non-zero value" not in html


def test_admin_uat_script_exposes_safe_mutation_and_audit_behaviour():
    script = TestClient(create_app()).get("/admin-assets/admin.js").text
    for marker in (
        "window.confirm", "mutationBusy", "findingMetricValues", "loadDraftActions",
        "loadFindingActions", "refreshAllPanels", "data-country-hunt-country",
        "graph-series/${encodeURIComponent(iso3)}", "selectViewFromLocation",
        'window.addEventListener("hashchange", selectViewFromLocation)',
        'setState(card, "completed", "API is online")', "updateAnalysisLog",
    ):
        assert marker in script
    assert 'body: { url: rawUrl, country_iso3' not in script


def test_research_uses_core_searches_and_opens_batch_results_in_findings():
    script = TestClient(create_app()).get("/admin-assets/admin.js").text
    assert "function coreResearchCategories()" in script
    assert "renderCategoryEditors(coreResearchCategories())" in script
    assert "The 3 core searches are ready." in script
    assert "await Promise.all([loadCountryQueue(), loadRunHistory(), loadFindings()])" in script
    assert 'window.location.hash = "findings"' in script
    assert "data-reset-research-settings" in TestClient(create_app()).get("/admin/").text
    assert "data-run-log-toggle" in TestClient(create_app()).get("/admin/").text
    assert "maxAttempts: 60" in script


def test_admin_topbar_stays_visible_when_anchor_navigation_scrolls():
    css = TestClient(create_app()).get("/admin-assets/admin.css").text
    assert "position:sticky" in css
    assert "scroll-margin-top:110px" in css


def test_model_output_uses_provider_safe_function_calling():
    source = (ROOT / "agents.py").read_text()
    assert 'with_structured_output(RelevantResult, method="function_calling")' in source
    assert 'with_structured_output(ComparisonResult, method="function_calling")' in source


def test_fixture_graph_route_adapts_country_without_reusing_japan_articles():
    script = (ROOT / "frontend/admin-assets/admin.js").read_text()
    fixture = json.loads((ROOT / "fixtures/api/graph-series.json").read_text())
    assert fixture["iso3"] == "JPN"
    assert "/fixtures/api/graph-series.json" in script
    assert 'iso3 === "AUS" ? "Australia"' in script
    assert 'if (iso3 !== "JPN") payload.findings = []' in script


def test_finding_action_timeline_endpoint_is_available():
    with patch("api.admin_services.list_finding_actions", return_value=[{"finding_id": 7, "action": "created"}]) as actions:
        response = TestClient(create_app()).get("/api/v1/admin/finding-actions?finding_id=7")
    assert response.status_code == 200
    assert response.json() == {"items": [{"finding_id": 7, "action": "created"}]}
    actions.assert_called_once_with(7)


def test_findings_public_list_includes_values_without_private_extraction_json():
    row = {
        "ID": 7, "Country": "Japan", "ISO3": "JPN",
        "Extracted JSON": json.dumps({"statistics": {
            "population": {"value": 125},
            "births": {"value": 800},
        }}),
    }
    with patch.object(read_services, "list_webpage_findings", return_value=[row]):
        result = read_services.list_findings()
    assert result[0]["Metric values"] == {"population": 125, "births": 800}
    assert "Extracted JSON" not in result[0]
