import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from data import tools
from core import research_store as store
from core.research import (BossAgent, ResearchSkills, ResearchStopRequested, ReviewDecision, SearchSettings,
                      canonical_url, compact_article_text, discovery_issue, publisher_domain,
                      recommended_categories, reddit_links, tavily_extract_articles, tavily_links,
                      SearchCategory, SummaryReview)


def candidate(url='https://example.test/article', **kwargs):
    return {'url': url, 'source': 'tavily', 'category': 'Population', 'title': 'New figures', 'snippet': 'A release', **kwargs}


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(tools, 'DB_PATH', Path(self.directory.name) / 'test.sqlite')
        self.db_patch.start()
        self.settings = SearchSettings(categories=[SearchCategory(name='Population', query='population')], reddit_enabled=False).model_dump()
        self.summary_decision = ReviewDecision(decision='relevant', reason='National release')

        def summary_review(candidates, _criteria):
            return [SummaryReview(candidate_id=candidate_id, **self.summary_decision.model_dump())
                    for candidate_id, _candidate in candidates]

        self.summary_review = MagicMock(side_effect=summary_review)
        self.full_review = MagicMock(return_value=ReviewDecision(decision='relevant', reason='Full page evidence'))
        result = MagicMock()
        result.model_dump.return_value = {'url': 'https://example.test/article'}
        comparison = MagicMock()
        comparison.model_dump.return_value = {'overall_assessment': 'Compared'}
        self.extract = MagicMock(return_value={'result': result, 'storage': {'status': 'stored', 'id': 7}})
        self.compare = MagicMock(return_value={'comparison': comparison, 'un_data': []})
        self.fetch = MagicMock(return_value='Full national demographic release')
        self.skills = ResearchSkills(
            self.summary_review, self.fetch, self.extract, self.compare, self.full_review,
        )
        # Tests must not let a developer's Tavily credential turn an access
        # failure into a live recovery-search request.
        self.skills.find_alternative_sources = MagicMock(return_value=[])

    def tearDown(self):
        self.db_patch.stop()
        self.directory.cleanup()

    def run_boss(self, rows, providers=None):
        updates = list(BossAgent(self.skills, providers or {'tavily': lambda c: rows}).run(self.settings))
        return store.list_candidates(updates[-1][0])

    def test_reject_summary_is_advisory_and_article_is_still_processed(self):
        self.summary_decision = ReviewDecision(decision='irrelevant', reason='Opinion only')
        row = self.run_boss([candidate()])[0]
        self.assertEqual(row['status'], 'complete')
        self.assertEqual(row['summary_reason'], 'Opinion only')
        self.fetch.assert_called_once()
        self.full_review.assert_called_once()
        self.extract.assert_called_once()
        self.compare.assert_not_called()

    def test_forced_url_reaches_secondary_review_and_stops_when_rejected(self):
        self.settings['forced_urls'] = ['https://example.test/known-source']
        self.summary_decision = ReviewDecision(decision='irrelevant', reason='Summary cannot establish relevance')
        self.full_review.return_value = ReviewDecision(decision='irrelevant', reason='Full page is not demographic')
        row = self.run_boss([])[0]
        self.assertEqual(row['status'], 'excluded_full_review')
        self.assertEqual(row['full_reason'], 'Full page is not demographic')
        self.fetch.assert_called_once_with('https://example.test/known-source')
        self.full_review.assert_called_once()
        self.extract.assert_not_called()

    def test_reviews_twenty_summaries_in_one_model_call(self):
        rows = [candidate(f'https://publisher-{index}.test/release') for index in range(20)]
        self.run_boss(rows)
        self.assertEqual(self.summary_review.call_count, 1)
        self.assertEqual(len(self.summary_review.call_args.args[0]), 20)

    def test_recommended_searches_target_demographic_release_language(self):
        categories = {category.name: category for category in recommended_categories(2026)}
        self.assertEqual(categories['Population'].topic, 'news')
        self.assertEqual(categories['Population'].time_range, 'day')
        self.assertEqual(categories['Population'].search_depth, 'advanced')
        self.assertIn('"annual vital statistics"', categories['Births, deaths and fertility'].query)
        self.assertIn('"annual net international migration"', categories['Migration'].query)

    def test_splits_more_than_twenty_summaries_into_bounded_batches(self):
        self.settings['max_candidates'] = 21
        rows = [candidate(f'https://publisher-{index}.test/release') for index in range(21)]
        self.run_boss(rows)
        self.assertEqual([len(call.args[0]) for call in self.summary_review.call_args_list], [20, 1])

    def test_full_review_is_advisory_and_all_articles_reach_extraction(self):
        for index, verdict in enumerate(('irrelevant', 'unclear', 'relevant')):
            with self.subTest(verdict=verdict):
                self.summary_decision = ReviewDecision(decision='unclear', reason='No figures in snippet')
                self.full_review.return_value = ReviewDecision(decision=verdict, reason='Full page evidence')
                row = self.run_boss([candidate(f'https://example.test/unclear-{index}')])[0]
                self.assertEqual(row['status'], 'complete')
                details = store.get_candidate(row['id'])['details']
                self.assertTrue(details['page_loaded'])
                self.assertNotIn('full_text', details)
        self.assertEqual(self.extract.call_count, 3)
        # Automatic discovery extracts and stores; UN comparison is reserved
        # for the manual Analyse webpage flow.
        self.compare.assert_not_called()
        provenance = self.extract.call_args.args[2]
        self.assertEqual(provenance['submission_type'], 'automatic')
        self.assertEqual(provenance['search_candidate_id'], row['id'])

    def test_terminal_candidate_keeps_compact_loaded_audit_not_article_content(self):
        row = self.run_boss([candidate()])[0]

        details = store.get_candidate(row['id'])['details']
        self.assertEqual(row['status'], 'complete')
        self.assertTrue(details['page_loaded'])
        self.assertEqual(details['loaded_url'], 'https://example.test/article')
        self.assertNotIn('full_text', details)

    def test_access_blocks_are_distinguished_from_other_article_errors(self):
        self.fetch.side_effect = tools.PageAccessError('Publisher denied access')
        self.summary_decision = ReviewDecision(decision='relevant', reason='National figures')
        row = self.run_boss([candidate()])[0]
        self.assertEqual(row['status'], 'relevant_access_blocked')
        self.assertIn('denied access', row['error'])

        self.fetch.reset_mock(side_effect=True)
        self.fetch.side_effect = tools.PageAccessError('Publisher denied access')
        self.summary_decision = ReviewDecision(decision='unclear', reason='Potential national figures')
        row = self.run_boss([candidate('https://example.test/unclear')])[0]
        self.assertEqual(row['status'], 'unclear_access_blocked')

    def test_exact_duplicates_preserve_both_discoveries_but_process_once(self):
        rows = self.run_boss([candidate(), candidate(source='reddit')])
        self.assertEqual([r['status'] for r in rows], ['duplicate', 'complete'])
        self.assertEqual(rows[0]['duplicate_of'], f"candidate #{rows[1]['id']} in this run")
        self.assertIn('Duplicate of candidate', rows[0]['full_reason'])
        self.fetch.assert_called_once()
        self.assertEqual(rows[0]['source'], 'reddit')

    def test_historical_loaded_page_is_duplicate_before_review(self):
        prior_run = store.start_run(self.settings)
        prior_id = store.add_candidate(prior_run, candidate())
        store.update_candidate(
            prior_id, status='irrelevant_full_text', page_loaded=True,
            loaded_url='https://example.test/article', summary_reason='Previously reviewed as not relevant',
        )
        store.finish_run(prior_run, 'completed')

        rows = self.run_boss([candidate()])

        self.assertEqual(rows[0]['status'], 'duplicate')
        self.assertEqual(rows[0]['duplicate_of'], f'candidate #{prior_id} in an earlier run')
        self.assertIn('exact article URL is https://example.test/article', rows[0]['full_reason'])
        self.summary_review.assert_not_called()
        self.fetch.assert_not_called()
        self.compare.assert_not_called()

    def test_legacy_full_text_marker_remains_a_historical_duplicate(self):
        prior_run = store.start_run(self.settings)
        prior_id = store.add_candidate(prior_run, candidate())
        store.update_candidate(
            prior_id, status='irrelevant_full_text', full_text='Previously loaded page',
            loaded_url='https://example.test/article',
        )
        store.finish_run(prior_run, 'completed')

        row = self.run_boss([candidate()])[0]

        self.assertEqual(row['status'], 'duplicate')
        self.assertEqual(row['duplicate_candidate_id'], prior_id)
        self.fetch.assert_not_called()

    def test_historical_unloaded_candidate_is_retried(self):
        prior_run = store.start_run(self.settings)
        prior_id = store.add_candidate(prior_run, candidate())
        store.update_candidate(prior_id, status='irrelevant_summary', summary_reason='No page was loaded')
        store.finish_run(prior_run, 'completed')

        rows = self.run_boss([candidate()])

        self.assertEqual(rows[0]['status'], 'complete')
        self.fetch.assert_called_once()

    def test_remove_and_allow_rerun_bypasses_one_historical_loaded_duplicate(self):
        prior_run = store.start_run(self.settings)
        prior_id = store.add_candidate(prior_run, candidate())
        store.update_candidate(
            prior_id, status='irrelevant_full_text', page_loaded=True,
            loaded_url='https://example.test/article',
        )
        store.finish_run(prior_run, 'completed')
        stored = tools.store_webpage_finding({
            'url': 'https://example.test/article', 'geography': 'Japan',
            'statistics': {'population': {'value': 100}},
        })
        tools.delete_webpage_finding(stored['id'])

        row = self.run_boss([candidate()])[0]

        self.assertEqual(row['status'], 'complete')
        self.fetch.assert_called_once_with('https://example.test/article')
        recheck = tools.list_automatic_rechecks()[0]
        self.assertEqual(recheck['state'], 'loaded')
        self.assertEqual(recheck['consumed_candidate_id'], row['id'])

        self.fetch.reset_mock()
        later = self.run_boss([candidate()])[0]
        self.assertEqual(later['status'], 'duplicate')
        self.fetch.assert_not_called()

    def test_a_failed_recheck_remains_eligible_until_the_url_loads(self):
        prior_run = store.start_run(self.settings)
        prior_id = store.add_candidate(prior_run, candidate())
        store.update_candidate(prior_id, status='irrelevant_full_text', page_loaded=True)
        store.finish_run(prior_run, 'completed')
        stored = tools.store_webpage_finding({
            'url': 'https://example.test/article', 'geography': 'Japan',
            'statistics': {'population': {'value': 100}},
        })
        tools.delete_webpage_finding(stored['id'])
        self.fetch.side_effect = tools.PageAccessError('Still unavailable')

        failed = self.run_boss([candidate()])[0]

        self.assertEqual(failed['status'], 'relevant_access_blocked')
        self.assertEqual(tools.list_automatic_rechecks()[0]['state'], 'consumed')
        self.fetch.side_effect = None
        self.fetch.return_value = 'Now available'
        later = self.run_boss([candidate()])[0]
        self.assertEqual(later['status'], 'complete')

    def test_canonical_url_unifies_http_and_www_variants(self):
        self.assertEqual(
            canonical_url('http://www.ons.gov.uk/release/?b=2&utm_source=test&a=1#section'),
            'https://ons.gov.uk/release?a=1&b=2',
        )

    def test_meaningful_query_parameters_are_not_collapsed(self):
        self.assertNotEqual(
            canonical_url('https://example.test/release?edition=mobile'),
            canonical_url('https://example.test/release?edition=print'),
        )

    def test_fetch_uses_original_url_while_deduplication_uses_canonical_url(self):
        original = 'http://www.example.test/article?utm_source=search'
        self.run_boss([candidate(original)])
        self.fetch.assert_called_once_with(original)
        extraction_candidate = self.extract.call_args.args[0]
        self.assertEqual(extraction_candidate['url'], original)

    def test_discovery_heuristics_are_warnings_not_exclusions(self):
        rows = self.run_boss([
            candidate('https://www.ons.gov.uk/methodologies/understanding-statistics'),
            candidate('https://www.facebook.com/ons/posts/123'),
        ])
        self.assertEqual({row['status'] for row in rows}, {'complete'})
        self.assertEqual(self.fetch.call_count, 2)
        self.summary_review.assert_called_once()
        self.assertEqual(self.full_review.call_count, 2)
        self.assertTrue(all(
            store.get_candidate(row['id'])['details']['discovery_warning'] for row in rows
        ))

    def test_publisher_cap_does_not_discard_articles(self):
        self.settings['max_per_domain'] = 1
        rows = self.run_boss([
            candidate('https://www.ons.gov.uk/release-one'),
            candidate('https://cy.ons.gov.uk/release-two'),
        ])
        self.assertEqual([row['status'] for row in rows], ['complete', 'complete'])
        self.assertEqual(publisher_domain('https://cy.ons.gov.uk/release-two'), 'ons.gov.uk')

    def test_publisher_domain_respects_multi_part_public_suffixes(self):
        self.assertEqual(publisher_domain('https://www.taiwannews.com.tw/release'), 'taiwannews.com.tw')
        self.assertEqual(publisher_domain('https://statistics.example.com.au/release'), 'example.com.au')
        self.assertNotEqual(
            publisher_domain('https://first.com.tw/release'),
            publisher_domain('https://second.com.tw/release'),
        )

    def test_stale_provider_date_is_an_advisory_warning(self):
        self.settings['categories'][0]['time_range'] = 'week'
        rows = self.run_boss([candidate(published_date='2020-01-01', time_range='week')])
        self.assertEqual(rows[0]['status'], 'complete')
        self.assertIn('stale', store.get_candidate(rows[0]['id'])['details']['discovery_warning'])

    def test_processing_cap_defers_after_recording_discoveries(self):
        self.settings['max_candidates'] = 1
        rows = self.run_boss([candidate(), candidate('https://example.test/second')])
        self.assertEqual([row['status'] for row in rows], ['deferred_budget', 'complete'])
        self.fetch.assert_called_once()

    def test_compact_article_text_keeps_evidence_and_bounds_model_input(self):
        text = 'Intro. ' * 2_000 + 'The total fertility rate was 1.24 births per woman. ' + 'Tail. ' * 2_000
        compact = compact_article_text(text, limit=2_000)
        self.assertLessEqual(len(compact), 2_200)  # Includes omission markers between windows.
        self.assertIn('total fertility rate was 1.24', compact)

    def test_provider_and_article_failures_do_not_stop_other_candidates(self):
        self.settings['reddit_enabled'] = True
        self.fetch.side_effect = [tools.PageAccessError('Unavailable'), 'Actual release']
        providers = {'tavily': lambda c: [candidate(), candidate('https://example.test/second')],
                     'reddit': MagicMock(side_effect=RuntimeError('Reddit blocked'))}
        rows = self.run_boss([], providers)
        self.assertEqual([r['status'] for r in rows], ['complete', 'relevant_access_blocked'])
        self.assertEqual(store.list_runs()[0]['status'], 'completed_with_errors')
        self.assertIn('Reddit blocked', store.list_runs()[0]['events_json'])
        self.compare.assert_not_called()

    def test_access_failure_uses_an_accessible_alternative_source(self):
        alternative = 'https://official.example.test/release'
        self.fetch.side_effect = [tools.PageAccessError('Timed out'), 'Official release text']
        self.skills.find_alternative_sources = MagicMock(return_value=[{
            'url': alternative, 'canonical_url': alternative,
            'title': 'Official release', 'snippet': 'Official figures', 'published_date': '2026-09-01',
        }])

        row = self.run_boss([candidate()])[0]

        self.assertEqual(row['status'], 'complete')
        stored = store.get_candidate(row['id'])['details']
        self.assertEqual(stored['recovered_from_url'], 'https://example.test/article')
        self.assertEqual(stored['replacement_url'], alternative)
        self.assertEqual(stored['alternative_sources'][0]['status'], 'accessed')
        self.assertEqual(self.extract.call_args.args[0]['url'], alternative)
        self.assertEqual(self.extract.call_args.args[2]['published_date'], '2026-09-01')

    def test_access_recovery_skips_low_value_alternative_source(self):
        alternative = 'https://official.example.test/release'
        self.fetch.side_effect = [tools.PageAccessError('Timed out'), 'Official release text']
        self.skills.find_alternative_sources = MagicMock(return_value=[
            {'url': 'https://m.facebook.com/agency/posts/123', 'canonical_url': 'https://facebook.com/agency/posts/123',
             'title': 'Social post', 'snippet': 'Unverified social post', 'published_date': '2026-09-01'},
            {'url': alternative, 'canonical_url': alternative, 'title': 'Official release',
             'snippet': 'Official figures', 'published_date': '2026-09-01'},
        ])

        row = self.run_boss([candidate()])[0]

        self.assertEqual(row['status'], 'complete')
        self.assertEqual(self.fetch.call_args_list[0].args[0], 'https://example.test/article')
        self.assertEqual(self.fetch.call_args_list[1].args[0], alternative)
        self.assertEqual(self.fetch.call_count, 2)
        attempts = store.get_candidate(row['id'])['details']['alternative_sources']
        self.assertEqual(attempts[0]['status'], 'excluded_discovery')
        self.assertIn('facebook.com', attempts[0]['reason'])

    def test_access_recovery_applies_source_rules_before_fetching(self):
        blocked_alternative = 'https://excluded.example.test/release'
        alternative = 'https://official.example.test/release'
        tools.add_source_rule('domain', 'excluded.example.test', 'exclude', note='Excluded publisher')
        self.fetch.side_effect = [tools.PageAccessError('Timed out'), 'Official release text']
        self.skills.find_alternative_sources = MagicMock(return_value=[
            {'url': blocked_alternative, 'canonical_url': blocked_alternative, 'title': 'Excluded release',
             'snippet': 'Excluded figures', 'published_date': '2026-09-01'},
            {'url': alternative, 'canonical_url': alternative, 'title': 'Official release',
             'snippet': 'Official figures', 'published_date': '2026-09-01'},
        ])

        row = self.run_boss([candidate()])[0]

        self.assertEqual(row['status'], 'complete')
        self.assertEqual(self.fetch.call_count, 2)
        attempts = store.get_candidate(row['id'])['details']['alternative_sources']
        self.assertEqual(attempts[0]['status'], 'excluded_source_rule')
        self.assertEqual(attempts[0]['reason'], 'Excluded publisher')

    def test_different_url_variant_is_processed_as_distinct_evidence(self):
        tools.store_webpage_finding({'url': 'https://example.test/article', 'statistics': {}})
        row = self.run_boss([candidate(url='http://www.example.test/article/?utm_source=search#top')])[0]
        self.assertEqual(row['status'], 'complete')
        self.fetch.assert_called_once()
        self.summary_review.assert_called_once()
        self.assertEqual(tools.list_webpage_findings()[0]['Submission type'], 'manual')

    def test_excluded_source_rule_stops_before_summary_or_fetch(self):
        tools.add_source_rule('domain', 'example.test', 'exclude', note='Publisher is out of scope')
        row = self.run_boss([candidate()])[0]
        self.assertEqual(row['status'], 'excluded_source_rule')
        self.assertEqual(row['full_reason'], 'Publisher is out of scope')
        self.summary_review.assert_not_called()
        self.fetch.assert_not_called()

    def test_exact_duplicate_found_during_extraction_is_shown_as_duplicate(self):
        state = self.extract.return_value
        state['storage'] = {'status': 'excluded_duplicate_url', 'existing_id': 42}
        row = self.run_boss([candidate()])[0]
        self.assertEqual(row['status'], 'duplicate')
        self.assertEqual(row['duplicate_of'], 'database finding #42')
        self.assertIn('exact same article URL', row['full_reason'])
        self.compare.assert_not_called()

    def test_article_outside_un_bounds_is_not_stored(self):
        state = self.extract.return_value
        state['storage'] = {
            'status': 'excluded_un_bounds',
            'reason': 'Outside the 25% UN comparison bound for: population.',
        }
        row = self.run_boss([candidate()])[0]
        self.assertEqual(row['status'], 'excluded_un_bounds')
        self.assertIn('25% UN comparison bound', row['full_reason'])

    def test_interrupted_generator_is_recorded(self):
        runner = BossAgent(self.skills, {'tavily': lambda c: []}).run(self.settings)
        next(runner)
        runner.close()
        self.assertEqual(store.list_runs()[0]['status'], 'interrupted')

    def test_stop_event_interrupts_run_and_records_reason(self):
        stop_event = threading.Event()
        stop_event.set()
        runner = BossAgent(self.skills, {'tavily': lambda c: []}).run(self.settings, stop_event=stop_event)
        next(runner)
        next(runner)
        with self.assertRaises(ResearchStopRequested):
            next(runner)
        run = store.list_runs()[0]
        self.assertEqual(run['status'], 'interrupted')
        self.assertIn('stop_requested', run['events_json'])

    def test_startup_recovers_runs_left_running_by_a_crash(self):
        run_id = store.start_run(self.settings)
        self.assertEqual(store.recover_orphaned_runs(), 1)
        run = next(run for run in store.list_runs() if run['id'] == run_id)
        self.assertEqual(run['status'], 'interrupted')
        self.assertIn('recovered_orphan', run['events_json'])

    def test_ui_start_returns_while_research_continues_in_background(self):
        import research_ui

        entered_review = threading.Event()
        release_review = threading.Event()
        original_review = self.summary_review.side_effect

        def delayed_review(candidates, criteria):
            entered_review.set()
            release_review.wait(timeout=2)
            return original_review(candidates, criteria)

        self.summary_review.side_effect = delayed_review
        boss = BossAgent(self.skills, {'tavily': lambda category: [candidate()]})
        rows = research_ui.settings_rows(self.settings)
        with patch.object(research_ui, 'BossAgent', return_value=boss):
            response = research_ui.run_search(rows, 'news', 'day', False, 30, 20, 2, self.settings['review_criteria'])
        run_id = response[2]
        self.assertTrue(run_id)
        self.assertTrue(entered_review.wait(timeout=1))
        self.assertEqual(store.get_run(run_id)['status'], 'running')
        with research_ui._ACTIVE_RUNS_LOCK:
            worker = research_ui._ACTIVE_RUNS[run_id]['thread']
        release_review.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(store.get_run(run_id)['status'], 'complete')
        self.assertIn('Boss agent: complete', store.get_run(run_id)['events_json'])

    def test_saved_settings_gain_new_defaults_after_an_upgrade(self):
        legacy = {key: value for key, value in self.settings.items() if key != 'max_candidates'}
        store.save_settings(legacy)
        loaded = store.load_settings(self.settings)
        self.assertEqual(loaded['max_candidates'], 40)
        self.assertEqual(loaded['categories'], legacy['categories'])

    def test_ui_upgrades_untouched_legacy_search_defaults(self):
        import research_ui

        legacy = SearchSettings(categories=[SearchCategory(
            name='Population', query='2026 national population estimate official statistics release', topic='news'
        )]).model_dump()
        upgraded = research_ui.upgrade_legacy_defaults(legacy)
        category = upgraded['categories'][0]
        self.assertEqual(category['topic'], 'news')
        self.assertEqual(category['time_range'], 'day')
        self.assertEqual(category['search_depth'], 'advanced')

    def test_ui_upgrades_previous_month_default_to_previous_day(self):
        import research_ui

        previous = SearchSettings(categories=recommended_categories(2026)).model_dump()
        previous['categories'][0]['time_range'] = 'month'
        upgraded = research_ui.upgrade_legacy_defaults(previous)
        self.assertEqual(upgraded['categories'][0]['time_range'], 'day')

    def test_search_window_dropdown_applies_to_every_category(self):
        import research_ui

        rows = research_ui.settings_rows(self.settings)
        settings = research_ui.parse_settings(
            rows, 'news', 'week', False, 30, 20, 2, self.settings['review_criteria'],
        )
        self.assertEqual({category['time_range'] for category in settings['categories']}, {'week'})
        self.assertEqual({category['topic'] for category in settings['categories']}, {'news'})

    def test_administrator_can_explicitly_include_a_fallback_domain(self):
        import research_ui

        rows = research_ui.settings_rows(self.settings)
        rows[0][5] = 'ourworldindata.org, statista.com'
        settings = research_ui.parse_settings(
            rows, 'news', 'week', False, 30, 20, 2, self.settings['review_criteria'],
        )
        self.assertEqual(
            settings['categories'][0]['include_domains'], ['ourworldindata.org', 'statista.com'],
        )

    def test_country_hunt_uses_a_one_year_country_specific_query(self):
        import research_ui

        with patch.object(research_ui, 'list_country_names', return_value=['Japan']):
            settings = research_ui.country_hunt_settings('Japan')
        category = settings['categories'][0]
        self.assertEqual(category['time_range'], 'year')
        self.assertEqual(category['topic'], 'news')
        self.assertIn('Japan', category['query'])
        self.assertFalse(settings['reddit_enabled'])
        self.assertEqual(settings['domain_limit_scope'], 'category')
        self.assertEqual(settings['max_per_domain'], 5)

    def test_bulk_hunt_selects_only_countries_without_a_recent_finding(self):
        import research_ui
        from datetime import datetime, timezone

        findings = [
            {'Country': 'Australia', 'Extracted at (UTC)': datetime.now(timezone.utc).isoformat()},
            {'Country': 'Austria', 'Extracted at (UTC)': '2020-01-01T00:00:00+00:00'},
        ]
        with patch.object(research_ui, 'list_country_names', return_value=['Australia', 'Austria', 'Japan']), \
             patch.object(research_ui, 'list_webpage_findings', return_value=findings):
            settings, countries = research_ui.bulk_country_hunt_settings('Aus', 31)
        self.assertEqual(countries, ['Austria'])
        self.assertEqual(settings['categories'][0]['name'], 'Gap hunt: Austria')
        self.assertEqual(settings['categories'][0]['time_range'], 'year')
        self.assertEqual(settings['categories'][0]['topic'], 'news')
        self.assertEqual(settings['categories'][0]['max_results'], 5)
        self.assertEqual(settings['max_candidates'], 5)
        self.assertEqual(settings['domain_limit_scope'], 'category')
        self.assertEqual(settings['max_per_domain'], 5)

    def test_bulk_hunt_supports_numbered_batches(self):
        import research_ui

        matching = [f'Country {index:02d}' for index in range(1, 11)]
        with patch.object(research_ui, 'countries_missing_recent_data', return_value=matching):
            settings, countries = research_ui.bulk_country_hunt_settings('C', 31, 5, 6)
        self.assertEqual(countries, matching[5:10])
        self.assertEqual(settings['max_candidates'], 25)

    def test_candidate_table_names_duplicate_target_and_reason(self):
        import research_ui

        rows = self.run_boss([candidate(), candidate()])
        table = research_ui.candidate_table(rows[0]['run_id'])
        duplicate = table.loc[table['Outcome'] == '🔁 DUPLICATE'].iloc[0]
        self.assertTrue(duplicate['Duplicate of'].startswith('candidate #'))
        self.assertIn('Duplicate of candidate', duplicate['Explanation'])

    def test_candidate_table_explains_historical_duplicate_rows(self):
        import research_ui

        legacy_duplicate = {
            'id': 9, 'run_id': 'run', 'status': 'duplicate', 'duplicate_candidate_id': 4,
            'title': 'Repeated article', 'source': 'tavily', 'category': 'Population',
            'url': 'https://example.test/article', 'updated_at': 'then',
        }
        with patch.object(store, 'list_candidates', return_value=[legacy_duplicate]):
            duplicate = research_ui.candidate_table('run').iloc[0]
        self.assertEqual(duplicate['Duplicate of'], 'candidate #4 in this run')
        self.assertEqual(duplicate['Explanation'], 'Duplicate of candidate #4 in this run.')

    def test_run_table_keeps_raw_json_out_of_the_grid(self):
        import research_ui

        table = research_ui.run_table([{
            'id': 'run', 'started_at': 'start', 'finished_at': 'finish', 'status': 'complete',
            'settings_json': json.dumps({'categories': [{'name': 'Population', 'enabled': True}]}),
            'events_json': json.dumps([{'outcomes': {'complete': 2, 'duplicate': 1}}]),
        }])
        self.assertNotIn('settings_json', table.columns)
        self.assertNotIn('events_json', table.columns)
        self.assertEqual(table.iloc[0]['Outcomes'], '2 complete, 1 duplicate')

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
        self.assertEqual(tools.get_webpage_finding(saved['id']), {
            **finding, 'source_classification': 'secondary_unattributed',
        })

    def test_tavily_settings_reach_provider(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.json.return_value = {'results': [{'url': 'https://example.test', 'content': 'Snippet', 'score': .8}]}
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'fake'}), patch('core.research.requests.post', return_value=response) as post:
            rows = tavily_links(SearchCategory(name='Custom', query='custom query', topic='news', max_results=4, time_range='all', include_domains=['example.test']))
        payload = post.call_args.kwargs['json']
        self.assertNotIn('time_range', payload)
        self.assertEqual(payload['max_results'], 4)
        self.assertEqual(payload['query'], 'custom query')
        self.assertEqual(rows[0]['snippet'], 'Snippet')

    def test_tavily_extract_returns_pages_and_failed_urls(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.json.return_value = {
            'results': [{'url': 'https://www.example.test/article', 'raw_content': 'Verified article text'}],
            'failed_results': [{'url': 'https://blocked.test/article', 'error': 'Access denied'}],
        }
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'fake'}), patch('core.research.requests.post', return_value=response) as post:
            pages, failures = tavily_extract_articles(['https://example.test/article', 'https://blocked.test/article'])
        self.assertEqual(pages[canonical_url('https://example.test/article')], 'Verified article text')
        self.assertEqual(failures[canonical_url('https://blocked.test/article')], 'Access denied')
        self.assertEqual(post.call_args.kwargs['json']['urls'][0], 'https://example.test/article')

    def test_tavily_extracted_page_skips_direct_fetch(self):
        original = 'http://www.example.test/article?utm_source=search'
        extracted = {canonical_url(original): 'Tavily full article text'}
        self.skills.extract_tavily_articles = MagicMock(return_value=(extracted, {}))
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'fake'}):
            row = self.run_boss([candidate(original)])[0]
        self.assertEqual(row['status'], 'complete')
        self.fetch.assert_not_called()
        self.skills.extract_tavily_articles.assert_called_once_with([original])
        details = store.get_candidate(row['id'])['details']
        self.assertEqual(details['content_transport'], 'tavily_extract')

    def test_reddit_keeps_external_link_and_submission(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.json.return_value = {'data': {'children': [
            {'data': {'title': 'Article', 'url_overridden_by_dest': 'https://publisher.test/story', 'permalink': '/r/Natalism/comments/1/title/'}},
            {'data': {'title': 'Text', 'is_self': True, 'selftext_html': '<p><a href="https://official.test/release">Release</a></p>', 'permalink': '/r/Natalism/comments/2/title/'}},
            {'data': {'title': 'Discussion', 'is_self': True, 'permalink': '/r/Natalism/comments/3/title/'}},
        ]}}
        with patch('core.research.requests.get', return_value=response):
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
            from core import agents
        result = agents.RelevantResult(
            title='Article', url='https://example.test', source='Example', site_seen='example.test',
            statistics=agents.Statistics(population=agents.Statistic(
                value=123_456, evidence_excerpt='Population was 123,456 in 2026.',
                metric_type='population', measured_period='2026',
            )),
        )
        with patch.object(agents, 'web_llm') as web, patch.object(agents, 'research_llm') as extraction, patch.object(agents, 'store_webpage_finding'):
            extraction.invoke.return_value = result
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
        with patch('core.research.requests.get', side_effect=[requests.HTTPError('403'), response]):
            rows = reddit_links(3)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['url'], 'https://publisher.test/report')
        self.assertEqual(rows[0]['transport'], 'rss')
        self.assertIn('403', rows[0]['provider_note'])
