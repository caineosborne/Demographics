"""Gradio controls for bounded discovery and inspecting persistent search runs."""
import json
import copy
import re
import threading

import gradio as gr
import pandas as pd

from research import BossAgent, CRITERIA, DEFAULT_SETTINGS, ResearchStopRequested, SearchSettings, recommended_categories
import research_store as store


HEADERS = ['Enabled', 'Category', 'Search terms', 'Max results',
           'Search depth', 'Include domains', 'Exclude domains']
SEARCH_WINDOWS = [
    ('Previous 24 hours', 'day'),
    ('Previous 7 days', 'week'),
    ('Previous 30 days', 'month'),
]

_ACTIVE_RUNS = {}
_ACTIVE_RUNS_LOCK = threading.Lock()
_BACKGROUND_STARTING = False

_LEGACY_DEFAULT_CRITERIA = '''Find factual national demographic statistics: population, births,
deaths, fertility, or migration, including new releases and substantive revisions.
Include official releases and reporting that cites demographic figures. Exclude
opinion without new figures, unrelated meanings of migration/deaths, generic
portals, scheduled releases with no figures, and regional-only reports. Do not
reject merely because a snippet lacks figures or a publication date: use unclear.
Distinguish publication date from the period measured; historic measurement
periods may appear in newly published releases. Explain date uncertainty.'''


def upgrade_legacy_defaults(settings):
    """Adopt improved defaults only when a saved value is an untouched old default."""
    upgraded = copy.deepcopy(settings)
    recommended = {category.name: category.model_dump() for category in recommended_categories()}
    old_patterns = {
        'Population': r'(?:\d{4} )?(?:national population estimate (?:official statistics release|latest statistical release)|"national population estimate" census statistical release)',
        'Births, deaths and fertility': r'(?:\d{4} )?(?:national births deaths (?:total fertility rate official statistics release|fertility statistics latest release)|"vital statistics" births deaths "total fertility rate" national)',
        'Migration': r'(?:\d{4} )?(?:national net (?:international )?migration (?:estimate official statistics release|statistics latest release)|"net international migration" immigration emigration national statistics)',
    }
    for category in upgraded.get('categories', []):
        replacement = recommended.get(category.get('name'))
        pattern = old_patterns.get(category.get('name'))
        if replacement and pattern and re.fullmatch(pattern, str(category.get('query') or '')):
            category.update({key: replacement[key] for key in ('query', 'topic', 'time_range', 'search_depth')})
    if upgraded.get('review_criteria') == _LEGACY_DEFAULT_CRITERIA:
        upgraded['review_criteria'] = CRITERIA
    return upgraded


def settings_rows(settings):
    return [[c['enabled'], c['name'], c['query'], c['max_results'],
             c['search_depth'], ', '.join(c['include_domains']), ', '.join(c['exclude_domains'])]
            for c in settings['categories']]


def settings_search_window(settings):
    ranges = {category.get('time_range', 'day') for category in settings.get('categories', [])}
    value = ranges.pop() if len(ranges) == 1 else 'day'
    return value if value in {'day', 'week', 'month'} else 'day'


def settings_search_topic(settings):
    topics = {category.get('topic', 'news') for category in settings.get('categories', [])}
    value = topics.pop() if len(topics) == 1 else 'news'
    return value if value in {'news', 'general'} else 'news'


def parse_settings(rows, search_topic, search_window, reddit_enabled, reddit_limit, max_candidates, max_per_domain, criteria):
    if hasattr(rows, 'values'):
        rows = rows.values.tolist()
    categories = []
    for row in rows:
        if not any(str(v or '').strip() for v in row[1:]):
            continue
        enabled = row[0] is True or str(row[0]).lower() in {'true', '1'}
        categories.append(dict(enabled=enabled, name=str(row[1]).strip(), query=str(row[2]).strip(),
                               topic=search_topic, max_results=row[3], time_range=search_window,
                               search_depth=str(row[4]).strip(),
                               include_domains=[d.strip() for d in str(row[5] or '').split(',') if d.strip()],
                               exclude_domains=[d.strip() for d in str(row[6] or '').split(',') if d.strip()]))
    result = SearchSettings(categories=categories, reddit_enabled=reddit_enabled,
                            reddit_limit=reddit_limit, max_candidates=max_candidates,
                            max_per_domain=max_per_domain,
                            review_criteria=criteria).model_dump()
    if not any(c['enabled'] for c in result['categories']) and not reddit_enabled:
        raise ValueError('Enable a search category or Reddit before running discovery.')
    return result


