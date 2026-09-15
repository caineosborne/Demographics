"""Durability and boundary-contract tests for Phase 3 Step 3.3."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from services import read_services, research_services
from core import research_store
from data import tools
import worker


class Step33DurabilityTests(unittest.TestCase):
    def test_live_owner_is_not_recovered_until_lease_expires(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'durable.sqlite'
        ):
            job = research_store.create_job('news_search', {'settings': {}})
            owner = research_store.owner_id()
            research_store.claim_job(job['id'], owner)
            research_store.acquire_worker_lock('discovery', job['id'], owner)

            self.assertEqual(research_store.recover_orphaned_worker_jobs(), 0)
            self.assertEqual(research_store.get_job(job['id'])['status'], 'running')

            with tools.get_connection() as connection:
                connection.execute(
                    "UPDATE worker_jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                    (job['id'],),
                )
            self.assertEqual(research_store.recover_orphaned_worker_jobs(), 1)
            self.assertEqual(research_store.get_job(job['id'])['status'], 'interrupted')
            research_store.acquire_worker_lock('discovery', 'next-job', research_store.owner_id())

    def test_terminal_stop_preserves_existing_run_status(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'durable.sqlite'
        ):
            run_id = research_store.start_run({'categories': []})
            research_store.finish_run(run_id, 'complete')
            self.assertEqual(research_services.stop_research(run_id), {
                'run_id': run_id, 'status': 'complete'
            })

    def test_external_live_run_stop_is_cooperative_until_worker_observes_it(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'durable.sqlite'
        ):
            owner = research_store.owner_id()
            run_id = research_store.start_run({'categories': []}, owner_id=owner)
            self.assertEqual(research_services.stop_research(run_id), {
                'run_id': run_id, 'status': 'stopping'
            })
            self.assertEqual(research_store.get_run(run_id)['status'], 'stopping')
            self.assertEqual(research_services.stop_research(run_id)['status'], 'stopping')
            # A live owner remains protected while it drains its work.
            research_store.heartbeat_worker('unrelated-job', owner)
            self.assertEqual(research_store.recover_orphaned_runs(), 0)
            research_store.finish_run(run_id, 'interrupted')
            self.assertEqual(research_services.stop_research(run_id)['status'], 'interrupted')

    def test_retry_clears_terminal_job_artifacts(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'durable.sqlite'
        ):
            job = research_store.create_job('maintenance')
            owner = research_store.owner_id()
            research_store.claim_job(job['id'], owner)
            research_store.update_job(job['id'], status='failed', error='old error',
                                      result={'old': True}, progress={'stage': 'failed'}, owner_id=None)
            retried = research_store.claim_job(job['id'], research_store.owner_id(), retry=True)
        self.assertEqual(retried['status'], 'running')
        self.assertIsNone(retried['error'])
        self.assertIsNone(retried['result'])
        self.assertEqual(retried['progress'], {})

    def test_second_owner_cannot_claim_a_live_job_and_export_is_not_a_command(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'durable.sqlite'
        ):
            job = research_store.create_job('maintenance')
            research_store.claim_job(job['id'], research_store.owner_id())
            with self.assertRaisesRegex(RuntimeError, 'not claimable'):
                research_store.claim_job(job['id'], research_store.owner_id())
        with self.assertRaises(SystemExit):
            worker._parser().parse_args(['export', 'release.json'])

    def test_http_missing_resources_are_consistent_and_gap_scope_is_explicit(self):
        choices = [{'name': 'Japan', 'iso3': 'JPN'}, {'name': 'Jamaica', 'iso3': 'JAM'}]
        with patch.object(read_services, 'list_country_choices', return_value=choices), \
             patch.object(read_services, 'list_webpage_findings', return_value=[]), \
             patch.object(read_services, '_strict_iso3', side_effect=lambda value: str(value).upper()):
            client = TestClient(api.create_app())
            preview = client.post('/api/v1/research/country-gap-preview', json={
                'prefix': 'Ja', 'scope_iso3s': ['JPN'], 'country_count': 1,
            })
            missing_job = client.get('/api/v1/worker/jobs/missing')
            missing_candidate = client.get('/api/v1/research/candidates/999')

        self.assertEqual(preview.status_code, 200)
        self.assertEqual([item['iso3'] for item in preview.json()['selected']], ['JPN'])
        self.assertEqual(preview.json()['excluded'][0]['reason'], 'outside_requested_scope')
        self.assertEqual(missing_job.status_code, 404)
        self.assertEqual(missing_candidate.status_code, 404)

    def test_public_list_contract_redacts_storage_payloads(self):
        with patch.object(read_services, 'list_webpage_findings', return_value=[{
            'ID': 7, 'Country': 'Japan', 'Extracted JSON': '{"secret": true}',
            'Search run ID': 'private-run', 'Extracted at (UTC)': 'now',
        }]):
            result = read_services.list_findings()
        self.assertEqual(result, [{'ID': 7, 'Country': 'Japan'}])

    def test_http_worker_and_candidate_details_use_the_temporary_database(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'durable.sqlite'
        ):
            job = research_store.create_job('maintenance', {'private': 'not returned'})
            run_id = research_store.start_run({'categories': []})
            candidate_id = research_store.add_candidate(run_id, {
                'url': 'https://example.test/release', 'source': 'tavily',
                'category': 'Population', 'title': 'Release', 'raw': 'secret body',
            })
            client = TestClient(api.create_app())
            worker_response = client.get(f'/api/v1/worker/jobs/{job["id"]}')
            candidate_response = client.get(f'/api/v1/research/candidates/{candidate_id}')

        self.assertEqual(worker_response.status_code, 200)
        self.assertNotIn('private', worker_response.json())
        self.assertEqual(candidate_response.status_code, 200)
        self.assertNotIn('raw', candidate_response.json()['details'])

    def test_openapi_declares_stable_fields_for_step_3_3_models(self):
        schemas = api.create_app().openapi()['components']['schemas']
        self.assertIn('status', schemas['MutationResponse']['properties'])
        self.assertIn('status', schemas['JobStartResponse']['properties'])
        self.assertIn('candidates', schemas['RunDetailResponse']['properties'])
        self.assertIn('details', schemas['CandidateDetailResponse']['properties'])
        self.assertIn('selected', schemas['CountryGapPreviewResponse']['properties'])


if __name__ == '__main__':
    unittest.main()
