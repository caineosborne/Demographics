import unittest
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Tests use fake model results and must not load local credentials or tracing.
with patch('dotenv.load_dotenv'), patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-key'}):
    from main import (
        delete_article_everywhere_simple, delete_metric_from_database,
        load_database_table_row, remove_database_metric, run_pipeline,
    )
    from tools import PageAccessError


class PipelineTests(unittest.TestCase):
    def test_delete_everywhere_removes_the_database_finding(self):
        with patch('main.delete_webpage_finding') as delete, \
             patch('main.build_visualisation_for_latest_analysis', return_value=('population', 'flows', 'status')):
            result = delete_article_everywhere_simple('Algeria', None, ['population'], '42', {})
        delete.assert_called_once_with(42)
        self.assertIn('database and graphs', result[-1])
        self.assertIn('#42', result[-1])

    def test_database_selection_uses_the_id_in_the_displayed_row(self):
        event = SimpleNamespace(index=[0, 0], row_value=[7, 'Taiwan'])
        with patch('main.load_database_record', return_value=('7', '{}', 'loaded', 'link')) as load:
            result = load_database_table_row(event)
        load.assert_called_once_with(7)
        self.assertEqual(result[0], '7')

    def test_database_metric_delete_reloads_the_saved_record(self):
        with patch('main.delete_finding_metric') as delete, \
             patch('main.load_database_record', return_value=('7', '{"statistics": {}}', 'loaded', 'link')), \
             patch('main.load_database_table', return_value='table'), \
             patch('main.load_database_country_summary', return_value='summary'):
            result = remove_database_metric('7', 'births', 3)
        delete.assert_called_once_with(7, 'births')
        self.assertEqual(result, (
            'table', 'summary', 4, '7', '{"statistics": {}}',
            'Deleted births from finding #7. Other metrics and the source URL were retained.', 'link',
        ))

    def test_visual_metric_delete_redraws_from_durable_storage(self):
        with patch('main.delete_finding_metric') as delete, \
             patch('main.build_visualisation_for_latest_analysis', return_value=('population', 'flows', 'status')):
            result = delete_metric_from_database('Japan', None, ['population', 'births'], '7', 'births', {})
        delete.assert_called_once_with(7, 'births')
        self.assertEqual(result[0:2], ('population', 'flows'))
        self.assertIn('other metrics were retained', result[-1])

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