def save_controls(*values):
    try:
        settings = parse_settings(*values)
        store.save_settings(settings)
        count = sum(c['max_results'] for c in settings['categories'] if c['enabled'])
        return (f'Settings saved. Up to {count} Tavily results plus links from '
                f'{settings["reddit_limit"] if settings["reddit_enabled"] else 0} Reddit submissions; '
                f'up to {settings["max_candidates"]} unique articles and '
                f'{settings["max_per_domain"]} per publisher will use model processing.')
    except ValueError as exc:
        return f'Settings not saved: {exc}'


def _run_snapshot(run_id):
    run = store.get_run(run_id)
    if not run:
        return '', pd.DataFrame(), run_id or '', '**Run not found.**'
    events = json.loads(run.get('events_json') or '[]')
    messages = [event.get('message') for event in events if event.get('event') == 'progress' and event.get('message')]
    log = '\n'.join(messages)
    status = run.get('status', 'unknown')
    if status == 'running':
        latest = messages[-1] if messages else 'Starting background research'
        summary = f'**Running in background · {latest}**'
    elif status == 'complete':
        summary = f'**{messages[-1] if messages else "Run complete"}**'
    elif status == 'completed_with_errors':
        summary = f'**{messages[-1] if messages else "Run completed with errors"}**'
    elif status == 'interrupted':
        summary = '**Run interrupted. Completed findings and the audit remain saved.**'
    else:
        summary = f'**Run status: {status}**'
    return log, candidate_table(run_id), run_id, summary


def candidate_table(run_id):
    """Present every candidate with a visible outcome and supporting reason."""
    displayed = []
    for row in store.list_candidates(run_id):
        status = row.get('status') or 'unknown'
        duplicate_of = row.get('duplicate_of') or ''
        if status == 'duplicate' and not duplicate_of:
            targets = []
            if row.get('duplicate_candidate_id'):
                targets.append(f"candidate #{row['duplicate_candidate_id']} in this run")
            if row.get('finding_id'):
                targets.append(f"database finding #{row['finding_id']}")
            duplicate_of = ' and '.join(targets)
        explanation = row.get('full_reason') or row.get('error') or row.get('summary_reason') or ''
        if status == 'duplicate' and not explanation:
            explanation = f'Duplicate of {duplicate_of}.' if duplicate_of else 'Duplicate article URL.'
        summary_decision = row.get('summary_decision')
        full_decision = row.get('full_decision')
        relevance = full_decision or summary_decision
        relevance_label = {
            'relevant': 'YES', 'irrelevant': 'NO', 'unclear': 'UNCLEAR',
        }.get(relevance, 'NOT REVIEWED')
        if status == 'excluded_subnational':
            geography_status = 'SUBNATIONAL / NO ISO3'
        elif status in {'complete', 'comparing'}:
            geography_status = 'COUNTRY / ISO3 CHECKED'
        else:
            geography_status = 'NOT CHECKED'
        displayed.append({
            'ID': row.get('id'),
            'Outcome': '🔁 DUPLICATE' if status == 'duplicate' else status.replace('_', ' ').upper(),
            'Relevance': relevance_label,
            'Geography status': geography_status,
            'Duplicate of': duplicate_of,
            'Title': row.get('title') or '',
            'Source': row.get('source') or '',
            'Category': row.get('category') or '',
            'URL': row.get('url') or '',
            'Explanation': explanation,
            'Updated (UTC)': row.get('updated_at') or '',
        })
    return pd.DataFrame(displayed)


