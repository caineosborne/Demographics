import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import visualisation


class VisualisationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "visualisation.sqlite"
        with sqlite3.connect(self.db_path) as conn:
            columns = ', '.join(f'"{column}" TEXT' for _, column, _, _ in visualisation.METRICS.values())
            for table in ("estimates", "medium_variant"):
                conn.execute(f'CREATE TABLE {table} (Country TEXT, Year TEXT, {columns})')
            for year in range(2014, 2024):
                conn.execute('INSERT INTO estimates VALUES (?, ?, ?, ?, ?, ?, ?, ?)', ('Japan', str(year), '100', '1', '2', '-1', '0', '1.5'))
            for year in range(2024, 2034):
                conn.execute('INSERT INTO medium_variant VALUES (?, ?, ?, ?, ?, ?, ?, ?)', ('Japan', str(year), '101', '1.1', '2.1', '-1', '-0.1', '1.4'))
            conn.execute('''CREATE TABLE webpage_findings (
                id INTEGER PRIMARY KEY, source_url TEXT, effective_date TEXT,
                population_value REAL, official_source INTEGER, quoted_source TEXT,
                quoted_source_url TEXT, extracted_at TEXT, finding_json TEXT)''')
            finding = {
                'geography': 'Japan', 'source': 'Reuters', 'url': 'https://example.test/article',
                'quoted_source': 'Japan Statistics Bureau',
                'statistics': {
                    'population': {'value': 102000}, 'births': {'value': 1000},
                    'deaths': {'value': 2000}, 'natural_change': {'value': -1000},
                    'net_overseas_migration': {'value': 200},
                    'total_fertility_rate': {'value': 1.2},
                },
            }
            conn.execute('INSERT INTO webpage_findings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         (1, finding['url'], '2026-06-30', 102000, 0, finding['quoted_source'], None,
                          '2026-09-10T00:00:00+00:00', json.dumps(finding)))
            conn.execute('''CREATE TABLE wpp_release_history (
                revision INTEGER, Country TEXT, "ISO3 Alpha-code" TEXT, Year INTEGER,
                "Population 1 Jul" REAL, "Total Births" REAL, "Total Deaths" REAL,
                "Natural Change" REAL, "Net Migration" REAL,
                "Total Fertility Rate (live births per woman)" REAL,
                cadence_years INTEGER, source_url TEXT)''')
            for revision, years in ((2022, range(2018, 2023)), (2017, range(2010, 2021, 5)), (2012, range(2005, 2016, 5))):
                for year in years:
                    conn.execute('INSERT INTO wpp_release_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                 (revision, 'Japan', 'JPN', year, 99, 1, 2, -1, 0, 1.5,
                                  1 if revision == 2022 else 5, 'https://population.un.org/wpp'))

    def tearDown(self):
        self.tempdir.cleanup()

    def connection(self):
        return sqlite3.connect(self.db_path)

    def test_charts_include_un_history_forecast_and_current_finding(self):
        with patch.object(visualisation, 'get_connection', self.connection), patch.object(visualisation, 'initialise_findings_table'):
            population, flows, message = visualisation.build_visualisation(
                'Japan', list(visualisation.METRICS)
            )
        self.assertIn('10 UN historical years, 10 UN forecast years, and 1 webpage finding', message)
        self.assertEqual({trace.name for trace in population.data}, {
            'UN historic', 'UN forecast',
            'Stored estimates (◆ official · ● secondary)', 'Current stored estimate',
        })
        self.assertEqual({trace.name for trace in flows.data}, {
            'UN historic', 'UN forecast', 'Stored estimates (◆ official · ● secondary)', 'Current stored estimate',
        })
        self.assertEqual(flows.layout.height, 1250)

    def test_tfr_uses_births_per_woman_without_people_scaling(self):
        with patch.object(visualisation, 'get_connection', self.connection), patch.object(visualisation, 'initialise_findings_table'):
            _, flows, _ = visualisation.build_visualisation('Japan', ['total_fertility_rate'])
        historic = next(trace for trace in flows.data if trace.name == 'UN historic')
        stored = next(trace for trace in flows.data if trace.name == 'Stored estimates (◆ official · ● secondary)')
        self.assertEqual(historic.y[0], 1.5)
        self.assertEqual(stored.y[0], 1.2)
        self.assertEqual(flows.layout.yaxis.title.text, 'Live births per woman')
        self.assertIn('y:,.2f', historic.hovertemplate)
        self.assertIn('y:,.2f', stored.hovertemplate)
        self.assertIn('Source status', stored.hovertemplate)

    def test_metrics_can_be_disabled(self):
        with patch.object(visualisation, 'get_connection', self.connection), patch.object(visualisation, 'initialise_findings_table'):
            population, flows, _ = visualisation.build_visualisation('Japan', ['population'])
        self.assertTrue(population.data)
        self.assertFalse(flows.data)

    def test_selected_prior_revisions_are_dotted_optional_overlays(self):
        with patch.object(visualisation, 'get_connection', self.connection), patch.object(visualisation, 'initialise_findings_table'), patch.object(visualisation, 'resolve_country_iso3', return_value='JPN'):
            population, _, message = visualisation.build_visualisation('Japan', ['population'], alternate_revisions=[2022, 2017, 2012])
        traces = {trace.name: trace for trace in population.data}
        self.assertEqual(traces['UN historic'].line.dash, 'solid')
        self.assertEqual(traces['UN 2022 alternate history'].line.dash, 'dot')
        self.assertEqual(traces['UN 2017 alternate history'].line.dash, 'dot')
        self.assertEqual(traces['UN 2012 alternate history'].line.dash, 'dot')
        self.assertEqual(list(traces['UN 2017 alternate history'].x), ['2015-07-01', '2020-07-01'])
        self.assertIn('WPP 2024 remains primary', message)
        self.assertIn('WPP 2022, 2017, 2012', message)

    def test_requires_country_before_querying(self):
        population, flows, message = visualisation.build_visualisation('', ['population'])
        self.assertFalse(population.data)
        self.assertFalse(flows.data)
        self.assertIn('Enter the country name', message)

    def test_blank_country_uses_the_latest_analysis_country(self):
        with patch.object(visualisation, 'get_connection', self.connection), patch.object(visualisation, 'initialise_findings_table'):
            population, _, message = visualisation.build_visualisation_for_latest_analysis('', 'Japan', ['population'])
        self.assertTrue(population.data)
        self.assertIn('10 UN historical years', message)

    def test_country_matching_is_case_insensitive_for_un_and_stored_data(self):
        with patch.object(visualisation, 'get_connection', self.connection), patch.object(visualisation, 'initialise_findings_table'), patch.object(visualisation, 'normalise_country_name', return_value='Japan'):
            population, _, message = visualisation.build_visualisation('japan', ['population'])
        self.assertIn('for Japan', message)
        self.assertIn('UN historic', {trace.name for trace in population.data})
        self.assertIn('Current stored estimate', {trace.name for trace in population.data})

    def test_population_millions_are_scaled_and_changes_are_not_plotted_as_totals(self):
        millions = {'title': 'Population reaches 342 million', 'summary': 'The population was 342.28 million.',
                    'statistics': {'population': {'value': 342.28}}}
        change = {'title': 'Population increase by 21.5 million', 'summary': 'An increase by 21.5 million is expected.',
                  'statistics': {'population': {'value': 21500000}}}
        self.assertEqual(visualisation._metric_value(millions, 'population'), 342280000)
        self.assertIsNone(visualisation._metric_value(change, 'population'))
