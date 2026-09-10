"""Durable discovery audit, in the same SQLite database as the UN model."""
import json
from datetime import datetime, timezone
from uuid import uuid4

import tools


def now():
    return datetime.now(timezone.utc).isoformat()


def initialise():
    with tools.get_connection() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS research_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1), settings_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS search_runs (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                status TEXT NOT NULL, settings_json TEXT NOT NULL,
                events_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS search_candidates (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, url TEXT,
                source TEXT NOT NULL, category TEXT NOT NULL, status TEXT NOT NULL,
                updated_at TEXT NOT NULL, details_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_search_candidates_run ON search_candidates(run_id);
        ''')


def save_settings(settings):
    initialise()
    with tools.get_connection() as conn:
        conn.execute('INSERT OR REPLACE INTO research_settings VALUES (1, ?)', (json.dumps(settings),))


def load_settings(default):
    initialise()
    with tools.get_connection() as conn:
        row = conn.execute('SELECT settings_json FROM research_settings WHERE id = 1').fetchone()
    return json.loads(row[0]) if row else default


def start_run(settings):
    initialise()
    run_id = str(uuid4())
    with tools.get_connection() as conn:
        conn.execute('INSERT INTO search_runs(id, started_at, status, settings_json) VALUES (?, ?, ?, ?)',
                     (run_id, now(), 'running', json.dumps(settings)))
    return run_id


def finish_run(run_id, status):
    with tools.get_connection() as conn:
        conn.execute('UPDATE search_runs SET status = ?, finished_at = ? WHERE id = ?', (status, now(), run_id))


def log_event(run_id, event):
    with tools.get_connection() as conn:
        events = json.loads(conn.execute('SELECT events_json FROM search_runs WHERE id = ?', (run_id,)).fetchone()[0])
        events.append({'at': now(), **event})
        conn.execute('UPDATE search_runs SET events_json = ? WHERE id = ?', (json.dumps(events), run_id))


def add_candidate(run_id, candidate):
    with tools.get_connection() as conn:
        cursor = conn.execute('''INSERT INTO search_candidates
            (run_id, url, source, category, status, updated_at, details_json) VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (run_id, candidate.get('url'), candidate['source'], candidate['category'], 'discovered', now(), json.dumps(candidate)))
        return cursor.lastrowid


def update_candidate(candidate_id, **updates):
    with tools.get_connection() as conn:
        row = conn.execute('SELECT details_json, status FROM search_candidates WHERE id = ?', (candidate_id,)).fetchone()
        details = {**json.loads(row[0]), **updates}
        conn.execute('UPDATE search_candidates SET details_json = ?, status = ?, updated_at = ? WHERE id = ?',
                     (json.dumps(details, default=str), updates.get('status', row[1]), now(), candidate_id))


def list_runs():
    initialise()
    with tools.get_connection() as conn:
        conn.row_factory = tools.sqlite3.Row
        return [dict(row) for row in conn.execute('SELECT * FROM search_runs ORDER BY started_at DESC LIMIT 100')]


def list_candidates(run_id=None):
    initialise()
    with tools.get_connection() as conn:
        conn.row_factory = tools.sqlite3.Row
        rows = conn.execute('SELECT * FROM search_candidates' + (' WHERE run_id = ?' if run_id else '') + ' ORDER BY id DESC LIMIT 1000', (run_id,) if run_id else ()).fetchall()
    return [{**{key: value for key, value in dict(row).items() if key != 'details_json'}, **{key: value for key, value in json.loads(row['details_json']).items()
                           if key in {'title', 'snippet', 'summary_decision', 'summary_reason', 'full_decision', 'full_reason', 'error', 'finding_id'}}}
            for row in rows]


def get_candidate(candidate_id):
    with tools.get_connection() as conn:
        conn.row_factory = tools.sqlite3.Row
        row = conn.execute('SELECT * FROM search_candidates WHERE id = ?', (candidate_id,)).fetchone()
    if row is None:
        raise ValueError('Candidate not found.')
    result = dict(row)
    result['details'] = json.loads(result.pop('details_json'))
    return result
