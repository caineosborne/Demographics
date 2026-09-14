from datetime import date
import os
import unittest
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage

with patch('dotenv.load_dotenv'), patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-key'}):
    import agents

from temporal_context import temporal_context


class TemporalContextTests(unittest.TestCase):
    def test_date_refreshes_without_restarting(self):
        with patch('temporal_context.date') as clock:
            clock.today.return_value = date(2026, 9, 10)
            self.assertIn('2026-09-10', temporal_context())
            clock.today.return_value = date(2027, 1, 1)
            self.assertIn('2027-01-01', temporal_context())
            self.assertNotIn('2026-09-10', temporal_context())

    def test_all_production_model_stages_receive_date_and_vintage(self):
        research = agents.RelevantResult(
            title='Article', url='https://example.test/article', source='Example', site_seen='example.test',
            geography='Australia', geography_iso3='AUS', effective_date='2026-07-31',
            statistics=agents.Statistics(population=agents.Statistic(
                value=100_000, evidence_excerpt='Population was 100,000 in 2026.',
                metric_type='population', measured_period='2026',
            )),
        )
        with (
            patch('temporal_context.date') as clock,
            patch.object(agents, 'web_llm') as web,
            patch.object(agents, 'research_llm') as extraction,
            patch.object(agents, 'get_population_forecast') as un_lookup,
            patch.object(agents, 'store_webpage_finding', return_value={'status': 'stored', 'id': 1}),
            patch.object(agents, 'resolve_country_iso3', return_value='AUS'),
        ):
            clock.today.return_value = date(2026, 9, 10)
            web.invoke.return_value = AIMessage(content='Retrieved article')
            extraction.invoke.return_value = research
            un_lookup.invoke.return_value = [{'Year': 2026, 'Total Births': 100}]
            agents.research_agent({'messages': []})
            for model in (web, extraction):
                with self.subTest(model=model):
                    system_prompt = model.invoke.call_args.args[0][0].content
                    self.assertIn('2026-09-10', system_prompt)
                    self.assertIn('vintage/effective as-of year is 2024', system_prompt)
                    self.assertIn('historical estimates cover 1950–2023', system_prompt)
                    self.assertIn('not future-dated merely because it is after 2024', system_prompt)
