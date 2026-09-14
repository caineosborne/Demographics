"""Durable command-line worker for API jobs.

Examples::

    python worker.py manual-analysis https://example.test/release --country-iso3 JPN
    python worker.py news-search settings.json
    python worker.py country-search JPN
    python worker.py run JOB_ID

Commands create a durable job first. ``run`` is suitable for a process
manager, while the convenience commands create and execute one job directly.
"""

from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path
from typing import Any

import database_maintenance
import research_store
import research_services
from research import BossAgent, SearchSettings


def run_job(job_id: str) -> dict[str, Any]:
    job = research_store.get_job(job_id)
    if not job:
        raise ValueError(f"Unknown worker job: {job_id}.")
    if job['status'] in {'complete', 'failed', 'interrupted'}:
        # A retry is explicit through a second `run` invocation; terminal
        # statuses are not silently rewritten by a duplicate command.
        if job['status'] != 'failed' and job['status'] != 'interrupted':
            return job
    worker_owner = research_store.owner_id()
    research_store.claim_job(job_id, worker_owner, retry=job['status'] in {'failed', 'interrupted'})
    job = research_store.get_job(job_id)
    heartbeat_stop = None
    if job['kind'] != 'manual_analysis':
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=_heartbeat_loop, args=(job_id, worker_owner, heartbeat_stop), daemon=True
        )
        heartbeat.start()
    try:
        if job['kind'] == 'manual_analysis':
            payload = job['payload']
            # Use the service's established pipeline and durable progress
            # adapter; this command is intentionally process-local while the
            # job record remains restart-safe.
            research_services._jobs[job_id] = {
                'id': job_id, 'kind': job['kind'], 'status': 'queued',
                'url': payload['url'], 'country_iso3': payload.get('country_iso3'),
                'country': None, 'compare': payload.get('compare', True),
                'logs': [], 'fetch_status': None,
                'created_at': job['created_at'], 'updated_at': job['updated_at'],
            }
            context = (research_services._country_context(payload['country_iso3'])
                       if payload.get('country_iso3') else None)
            research_services._run_manual(
                job_id, payload['url'], context, payload.get('compare', True), worker_owner,
                direct_service=True,
            )
        elif job['kind'] in {'news_search', 'country_search'}:
            if job['kind'] == 'country_search':
                context = research_services._country_context(job['payload']['country_iso3'])
                research_store.upsert_country_hunt_queue([context], job_id=job_id)
                settings = research_services.country_hunt_settings(
                    context, job['payload'].get('max_results', 12)
                ).model_dump()
            else:
                settings = SearchSettings.model_validate(job['payload']['settings']).model_dump()
            research_store.acquire_worker_lock('discovery', job_id, worker_owner)
            try:
                messages = list(BossAgent().run(
                    settings, owner_id=worker_owner,
                    persist_settings=(job['kind'] != 'country_search'),
                ))
                if job['kind'] == 'country_search' and messages:
                    run_id = messages[-1][0]
                    research_store.mark_country_hunt_run(run_id, [context['iso3']])
                    candidates = research_store.list_candidates(run_id)
                    successes = {candidate.get('extracted_iso3') for candidate in candidates if candidate.get('status') == 'complete'}
                    research_store.finish_country_hunt_queue(
                        run_id, successful_iso3s=successes,
                        outcomes_by_iso3={context['iso3']: 'finding_ready' if context['iso3'] in successes else (candidates[0].get('status') if candidates else 'no_candidate')},
                    )
                research_store.update_job(job_id, status='complete', result={
                    'run_id': messages[-1][0] if messages else None,
                    'message': messages[-1][1] if messages else None,
                })
            finally:
                research_store.release_worker_lock('discovery', job_id, worker_owner)
        elif job['kind'] == 'maintenance':
            result = database_maintenance.inventory_database()
            research_store.update_job(job_id, status='complete', result=result)
        elif job['kind'] == 'export':
            raise ValueError('The export worker is disabled until Phase 7.')
        else:
            raise ValueError(f"Unsupported worker job kind: {job['kind']}.")
    except Exception as exc:
        if job['kind'] == 'country_search':
            research_store.finish_country_hunt_queue_for_job(
                job_id, outcome='error',
                next_eligible_at=(research_services._now_datetime() + research_services.timedelta(days=31)).isoformat(),
            )
        research_store.update_job(job_id, status='failed', error=str(exc))
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        research_store.update_job(job_id, owner_id=None)
    return research_store.get_job(job_id)


def _heartbeat_loop(job_id: str, worker_owner: str, stop_event) -> None:
    while not stop_event.wait(research_store.WORKER_HEARTBEAT_SECONDS):
        if not research_store.heartbeat_worker(job_id, worker_owner):
            return


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    manual = sub.add_parser('manual-analysis')
    manual.add_argument('url')
    manual.add_argument('--country-iso3')
    manual.add_argument('--no-compare', action='store_true')
    news = sub.add_parser('news-search')
    news.add_argument('settings_json', type=Path)
    country = sub.add_parser('country-search')
    country.add_argument('country_iso3')
    country.add_argument('--max-results', type=int, default=12)
    sub.add_parser('maintenance')
    sub.add_parser('recover')
    run = sub.add_parser('run')
    run.add_argument('job_id')
    return parser


def main(argv=None) -> None:
    args = _parser().parse_args(argv)
    if args.command == 'recover':
        recovered_jobs = research_store.recover_orphaned_worker_jobs()
        recovered_runs = research_store.recover_orphaned_runs()
        print(json.dumps({'interrupted_jobs': recovered_jobs, 'interrupted_runs': recovered_runs}))
        return
    if args.command == 'run':
        job = run_job(args.job_id)
    elif args.command == 'manual-analysis':
        job = research_store.create_job('manual_analysis', {
            'url': args.url, 'country_iso3': args.country_iso3,
            'compare': not args.no_compare,
        })
        job = run_job(job['id'])
    elif args.command == 'news-search':
        settings = json.loads(args.settings_json.read_text(encoding='utf-8'))
        job = research_store.create_job('news_search', {'settings': settings})
        job = run_job(job['id'])
    elif args.command == 'country-search':
        job = research_store.create_job('country_search', {
            'country_iso3': args.country_iso3, 'max_results': args.max_results,
        })
        job = run_job(job['id'])
    elif args.command == 'maintenance':
        job = research_store.create_job('maintenance')
        job = run_job(job['id'])
    else:
        raise ValueError('The export command is disabled until Phase 7.')
    print(json.dumps(job, indent=2, default=str))
    if job['status'] in {'failed', 'interrupted'}:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
