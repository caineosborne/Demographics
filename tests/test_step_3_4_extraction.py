import json
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import (
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_RULE_VERSION,
    RelevantResult,
    validate_extracted_result,
)


FIXTURE = Path(__file__).parents[1] / "fixtures" / "extraction_step_3_4.json"


class Step34ExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.examples = json.loads(FIXTURE.read_text())["examples"]

    def test_versioned_positive_and_negative_fixtures(self):
        fixture = json.loads(FIXTURE.read_text())
        self.assertEqual(fixture["version"], EXTRACTION_PROMPT_VERSION)
        self.assertEqual(fixture["version"], EXTRACTION_RULE_VERSION)
        for example in self.examples:
            with self.subTest(example=example["name"]):
                result = RelevantResult.model_validate(example["finding"])
                validation = validate_extracted_result(result)
                if example["expected"] == "accept":
                    self.assertEqual(validation["status"], "validated")
                    self.assertTrue(any(getattr(result.statistics, name).value is not None
                                        for name in result.statistics.__class__.model_fields))
                else:
                    self.assertNotEqual(validation["status"], "validated")
                    self.assertTrue(all(getattr(result.statistics, name).value is None
                                        for name in result.statistics.__class__.model_fields
                                        if getattr(result.statistics, name) is not None))

    def test_ambiguous_missing_evidence_is_reviewable(self):
        finding = self.examples[-1]["finding"].copy()
        finding["statistics"] = {"population": {"value": 124600000}}
        result = RelevantResult.model_validate(finding)
        validation = validate_extracted_result(result)
        self.assertEqual(validation["status"], "needs_review")
        self.assertEqual(result.statistics.population.value, 124600000)

    def test_metric_type_must_match_the_statistics_field(self):
        finding = self.examples[-1]["finding"].copy()
        finding["statistics"] = {
            "population": {
                "value": 727000,
                "evidence_excerpt": "There were 727,000 births nationwide in 2023.",
                "metric_type": "births",
                "unit": "births",
                "observation_status": "observed",
                "national_scope_status": "national",
                "measured_period": "2023",
            }
        }
        result = RelevantResult.model_validate(finding)
        validation = validate_extracted_result(result)
        self.assertEqual(validation["status"], "rejected")
        self.assertIsNone(result.statistics.population.value)

    def test_population_subset_and_category_phrases_are_rejected(self):
        phrases = (
            "Germany counted 1.2 million visa holders in 2023.",
            "Germany had 18 million residents aged 65 and over in 2023.",
            "Germany had 3 million Muslim residents in 2023.",
            "Germany counted 5 million foreign nationals in 2023.",
        )
        for index, evidence in enumerate(phrases):
            with self.subTest(evidence=evidence):
                finding = self.examples[-1]["finding"].copy()
                finding["statistics"] = {
                    "population": {
                        "value": (1200000, 18000000, 3000000, 5000000)[index],
                        "evidence_excerpt": evidence,
                        "metric_type": "population",
                        "unit": "people",
                        "observation_status": "observed",
                        "national_scope_status": "national",
                        "measured_period": "2023",
                    }
                }
                result = RelevantResult.model_validate(finding)
                validation = validate_extracted_result(result)
                self.assertEqual(validation["status"], "rejected")
                self.assertIsNone(result.statistics.population.value)

    def test_negative_extraction_is_not_sent_to_storage(self):
        negative = self.examples[0]["finding"]
        result = RelevantResult.model_validate(negative)
        with patch("agents.research_llm") as model, patch("agents.store_webpage_finding") as store:
            model.invoke.return_value = result
            response = __import__("agents").research_agent({
                "messages": [], "article_url": negative["url"], "page_text": "scenario text",
                "provenance": {"submission_type": "manual"},
            })
        self.assertNotEqual(response["storage"]["status"], "stored")
        store.assert_not_called()

    def test_valid_observation_reaches_storage_with_versions(self):
        positive = self.examples[-1]["finding"]
        result = RelevantResult.model_validate(positive)
        with patch("agents.research_llm") as model, patch(
                "agents.store_webpage_finding", return_value={"status": "stored", "id": 9}) as store:
            model.invoke.return_value = result
            response = __import__("agents").research_agent({
                "messages": [], "article_url": positive["url"], "page_text": "national observations",
                "provenance": {"submission_type": "manual"},
            })
        self.assertEqual(response["storage"]["status"], "stored")
        self.assertEqual(response["result"].extraction_prompt_version, "3.4.0")
        stored_finding = store.call_args.args[0]
        self.assertEqual(stored_finding["extraction_rule_version"], "3.4.0")
        self.assertEqual(stored_finding["statistics"]["population"]["value"], 124600000)


if __name__ == "__main__":
    unittest.main()
