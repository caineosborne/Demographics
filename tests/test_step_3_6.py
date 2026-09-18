import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

import api
from services import research_services
from core import research_store
from data import tools
from core.research import BossAgent, ResearchSkills, ReviewDecision, SearchCategory, SearchSettings, SummaryReview


class Step36ResearchControlsTests(unittest.TestCase):
    def test_direct_and_bulk_hunts_do_not_replace_saved_automatic_settings(self):
        saved = SearchSettings(
            categories=[SearchCategory(name='Saved population', query='saved population')],
            reddit_enabled=False, max_candidates=7,
        ).model_dump()
        for mode in ('direct', 'bulk'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, patch.object(
                tools, 'DB_PATH', Path(directory) / 'research.sqlite'
            ):
                research_store.save_settings(saved)
                hunt_settings = SearchSettings(
                    categories=[SearchCategory(
                        name='Generated hunt', query='Japan demographics', country_iso3='JPN',
                    )],
                    reddit_enabled=False, country_hunt_mode=mode,
                    country_hunt_iso3s=['JPN'], max_candidates=1,
                ).model_dump()
                list(BossAgent(providers={'tavily': lambda _category: []}).run(hunt_settings))
                self.assertEqual(research_store.load_settings({}), saved)

    def test_country_hunt_service_marks_generated_run_as_non_persisting(self):
        with patch.object(research_services, '_country_context', return_value={'iso3': 'JPN', 'label': 'Japan'}), \
             patch.object(research_services.research_store, 'upsert_country_hunt_queue'), \
             patch.object(research_services, '_start_research', return_value={'run_id': 'run-1'}) as start:
            research_services.start_country_hunt('jpn')
        self.assertFalse(start.call_args.kwargs['persist_settings'])

    def test_country_queue_is_durable_and_tracks_attempt_fields(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'DB_PATH', Path(directory) / 'research.sqlite'):
            research_store.upsert_country_hunt_queue([{'iso3': 'JPN', 'label': 'Japan'}], job_id='job-1')
            rows = research_store.list_country_hunt_queue()
        self.assertEqual(rows[0]['iso3'], 'JPN')
        self.assertEqual(rows[0]['country'], 'Japan')
        self.assertEqual(rows[0]['outcome'], 'queued')
        self.assertEqual(rows[0]['last_job_id'], 'job-1')
        self.assertIsNotNone(rows[0]['last_attempt_at'])

    def test_country_queue_can_attach_job_and_close_failed_attempt_without_run_id(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'DB_PATH', Path(directory) / 'research.sqlite'):
            research_store.upsert_country_hunt_queue([{'iso3': 'JPN', 'label': 'Japan'}])
            research_store.attach_country_hunt_job(['JPN'], 'job-2')
            research_store.finish_country_hunt_queue_for_job('job-2', outcome='error', next_eligible_at='2026-10-14T00:00:00+00:00')
            row = research_store.list_country_hunt_queue()[0]
        self.assertEqual(row['last_job_id'], 'job-2')
        self.assertEqual(row['outcome'], 'error')
        self.assertEqual(row['next_eligible_at'], '2026-10-14T00:00:00+00:00')

    def test_country_queue_accepts_per_country_retry_dates(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'DB_PATH', Path(directory) / 'research.sqlite'):
            research_store.upsert_country_hunt_queue([
                {'iso3': 'JPN', 'label': 'Japan'}, {'iso3': 'AUS', 'label': 'Australia'},
            ])
            research_store.mark_country_hunt_run('run-1', ['JPN', 'AUS'])
            research_store.finish_country_hunt_queue(
                'run-1', outcomes_by_iso3={'JPN': 'finding_ready', 'AUS': 'error'},
                successful_iso3s={'JPN'},
                clear_successful_iso3s={'AUS'},
                next_eligible_by_iso3={'JPN': '2026-10-14T00:00:00+00:00', 'AUS': '2026-09-15T00:00:00+00:00'},
            )
            rows = {row['iso3']: row for row in research_store.list_country_hunt_queue()}
        self.assertEqual(rows['JPN']['next_eligible_at'], '2026-10-14T00:00:00+00:00')
        self.assertEqual(rows['AUS']['next_eligible_at'], '2026-09-15T00:00:00+00:00')

    def test_recovering_orphaned_worker_interrupts_linked_country_queue(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'research.sqlite'
        ):
            job = research_store.create_job('country_search', {'country_iso3': 'JPN'})
            research_store.upsert_country_hunt_queue([{'iso3': 'JPN', 'label': 'Japan'}], job_id=job['id'])
            owner = research_store.owner_id()
            research_store.claim_job(job['id'], owner)
            with tools.get_connection() as connection:
                connection.execute(
                    "UPDATE worker_jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                    (job['id'],),
                )
            self.assertEqual(research_store.recover_orphaned_worker_jobs(), 1)
            row = research_store.list_country_hunt_queue()[0]
        self.assertEqual(row['outcome'], 'interrupted')
        self.assertIsNone(row['next_eligible_at'])
        self.assertEqual(row['last_job_id'], job['id'])

    def test_recovering_orphaned_run_interrupts_linked_country_queue(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'research.sqlite'
        ):
            run_id = research_store.start_run({'country_hunt_mode': 'direct'})
            research_store.upsert_country_hunt_queue([{'iso3': 'JPN', 'label': 'Japan'}])
            research_store.mark_country_hunt_run(run_id, ['JPN'])
            self.assertEqual(research_store.recover_orphaned_runs(), 1)
            row = research_store.list_country_hunt_queue()[0]
        self.assertEqual(row['outcome'], 'interrupted')
        self.assertIsNone(row['next_eligible_at'])
        self.assertEqual(row['last_run_id'], run_id)

    def test_direct_country_hunt_marks_mode_and_uses_iso3(self):
        with patch.object(research_services.tools, 'resolve_country_iso3', return_value='JPN'), \
             patch.object(research_services.tools, 'normalise_country_name', return_value='Japan'), \
             patch.object(research_services.research_store, 'upsert_country_hunt_queue'), \
             patch.object(research_services, '_start_research', return_value={'run_id': 'run-1', 'status': 'running'}) as start:
            result = research_services.start_country_hunt('jpn')
        settings = start.call_args.args[0]
        self.assertEqual(result['country_iso3'], 'JPN')
        self.assertEqual(settings['country_hunt_mode'], 'direct')
        self.assertEqual(settings['country_hunt_iso3s'], ['JPN'])
        self.assertEqual(settings['categories'][0]['country_iso3'], 'JPN')

    def test_direct_country_hunt_does_not_apply_stale_discovery_gate(self):
        result = MagicMock()
        result.model_dump.return_value = {'url': 'https://publisher.test/old-release'}
        skills = ResearchSkills(
            review_summaries=lambda rows, _criteria: [SummaryReview(candidate_id=i, decision='relevant', reason='figures') for i, _ in rows],
            fetch_article=lambda _url: 'national demographic figures',
            extract_useful_info=lambda *_args: {'result': result, 'storage': {'status': 'stored', 'id': 1}},
            review_full_article=lambda *_args: ReviewDecision(decision='relevant', reason='figures'),
        )
        settings = SearchSettings(
            categories=[SearchCategory(name='Japan', query='Japan demographics', country_iso3='JPN')],
            reddit_enabled=False, country_hunt_mode='direct', country_hunt_iso3s=['JPN'],
        ).model_dump()
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'DB_PATH', Path(directory) / 'research.sqlite'):
            rows = list(BossAgent(skills, {'tavily': lambda _category: [{
                'url': 'https://publisher.test/old-release', 'source': 'tavily', 'category': 'Japan',
                'title': 'Old national release', 'snippet': 'figures', 'published_date': '2020-01-01',
            }]}).run(settings))
            candidates = research_store.list_candidates(rows[-1][0])
        self.assertNotEqual(candidates[0]['status'], 'excluded_discovery')

    def test_country_queue_route_is_available(self):
        with patch.object(api.research_services, 'get_country_hunt_queue', return_value=[{'iso3': 'JPN', 'country': 'Japan', 'outcome': 'queued'}]):
            response = TestClient(api.create_app()).get('/api/v1/research/country-queue')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['items'][0]['iso3'], 'JPN')

    def test_frontend_declares_error_terminal_state_and_research_controls(self):
        from pathlib import Path
        client_script = Path('frontend/admin-assets/api-client.js').read_text()
        admin_script = Path('frontend/admin-assets/admin.js').read_text()
        self.assertIn('completed_with_errors', client_script)
        self.assertIn('runDiscovery', admin_script)
        self.assertIn('/api/v1/research/candidates/', admin_script)

    def test_candidate_detail_exposes_scope_and_recovery_audit(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'DB_PATH', Path(directory) / 'research.sqlite'):
            run_id = research_store.start_run({'categories': []})
            candidate_id = research_store.add_candidate(run_id, {
                'url': 'https://example.test/release', 'source': 'tavily', 'category': 'Japan',
                'country_iso3': 'JPN', 'alternative_sources': [{'url': 'https://mirror.test/release', 'status': 'accessed'}],
            })
            research_store.update_candidate(candidate_id, status='excluded_country_mismatch',
                                            extraction={'geography_iso3': 'AUS'}, error='scope mismatch')
            detail = research_store.get_candidate(candidate_id)
        self.assertEqual(detail['details']['scope_country_iso3'], 'JPN')
        self.assertTrue(detail['details']['scope_mismatch'])
        self.assertEqual(detail['details']['alternative_sources'][0]['status'], 'accessed')
        self.assertEqual(detail['details']['error'], 'scope mismatch')


if __name__ == '__main__':
    unittest.main()
