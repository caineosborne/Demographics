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
        research = MagicMock()
        sql_tool = MagicMock(name='population_tool')
        sql_tool.name = 'get_population_forecast'
        sql_tool.invoke.return_value = [{'Year': 2026, 'Total Births': 100}]
        with (
            patch('temporal_context.date') as clock,
            patch.object(agents, 'web_llm') as web,
            patch.object(agents, 'research_llm') as extraction,
            patch.object(agents, 'sql_llm_required') as sql_required,
            patch.object(agents, 'sql_llm') as sql,
            patch.object(agents, 'comparison_llm') as comparison,
            patch.object(agents, 'SQL_TOOLS', [sql_tool]),
            patch.object(agents, 'store_webpage_finding', return_value={'status': 'stored', 'id': 1}),
            patch.object(agents, 'resolve_country_iso3', return_value='AUS'),
        ):
            clock.today.return_value = date(2026, 9, 10)
            web.invoke.return_value = AIMessage(content='Retrieved article')
            extraction.invoke.return_value = research
            sql_required.invoke.return_value = AIMessage(content='', tool_calls=[{
                'name': 'get_population_forecast',
                'args': {'country_iso3': 'AUS', 'years': [2026]}, 'id': 'lookup',
            }])
            sql.invoke.return_value = AIMessage(content='Lookup complete')
            agents.research_agent({'messages': []})
            agents.compare_to_un({'messages': [], 'result': research})
            for model in (web, extraction, sql_required, sql, comparison):
                with self.subTest(model=model):
                    system_prompt = model.invoke.call_args.args[0][0].content
                    self.assertIn('2026-09-10', system_prompt)
                    self.assertIn('vintage/effective as-of year is 2024', system_prompt)
                    self.assertIn('historical estimates cover 1950–2023', system_prompt)
                    self.assertIn('not future-dated merely because it is after 2024', system_prompt)
