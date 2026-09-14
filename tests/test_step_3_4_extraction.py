import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import (
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_RULE_VERSION,
    LLMInvocationTimeout,
    RelevantResult,
    invoke_llm_with_timeout,
    normalize_extracted_result,
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

    def test_application_timeout_detaches_a_provider_call(self):
        class SlowRunnable:
            def invoke(self, _messages):
                time.sleep(0.2)
                return "too late"

        with patch("agents._llm_timeout_seconds", return_value=0.01):
            started = time.perf_counter()
            with self.assertRaises(LLMInvocationTimeout):
                invoke_llm_with_timeout("extraction_low_effort", SlowRunnable(), [])

        self.assertLess(time.perf_counter() - started, 0.1)

    def test_ambiguous_missing_evidence_is_reviewable(self):
        finding = self.examples[-1]["finding"].copy()
        finding["statistics"] = {"population": {"value": 124600000}}
        result = RelevantResult.model_validate(finding)
        validation = validate_extracted_result(result)
        self.assertEqual(validation["status"], "needs_review")
        self.assertEqual(result.statistics.population.value, 124600000)

    def test_source_value_is_used_when_provider_leaves_normalized_value_empty(self):
        finding = self.examples[-1]["finding"].copy()
        finding["statistics"] = {"population": {
            "value": None, "source_value": 5324700,
            "evidence_excerpt": "The estimated resident population was 5,324,700 in August 2025.",
            "metric_type": "resident population", "unit": "people",
            "observation_status": "provisional", "national_scope_status": "national",
            "measured_period": "August 2025",
        }}
        result = RelevantResult.model_validate(finding)
        validation = validate_extracted_result(result)
        self.assertEqual(validation["status"], "validated")
        self.assertEqual(result.statistics.population.value, 5324700)

    def test_space_grouped_evidence_numbers_validate_without_a_retry(self):
        finding = self.examples[-1]["finding"].copy()
        finding["statistics"] = {"population": {
            "value": 11424031, "source_value": 11424031,
            "evidence_excerpt": "11 424 031 No. Resident population in 2025.",
            "metric_type": "population", "unit": "people",
            "observation_status": "estimated", "national_scope_status": "national",
            "measured_period": "2025",
        }}
        result = RelevantResult.model_validate(finding)
        self.assertEqual(validate_extracted_result(result)["status"], "validated")

    def test_explicit_million_unit_is_normalized_before_un_comparison(self):
        finding = self.examples[-1]["finding"].copy()
        finding["statistics"] = {"population": {
            "value": 58.943, "source_value": 58.943,
            "evidence_excerpt": "Italy's population was steady at 58.943 at the start of 2026.",
            "metric_type": "population", "unit": "million people",
            "observation_status": "reported", "national_scope_status": "national",
            "measured_period": "start of 2026",
        }}
        result = RelevantResult.model_validate(finding)
        normalize_extracted_result(result)
        population = result.statistics.population
        self.assertEqual(population.value, 58_943_000)
        self.assertEqual(population.source_value, 58.943)
        self.assertIn("Normalized", population.normalization_note)
        self.assertEqual(validate_extracted_result(result)["status"], "validated")

    def test_normalized_million_value_is_not_scaled_twice(self):
        finding = self.examples[-1]["finding"].copy()
        finding["statistics"] = {"population": {
            "value": 58_943_000, "source_value": 58.943,
            "evidence_excerpt": "Italy's population was steady at 58.943 at the start of 2026.",
            "metric_type": "population", "unit": "million people",
            "observation_status": "reported", "national_scope_status": "national",
            "measured_period": "start of 2026",
        }}
        result = RelevantResult.model_validate(finding)
        normalize_extracted_result(result)
        self.assertEqual(result.statistics.population.value, 58_943_000)
        self.assertEqual(validate_extracted_result(result)["status"], "validated")

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

    def test_population_disease_patient_and_generic_immigrant_claims_are_rejected(self):
        phrases = (
            "5 million people living with diabetes in Japan in 2023.",
            "5 million cancer patients in Japan in 2023.",
            "5 million immigrant population lived in Japan in 2023.",
            "5 million immigrants lived in Japan in 2023.",
        )
        for evidence in phrases:
            with self.subTest(evidence=evidence):
                finding = self.examples[-1]["finding"].copy()
                finding["statistics"] = {"population": {
                    "value": 5000000, "evidence_excerpt": evidence,
                    "metric_type": "population", "unit": "people",
                    "observation_status": "observed",
                    "national_scope_status": "national", "measured_period": "2023",
                }}
                result = RelevantResult.model_validate(finding)
                validation = validate_extracted_result(result)
                self.assertEqual(validation["status"], "rejected")
                self.assertIsNone(result.statistics.population.value)

    def test_population_total_is_not_rejected_for_a_separate_subgroup_mention(self):
        finding = self.examples[-1]["finding"].copy()
        finding["statistics"] = {"population": {
            "value": 125000000,
            "evidence_excerpt": "Japan's total population was 125 million, including 5 million immigrants in 2023.",
            "metric_type": "population", "unit": "people",
            "observation_status": "observed",
            "national_scope_status": "national", "measured_period": "2023",
        }}
        result = RelevantResult.model_validate(finding)
        self.assertEqual(validate_extracted_result(result)["status"], "validated")
        self.assertEqual(result.statistics.population.value, 125000000)

    def test_negative_extraction_is_not_sent_to_storage(self):
        negative = self.examples[0]["finding"]
        result = RelevantResult.model_validate(negative)
        with patch("agents.research_llm") as model, patch("agents.research_llm_medium") as medium, patch("agents.store_webpage_finding") as store:
            model.invoke.return_value = result
            medium.invoke.return_value = result
            response = __import__("agents").research_agent({
                "messages": [], "article_url": negative["url"], "page_text": "scenario text",
                "provenance": {"submission_type": "manual"},
            })
        self.assertNotEqual(response["storage"]["status"], "stored")
        store.assert_not_called()

    def test_no_numeric_low_extraction_does_not_retry_at_medium(self):
        empty = RelevantResult(
            title='Article', url='https://example.test/article', source='Example', site_seen='example.test',
            statistics={},
        )
        recovered = RelevantResult(
            title='Article', url='https://example.test/article', source='Example', site_seen='example.test',
            geography='Japan', geography_iso3='JPN',
            statistics={'population': {
                'value': 124_600_000,
                'evidence_excerpt': 'Japan population was 124.6 million in 2023.',
                'metric_type': 'population', 'measured_period': '2023',
            }},
        )
        with patch('agents.research_llm') as low, patch('agents.research_llm_medium') as medium:
            low.invoke.return_value = empty
            medium.invoke.return_value = recovered
            response = __import__('agents').extract_from_page_text(
                'Japan population was 124.6 million in 2023.', empty.url,
                {'submission_type': 'manual'},
            )
        low.invoke.assert_called_once()
        medium.invoke.assert_not_called()
        self.assertIsNone(response['result'].statistics.population)

    def test_medium_extraction_retries_for_partial_or_unclear_low_data(self):
        partial = RelevantResult(
            title='Article', url='https://example.test/article', source='Example', site_seen='example.test',
            statistics={
                'population': {
                    'value': 124_600_000,
                    'evidence_excerpt': 'Japan population was 124.6 million in 2023.',
                    'metric_type': 'population', 'measured_period': '2023',
                },
                'births': {
                    'value': 7.2,
                    'evidence_excerpt': 'The birth rate was 7.2 per 1,000 people.',
                    'metric_type': 'births', 'measured_period': '2023',
                },
            },
        )
        recovered = RelevantResult(
            title='Article', url='https://example.test/article', source='Example', site_seen='example.test',
            geography='Japan', geography_iso3='JPN',
            statistics={'population': {
                'value': 124_600_000,
                'evidence_excerpt': 'Japan population was 124.6 million in 2023.',
                'metric_type': 'population', 'measured_period': '2023',
            }},
        )
        with patch('agents.research_llm') as low, patch('agents.research_llm_medium') as medium:
            low.invoke.return_value = partial
            medium.invoke.return_value = recovered
            response = __import__('agents').extract_from_page_text(
                'Japan population was 124.6 million in 2023.', partial.url,
                {'submission_type': 'manual'},
            )
        medium.invoke.assert_called_once()
        self.assertEqual(response['result'].statistics.population.value, 124_600_000)

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
        self.assertEqual(response["result"].extraction_prompt_version, EXTRACTION_PROMPT_VERSION)
        stored_finding = store.call_args.args[0]
        self.assertEqual(stored_finding["extraction_rule_version"], EXTRACTION_RULE_VERSION)
        self.assertEqual(stored_finding["statistics"]["population"]["value"], 124600000)


if __name__ == "__main__":
    unittest.main()
