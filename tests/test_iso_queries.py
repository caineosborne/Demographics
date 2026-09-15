import unittest

from data.tools import get_list_of_countries, get_population_forecast, resolve_country_iso3


class IsoQueryTests(unittest.TestCase):
    def test_resolves_and_queries_japan_by_iso3(self):
        self.assertEqual(resolve_country_iso3('Japan'), 'JPN')
        rows = get_population_forecast.invoke({'country_iso3': 'jpn', 'years': [2026]})
        self.assertEqual(rows[0]['Country'], 'Japan')
        self.assertEqual(rows[0]['ISO3'], 'JPN')

    def test_country_reference_retains_names_and_codes(self):
        countries = get_list_of_countries.invoke({})
        self.assertIn({'Country': 'Japan', 'ISO3': 'JPN'}, countries)

    def test_rejects_country_names_for_iso_query(self):
        with self.assertRaisesRegex(ValueError, 'country_iso3'):
            get_population_forecast.invoke({'country_iso3': 'Japan', 'years': [2026]})
