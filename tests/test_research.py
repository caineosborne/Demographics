import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import tools
import research_store as store
from research import BossAgent, ResearchSkills, ReviewDecision, SearchSettings, canonical_url, reddit_links, tavily_links, SearchCategory


def candidate(url='https://example.test/article', **kwargs):
    return {'url': url, 'source': 'tavily', 'category': 'Population', 'title': 'New figures', 'snippet': 'A release', **kwargs}


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(tools, 'DB_PATH', Path(self.directory.name) / 'test.sqlite')
        self.db_patch.start()
        self.settings = SearchSettings(categories=[SearchCategory(name='Population', query='population')], reddit_enabled=False).model_dump()
        self.review = MagicMock(return_value=ReviewDecision(decision='relevant', reason='National release'))
        result = MagicMock()
        result.model_dump.return_value = {'url': 'https://example.test/article'}
        comparison = MagicMock()
        comparison.model_dump.return_value = {'overall_assessment': 'Compared'}
        self.extract = MagicMock(return_value={'result': result, 'storage': {'status': 'stored', 'id': 7}})
        self.compare = MagicMock(return_value={'comparison': comparison, 'un_data': []})
        self.fetch = MagicMock(return_value='Full national demographic release')
        self.skills = ResearchSkills(self.review, self.fetch, self.extract, self.compare)

    def tearDown(self):
        self.db_patch.stop()
        self.directory.cleanup()

    def run_boss(self, rows, providers=None):
        updates = list(BossAgent(self.skills, providers or {'tavily': lambda c: rows}).run(self.settings))
        return store.list_candidates(updates[-1][0])

    def test_reject_summary_never_fetches(self):
        self.review.return_value = ReviewDecision(decision='irrelevant', reason='Opinion only')
        row = self.run_boss([candidate()])[0]
        self.assertEqual(row['status'], 'irrelevant_summary')
        self.assertEqual(row['summary_reason'], 'Opinion only')
        self.fetch.assert_not_called()
        self.compare.assert_not_called()

    def test_unclear_summary_fetches_and_full_review_gates_extraction(self):
        for verdict, expected in [('irrelevant', 'irrelevant_full_text'), ('unclear', 'needs_review'), ('relevant', 'complete')]:
            with self.subTest(verdict=verdict):
                self.review.side_effect = [ReviewDecision(decision='unclear', reason='No figures in snippet'),
                                           ReviewDecision(decision=verdict, reason='Full page evidence')]
                row = self.run_boss([candidate()])[0]
                self.assertEqual(row['status'], expected)
                self.assertEqual(store.get_candidate(row['id'])['details']['full_text'], self.fetch.return_value)
        self.extract.assert_called_once()
        self.compare.assert_called_once()
        provenance = self.extract.call_args.args[2]
        self.assertEqual(provenance['submission_type'], 'automatic')
        self.assertEqual(provenance['search_candidate_id'], row['id'])

    def test_duplicates_preserve_both_discoveries_but_process_once(self):
        rows = self.run_boss([candidate(), candidate('https://example.test/article?utm_source=reddit#heading', source='reddit')])
        self.assertEqual([r['status'] for r in rows], ['duplicate', 'complete'])
        self.fetch.assert_called_once()
        self.assertEqual(rows[0]['source'], 'reddit')

    def test_provider_and_article_failures_do_not_stop_other_candidates(self):
        self.settings['reddit_enabled'] = True
        self.fetch.side_effect = [tools.PageAccessError('Unavailable'), 'Actual release']
        providers = {'tavily': lambda c: [candidate(), candidate('https://example.test/second')],
                     'reddit': MagicMock(side_effect=RuntimeError('Reddit blocked'))}
        rows = self.run_boss([], providers)
        self.assertEqual([r['status'] for r in rows], ['complete', 'error'])
        self.assertEqual(store.list_runs()[0]['status'], 'completed_with_errors')
        self.assertIn('Reddit blocked', store.list_runs()[0]['events_json'])
        self.compare.assert_called_once()

    def test_existing_finding_is_not_downloaded_or_relabelled(self):
        tools.store_webpage_finding({'url': 'https://example.test/article', 'statistics': {}})
        row = self.run_boss([candidate()])[0]
        self.assertEqual(row['status'], 'duplicate')
        self.fetch.assert_not_called()
        self.assertEqual(tools.list_webpage_findings()[0]['Submission type'], 'manual')

    def test_interrupted_generator_is_recorded(self):
        runner = BossAgent(self.skills, {'tavily': lambda c: []}).run(self.settings)
        next(runner)
        runner.close()
        self.assertEqual(store.list_runs()[0]['status'], 'interrupted')

    def test_provenance_migration_preserves_legacy_json(self):
        with tools.get_connection() as conn:
            conn.execute('''CREATE TABLE webpage_findings (id INTEGER PRIMARY KEY,
                source_url TEXT NOT NULL UNIQUE, effective_date TEXT, population_value REAL,
                official_source INTEGER NOT NULL, quoted_source TEXT, quoted_source_url TEXT,
                extracted_at TEXT NOT NULL, finding_json TEXT NOT NULL)''')
            conn.execute("INSERT INTO webpage_findings VALUES (1, 'https://legacy.test', NULL, NULL, 0, NULL, NULL, 'then', '{}')")
        tools.initialise_findings_table()
        tools.initialise_findings_table()
        self.assertEqual(tools.list_webpage_findings()[0]['Submission type'], 'legacy_unknown')
        finding = {'url': 'https://automatic.test', 'statistics': {}}
        saved = tools.store_webpage_finding(finding, {'submission_type': 'automatic', 'discovery_source': 'reddit', 'search_run_id': 'run', 'search_candidate_id': 9})
        record = next(r for r in tools.list_webpage_findings() if r['ID'] == saved['id'])
        self.assertEqual(record['Discovery source'], 'reddit')
        self.assertEqual(record['Search candidate ID'], 9)
        self.assertEqual(tools.get_webpage_finding(saved['id']), finding)

    def test_tavily_settings_reach_provider(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.json.return_value = {'results': [{'url': 'https://example.test', 'content': 'Snippet', 'score': .8}]}
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'fake'}), patch('research.requests.post', return_value=response) as post:
            rows = tavily_links(SearchCategory(name='Custom', query='custom query', topic='news', max_results=4, time_range='all', include_domains=['example.test']))
        payload = post.call_args.kwargs['json']
        self.assertNotIn('time_range', payload)
        self.assertEqual(payload['max_results'], 4)
        self.assertEqual(payload['query'], 'custom query')
        self.assertEqual(rows[0]['snippet'], 'Snippet')

    def test_reddit_keeps_external_link_and_submission(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.json.return_value = {'data': {'children': [
            {'data': {'title': 'Article', 'url_overridden_by_dest': 'https://publisher.test/story', 'permalink': '/r/Natalism/comments/1/title/'}},
            {'data': {'title': 'Text', 'is_self': True, 'selftext_html': '<p><a href="https://official.test/release">Release</a></p>', 'permalink': '/r/Natalism/comments/2/title/'}},
            {'data': {'title': 'Discussion', 'is_self': True, 'permalink': '/r/Natalism/comments/3/title/'}},
        ]}}
        with patch('research.requests.get', return_value=response):
            rows = reddit_links(3)
        self.assertEqual(rows[0]['url'], 'https://publisher.test/story')
        self.assertIn('/comments/1/', rows[0]['submission_url'])
        self.assertEqual(rows[1]['url'], 'https://official.test/release')
        self.assertTrue(rows[2]['discovery_only'])

    def test_invalid_settings_fail_before_starting_run(self):
        self.settings['categories'][0]['max_results'] = 100
        with self.assertRaises(ValueError):
            self.run_boss([])
        self.assertEqual(store.list_runs(), [])

    def test_prefetched_extraction_does_not_fetch_again(self):
        with patch('dotenv.load_dotenv'), patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-key'}):
            import agents
        with patch.object(agents, 'web_llm') as web, patch.object(agents, 'research_llm') as extraction, patch.object(agents, 'store_webpage_finding'):
            agents.research_agent({'messages': [], 'article_url': 'https://example.test', 'page_text': 'Verified full text'})
        web.invoke.assert_not_called()
        self.assertEqual(extraction.invoke.return_value.url, 'https://example.test')
        self.assertTrue(any('Verified full text' in str(m.content) for m in extraction.invoke.call_args.args[0]))

    def test_reddit_json_failure_falls_back_to_public_rss(self):
        import requests
        response = MagicMock()
        response.__enter__.return_value = response
        response.content = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
            <title>New national figures</title><link href="https://www.reddit.com/r/Natalism/comments/1"/>
            <content type="html">&lt;a href="https://publisher.test/report"&gt;[link]&lt;/a&gt;
            &lt;a href="https://reddit.com/r/Natalism/comments/1"&gt;[comments]&lt;/a&gt;</content>
            </entry></feed>'''
        with patch('research.requests.get', side_effect=[requests.HTTPError('403'), response]):
            rows = reddit_links(3)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['url'], 'https://publisher.test/report')
        self.assertEqual(rows[0]['transport'], 'rss')
        self.assertIn('403', rows[0]['provider_note'])
