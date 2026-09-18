"""Durable discovery audit, in the same SQLite database as the UN model."""
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from data import tools
from data.database_maintenance import compact_candidate_details


# A worker is considered alive for a deliberately generous period.  The
# heartbeat is short enough to notice a dead process without allowing an API
# restart to interrupt a valid CLI worker doing provider I/O.
WORKER_HEARTBEAT_SECONDS = 30
WORKER_LEASE_SECONDS = 180
_UNSET = object()


def candidate_audit_is_final(status):
    """Whether a candidate has reached an outcome that must not retain content."""
    return (
        status in {"complete", "duplicate", "discovery_only", "error"}
        or status.startswith(("excluded_", "irrelevant_", "needs_review", "deferred_"))
        or status.endswith("_access_blocked")
    )


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
                events_json TEXT NOT NULL DEFAULT '[]',
                owner_id TEXT, heartbeat_at TEXT, lease_expires_at TEXT
            );
            CREATE TABLE IF NOT EXISTS search_candidates (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, url TEXT,
                source TEXT NOT NULL, category TEXT NOT NULL, status TEXT NOT NULL,
                updated_at TEXT NOT NULL, details_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_search_candidates_run ON search_candidates(run_id);
            CREATE TABLE IF NOT EXISTS worker_jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}', result_json TEXT,
                progress_json TEXT NOT NULL DEFAULT '{}', error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                owner_id TEXT, heartbeat_at TEXT, lease_expires_at TEXT
            );
            CREATE TABLE IF NOT EXISTS worker_locks (
                name TEXT PRIMARY KEY, job_id TEXT NOT NULL, acquired_at TEXT NOT NULL,
                owner_id TEXT, heartbeat_at TEXT, lease_expires_at TEXT
            );
            CREATE TABLE IF NOT EXISTS analysis_drafts (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending_review',
                finding_json TEXT NOT NULL DEFAULT '{}', comparison_json TEXT,
                un_data_json TEXT NOT NULL DEFAULT '[]', validation_json TEXT NOT NULL DEFAULT '{}',
                revision INTEGER NOT NULL DEFAULT 1, finding_id INTEGER, reference_finding_id INTEGER,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS analysis_draft_actions (
                id INTEGER PRIMARY KEY, draft_id TEXT NOT NULL,
                action TEXT NOT NULL, revision INTEGER NOT NULL,
                acted_at TEXT NOT NULL, note TEXT
            );
            CREATE TABLE IF NOT EXISTS analysis_idempotency (
                key TEXT PRIMARY KEY, job_id TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS country_hunt_queue (
                iso3 TEXT PRIMARY KEY, country TEXT NOT NULL,
                last_attempt_at TEXT, last_successful_finding_at TEXT,
                next_eligible_at TEXT, outcome TEXT NOT NULL DEFAULT 'never_run',
                last_run_id TEXT, last_job_id TEXT, updated_at TEXT NOT NULL
            );
        ''')
        # The application database predates leases.  Keep migrations explicit
        # and idempotent so a new API process can safely open an old database.
        job_columns = {row[1] for row in conn.execute('PRAGMA table_info(worker_jobs)')}
        for name in ('owner_id', 'heartbeat_at', 'lease_expires_at'):
            if name not in job_columns:
                conn.execute(f'ALTER TABLE worker_jobs ADD COLUMN {name} TEXT')
        lock_columns = {row[1] for row in conn.execute('PRAGMA table_info(worker_locks)')}
        for name in ('owner_id', 'heartbeat_at', 'lease_expires_at'):
            if name not in lock_columns:
                conn.execute(f'ALTER TABLE worker_locks ADD COLUMN {name} TEXT')
        run_columns = {row[1] for row in conn.execute('PRAGMA table_info(search_runs)')}
        for name in ('owner_id', 'heartbeat_at', 'lease_expires_at'):
            if name not in run_columns:
                conn.execute(f'ALTER TABLE search_runs ADD COLUMN {name} TEXT')
        draft_columns = {row[1] for row in conn.execute('PRAGMA table_info(analysis_drafts)')}
        if 'reference_finding_id' not in draft_columns:
            conn.execute('ALTER TABLE analysis_drafts ADD COLUMN reference_finding_id INTEGER')


def create_job(kind, payload=None, job_id=None):
    initialise()
    job_id = job_id or str(uuid4())
    timestamp = now()
    with tools.get_connection() as conn:
        conn.execute(
            '''INSERT INTO worker_jobs
               (id, kind, status, payload_json, created_at, updated_at)
               VALUES (?, ?, 'queued', ?, ?, ?)''',
            (job_id, kind, json.dumps(payload or {}), timestamp, timestamp),
        )
    return get_job(job_id)


def get_idempotent_job(key):
    initialise()
    with tools.get_connection() as conn:
        row = conn.execute('SELECT job_id FROM analysis_idempotency WHERE key = ?', (str(key),)).fetchone()
    return get_job(row[0]) if row else None


def register_idempotency(key, job_id):
    initialise()
    with tools.get_connection() as conn:
        conn.execute('INSERT OR IGNORE INTO analysis_idempotency(key, job_id, created_at) VALUES (?, ?, ?)',
                     (str(key), str(job_id), now()))
    return get_idempotent_job(key)


def reserve_analysis_job(key, job_id, payload):
    """Atomically reserve an analysis key and create its queued job.

    The job row is created in the same transaction as the unique-key insert,
    so a losing concurrent request can always read the winner immediately.
    Returns ``(winning_job_id, inserted_by_this_caller)``.
    """
    initialise()
    timestamp = now()
    with tools.get_connection() as conn:
        cursor = conn.execute(
            'INSERT OR IGNORE INTO analysis_idempotency(key, job_id, created_at) VALUES (?, ?, ?)',
            (str(key), str(job_id), timestamp),
        )
        row = conn.execute(
            'SELECT job_id FROM analysis_idempotency WHERE key = ?', (str(key),)
        ).fetchone()
        winning_job_id = row[0]
        inserted = cursor.rowcount == 1
        if inserted:
            conn.execute(
                '''INSERT INTO worker_jobs
                   (id, kind, status, payload_json, created_at, updated_at)
                   VALUES (?, 'manual_analysis', 'queued', ?, ?, ?)''',
                (str(job_id), json.dumps(payload or {}), timestamp, timestamp),
            )
    return winning_job_id, inserted


def create_analysis_draft(job_id, *, finding=None, comparison=None, un_data=None,
                          validation=None, draft_id=None, status='pending_review',
                          reference_finding_id=None):
    """Persist a manual result for review; approval is a separate transition."""
    initialise()
    draft_id = draft_id or str(uuid4())
    timestamp = now()
    with tools.get_connection() as conn:
        conn.execute(
            '''INSERT INTO analysis_drafts
               (id, job_id, status, finding_json, comparison_json, un_data_json,
                validation_json, revision, reference_finding_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)''',
            (draft_id, str(job_id), str(status), json.dumps(finding or {}, default=str),
             json.dumps(comparison, default=str) if comparison is not None else None,
             json.dumps(un_data or [], default=str), json.dumps(validation or {}, default=str),
             reference_finding_id, timestamp, timestamp),
        )
        conn.execute(
            '''INSERT INTO analysis_draft_actions
               (draft_id, action, revision, acted_at) VALUES (?, 'created', 1, ?)''',
            (draft_id, timestamp),
        )
    return get_analysis_draft(draft_id)


def get_analysis_draft(draft_id):
    initialise()
    with tools.get_connection() as conn:
        conn.row_factory = tools.sqlite3.Row
        row = conn.execute('SELECT * FROM analysis_drafts WHERE id = ?', (str(draft_id),)).fetchone()
    if row is None:
        return None
    result = dict(row)
    for field in ('finding_json', 'comparison_json', 'un_data_json', 'validation_json'):
        raw = result.pop(field)
        result[field.removesuffix('_json')] = json.loads(raw) if raw else None
    return result


def get_analysis_draft_for_job(job_id):
    initialise()
    with tools.get_connection() as conn:
        row = conn.execute('SELECT id FROM analysis_drafts WHERE job_id = ?', (str(job_id),)).fetchone()
    return get_analysis_draft(row[0]) if row else None


def update_analysis_draft(draft_id, *, finding=None, expected_revision=None,
                          status=None, finding_id=None, validation=None,
                          action='edited', note=None):
    """Apply an optimistic and auditable draft edit or state transition."""
    initialise()
    timestamp = now()
    updates = ['updated_at = ?', 'revision = revision + 1']
    values = [timestamp]
    if finding is not None:
        updates.append('finding_json = ?'); values.append(json.dumps(finding, default=str))
    if status is not None:
        updates.append('status = ?'); values.append(str(status))
    if finding_id is not None:
        updates.append('finding_id = ?'); values.append(int(finding_id))
    if validation is not None:
        updates.append('validation_json = ?')
        values.append(json.dumps(validation, default=str))
    values.append(str(draft_id))
    where = 'id = ?'
    if expected_revision is not None:
        where += ' AND revision = ?'; values.append(int(expected_revision))
    with tools.get_connection() as conn:
        cursor = conn.execute(f'UPDATE analysis_drafts SET {", ".join(updates)} WHERE {where}', values)
        if cursor.rowcount != 1:
            if get_analysis_draft(draft_id) is None:
                raise ValueError('Analysis draft not found.')
            raise RuntimeError('Analysis draft changed; reload it before saving.')
        revision = conn.execute('SELECT revision FROM analysis_drafts WHERE id = ?', (str(draft_id),)).fetchone()[0]
        conn.execute(
            '''INSERT INTO analysis_draft_actions
               (draft_id, action, revision, acted_at, note) VALUES (?, ?, ?, ?, ?)''',
            (str(draft_id), action, revision, timestamp, note),
        )
    return get_analysis_draft(draft_id)


def list_analysis_draft_actions(draft_id):
    initialise()
    with tools.get_connection() as conn:
        conn.row_factory = tools.sqlite3.Row
        return [dict(row) for row in conn.execute(
            'SELECT * FROM analysis_draft_actions WHERE draft_id = ? ORDER BY id DESC', (str(draft_id),)
        ).fetchall()]


def get_job(job_id):
    initialise()
    with tools.get_connection() as conn:
        conn.row_factory = tools.sqlite3.Row
        row = conn.execute('SELECT * FROM worker_jobs WHERE id = ?', (str(job_id),)).fetchone()
    if not row:
        return None
    result = dict(row)
    for field in ('payload_json', 'result_json', 'progress_json'):
        value = result.pop(field)
        result[field.removesuffix('_json')] = json.loads(value) if value else None
    return result


def update_job(job_id, *, status=None, result=None, progress=None, error=None,
               increment_attempts=False, owner_id=_UNSET):
    initialise()
    updates = ['updated_at = ?']
    values = [now()]
    if status is not None:
        updates.append('status = ?'); values.append(status)
    if result is not None:
        updates.append('result_json = ?'); values.append(json.dumps(result, default=str))
    if progress is not None:
        updates.append('progress_json = ?'); values.append(json.dumps(progress, default=str))
    if error is not None:
        updates.append('error = ?'); values.append(str(error))
    if increment_attempts:
        updates.append('attempts = attempts + 1')
    if owner_id is not _UNSET:
        updates.extend(['owner_id = ?', 'heartbeat_at = ?', 'lease_expires_at = ?'])
        values.extend([owner_id, None, None])
    values.append(str(job_id))
    with tools.get_connection() as conn:
        conn.execute(f"UPDATE worker_jobs SET {', '.join(updates)} WHERE id = ?", values)
    return get_job(job_id)


def owner_id() -> str:
    """Create a stable identity for one worker process/thread lease."""
    return str(uuid4())


def _lease_expiry(seconds=WORKER_LEASE_SECONDS) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=int(seconds))).isoformat()


def claim_job(job_id, worker_owner=None, *, owner_id=None, retry=False):
    """Atomically claim a queued/retryable job for a worker owner.

    A second process cannot claim a running job, even when it is still within
    its lease.  This is the durable counterpart to the in-process thread map.
    """
    initialise()
    worker_owner = worker_owner or owner_id
    if not worker_owner:
        raise ValueError('A worker owner ID is required to claim a job.')
    allowed = ('queued', 'failed', 'interrupted') if retry else ('queued',)
    placeholders = ','.join('?' for _ in allowed)
    timestamp = now()
    with tools.get_connection() as conn:
        retry_reset = ', result_json = NULL, progress_json = \'{}\', error = NULL' if retry else ''
        cursor = conn.execute(
            f'''UPDATE worker_jobs
                SET status = 'running', owner_id = ?, heartbeat_at = ?,
                    lease_expires_at = ?, updated_at = ?, attempts = attempts + 1{retry_reset}
                WHERE id = ? AND status IN ({placeholders})''',
            (str(worker_owner), timestamp, _lease_expiry(), timestamp,
             str(job_id), *allowed),
        )
        if cursor.rowcount != 1:
            row = conn.execute('SELECT status, owner_id FROM worker_jobs WHERE id = ?', (str(job_id),)).fetchone()
            if row is None:
                raise ValueError('Worker job not found.')
            raise RuntimeError(f'Worker job is not claimable: {row[0]}.')
    return get_job(job_id)


def heartbeat_worker(job_id, worker_owner=None, *, owner_id=None,
                     lease_seconds=WORKER_LEASE_SECONDS):
    """Renew a job lease and its matching discovery lock."""
    initialise()
    worker_owner = worker_owner or owner_id
    if not worker_owner:
        return False
    timestamp = now()
    expiry = _lease_expiry(lease_seconds)
    with tools.get_connection() as conn:
        cursor = conn.execute(
            '''UPDATE worker_jobs SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
               WHERE id = ? AND owner_id = ? AND status IN ('running', 'stopping')''',
            (timestamp, expiry, timestamp, str(job_id), str(worker_owner)),
        )
        conn.execute(
            '''UPDATE worker_locks SET heartbeat_at = ?, lease_expires_at = ?
               WHERE job_id = ? AND owner_id = ?''',
            (timestamp, expiry, str(job_id), str(worker_owner)),
        )
        conn.execute(
            '''UPDATE search_runs SET heartbeat_at = ?, lease_expires_at = ?
               WHERE owner_id = ? AND status IN ('running', 'stopping')''',
            (timestamp, expiry, str(worker_owner)),
        )
        # The search run is created by BossAgent after the durable job is
        # claimed, so renew it through the run id recorded in job progress.
        row = conn.execute(
            'SELECT progress_json, result_json FROM worker_jobs WHERE id = ? AND owner_id = ?',
            (str(job_id), str(worker_owner)),
        ).fetchone()
        if row:
            for raw in row:
                try:
                    payload = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    payload = {}
                run_id = payload.get('run_id') if isinstance(payload, dict) else None
                if run_id:
                    conn.execute(
                        '''UPDATE search_runs SET owner_id = ?, heartbeat_at = ?, lease_expires_at = ?
                           WHERE id = ? AND status IN ('running', 'stopping')''',
                        (str(worker_owner), timestamp, expiry, str(run_id)),
                    )
    return cursor.rowcount == 1


def recover_orphaned_worker_jobs():
    """Make work left by a terminated process safe to retry.

    Recovery is lease-aware: only jobs whose persisted owner has become stale
    are interrupted. A live CLI/API worker retains both its job and discovery
    lock across API startup/reload.
    """
    initialise()
    timestamp = now()
    with tools.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, owner_id FROM worker_jobs "
            "WHERE status IN ('running', 'stopping') "
            "AND (lease_expires_at IS NULL OR lease_expires_at < ?)",
            (timestamp,),
        ).fetchall()
        ids = [row[0] for row in rows]
        if ids:
            placeholders = ', '.join('?' for _ in ids)
            conn.execute(
                f"UPDATE worker_jobs SET status = 'interrupted', updated_at = ?, "
                f"error = COALESCE(error, ?), owner_id = NULL, heartbeat_at = NULL, "
                f"lease_expires_at = NULL WHERE id IN ({placeholders})",
                (timestamp, 'Worker process ended before the job completed.', *ids),
            )
            # A country hunt queue row is work-in-progress while its linked
            # worker job is queued.  Do not leave it looking runnable after
            # recovery: interrupted is terminal for this attempt, with no
            # cooldown so the operator can explicitly retry it.  Keep the
            # job linkage for audit and retry diagnostics.
            conn.execute(
                f'''UPDATE country_hunt_queue
                    SET outcome = 'interrupted', next_eligible_at = NULL,
                        updated_at = ?
                    WHERE last_job_id IN ({placeholders}) AND outcome = 'queued' ''',
                (timestamp, *ids),
            )
            conn.execute(f"DELETE FROM worker_locks WHERE job_id IN ({placeholders})", ids)
        # Only expired locks may be reclaimed.  A lock belonging to a
        # completed job but still leased is left alone until its owner expires.
        conn.execute(
            "DELETE FROM worker_locks WHERE lease_expires_at IS NULL OR lease_expires_at < ?",
            (timestamp,),
        )
    return len(ids)


def acquire_worker_lock(name, job_id, worker_owner=None, *, owner_id=None,
                        lease_seconds=WORKER_LEASE_SECONDS):
    """Acquire a named lock, reclaiming only an expired prior owner.

    ``worker_owner`` is required by all production callers.  The optional
    legacy form is retained for old local scripts and is immediately stale;
    this prevents that compatibility path from masquerading as a live lease.
    """
    initialise()
    worker_owner = worker_owner or owner_id
    owner = str(worker_owner) if worker_owner else None
    timestamp = now()
    try:
        with tools.get_connection() as conn:
            existing = conn.execute(
                'SELECT job_id, lease_expires_at FROM worker_locks WHERE name = ?', (name,)
            ).fetchone()
            if existing:
                expires = existing[1]
                if expires and expires >= timestamp:
                    # Include the durable job/run handle so the operator can
                    # stop the existing work instead of guessing which lock
                    # belongs to the active process.
                    progress_row = conn.execute(
                        'SELECT progress_json FROM worker_jobs WHERE id = ?', (existing[0],)
                    ).fetchone()
                    run_id = None
                    if progress_row and progress_row[0]:
                        try:
                            run_id = json.loads(progress_row[0]).get('run_id')
                        except (TypeError, ValueError):
                            run_id = None
                    handle = f" job {existing[0]}" + (f" (run {run_id})" if run_id else '')
                    raise RuntimeError(f'Worker lock is already held: {name}.{handle} Stop that run or wait for its lease to expire.')
                conn.execute('DELETE FROM worker_locks WHERE name = ?', (name,))
            expiry = _lease_expiry(lease_seconds) if owner else timestamp
            conn.execute(
                '''INSERT INTO worker_locks
                   (name, job_id, acquired_at, owner_id, heartbeat_at, lease_expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (name, str(job_id), timestamp, owner, timestamp, expiry),
            )
    except tools.sqlite3.IntegrityError as exc:
        raise RuntimeError(f'Worker lock is already held: {name}.') from exc


def release_worker_lock(name, job_id, worker_owner=None, *, owner_id=None):
    initialise()
    worker_owner = worker_owner or owner_id
    with tools.get_connection() as conn:
        if worker_owner is None:
            conn.execute('DELETE FROM worker_locks WHERE name = ? AND job_id = ?',
                         (name, str(job_id)))
        else:
            conn.execute(
                'DELETE FROM worker_locks WHERE name = ? AND job_id = ? AND owner_id = ?',
                (name, str(job_id), str(worker_owner)),
            )


def save_settings(settings):
    initialise()
    with tools.get_connection() as conn:
        conn.execute('INSERT OR REPLACE INTO research_settings VALUES (1, ?)', (json.dumps(settings),))


def load_settings(default):
    """Load saved controls, adding defaults introduced by newer app versions."""
    initialise()
    with tools.get_connection() as conn:
        row = conn.execute('SELECT settings_json FROM research_settings WHERE id = 1').fetchone()
    if not row:
        return default
    saved = json.loads(row[0])
    if not isinstance(saved, dict):
        return default
    return {**default, **saved}


def upsert_country_hunt_queue(items, *, job_id=None):
    """Record the durable state of direct and bulk country hunts."""
    initialise()
    timestamp = now()
    with tools.get_connection() as conn:
        for item in items:
            iso3 = str(item['iso3']).upper()
            conn.execute(
                '''INSERT INTO country_hunt_queue
                   (iso3, country, last_attempt_at, outcome, last_job_id, updated_at)
                   VALUES (?, ?, ?, 'queued', ?, ?)
                   ON CONFLICT(iso3) DO UPDATE SET
                     country=excluded.country, last_attempt_at=excluded.last_attempt_at,
                     outcome='queued', last_job_id=excluded.last_job_id, updated_at=excluded.updated_at''',
                (iso3, item.get('country') or item.get('label') or iso3, timestamp, job_id, timestamp),
            )


def finish_country_hunt_queue(run_id, *, outcome='complete', outcomes_by_iso3=None,
                              successful_iso3s=None, next_eligible_at=None,
                              next_eligible_by_iso3=None, clear_successful_iso3s=None):
    """Close queue rows after a run, retaining the last successful finding."""
    initialise()
    successes = {str(value).upper() for value in (successful_iso3s or [])}
    cleared_successes = {str(value).upper() for value in (clear_successful_iso3s or [])}
    timestamp = now()
    with tools.get_connection() as conn:
        rows = conn.execute('SELECT iso3 FROM country_hunt_queue WHERE last_run_id = ?',
                            (str(run_id),)).fetchall()
        for (iso3,) in rows:
            row_outcome = (outcomes_by_iso3 or {}).get(iso3, outcome)
            row_next_eligible_at = (next_eligible_by_iso3 or {}).get(iso3, next_eligible_at)
            conn.execute(
                '''UPDATE country_hunt_queue SET outcome=?, last_successful_finding_at=CASE
                   WHEN ? THEN ? WHEN ? THEN NULL ELSE last_successful_finding_at END,
                   next_eligible_at=?, last_run_id=?, updated_at=? WHERE iso3=?''',
                (row_outcome, iso3 in successes, timestamp, iso3 in cleared_successes,
                 row_next_eligible_at, str(run_id), timestamp, iso3),
            )


def attach_country_hunt_job(iso3s, job_id):
    """Associate queue rows with the durable worker job before it starts."""
    initialise()
    with tools.get_connection() as conn:
        for iso3 in iso3s:
            conn.execute('UPDATE country_hunt_queue SET last_job_id=?, updated_at=? WHERE iso3=?',
                         (str(job_id), now(), str(iso3).upper()))


def finish_country_hunt_queue_for_job(job_id, *, outcome='error', next_eligible_at=None):
    """Close queue rows when a run fails before a search run can be linked."""
    initialise()
    timestamp = now()
    with tools.get_connection() as conn:
        conn.execute(
            '''UPDATE country_hunt_queue SET outcome=?, next_eligible_at=?, updated_at=?
               WHERE last_job_id=? AND outcome='queued' ''',
            (outcome, next_eligible_at, timestamp, str(job_id)),
        )


def mark_country_hunt_run(run_id, iso3s):
    """Associate a newly-created discovery run with its country queue rows."""
    initialise()
    with tools.get_connection() as conn:
        for iso3 in iso3s:
            conn.execute('UPDATE country_hunt_queue SET last_run_id=?, updated_at=? WHERE iso3=?',
                         (str(run_id), now(), str(iso3).upper()))


def list_country_hunt_queue():
    initialise()
    with tools.get_connection() as conn:
        conn.row_factory = tools.sqlite3.Row
        return [dict(row) for row in conn.execute(
            'SELECT iso3, country, last_attempt_at, last_successful_finding_at, '
            'next_eligible_at, outcome, last_run_id, last_job_id, updated_at '
            'FROM country_hunt_queue ORDER BY country'
        ).fetchall()]


def start_run(settings, owner_id=None):
    initialise()
    run_id = str(uuid4())
    with tools.get_connection() as conn:
        timestamp = now()
        conn.execute(
            '''INSERT INTO search_runs
               (id, started_at, status, settings_json, owner_id, heartbeat_at, lease_expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (run_id, timestamp, 'running', json.dumps(settings), owner_id,
             timestamp if owner_id else None, _lease_expiry() if owner_id else None),
        )
    return run_id


def finish_run(run_id, status):
    with tools.get_connection() as conn:
        conn.execute(
            'UPDATE search_runs SET status = ?, finished_at = ?, '
            'owner_id = NULL, heartbeat_at = NULL, lease_expires_at = NULL WHERE id = ?',
            (status, now(), run_id),
        )


def request_run_stop(run_id):
    """Persist a cooperative stop request without pretending work finished."""
    initialise()
    timestamp = now()
    with tools.get_connection() as conn:
        row = conn.execute('SELECT status, events_json FROM search_runs WHERE id = ?', (str(run_id),)).fetchone()
        if row is None:
            raise ValueError('Research run not found.')
        if row[0] != 'running':
            return row[0]
        events = json.loads(row[1] or '[]')
        events.append({'at': timestamp, 'event': 'stop_requested', 'source': 'api'})
        conn.execute(
            "UPDATE search_runs SET status = 'stopping', finished_at = NULL, events_json = ? WHERE id = ?",
            (json.dumps(events), str(run_id)),
        )
    return 'stopping'


def run_stop_requested(run_id):
    initialise()
    with tools.get_connection() as conn:
        row = conn.execute('SELECT status FROM search_runs WHERE id = ?', (str(run_id),)).fetchone()
    return row is not None and row[0] == 'stopping'


def recover_orphaned_runs():
    """Close runs left as running after their owner lease expired."""
    initialise()
    with tools.get_connection() as conn:
        timestamp = now()
        rows = conn.execute(
            "SELECT id, events_json FROM search_runs WHERE status IN ('running', 'stopping') "
            "AND (lease_expires_at IS NULL OR lease_expires_at < ?)", (timestamp,)
        ).fetchall()
        for run_id, events_json in rows:
            events = json.loads(events_json or '[]')
            events.append({'at': now(), 'event': 'recovered_orphan',
                           'message': 'Marked interrupted when the application started.'})
            conn.execute(
                'UPDATE search_runs SET status = ?, finished_at = ?, events_json = ?, '
                'owner_id = NULL, heartbeat_at = NULL, lease_expires_at = NULL WHERE id = ?',
                ('interrupted', now(), json.dumps(events), run_id),
            )
            # This covers runs that were created/linked before a worker job
            # record was available, and is idempotent with worker-job
            # recovery above.  A queue row must never remain queued for an
            # orphaned run; NULL keeps the interrupted attempt retryable.
            conn.execute(
                '''UPDATE country_hunt_queue
                   SET outcome = 'interrupted', next_eligible_at = NULL,
                       updated_at = ?
                   WHERE last_run_id = ? AND outcome = 'queued' ''',
                (now(), str(run_id)),
            )
    return len(rows)


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
        status = updates.get('status', row[1])
        # Retain a page body only while it is needed by the active review and
        # extraction steps.  Finished, failed, and deferred candidates are an
        # outcome audit, not an article-content archive.
        if candidate_audit_is_final(status):
            details = compact_candidate_details(details)
        conn.execute('UPDATE search_candidates SET details_json = ?, status = ?, updated_at = ? WHERE id = ?',
                     (json.dumps(details, default=str), status, now(), candidate_id))


def list_runs():
    initialise()
    with tools.get_connection() as conn:
        conn.row_factory = tools.sqlite3.Row
        return [dict(row) for row in conn.execute('SELECT * FROM search_runs ORDER BY started_at DESC LIMIT 100')]


def get_run(run_id):
    initialise()
    with tools.get_connection() as conn:
        conn.row_factory = tools.sqlite3.Row
        row = conn.execute('SELECT * FROM search_runs WHERE id = ?', (run_id,)).fetchone()
    return dict(row) if row else None


def list_candidates(run_id=None):
    initialise()
    with tools.get_connection() as conn:
        conn.row_factory = tools.sqlite3.Row
        rows = conn.execute('SELECT * FROM search_candidates' + (' WHERE run_id = ?' if run_id else '') + ' ORDER BY id DESC LIMIT 1000', (run_id,) if run_id else ()).fetchall()
    displayed = []
    visible_fields = {
        'title', 'snippet', 'summary_decision', 'summary_reason', 'full_decision',
        'summary_warning', 'discovery_warning', 'review_warning',
        'full_reason', 'error', 'finding_id', 'duplicate_candidate_id',
        'duplicate_of', 'duplicate_kind', 'canonical_url', 'source_classification',
        'country_iso3', 'scope_country_iso3', 'scope_country', 'scope_mismatch',
    }
    for row in rows:
        details = json.loads(row['details_json'])
        extraction = details.get('extraction')
        result = {
            **{key: value for key, value in dict(row).items() if key != 'details_json'},
            **{key: value for key, value in details.items() if key in visible_fields},
        }
        if isinstance(extraction, dict):
            result['extracted_country'] = extraction.get('geography') or ''
            result['extracted_iso3'] = extraction.get('geography_iso3') or ''
            result['extracted_summary'] = extraction.get('summary') or ''
        else:
            result['extracted_country'] = ''
            result['extracted_iso3'] = ''
            result['extracted_summary'] = ''
        scoped_iso3 = str(details.get('country_iso3') or '').upper()
        if scoped_iso3:
            result['scope_country_iso3'] = scoped_iso3
            result['scope_country'] = tools.normalise_country_name(scoped_iso3) or scoped_iso3
            result['scope_mismatch'] = (
                bool(result['extracted_iso3']) and result['extracted_iso3'].upper() != scoped_iso3
            )
        displayed.append(result)
    return displayed


def list_historical_candidate_urls(exclude_run_id):
    """Return URLs whose content was successfully loaded in an earlier run.

    Discovery alone, a summary-only decision, or an access failure must not
    suppress a future retry.  ``loaded_url`` is the page actually retrieved;
    it differs from the original candidate when alternative-source recovery
    succeeded.
    """
    initialise()
    with tools.get_connection() as conn:
        # ``page_loaded`` is a deliberately small durable marker.  Keeping it
        # separate from the article body lets the later audit-cleanup job drop
        # large ``full_text`` values without re-enabling URLs that we already
        # successfully fetched.  The ``full_text`` branch keeps pre-marker
        # audit rows working until that cleanup has run.
        rows = conn.execute(
            'SELECT id, url, details_json FROM search_candidates '
            'WHERE run_id != ? AND (details_json LIKE ? OR details_json LIKE ?) ORDER BY id ASC',
            (exclude_run_id, '%"page_loaded"%', '%"full_text"%'),
        ).fetchall()
    loaded = []
    for candidate_id, candidate_url, details_json in rows:
        try:
            details = json.loads(details_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if details.get('page_loaded') is not True and 'full_text' not in details:
            continue
        loaded_url = details.get('loaded_url') or details.get('replacement_url') or candidate_url
        if loaded_url:
            loaded.append((candidate_id, loaded_url))
    return loaded


def get_candidate(candidate_id):
    initialise()
    with tools.get_connection() as conn:
        conn.row_factory = tools.sqlite3.Row
        row = conn.execute('SELECT * FROM search_candidates WHERE id = ?', (candidate_id,)).fetchone()
    if row is None:
        raise ValueError('Candidate not found.')
    result = dict(row)
    details = json.loads(result.pop('details_json'))
    # Candidate detail is a browser contract, not a page-content archive.
    # Keep extraction/review outcomes while excluding retrieved bodies and
    # provider payloads that can contain unbounded or sensitive content.
    details = compact_candidate_details(details)
    # Keep the original discovery URL in the detail contract even when the
    # audit payload has been compacted after a terminal outcome.
    if result.get('url') and not details.get('url'):
        details['url'] = result['url']
    for field in ('full_text', 'page_text', 'raw', 'provider_response'):
        details.pop(field, None)
    scoped_iso3 = str(details.get('country_iso3') or '').upper()
    extraction = details.get('extraction') if isinstance(details.get('extraction'), dict) else {}
    extracted_iso3 = str(extraction.get('geography_iso3') or '').upper()
    if scoped_iso3:
        details['scope_country_iso3'] = scoped_iso3
        details['scope_country'] = tools.normalise_country_name(scoped_iso3) or scoped_iso3
        details['scope_mismatch'] = bool(extracted_iso3 and extracted_iso3 != scoped_iso3)
    result['details'] = details
    return result
