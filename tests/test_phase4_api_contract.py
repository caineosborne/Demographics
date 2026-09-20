import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import create_app


class Phase4ApiContractTests(unittest.TestCase):
    def test_admin_shell_exposes_three_graph_modes_with_show_all_default(self):
        response = TestClient(create_app()).get("/admin/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('value="show_all" data-graph-mode checked', response.text)
        self.assertIn('value="primary" data-graph-mode', response.text)
        self.assertIn('value="primary_approved_secondary" data-graph-mode', response.text)

    def test_graph_series_exposes_value_clusters(self):
        series = {
            "country": "Japan",
            "iso3": "JPN",
            "metrics": ["population"],
            "historic": [],
            "forecast": [],
            "alternate_releases": {},
            "findings": [],
            "value_clusters": [{
                "id": 7,
                "observation_group_id": 3,
                "value": 100.0,
                "raw_points": 25,
                "effective_points": 9,
                "supporting_document_count": 11,
                "unresolved_conflict": True,
            }],
        }
        with patch("api.read_services.graph_series", return_value=series):
            response = TestClient(create_app()).get("/api/v1/graph-series/JPN")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["value_clusters"], series["value_clusters"])

    def test_claim_read_and_override_routes_use_phase4_services(self):
        with patch("api.claim_services.list_claims", return_value=[{"id": 3, "effective_points": 7}]) as listed, \
             patch("api.claim_services.override_claim", return_value={"id": 3, "effective_points": 5}) as overridden:
            client = TestClient(create_app())
            claims = client.get("/api/v1/claims?iso3=JPN&metric=population&mode=primary")
            updated = client.put(
                "/api/v1/admin/claims/3",
                json={"classification": "official_publisher", "actor": "reviewer", "reason": "confirmed"},
            )

        self.assertEqual(claims.status_code, 200)
        self.assertEqual(claims.json(), {"items": [{"id": 3, "effective_points": 7}]})
        listed.assert_called_once_with("JPN", "population", "primary")
        self.assertEqual(updated.status_code, 200)
        overridden.assert_called_once_with(
            3,
            actor="reviewer",
            classification="official_publisher",
            points=None,
            disposition=None,
            reason="confirmed",
            action=None,
            peer_claim_ids=[],
            definition=None,
        )

    def test_claim_audit_can_include_rejected_claims_without_changing_graph_modes(self):
        with patch("api.claim_services.list_claims", return_value=[{"id": 9, "display_disposition": "rejected"}]) as listed:
            response = TestClient(create_app()).get(
                "/api/v1/claims?iso3=JPN&mode=all&include_rejected=true"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": [{"id": 9, "display_disposition": "rejected"}]})
        listed.assert_called_once_with("JPN", None, "all", True)


if __name__ == "__main__":
    unittest.main()
