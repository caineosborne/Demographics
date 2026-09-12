import unittest
from unittest.mock import patch

import research_services


class ResearchServiceTests(unittest.TestCase):
    def test_country_hunt_resolves_iso3_and_persists_it_in_settings(self):
        with patch.object(research_services.tools, 'resolve_country_iso3', return_value='JPN'), \
             patch.object(research_services.tools, 'normalise_country_name', return_value='Japan'), \
             patch.object(research_services, '_start_research', return_value={'run_id': 'run-1', 'status': 'running'}) as start:
            result = research_services.start_country_hunt('jpn')

        self.assertEqual(result['country_iso3'], 'JPN')
        settings = start.call_args.args[0]
        self.assertEqual(settings['categories'][0]['country_iso3'], 'JPN')
        self.assertIn('Japan', settings['categories'][0]['query'])

    def test_bulk_country_hunt_rejects_more_than_one_hundred_countries(self):
        with self.assertRaisesRegex(ValueError, 'between 1 and 100'):
            research_services.start_bulk_country_hunt(['JPN'] * 101)


if __name__ == '__main__':
    unittest.main()
