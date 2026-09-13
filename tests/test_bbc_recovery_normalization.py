import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agents import (
    RelevantResult,
    extract_from_page_text,
    normalize_extracted_result,
    research_agent,
    validate_extracted_result,
)


FIXTURE = Path(__file__).parents[1] / "fixtures" / "bbc_recovered_extraction.json"


class BbcRecoveryNormalizationTests(unittest.TestCase):
    def test_recovered_bbc_result_gets_country_and_safe_metric_metadata(self):
        result = RelevantResult.model_validate(json.loads(FIXTURE.read_text()))

        normalize_extracted_result(
            result,
            "China's population fell to 1.416 billion people in 2025. "
            "China's total fertility rate was 1.03 live births per woman in 2025.",
            {"submission_type": "automatic"},
        )

        self.assertEqual(result.geography_iso3, "CHN")
        self.assertEqual(result.geography, "China")
        population = result.statistics.population
        self.assertEqual(population.metric_type, "population")
        self.assertEqual(population.unit, "people")
        self.assertEqual(population.observation_status, "reported")
        self.assertEqual(population.national_scope_status, "national")
        self.assertEqual(population.measured_period, "2025")
        fertility = result.statistics.total_fertility_rate
        self.assertEqual(fertility.metric_type, "total fertility rate")
        self.assertEqual(fertility.unit, "live births per woman")
        self.assertEqual(fertility.observation_status, "reported")
        self.assertEqual(fertility.national_scope_status, "national")
        self.assertEqual(fertility.measured_period, "2025")
        self.assertIsNone(result.comments)
        validation = validate_extracted_result(result)
        self.assertEqual(validation["status"], "rejected")
        self.assertIn("births", validation["rejected_metrics"])
        self.assertIn("deaths", validation["rejected_metrics"])
        self.assertIsNotNone(result.statistics.population.value)
        self.assertIsNotNone(result.statistics.total_fertility_rate.value)
        self.assertIsNone(result.statistics.births.value)
        self.assertIsNone(result.statistics.deaths.value)

    def test_underscore_tfr_metric_type_is_canonicalized(self):
        finding = json.loads(FIXTURE.read_text())
        finding["statistics"]["population"] = None
        finding["statistics"]["births"] = None
        finding["statistics"]["deaths"] = None
        result = RelevantResult.model_validate(finding)
        fertility = result.statistics.total_fertility_rate
        fertility.unit = "live births per woman"
        fertility.observation_status = "observed"
        fertility.national_scope_status = "national"
        fertility.measured_period = "2025"
        self.assertEqual(validate_extracted_result(result)["status"], "validated")

    def test_normalization_does_not_create_values_or_convert_rates(self):
        finding = json.loads(FIXTURE.read_text())
        finding["statistics"]["population"]["value"] = None
        finding["statistics"]["births"] = {
            "value": 10.5,
            "evidence_excerpt": "The birth rate was 10.5 per 1,000 people in 2025.",
        }
        result = RelevantResult.model_validate(finding)
        normalize_extracted_result(result, "China's birth rate was 10.5 per 1,000 people in 2025.")
        self.assertIsNone(result.statistics.population.value)
        self.assertEqual(result.statistics.births.value, 10.5)
        self.assertEqual(result.statistics.births.unit, "births")
        self.assertEqual(validate_extracted_result(result)["status"], "rejected")

    def test_automatic_storage_runs_un_comparison_after_country_recovery(self):
        result = RelevantResult.model_validate(json.loads(FIXTURE.read_text()))
        comparison = MagicMock()
        comparison.model_dump.return_value = {"overall_assessment": "Compared"}
        calls = []
        with patch("agents.tools.blocked_source_urls", return_value=set()), \
                patch("agents.tools.find_webpage_finding_by_url", return_value=None), \
                patch("agents.research_llm") as model, \
                patch("agents.compare_to_un", side_effect=lambda state: (
                    calls.append("compare"), {"comparison": comparison, "un_data": []}
                )[1]), \
                patch("agents.store_webpage_finding", side_effect=lambda *args, **kwargs: (
                    calls.append("store"), {"status": "stored", "id": 21}
                )[1]) as store:
            model.invoke.return_value = result
            response = research_agent({
                "messages": [], "article_url": result.url,
                "page_text": "China's population fell to 1.416 billion people in 2025.",
                "provenance": {"submission_type": "automatic"},
            })
        self.assertEqual(response["result"].geography_iso3, "CHN")
        self.assertEqual(calls, ["compare", "store"])
        stored = store.call_args.args[0]
        self.assertIsNotNone(stored["statistics"]["population"]["value"])
        self.assertIsNotNone(stored["statistics"]["total_fertility_rate"]["value"])
        self.assertIsNone(stored["statistics"]["births"]["value"])
        self.assertIsNone(stored["statistics"]["deaths"]["value"])
        self.assertNotIn("WPP projection", stored.get("comments") or "")

    def test_manual_extraction_keeps_valid_metrics_and_removes_rate_counts(self):
        result = RelevantResult.model_validate(json.loads(FIXTURE.read_text()))
        with patch("agents.research_llm") as model:
            model.invoke.return_value = result
            response = extract_from_page_text(
                "China's population fell to 1.416 billion people in 2025. "
                "China's total fertility rate was one birth per woman in 2025.",
                result.url,
                {"submission_type": "manual"},
            )
        self.assertEqual(response["storage"]["status"], "validated")
        self.assertIsNotNone(response["result"].statistics.population.value)
        self.assertIsNotNone(response["result"].statistics.total_fertility_rate.value)
        self.assertIsNone(response["result"].statistics.births.value)
        self.assertIsNone(response["result"].statistics.deaths.value)


if __name__ == "__main__":
    unittest.main()
