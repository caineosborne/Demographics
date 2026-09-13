"""Focused contracts for the non-Gradio manual analysis review desk."""

import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import research_store
import research_services
import tools


FINDING = {
    'title': 'Population release', 'url': 'https://example.test/release',
    'source': 'Example statistics office', 'site_seen': 'example.test',
    'geography': 'Japan', 'geography_iso3': 'JPN', 'effective_date': '2023-01-01',
    'statistics': {'population': {'value': 124600000, 'unit': 'people',
                                  'evidence_excerpt': 'Population was 124.6 million people in 2023.',
                                  'measured_period': '2023'}},
}


class Step35ManualAnalysisTests(unittest.TestCase):
    def test_http_boundary_requires_one_absolute_url(self):
        client = TestClient(api.create_app())
        for value in ('notes https://example.test/release', 'https://one.test https://two.test', 'ftp://example.test/release'):
            response = client.post('/api/v1/analysis/jobs', json={'url': value})
            self.assertEqual(response.status_code, 422)

    def test_analysis_creates_reviewable_draft_without_storing_finding(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'db.sqlite'
        ), patch.object(tools, 'get_page_text', return_value='article text'), patch.object(
            research_services.agents, 'extract_from_page_text',
            return_value={'result': FINDING, 'validation': {'status': 'validated'}, 'storage': {'status': 'validated'}},
        ), patch.object(research_services.agents, 'research_agent') as graph_agent:
            job = research_services.start_manual_analysis(FINDING['url'], compare=False)
            for _ in range(100):
                current = research_services.get_manual_analysis(job['id'])
                if current['status'] == 'complete':
                    break
                time.sleep(0.02)
            self.assertEqual(current['status'], 'complete')
            self.assertEqual(current['draft']['status'], 'pending_review')
            self.assertEqual(tools.list_webpage_findings(), [])
            graph_agent.assert_not_called()

    def test_edit_then_approve_stores_only_edited_draft(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'db.sqlite'
        ):
            draft = __import__('research_store').create_analysis_draft('job-1', finding=FINDING, validation={'status': 'validated'})
            edited = {**FINDING, 'comments': 'Reviewed by analyst.'}
            result = research_services.edit_analysis_draft(draft['id'], edited, expected_revision=1)
            self.assertEqual(result['revision'], 2)
            with patch.object(tools, 'store_webpage_finding', return_value={'id': 8, 'status': 'stored'}) as store:
                approved = research_services.approve_analysis_draft(draft['id'])
            self.assertEqual(approved['status'], 'approved')
            self.assertEqual(approved['finding']['comments'], 'Reviewed by analyst.')
            store.assert_called_once()

    def test_reject_closes_draft_without_storage_and_idempotency_reuses_job(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'db.sqlite'
        ), patch.object(research_services, '_run_manual', return_value=None):
            first = research_services.start_manual_analysis('https://example.test/release', idempotency_key='key-1')
            second = research_services.start_manual_analysis('https://example.test/release', idempotency_key='key-1')
            self.assertEqual(first['id'], second['id'])
            time.sleep(0.05)

    def test_concurrent_idempotency_reservation_creates_one_durable_job(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'db.sqlite'
        ), patch.object(research_services, '_run_manual', return_value=None):
            def submit(_index):
                return research_services.start_manual_analysis(
                    'https://example.test/release', idempotency_key='concurrent-key'
                )

            with ThreadPoolExecutor(max_workers=8) as executor:
                responses = list(executor.map(submit, range(8)))
            self.assertEqual({response['id'] for response in responses}, {responses[0]['id']})
            with tools.get_connection() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM worker_jobs WHERE kind = 'manual_analysis'"
                ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_duplicate_and_suppressed_sources_are_reference_only(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'db.sqlite'
        ), patch.object(tools, 'find_webpage_finding_by_url', return_value={
            'id': 41, 'source_url': FINDING['url'], 'finding': FINDING,
        }), patch.object(tools, 'get_page_text', side_effect=AssertionError('must not fetch')):
            job = research_services.start_manual_analysis(FINDING['url'], compare=False)
            current = self._wait(job['id'])
            self.assertEqual(current['draft']['status'], 'existing_record')
            self.assertEqual(current['draft']['reference_finding_id'], 41)
            with self.assertRaises(ValueError):
                research_services.approve_analysis_draft(current['draft']['id'])

        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'db.sqlite'
        ), patch.object(tools, 'blocked_source_urls', return_value={
            'https://example.test/release',
        }), patch.object(tools, 'get_page_text', side_effect=AssertionError('must not fetch')):
            job = research_services.start_manual_analysis(FINDING['url'], compare=False)
            current = self._wait(job['id'])
            self.assertEqual(current['draft']['status'], 'suppressed_source')
            self.assertEqual(research_services.reject_analysis_draft(current['draft']['id'])['status'], 'suppressed_source')

    def test_terminal_actions_are_idempotent_and_audited_once(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'db.sqlite'
        ), patch.object(tools, 'store_webpage_finding', return_value={'id': 8, 'status': 'stored'}) as store:
            draft = research_store.create_analysis_draft('job-1', finding=FINDING, validation={'status': 'validated'})
            approved = research_services.approve_analysis_draft(draft['id'])
            repeated = research_services.approve_analysis_draft(draft['id'])
            self.assertEqual(repeated['status'], 'approved')
            store.assert_called_once()
            actions = research_store.list_analysis_draft_actions(draft['id'])
            self.assertEqual([row['action'] for row in actions], ['approved', 'created'])

    def test_http_draft_routes_use_declared_schema_and_revision(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'db.sqlite'
        ):
            draft = research_store.create_analysis_draft('job-1', finding=FINDING, validation={'status': 'validated'})
            client = TestClient(api.create_app())
            fetched = client.get(f'/api/v1/analysis/drafts/{draft["id"]}')
            edited = client.patch(f'/api/v1/analysis/drafts/{draft["id"]}', json={
                'finding': {**FINDING, 'comments': 'Edited'}, 'expected_revision': 1,
            })
            actions = client.get(f'/api/v1/analysis/drafts/{draft["id"]}/actions')
            self.assertEqual(fetched.status_code, 200)
            self.assertEqual(fetched.json()['status'], 'pending_review')
            self.assertEqual(edited.json()['revision'], 2)
            self.assertEqual([item['action'] for item in actions.json()['items']], ['edited', 'created'])
            self.assertIn('AnalysisDraftResponse', client.app.openapi()['components']['schemas'])

    def _wait(self, job_id):
        for _ in range(100):
            current = research_services.get_manual_analysis(job_id)
            if current['status'] in {'complete', 'failed'}:
                return current
            time.sleep(0.02)
        self.fail('analysis job did not reach a terminal state')


if __name__ == '__main__':
    unittest.main()