def run_table(runs):
    """Keep run history scannable while retaining full JSON in the detail panel."""
    displayed = []
    for run in runs:
        settings = json.loads(run.get('settings_json') or '{}')
        events = json.loads(run.get('events_json') or '[]')
        categories = ', '.join(
            category.get('name', '') for category in settings.get('categories', []) if category.get('enabled')
        )
        errors = [event for event in events if event.get('error') or event.get('event') == 'worker_error']
        outcomes = next((event.get('outcomes') for event in reversed(events) if event.get('outcomes')), {})
        outcome_summary = ', '.join(f'{count} {name}' for name, count in sorted(outcomes.items()))
        displayed.append({
            'Run ID': run.get('id'),
            'Started (UTC)': run.get('started_at'),
            'Finished (UTC)': run.get('finished_at'),
            'Status': str(run.get('status') or '').replace('_', ' ').upper(),
            'Searches': categories or 'Reddit only',
            'Provider errors': len(errors),
            'Outcomes': outcome_summary,
        })
    return pd.DataFrame(displayed)


def inspect_run(run_id):
    run = store.get_run(run_id) if run_id else None
    if not run:
        return {}
    return {
        **{key: value for key, value in run.items() if key not in {'settings_json', 'events_json'}},
        'settings': json.loads(run.get('settings_json') or '{}'),
        'events': json.loads(run.get('events_json') or '[]'),
    }


def _background_search(settings, stop_event, ready, result):
    run_id = None
    try:
        for run_id, message in BossAgent().run(settings, stop_event=stop_event):
            if result.get('run_id') is None:
                result['run_id'] = run_id
                with _ACTIVE_RUNS_LOCK:
                    _ACTIVE_RUNS[run_id] = {'stop_event': stop_event, 'thread': threading.current_thread()}
                ready.set()
            store.log_event(run_id, {'event': 'progress', 'message': message})
    except ResearchStopRequested:
        pass
    except Exception as exc:
        result['error'] = str(exc)
        if run_id:
            try:
                store.log_event(run_id, {'event': 'worker_error', 'message': str(exc)})
            except Exception:
                pass
    finally:
        ready.set()
        if run_id:
            with _ACTIVE_RUNS_LOCK:
                _ACTIVE_RUNS.pop(run_id, None)


def run_search(*values):
    global _BACKGROUND_STARTING
    try:
        settings = parse_settings(*values)
    except ValueError as exc:
        return f'Invalid settings: {exc}', pd.DataFrame(), '', f'**Run not started:** {exc}'
    with _ACTIVE_RUNS_LOCK:
        active = next((run_id for run_id, task in _ACTIVE_RUNS.items() if task['thread'].is_alive()), None)
        if active:
            return _run_snapshot(active)
        if _BACKGROUND_STARTING:
            return '', pd.DataFrame(), '', '**A background run is already starting.**'
        _BACKGROUND_STARTING = True

    stop_event = threading.Event()
    ready = threading.Event()
    result = {}
    worker = threading.Thread(
        target=_background_search,
        args=(settings, stop_event, ready, result),
        name='automatic-demographics-research',
        daemon=True,
    )
    try:
        worker.start()
        ready.wait(timeout=5)
    finally:
        with _ACTIVE_RUNS_LOCK:
            _BACKGROUND_STARTING = False
    run_id = result.get('run_id')
    if run_id:
        return _run_snapshot(run_id)
    error = result.get('error') or 'The background worker did not start within five seconds.'
    return error, pd.DataFrame(), '', f'**Run not started: {error}**'


def poll_run(run_id):
    run_id = str(run_id or '').strip()
    if not run_id:
        run = next((run for run in store.list_runs() if run.get('status') == 'running'), None)
        if not run:
            return '', pd.DataFrame(), '', 'No automatic research run is active.'
        run_id = run['id']
    return _run_snapshot(run_id)


