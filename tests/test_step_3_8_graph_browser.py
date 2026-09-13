import json
import base64
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_graph_fixture_carries_reference_series_and_stable_finding_ids():
    series = json.loads((ROOT / "fixtures/api/graph-series.json").read_text())

    assert series["iso3"] == "JPN"
    assert series["historic"] and series["forecast"]
    assert series["alternate_releases"]["2022"]
    assert [finding["id"] for finding in series["findings"]] == [7, 8, 9]
    assert [finding["source_type"] for finding in series["findings"]] == [
        "official_publisher", "secondary_attributed", "secondary_unattributed"
    ]


def test_browser_renderer_keeps_marker_contract_and_graph_series_route():
    renderer = (ROOT / "frontend/admin-assets/graph-renderer.js").read_text()
    admin = (ROOT / "frontend/admin-assets/admin.js").read_text()
    template = (ROOT / "templates/admin.html").read_text()

    assert 'official_publisher: "diamond"' in renderer
    assert 'secondary_attributed: "circle-open"' in renderer
    assert 'secondary_unattributed: "cross"' in renderer
    assert "findingMetricValue" in renderer
    assert "/api/v1/graph-series/" in admin
    assert "data-finding-id" in renderer
    assert 'data-view="graphs"' in template
    assert 'data-graph-revision="2022"' in template


def test_browser_renderer_positions_july_rows_and_pads_y_extents():
    source = (ROOT / "frontend/admin-assets/graph-renderer.js").read_bytes()
    encoded = base64.b64encode(source).decode("ascii")
    script = f'''
      const renderer = await import("data:text/javascript;base64,{encoded}");
      const july = renderer.observationYearPosition(2024);
      if (!(july > 2024.49 && july < 2024.51)) throw new Error(`July position was ${{july}}`);
      const articleStart = renderer.dateYearPosition("2024-01-01");
      const articleEnd = renderer.dateYearPosition("2024-12-31");
      if (!(articleStart === 2024 && articleEnd > 2024.99)) throw new Error("article dates lost their exact positions");
      const positive = renderer.calculateYAxisExtent([100, 110], 1000);
      if (!(positive.min > 0 && positive.min < 100 && positive.max > 110)) throw new Error("positive extent was flattened or unpadded");
      const mixed = renderer.calculateYAxisExtent([-4, 6], 1000);
      if (!(mixed.min < -4 && mixed.max > 6 && mixed.min < 0 && mixed.max > 0)) throw new Error("mixed extent did not preserve zero");
      const negative = renderer.calculateYAxisExtent([-10, -4], 1000);
      if (!(negative.max < 0 && negative.min < -10)) throw new Error("negative extent crossed zero unexpectedly");
    '''
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
