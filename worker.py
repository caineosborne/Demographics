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
        return job
    research_store.update_job(job_id, status='running', increment_attempts=True)
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
            research_services._run_manual(job_id, payload['url'], context, payload.get('compare', True))
        elif job['kind'] in {'news_search', 'country_search'}:
            if job['kind'] == 'country_search':
                context = research_services._country_context(job['payload']['country_iso3'])
                settings = research_services.country_hunt_settings(
                    context, job['payload'].get('max_results', 12)
                ).model_dump()
            else:
                settings = SearchSettings.model_validate(job['payload']['settings']).model_dump()
            research_store.acquire_worker_lock('discovery', job_id)
            try:
                messages = list(BossAgent().run(settings))
                research_store.update_job(job_id, status='complete', result={
                    'run_id': messages[-1][0] if messages else None,
                    'message': messages[-1][1] if messages else None,
                })
            finally:
                research_store.release_worker_lock('discovery', job_id)
        elif job['kind'] == 'maintenance':
            result = database_maintenance.inventory_database()
            research_store.update_job(job_id, status='complete', result=result)
        elif job['kind'] == 'export':
            output = Path(job['payload']['output']).expanduser().resolve()
            output.write_text(json.dumps({
                'findings': research_services.research_store.list_runs(),
            }, indent=2, default=str) + '\n', encoding='utf-8')
            research_store.update_job(job_id, status='complete', result={'output': str(output)})
        else:
            raise ValueError(f"Unsupported worker job kind: {job['kind']}.")
    except Exception as exc:
        research_store.update_job(job_id, status='failed', error=str(exc))
    return research_store.get_job(job_id)


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
    export = sub.add_parser('export')
    export.add_argument('output', type=Path)
    run = sub.add_parser('run')
    run.add_argument('job_id')
    return parser


def main(argv=None) -> None:
    args = _parser().parse_args(argv)
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
        job = research_store.create_job('export', {'output': str(args.output)})
        job = run_job(job['id'])
    print(json.dumps(job, indent=2, default=str))


if __name__ == '__main__':
    main()
