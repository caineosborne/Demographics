import os
import unittest
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

with patch('dotenv.load_dotenv'), patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-key'}):
    import agents


class ModelHandoffTests(unittest.TestCase):
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
            agents.research_agent({'messages': [HumanMessage(content='Summarise the URL')]})
            messages = extraction.invoke.call_args.args[0]
            self.assertEqual(extraction.invoke.return_value.url, 'https://example.test')
            self.assertIsInstance(messages[-1], HumanMessage)
            self.assertTrue(any(isinstance(m, ToolMessage) and '123456' in m.content for m in messages))

    def test_comparison_handoff_ends_with_user_after_research_history(self):
        research = MagicMock()
        research.model_dump_json.return_value = '{"title": "Article"}'

        def check_request(messages):
            self.assertIsInstance(messages[-1], HumanMessage)
            self.assertIn('Research JSON:', messages[-1].content)
            return AIMessage(content='No lookup')

        with patch.object(agents, 'sql_llm_required') as sql, patch.object(agents, 'resolve_country_iso3', return_value='AUS'):
            sql.invoke.side_effect = check_request
            agents.compare_to_un({
                'messages': [HumanMessage(content='Summarise the URL'), AIMessage(content='Article retrieved.')],
                'result': research,
            })
            sql.invoke.assert_called_once()
