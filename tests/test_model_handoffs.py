import os
import unittest
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

with patch('dotenv.load_dotenv'), patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-key'}):
    import agents


class ModelHandoffTests(unittest.TestCase):
    def test_manual_duplicate_url_skips_retrieval_and_extraction(self):
        existing = {
            'id': 17,
            'source_url': 'https://example.test/article',
            'canonical_url': 'https://example.test/article',
            'finding': {
                'url': 'https://example.test/article',
                'title': 'Stored article',
                'source': 'Example publisher',
                'site_seen': 'example.test',
                'geography': 'Japan',
                'statistics': {},
            },
        }
        with (
            patch.object(agents.tools, 'blocked_source_urls', return_value=set()),
            patch.object(agents.tools, 'find_webpage_finding_by_url', return_value=existing),
            patch.object(agents, 'web_llm') as web,
            patch.object(agents, 'research_llm') as extraction,
        ):
            result = agents.research_agent({
                'messages': [HumanMessage(content='Please analyze https://example.test/article')],
            })
            comparison = agents.compare_to_un({
                'result': result['result'],
                'storage': result['storage'],
            })
        self.assertEqual(result['storage']['status'], 'excluded_duplicate_url')
        self.assertEqual(result['storage']['existing_id'], 17)
        self.assertEqual(comparison['un_data'], [])
        web.invoke.assert_not_called()
        extraction.invoke.assert_not_called()

    def test_research_handoff_ends_with_user_and_preserves_page(self):
        page_tool = MagicMock()
        page_tool.name = 'get_page_text'
        page_tool.invoke.return_value = 'Population: 123456'
        with (
            patch.object(agents, 'WEB_TOOLS', [page_tool]),
            patch.object(agents, 'web_llm') as web,
            patch.object(agents, 'research_llm') as extraction,
            patch.object(agents, 'store_webpage_finding', return_value={'status': 'stored', 'id': 1}),
        ):
            web.invoke.side_effect = [
                AIMessage(content='', tool_calls=[{
                    'name': 'get_page_text', 'args': {'url': 'https://example.test'}, 'id': 'page',
                }]),
                AIMessage(content='Article retrieved.'),
            ]
            extraction.invoke.return_value = agents.RelevantResult(
                title='Article', url='https://example.test', source='Example', site_seen='example.test',
                statistics=agents.Statistics(population=agents.Statistic(
                    value=123456, evidence_excerpt='Population: 123456',
                    metric_type='population', measured_period='2026',
                )),
            )
            agents.research_agent({'messages': [HumanMessage(content='Summarise the URL')]})
            messages = extraction.invoke.call_args.args[0]
            self.assertEqual(extraction.invoke.return_value.url, 'https://example.test')
            self.assertIsInstance(messages[-1], HumanMessage)
            self.assertTrue(any(isinstance(m, ToolMessage) and '123456' in m.content for m in messages))

    def test_comparison_uses_one_deterministic_un_lookup(self):
        research = agents.RelevantResult(
            title='Article', url='https://example.test/article', source='Example', site_seen='example.test',
            geography='Australia', geography_iso3='AUS', effective_date='2026-07-31',
            statistics=agents.Statistics(population=agents.Statistic(value=100_000, measured_period='2026')),
        )
        with (
            patch.object(agents, 'get_population_forecast') as lookup,
        ):
            lookup.invoke.return_value = [{'Year': '2026', 'Population 1 Jul': 100}]
            response = agents.compare_to_un({
                'messages': [HumanMessage(content='Summarise the URL'), AIMessage(content='Article retrieved.')],
                'result': research,
            })
            lookup.invoke.assert_called_once_with({
                'country_iso3': 'AUS', 'years': [2026, 2027], 'historic': False,
            })
        self.assertEqual(response['comparison'].population.un_expected, 100_000)
        self.assertEqual(response['comparison'].population.difference, 0)

    def test_comparison_supplies_the_closest_population_reference(self):
        research = agents.RelevantResult(
            title='Article', url='https://example.test/article', source='Example', site_seen='example.test',
            geography='Australia', geography_iso3='AUS', effective_date='2026-11-15',
            statistics=agents.Statistics(),
        )
        with (
            patch.object(agents, 'get_population_forecast') as lookup,
        ):
            lookup.invoke.return_value = [
                {'Year': '2026', 'Population 1 Jan': 100, 'Population 1 Jul': 101},
                {'Year': '2027', 'Population 1 Jan': 102, 'Population 1 Jul': 103},
            ]
            result = agents.compare_to_un({'messages': [], 'result': research})
        reference = result['un_data'][0]['population_reference']
        self.assertEqual(reference['observation_date'], '2027-01-01')
        self.assertEqual(reference['reference_field'], 'Population 1 Jan')
        self.assertEqual(reference['value_thousands'], 102)

    def test_comparison_queries_both_un_tables_across_2023_boundary(self):
        research = agents.RelevantResult(
            title='Article', url='https://example.test/article', source='Example', site_seen='example.test',
            geography='Australia', geography_iso3='AUS', effective_date='2023-12-31',
            statistics=agents.Statistics(),
        )
        with (
            patch.object(agents, 'get_population_forecast') as lookup,
        ):
            lookup.invoke.side_effect = [
                [{'Year': '2023', 'Population 1 Jan': 100, 'Population 1 Jul': 101}],
                [{'Year': '2024', 'Population 1 Jan': 102, 'Population 1 Jul': 103}],
            ]
            result = agents.compare_to_un({'messages': [], 'result': research})
        self.assertEqual([call.args[0] for call in lookup.invoke.call_args_list], [
            {'country_iso3': 'AUS', 'years': [2023], 'historic': True},
            {'country_iso3': 'AUS', 'years': [2024], 'historic': False},
        ])
        self.assertEqual(result['un_data'][0]['population_reference']['observation_date'], '2024-01-01')

    def test_bulk_un_bound_is_inclusive_at_25_percent(self):
        comparison = agents.ComparisonResult(
            population=agents.MetricComparison(reported=125, un_expected=100),
            births=agents.MetricComparison(), deaths=agents.MetricComparison(),
            natural_change=agents.MetricComparison(), net_migration=agents.MetricComparison(),
            total_fertility_rate=agents.MetricComparison(), overall_assessment='Compared',
        )
        comparison, excluded = agents.apply_outlier_filter(comparison)
        self.assertEqual(excluded, [])
        self.assertIsNone(agents.bulk_un_bounds_issue(comparison))

        comparison.population.reported = 125.01
        comparison, excluded = agents.apply_outlier_filter(comparison)
        self.assertEqual(excluded, ['population'])
        self.assertIn('25% UN comparison bound', agents.bulk_un_bounds_issue(comparison))
