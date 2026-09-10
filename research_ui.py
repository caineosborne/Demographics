"""Gradio controls for bounded discovery and inspecting persistent search runs."""
import gradio as gr
import pandas as pd

from research import BossAgent, DEFAULT_SETTINGS, SearchSettings
import research_store as store


HEADERS = ['Enabled', 'Category', 'Search terms', 'Tavily topic', 'Max results',
           'Time range', 'Search depth', 'Include domains', 'Exclude domains']


def settings_rows(settings):
    return [[c['enabled'], c['name'], c['query'], c['topic'], c['max_results'],
             c['time_range'], c['search_depth'], ', '.join(c['include_domains']), ', '.join(c['exclude_domains'])]
            for c in settings['categories']]


def parse_settings(rows, reddit_enabled, reddit_limit, criteria):
    if hasattr(rows, 'values'):
        rows = rows.values.tolist()
    categories = []
    for row in rows:
        if not any(str(v or '').strip() for v in row[1:]):
            continue
        enabled = row[0] is True or str(row[0]).lower() in {'true', '1'}
        categories.append(dict(enabled=enabled, name=str(row[1]).strip(), query=str(row[2]).strip(),
                               topic=str(row[3]).strip(), max_results=row[4], time_range=str(row[5]).strip(),
                               search_depth=str(row[6]).strip(),
                               include_domains=[d.strip() for d in str(row[7] or '').split(',') if d.strip()],
                               exclude_domains=[d.strip() for d in str(row[8] or '').split(',') if d.strip()]))
    result = SearchSettings(categories=categories, reddit_enabled=reddit_enabled,
                            reddit_limit=reddit_limit, review_criteria=criteria).model_dump()
    if not any(c['enabled'] for c in result['categories']) and not reddit_enabled:
        raise ValueError('Enable a search category or Reddit before running discovery.')
    return result


def save_controls(*values):
    try:
        settings = parse_settings(*values)
        store.save_settings(settings)
        count = sum(c['max_results'] for c in settings['categories'] if c['enabled'])
        return f'Settings saved. Up to {count} Tavily results plus links from {settings["reddit_limit"] if settings["reddit_enabled"] else 0} Reddit submissions.'
    except ValueError as exc:
        return f'Settings not saved: {exc}'


def run_search(*values):
    try:
        settings = parse_settings(*values)
    except ValueError as exc:
        yield f'Invalid settings: {exc}', pd.DataFrame(), ''
        return
    logs = []
    for run_id, message in BossAgent().run(settings):
        logs.append(message)
        yield '\n'.join(logs), pd.DataFrame(store.list_candidates(run_id)), run_id


def refresh_history():
    runs = store.list_runs()
    return pd.DataFrame(runs), gr.update(choices=[r['id'] for r in runs], value=runs[0]['id'] if runs else None)


def load_history(run_id):
    return pd.DataFrame(store.list_candidates(run_id))


def inspect_candidate(candidate_id):
    try:
        return store.get_candidate(int(candidate_id))
    except (TypeError, ValueError) as exc:
        return {'error': str(exc)}


def build_search_tabs():
    settings = store.load_settings(DEFAULT_SETTINGS)
    with gr.Tab('Automatic research'):
        gr.Markdown('Run discovery on demand. Edit or add categories below. Settings are saved with each run. '
                    'Relevant and unclear summaries advance to full article review; only confirmed relevant articles enter extraction and UN comparison.')
        categories = gr.Dataframe(headers=HEADERS, value=settings_rows(settings), type='array',
                                  datatype=['bool', 'str', 'str', 'str', 'number', 'str', 'str', 'str', 'str'],
                                  column_count=(len(HEADERS), 'fixed'), row_count=(3, 'dynamic'),
                                  interactive=True, wrap=True, label='Search categories')
        gr.Markdown('**Tavily topic:** general, news, finance · **Max results:** 1–20 per category · '
                    '**Time range:** day, week, month, year, all · **Depth:** basic, advanced, fast, ultra-fast. '
                    'Separate domains with commas. Totals are upper limits before deduplication.')
        with gr.Row():
            reddit_enabled = gr.Checkbox(value=settings['reddit_enabled'], label='Check new r/Natalism submissions')
            reddit_limit = gr.Number(value=settings['reddit_limit'], minimum=1, maximum=100, precision=0,
                                     label='Newest Reddit submissions to inspect')
        gr.Markdown('Reddit supplies external article links, including links in text posts. Discussion-only posts remain in the audit. '
                    'This checks the newest submissions, not the entire subreddit history; access failures appear in the run log.')
        criteria = gr.Textbox(value=settings['review_criteria'], lines=8, label='Relevance criteria')
        controls = [categories, reddit_enabled, reddit_limit, criteria]
        with gr.Row():
            save = gr.Button('Save search settings')
            run = gr.Button('Search and analyse', variant='primary')
        settings_status = gr.Markdown()
        run_id = gr.Textbox(label='Run ID', interactive=False)
        log = gr.Textbox(label='Research activity', lines=12, interactive=False)
        results = gr.Dataframe(label='This run — all candidates and decisions', interactive=False, wrap=True)
        save.click(save_controls, inputs=controls, outputs=settings_status)
        run.click(run_search, inputs=controls, outputs=[log, results, run_id], concurrency_limit=1,
                  concurrency_id='automatic-research')
    with gr.Tab('Search results') as history_tab:
        gr.Markdown('Inspect accepted, rejected, unclear, duplicate and failed results. The detailed record contains '
                    'the original provider response, full article text when fetched, review reasons, extracted facts and UN comparison.')
        refresh = gr.Button('Refresh search history')
        runs_table = gr.Dataframe(label='Runs, settings and provider errors (latest 100)', interactive=False, wrap=True)
        selected_run = gr.Dropdown(label='Filter by run', choices=[])
        candidates = gr.Dataframe(label='Candidates (latest 1,000)', interactive=False, wrap=True)
        candidate_id = gr.Number(label='Candidate ID to inspect', precision=0)
        inspect = gr.Button('Inspect candidate')
        detail = gr.JSON(label='Complete audit record')
        history_tab.select(refresh_history, outputs=[runs_table, selected_run])
        refresh.click(refresh_history, outputs=[runs_table, selected_run])
        selected_run.change(load_history, inputs=selected_run, outputs=candidates)
        inspect.click(inspect_candidate, inputs=candidate_id, outputs=detail)
