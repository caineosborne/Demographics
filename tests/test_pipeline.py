import unittest
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Tests use fake model results and must not load local credentials or tracing.
with patch('dotenv.load_dotenv'), patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-key'}):
    from main import load_database_table_row, run_pipeline
from tools import PageAccessError


class PipelineTests(unittest.TestCase):
    def test_database_selection_uses_the_id_in_the_displayed_row(self):
        event = SimpleNamespace(index=[0, 0], row_value=[7, 'Taiwan'])
        with patch('main.load_database_record', return_value=('7', '{}', 'loaded', 'link')) as load:
            result = load_database_table_row(event)
        load.assert_called_once_with(7)
        self.assertEqual(result[0], '7')

    def test_status_stream_and_original_outputs(self):
        research = MagicMock()
        research.model_dump.return_value = {'summary': 'Summary', 'comments': None, 'title': 'Article'}
        comparison = MagicMock()
        comparison.model_dump.return_value = {'overall_assessment': 'Assessment', 'notes': None}
        events = iter([
            ('custom', {'fetch_status': 'Trying Requests'}),
            ('custom', {'fetch_status': 'Trying Playwright'}),
            ('values', {'result': research, 'comparison': comparison, 'un_data': [], 'storage': {'status': 'stored', 'id': 1}}),
        ])
        with patch('main.graph.stream', return_value=events):
            updates = list(run_pipeline('Summarise a URL'))
        self.assertEqual([row[4] for row in updates], ['Starting analysis', 'Trying Requests', 'Trying Playwright', 'Complete'])
        self.assertEqual(updates[-1][:4], ({'title': 'Article', 'storage': {'status': 'stored', 'id': 1}}, 'Summary', {'sql_tool_data': []}, 'Assessment'))

    def test_fetch_failure_clears_outputs(self):
        with patch('main.graph.stream', side_effect=PageAccessError('Both fetch methods failed')):
            updates = list(run_pipeline('Summarise a URL'))
        self.assertIsNone(updates[-1][0])
        self.assertIsNone(updates[-1][2])
        self.assertIn('Both fetch methods failed', updates[-1][1])
        self.assertTrue(updates[-1][4].startswith('Failed'))

    def test_log_events_are_streamed_to_the_frontend(self):
        research = MagicMock()
        research.model_dump.return_value = {'summary': 'Summary', 'comments': None, 'title': 'Article'}
        comparison = MagicMock()
        comparison.model_dump.return_value = {'overall_assessment': 'Assessment', 'notes': None}
        events = iter([
            ('custom', {'log': '[Research agent] calling tool=get_page_text'}),
            ('values', {'result': research, 'comparison': comparison, 'un_data': [], 'storage': {}}),
        ])
        with patch('main.graph.stream', return_value=events):
            updates = list(run_pipeline('Summarise a URL'))
        self.assertIn('[Research agent] calling tool=get_page_text', updates[-1][6])