def stop_search(run_id):
    """Signal the active worker; the worker records interrupted in its finally block."""
    run_id = str(run_id or '').strip()
    if not run_id:
        return '**No active run is selected.**'
    with _ACTIVE_RUNS_LOCK:
        task = _ACTIVE_RUNS.get(run_id)
    if task is not None:
        stop_event = task['stop_event']
        if stop_event.is_set():
            return '**Stop already requested; waiting for the current operation to finish or time out.**'
        stop_event.set()
        try:
            store.log_event(run_id, {'event': 'stop_requested', 'source': 'stop_button'})
        except Exception:
            pass
        return '**Stop requested. The current provider/model call will finish or time out, then the run will be marked interrupted.**'
    runs = {run['id']: run for run in store.list_runs()}
    run = runs.get(run_id)
    if run and run.get('status') in {'running', 'stopping'}:
        store.finish_run(run_id, 'interrupted')
        try:
            store.log_event(run_id, {'event': 'recovered_orphan', 'message': 'Stopped from the UI after no worker was attached.'})
        except Exception:
            pass
        return '**Run marked interrupted; no active worker was attached.**'
    return '**That run is not active.**'


def refresh_history(selected_run=None):
    runs = store.list_runs()
    run_ids = [run['id'] for run in runs]
    selected_run = selected_run if selected_run in run_ids else (run_ids[0] if run_ids else None)
    candidates = candidate_table(selected_run) if selected_run else pd.DataFrame()
    return run_table(runs), gr.update(choices=run_ids, value=selected_run), candidates, inspect_run(selected_run)


def load_history(run_id):
    return candidate_table(run_id), inspect_run(run_id)


def inspect_candidate(candidate_id):
    try:
        return store.get_candidate(int(candidate_id))
    except (TypeError, ValueError) as exc:
        return {'error': str(exc)}


def recommended_category_rows():
    return settings_rows({'categories': [category.model_dump() for category in recommended_categories()]})


def recommended_controls():
    settings = {'categories': [category.model_dump() for category in recommended_categories()]}
    return settings_rows(settings), settings_search_topic(settings), settings_search_window(settings)


