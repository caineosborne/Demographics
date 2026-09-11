import unittest

from tools import list_country_names, normalise_country_name


class CountryNormalisationTests(unittest.TestCase):
    def test_normalises_case_to_the_database_country_name(self):
        self.assertEqual(normalise_country_name('japan'), 'Japan')
        self.assertEqual(normalise_country_name(' JAPAN '), 'Japan')
        self.assertEqual(normalise_country_name('JPN'), 'Japan')

    def test_normalises_taiwan_to_the_un_country_label(self):
        self.assertEqual(normalise_country_name('Taiwan'), 'China, Taiwan Province of China')

    def test_normalises_common_country_aliases_to_canonical_names(self):
        self.assertEqual(normalise_country_name('US'), 'United States of America')
        self.assertEqual(normalise_country_name('USA'), 'United States of America')
        self.assertEqual(normalise_country_name('United States'), 'United States of America')
        self.assertEqual(normalise_country_name('UK'), 'United Kingdom')

    def test_country_names_are_available_for_dropdowns(self):
        self.assertIn('Japan', list_country_names())
