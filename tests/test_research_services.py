import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import research_services
import research_store
import tools


class ResearchServiceTests(unittest.TestCase):
    def test_orphaned_worker_job_is_interrupted_and_its_lock_is_released(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, 'DB_PATH', Path(directory) / 'test.sqlite'
        ):
            job = research_store.create_job('news_search', {'settings': {}})
            research_store.update_job(job['id'], status='running', increment_attempts=True)
            research_store.acquire_worker_lock('discovery', job['id'])

            self.assertEqual(research_store.recover_orphaned_worker_jobs(), 1)
            self.assertEqual(research_store.get_job(job['id'])['status'], 'interrupted')
            research_store.acquire_worker_lock('discovery', 'next-job')

    def test_country_hunt_resolves_iso3_and_persists_it_in_settings(self):
        with patch.object(research_services.tools, 'resolve_country_iso3', return_value='JPN'), \
             patch.object(research_services.tools, 'normalise_country_name', return_value='Japan'), \
             patch.object(research_services, '_start_research', return_value={'run_id': 'run-1', 'status': 'running'}) as start:
            result = research_services.start_country_hunt('jpn')

        self.assertEqual(result['country_iso3'], 'JPN')
        settings = start.call_args.args[0]
        self.assertEqual(settings['categories'][0]['country_iso3'], 'JPN')
        self.assertIn('Japan', settings['categories'][0]['query'])
        self.assertEqual(settings['categories'][0]['topic'], 'general')

    def test_country_hunt_allows_the_user_to_choose_news(self):
        with patch.object(research_services, '_country_context', return_value={'iso3': 'JPN', 'label': 'Japan'}), \
             patch.object(research_services.research_store, 'upsert_country_hunt_queue'), \
             patch.object(research_services, '_start_research', return_value={'run_id': 'run-1', 'status': 'running'}) as start:
            research_services.start_country_hunt('jpn', topic='news')

        settings = start.call_args.args[0]
        self.assertEqual(settings['categories'][0]['topic'], 'news')

    def test_bulk_country_hunt_rejects_more_than_one_hundred_countries(self):
        with self.assertRaisesRegex(ValueError, 'between 1 and 100'):
            research_services.start_bulk_country_hunt(['JPN'] * 101)


if __name__ == '__main__':
    unittest.main()