def build_search_tabs():
    store.recover_orphaned_runs()
    settings = upgrade_legacy_defaults(store.load_settings(DEFAULT_SETTINGS))
    search_window_value = settings_search_window(settings)
    search_topic_value = settings_search_topic(settings)
    with gr.Tab('Automatic research'):
        gr.Markdown('Runs continue in the background if this page is refreshed or closed. Edit or add categories below. Settings are saved with each run. '
                    'Relevant and unclear summaries advance to full article review; only confirmed relevant articles enter extraction and UN comparison.')
        categories = gr.Dataframe(headers=HEADERS, value=settings_rows(settings), type='array',
                                  datatype=['bool', 'str', 'str', 'number', 'str', 'str', 'str'],
                                  column_count=(len(HEADERS), 'fixed'), row_count=(3, 'dynamic'),
                                  interactive=True, wrap=False, line_breaks=False,
                                  column_widths=[75, 180, 500, 90, 100, 160, 160],
                                  label='Search categories', max_height=180,
                                  elem_classes='research-categories')
        gr.Markdown('**Max results:** 1–20 per category · '
                    '**Depth:** basic, advanced, fast, ultra-fast. '
                    'Separate domains with commas. Totals are upper limits before deduplication.')
        with gr.Row():
            search_topic = gr.Dropdown(
                choices=[('News', 'news'), ('General web', 'general')], value=search_topic_value,
                label='Search type', info='Applied to every Tavily category.', filterable=False,
            )
            search_window = gr.Dropdown(
                choices=SEARCH_WINDOWS, value=search_window_value, label='Search window',
                info='Applied to every Tavily category.', filterable=False,
            )
            reddit_enabled = gr.Checkbox(value=settings['reddit_enabled'], label='Check new r/Natalism submissions')
            reddit_limit = gr.Number(value=settings['reddit_limit'], minimum=1, maximum=100, precision=0,
                                     label='Newest Reddit submissions to inspect')
            max_candidates = gr.Number(value=settings['max_candidates'], minimum=1, maximum=100, precision=0,
                                       label='Maximum unique articles to process')
            max_per_domain = gr.Number(value=settings['max_per_domain'], minimum=1, maximum=10, precision=0,
                                       label='Maximum articles per publisher')
        gr.Markdown('Reddit supplies external article links, including links in text posts. Discussion-only posts remain in the audit. '
                    'This checks the newest submissions, not the entire subreddit history; access failures appear in the run log.')
        gr.Markdown('The processing and publisher limits control costly article fetches and model calls. All discoveries remain in Search results; excluded or deferred links retain a reason in their audit record.')
        criteria = gr.Textbox(value=settings['review_criteria'], lines=8, label='Relevance criteria')
        controls = [categories, search_topic, search_window, reddit_enabled, reddit_limit, max_candidates, max_per_domain, criteria]
        with gr.Row():
            recommended = gr.Button('Use recommended search terms')
            save = gr.Button('Save search settings')
            run = gr.Button('Search and analyse', variant='primary')
            stop = gr.Button('Stop current run', variant='stop')
        gr.Markdown('The tables refresh automatically. Stop requests are checked between pipeline stages; an in-flight network or model call finishes or times out before the run is marked interrupted.')
        settings_status = gr.Markdown()
        run_id = gr.Textbox(label='Run ID', interactive=False)
        run_summary = gr.Markdown('Run outcome will appear here.')
        log = gr.Textbox(label='Research activity', lines=12, interactive=False)
        results = gr.Dataframe(
            label='This run — every candidate and its outcome', interactive=False, wrap=False,
            line_breaks=False, max_height=360, pinned_columns=2, show_search='filter',
            max_chars=120,
            column_widths=[60, 175, 115, 155, 230, 500, 110, 190, 360, 420, 180],
            elem_classes='research-candidates',
        )
        run_monitor = gr.Timer(2, active=True)
        recommended.click(recommended_controls, outputs=[categories, search_topic, search_window])
        save.click(save_controls, inputs=controls, outputs=settings_status)
        run.click(run_search, inputs=controls, outputs=[log, results, run_id, run_summary], concurrency_limit=1,
                  concurrency_id='automatic-research')
        stop.click(stop_search, inputs=run_id, outputs=run_summary)
        run_monitor.tick(poll_run, inputs=run_id, outputs=[log, results, run_id, run_summary], show_progress='hidden')
    with gr.Tab('Search results') as history_tab:
        gr.Markdown('Inspect accepted, rejected, unclear, duplicate and failed results. The detailed record contains '
                    'the original provider response, full article text when fetched, review reasons, extracted facts and UN comparison.')
        refresh = gr.Button('Refresh search history')
        runs_table = gr.Dataframe(
            label='Runs (latest 100)', interactive=False, wrap=False, max_height=220,
            elem_classes='research-runs',
            pinned_columns=1, show_search='filter', max_chars=90,
            column_widths=[290, 210, 210, 180, 220, 100, 420],
        )
        selected_run = gr.Dropdown(label='Filter by run', choices=[])
        with gr.Accordion('Selected run settings and events', open=False):
            run_detail = gr.JSON(label='Complete run audit')
        candidates = gr.Dataframe(
            label='Candidates (latest 1,000)', interactive=False, wrap=False,
            line_breaks=False, max_height=360, pinned_columns=2, show_search='filter',
            max_chars=120,
            column_widths=[60, 175, 115, 155, 230, 500, 110, 190, 360, 420, 180],
            elem_classes='research-candidates',
        )
        candidate_id = gr.Number(label='Candidate ID to inspect', precision=0)
        inspect = gr.Button('Inspect candidate')
        detail = gr.JSON(label='Complete audit record')
        history_monitor = gr.Timer(5, active=True)
        history_outputs = [runs_table, selected_run, candidates, run_detail]
        history_tab.select(refresh_history, inputs=selected_run, outputs=history_outputs)
        refresh.click(refresh_history, inputs=selected_run, outputs=history_outputs)
        history_monitor.tick(refresh_history, inputs=selected_run, outputs=history_outputs,
                             show_progress='hidden')
        selected_run.change(load_history, inputs=selected_run, outputs=[candidates, run_detail])
        inspect.click(inspect_candidate, inputs=candidate_id, outputs=detail)
