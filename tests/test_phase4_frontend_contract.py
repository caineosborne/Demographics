import base64
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_phase4_review_controls_are_present_without_changing_graph_defaults():
    renderer = (ROOT / "frontend/admin-assets/graph-renderer.js").read_text()
    admin = (ROOT / "frontend/admin-assets/admin.js").read_text()
    template = (ROOT / "templates/admin.html").read_text()

    assert "export const EVIDENCE_BANDS" in renderer
    assert "export const GRAPH_MODES" in renderer
    assert "display_rejected" not in renderer
    assert 'value="show_all" data-graph-mode checked' in template
    assert "data-claims-include-rejected" in template
    assert "claims-table" in template
    assert "merge_equivalent" in admin
    assert "separate_definition" in admin
    assert 'if (selectedAction)' in admin
    assert "body.classification = classSelect.value" in admin
    assert "body.definition = definition.value.trim()" in admin
    assert "include_rejected=true" in admin


def test_phase4_frontend_cluster_modes_keep_rejected_claims_out_of_graphs():
    source = (ROOT / "frontend/admin-assets/graph-renderer.js").read_bytes()
    encoded = base64.b64encode(source).decode("ascii")
    script = f'''
      const renderer = await import("data:text/javascript;base64,{encoded}");
      const payload = {{ value_clusters: [
        {{ id: 1, observation_group_id: 7, metric: "population", value: 100, period: "2024", effective_points: 7, display_disposition: "approved_secondary" }},
        {{ id: 2, observation_group_id: 7, metric: "population", value: 101, period: "2024", effective_points: 9, display_disposition: "primary" }},
        {{ id: 3, observation_group_id: 7, metric: "population", value: 102, period: "2024", effective_points: 16, display_disposition: "rejected" }}
      ] }};
      if (renderer.visibleGraphClusters(payload, renderer.GRAPH_MODES.show_all).length !== 2) throw new Error("rejected claim was plotted");
      if (renderer.visibleGraphClusters(payload, renderer.GRAPH_MODES.primary).map(row => row.id).join() !== "2") throw new Error("primary mode selected the wrong cluster");
      if (renderer.visibleGraphClusters(payload, renderer.GRAPH_MODES.primary_approved_secondary).length !== 2) throw new Error("approved secondary was hidden");
      if (renderer.evidenceBand(7).label !== "Strong evidence") throw new Error("threshold at exactly seven was not strong");
    '''
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_phase4_frontend_preserves_month_resolution_and_raw_comparison_context():
    renderer = (ROOT / "frontend/admin-assets/graph-renderer.js").read_text()
    admin = (ROOT / "frontend/admin-assets/admin.js").read_text()
    template = (ROOT / "templates/admin.html").read_text()
    assert "comparison_period" in renderer
    assert "Reported value:" in renderer
    assert "Comparison value:" in renderer
    assert "Observation period (monthly comparison)" in renderer
    assert "claim.normalized_value" in admin
    assert "Reported / comparison value and period" in template

    source = renderer.encode("utf-8")
    encoded = base64.b64encode(source).decode("ascii")
    script = f'''
      const renderer = await import("data:text/javascript;base64,{encoded}");
      const june = renderer.dateYearPosition("2025-06");
      const july = renderer.dateYearPosition("2025-07");
      const quarter = renderer.dateYearPosition("2025-07/2025-09");
      const august = renderer.dateYearPosition("2025-08");
      if (!(july > june)) throw new Error("month-only periods are not ordered by month");
      if (Math.abs(quarter - august) > 0.01) throw new Error("range period was not plotted at its midpoint");
    '''
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
