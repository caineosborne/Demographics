import unittest

from agents import RelevantResult, annualize_flow_statistics, mark_partial_periods


class FlowPeriodNormalisationTests(unittest.TestCase):
    def test_human_formatted_numbers_are_parsed(self):
        result = RelevantResult.model_validate({
            'title': 'Formatted', 'url': 'https://example.test', 'source': 'Example',
            'site_seen': '2026-09-11', 'geography': 'Japan',
            'statistics': {
                'population': {'value': 'over 33,000'},
                'deaths': {'source_value': '4.9 million'},
            },
        })
        self.assertEqual(result.statistics.population.value, 33000)
        self.assertEqual(result.statistics.deaths.source_value, 4900000)

    def test_daily_flows_are_annualized_and_keep_source_provenance(self):
        result = RelevantResult.model_validate({
            'title': 'Argentina',
            'url': 'https://example.test/argentina',
            'source': 'Example',
            'site_seen': '2026-09-11',
            'geography': 'Argentina',
            'effective_date': '2026-01-01',
            'statistics': {
                'births': {'value': 1397, 'time_period': 'daily'},
                'deaths': {'value': 990, 'time_period': 'per day'},
                'natural_change': {'value': 413, 'time_period': 'daily'},
                'net_overseas_migration': {'value': 6, 'time_period': 'daily'},
                'population': {'value': 46003734, 'time_period': 'annual'},
                'total_fertility_rate': {'value': 1.5, 'time_period': 'annual'},
            },
        })

        annualize_flow_statistics(result)
        mark_partial_periods(result)

        self.assertEqual(result.statistics.births.value, 509_905)
        self.assertEqual(result.statistics.deaths.value, 361_350)
        self.assertEqual(result.statistics.natural_change.value, 150_745)
        self.assertEqual(result.statistics.net_overseas_migration.value, 2_190)
        self.assertEqual(result.statistics.births.source_value, 1_397)
        self.assertEqual(result.statistics.births.source_time_period, 'daily')
        self.assertEqual(result.statistics.births.conversion_factor, 365)
        self.assertEqual(result.statistics.births.time_period, 'annual')
        self.assertEqual(result.statistics.births.period_end, '2026-12-31')
        self.assertTrue(result.statistics.births.comparison_eligible)
        self.assertEqual(result.statistics.population.value, 46_003_734)
        self.assertIsNone(result.statistics.population.source_value)
        self.assertEqual(result.statistics.total_fertility_rate.value, 1.5)
        self.assertIsNone(result.statistics.total_fertility_rate.source_value)

    def test_monthly_and_quarterly_flow_counts_use_fixed_annual_factors(self):
        result = RelevantResult.model_validate({
            'title': 'Example', 'url': 'https://example.test', 'source': 'Example',
            'site_seen': '2026-09-11', 'geography': 'Argentina',
            'effective_date': '2024-06-30',
            'statistics': {
                'births': {'value': 100, 'time_period': 'monthly'},
                'deaths': {'value': 50, 'time_period': 'quarterly'},
            },
        })

        annualize_flow_statistics(result)

        self.assertEqual(result.statistics.births.value, 1_200)
        self.assertEqual(result.statistics.births.conversion_factor, 12)
        self.assertEqual(result.statistics.deaths.value, 200)
        self.assertEqual(result.statistics.deaths.conversion_factor, 4)

    def test_daily_flow_without_a_year_is_not_converted(self):
        result = RelevantResult.model_validate({
            'title': 'Example', 'url': 'https://example.test', 'source': 'Example',
            'site_seen': '2026-09-11', 'geography': 'Argentina',
            'statistics': {'births': {'value': 100, 'time_period': 'daily'}},
        })

        annualize_flow_statistics(result)

        self.assertEqual(result.statistics.births.value, 100)
        self.assertFalse(result.statistics.births.comparison_eligible)
        self.assertIn('no reporting year', result.statistics.births.comparison_reason)

    def test_daily_observation_date_uses_all_days_in_its_calendar_year(self):
        result = RelevantResult.model_validate({
            'title': 'Example', 'url': 'https://example.test', 'source': 'Example',
            'site_seen': '2026-09-11', 'geography': 'Argentina',
            'statistics': {
                'births': {
                    'value': 100, 'time_period': 'daily',
                    'period_start': '2024-02-29', 'period_end': '2024-02-29',
                },
            },
        })

        annualize_flow_statistics(result)

        self.assertEqual(result.statistics.births.value, 36_600)
        self.assertEqual(result.statistics.births.conversion_factor, 366)
        self.assertEqual(result.statistics.births.period_start, '2024-01-01')
