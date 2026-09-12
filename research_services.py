"""API services for manual analysis, discovery, and country hunts.

These services intentionally contain no FastAPI or Gradio imports. Jobs use the
existing durable research store and finding pipeline; the HTTP layer only
validates and serializes their plain-data results.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage

import agents
import research_store
import tools
from research import BossAgent, CRITERIA, DEFAULT_SETTINGS, SearchSettings


HUNT_QUERY = ('"{country}" (population OR births OR deaths OR fertility OR migration) '
              '("official statistics" OR "statistical office" census OR release)')

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.RLock()
_research_workers: dict[str, tuple[threading.Thread, threading.Event]] = {}
_research_lock = threading.RLock()


def start_manual_analysis(url: str, *, country_iso3: str | None = None,
                          compare: bool = True) -> dict[str, Any]:
    url = str(url or '').strip()
    if not url.startswith(('http://', 'https://')):
        raise ValueError('url must be an absolute HTTP or HTTPS URL.')
    context = _country_context(country_iso3) if country_iso3 else None
    job_id = str(uuid4())
    job = {
        'id': job_id, 'kind': 'manual_analysis', 'status': 'queued',
        'url': url, 'country_iso3': context['iso3'] if context else None,
        'country': context['label'] if context else None,
        'compare': bool(compare), 'logs': [], 'fetch_status': None,
        'created_at': _now(), 'updated_at': _now(),
    }
    with _jobs_lock:
        _jobs[job_id] = job
    research_store.create_job('manual_analysis', {
        'url': url, 'country_iso3': job['country_iso3'], 'compare': bool(compare)
    }, job_id=job_id)
    worker = threading.Thread(target=_run_manual, args=(job_id, url, context, bool(compare)),
                              name=f'manual-analysis-{job_id}', daemon=True)
    worker.start()
    return _public_job(job)


def get_manual_analysis(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(str(job_id))
    if job is not None:
        return _public_job(job)
    durable = research_store.get_job(str(job_id))
    if durable is None or durable['kind'] != 'manual_analysis':
        raise ValueError('Analysis job not found.')
    return _durable_manual_public_job(durable)


def start_research(settings: dict[str, Any]) -> dict[str, Any]:
    validated = SearchSettings.model_validate(settings)
    _validate_country_scopes(validated)
    if not any(category.enabled for category in validated.categories) and not validated.reddit_enabled:
        raise ValueError('Enable a search category or Reddit before starting research.')
    return _start_research(validated.model_dump())


def get_research_settings() -> dict[str, Any]:
    return research_store.load_settings(DEFAULT_SETTINGS)


def get_worker_job(job_id: str) -> dict[str, Any]:
    job = research_store.get_job(str(job_id))
    if job is None:
        raise ValueError('Worker job not found.')
    # Payloads can contain provider settings and are not part of the client
    # contract. Progress/result are deliberately JSON-only and restart-safe.
    return {
        'id': job['id'], 'kind': job['kind'], 'status': job['status'],
        'result': job.get('result'), 'progress': job.get('progress') or {},
        'error': job.get('error'), 'created_at': job['created_at'],
        'updated_at': job['updated_at'], 'attempts': job['attempts'],
    }


def save_research_settings(settings: dict[str, Any]) -> dict[str, Any]:
    validated = SearchSettings.model_validate(settings)
    _validate_country_scopes(validated)
    if not any(category.enabled for category in validated.categories) and not validated.reddit_enabled:
        raise ValueError('Enable a search category or Reddit before saving settings.')
    payload = validated.model_dump()
    research_store.save_settings(payload)
    return payload


def start_country_hunt(country_iso3: str, *, max_results: int = 12) -> dict[str, Any]:
    context = _country_context(country_iso3)
    settings = country_hunt_settings(context, max_results)
    result = _start_research(settings.model_dump())
    result.update({'country_iso3': context['iso3'], 'country': context['label']})
    return result


def country_hunt_settings(context: dict[str, str], max_results: int = 12) -> SearchSettings:
    if not 1 <= int(max_results) <= 20:
        raise ValueError('max_results must be between 1 and 20.')
    return SearchSettings(
        categories=[{
            'name': f'Country hunt: {context["label"]}',
            'query': HUNT_QUERY.format(country=context['label']),
            'topic': 'news', 'max_results': int(max_results),
            'time_range': 'year', 'search_depth': 'advanced',
            'country_iso3': context['iso3'],
        }],
        reddit_enabled=False, max_candidates=int(max_results), max_per_domain=5,
        domain_limit_scope='category', review_criteria=CRITERIA,
    )


def start_bulk_country_hunt(country_iso3s: list[str], *, max_results: int = 5) -> dict[str, Any]:
    if not country_iso3s or len(country_iso3s) > 100:
        raise ValueError('country_iso3s must contain between 1 and 100 countries.')
    contexts = [_country_context(value) for value in country_iso3s]
    if not 1 <= int(max_results) <= 20:
        raise ValueError('max_results must be between 1 and 20.')
    categories = [{
        'name': f'Country hunt: {context["label"]}',
        'query': HUNT_QUERY.format(country=context['label']),
        'topic': 'news', 'max_results': int(max_results),
        'time_range': 'year', 'search_depth': 'advanced',
        'country_iso3': context['iso3'],
    } for context in contexts]
    settings = SearchSettings(
        categories=categories, reddit_enabled=False,
        max_candidates=int(max_results) * len(contexts), max_per_domain=5,
        domain_limit_scope='category', review_criteria=CRITERIA,
    )
    result = _start_research(settings.model_dump())
    result.update({'country_iso3s': [context['iso3'] for context in contexts]})
    return result


def get_research_run(run_id: str) -> dict[str, Any]:
    run = research_store.get_run(str(run_id))
    if not run:
        raise ValueError('Research run not found.')
    return {
        **run,
        'candidates': research_store.list_candidates(str(run_id)),
    }


def stop_research(run_id: str) -> dict[str, Any]:
    run_id = str(run_id)
    with _research_lock:
        task = _research_workers.get(run_id)
    if task is not None:
        task[1].set()
        try:
            research_store.log_event(run_id, {'event': 'stop_requested', 'source': 'api'})
        except Exception:
            pass
        return {'run_id': run_id, 'status': 'stopping'}
    run = research_store.get_run(run_id)
    if not run:
        raise ValueError('Research run not found.')
    if run['status'] in {'running', 'stopping'}:
        research_store.finish_run(run_id, 'interrupted')
    return {'run_id': run_id, 'status': 'interrupted'}


def _start_research(settings: dict[str, Any]) -> dict[str, Any]:
    ready = threading.Event()
    holder: dict[str, Any] = {}
    stop_event = threading.Event()
    lock_id = str(uuid4())
    research_store.acquire_worker_lock('discovery', lock_id)

    def worker() -> None:
        try:
            iterator = BossAgent().run(settings, stop_event=stop_event)
            first = next(iterator)
            holder['run_id'] = first[0]
            ready.set()
            for _run_id, _message in iterator:
                pass
        except StopIteration:
            ready.set()
        except Exception as exc:
            holder['error'] = str(exc)
            ready.set()
        finally:
            research_store.release_worker_lock('discovery', lock_id)
            run_id = holder.get('run_id')
            if run_id:
                with _research_lock:
                    _research_workers.pop(run_id, None)

    thread = threading.Thread(target=worker, name='demographics-research-api', daemon=True)
    thread.start()
    if not ready.wait(timeout=10):
        research_store.release_worker_lock('discovery', lock_id)
        raise RuntimeError('Research job did not start within ten seconds.')
    if holder.get('error'):
        research_store.release_worker_lock('discovery', lock_id)
        raise ValueError(holder['error'])
    run_id = holder.get('run_id')
    if not run_id:
        raise RuntimeError('Research job did not return a run identifier.')
    with _research_lock:
        _research_workers[run_id] = (thread, stop_event)
    return {'run_id': run_id, 'status': 'running'}


def _run_manual(job_id: str, url: str, context: dict[str, str] | None, compare: bool) -> None:
    _set_job(job_id, status='running', stage='fetching')
    progress_token = tools.set_progress_callback(
        lambda event: _record_job_progress(job_id, event)
    )
    try:
        state = {
            'messages': [HumanMessage(content=f'Analyse the article at {url}')],
            'article_url': url,
            'page_text': '',
            'provenance': {
                'submission_type': 'manual', 'discovery_source': 'api',
                **({'country_iso3': context['iso3']} if context else {}),
            },
        }
        if context:
            state['country_context_iso3'] = context['iso3']
            state['country_context_label'] = context['label']
        extracted = agents.research_agent(state)
        _set_job(job_id, stage='extracting')
        result = {**state, **extracted}
        if compare:
            _set_job(job_id, stage='comparing')
            result.update(agents.compare_to_un(result))
        else:
            result['comparison'] = None
            result['un_data'] = []
        # Do not expose LangChain message objects or retrieved page text in the
        # status payload. The durable finding/candidate audit remains the
        # source for stored evidence; this response is a compact operation
        # result.
        payload = {key: result.get(key) for key in ('result', 'comparison', 'un_data', 'storage')}
        _set_job(job_id, status='complete', stage='complete', result=_jsonable(payload))
    except Exception as exc:
        _set_job(job_id, status='failed', stage='failed', error=str(exc))
    finally:
        tools.reset_progress_callback(progress_token)


def _country_context(iso3: str) -> dict[str, str]:
    value = str(iso3 or '').strip().upper()
    if len(value) != 3 or not value.isalpha():
        raise ValueError('country_iso3 must be a three-letter ISO3 code.')
    resolved = tools.resolve_country_iso3(value)
    if not resolved or resolved.upper() != value:
        raise ValueError(f'Unknown ISO3 country code: {value}.')
    label = tools.normalise_country_name(value)
    if not label:
        raise ValueError(f'No canonical country label exists for ISO3 {value}.')
    return {'iso3': value, 'label': label}


def _validate_country_scopes(settings: SearchSettings) -> None:
    for category in settings.categories:
        if category.country_iso3:
            category.country_iso3 = _country_context(category.country_iso3)['iso3']


def _set_job(job_id: str, **updates: Any) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(updates, updated_at=_now())
            job = _jobs[job_id]
    durable_updates = {}
    if 'status' in updates:
        durable_updates['status'] = updates['status']
    if 'result' in updates:
        durable_updates['result'] = updates['result']
    if 'error' in updates:
        durable_updates['error'] = updates['error']
    if 'stage' in updates or 'fetch_status' in updates or 'logs' in updates:
        durable_updates['progress'] = {
            'stage': job.get('stage'), 'fetch_status': job.get('fetch_status'),
            'logs': job.get('logs', []),
        }
    if durable_updates:
        research_store.update_job(job_id, **durable_updates)


def _record_job_progress(job_id: str, event: dict[str, Any]) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        logs = job.setdefault('logs', [])
        message = event.get('message')
        if message:
            logs.append({'at': _now(), 'message': str(message)})
            del logs[:-100]
        if event.get('type') == 'fetch_status':
            job['fetch_status'] = event.get('status')
        job['updated_at'] = _now()
        progress = {'stage': job.get('stage'), 'fetch_status': job.get('fetch_status'), 'logs': logs}
    research_store.update_job(job_id, progress=progress)


def _durable_manual_public_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get('payload') or {}
    progress = job.get('progress') or {}
    return {
        'id': job['id'], 'kind': job['kind'], 'status': job['status'],
        'url': payload.get('url'), 'country_iso3': payload.get('country_iso3'),
        'compare': payload.get('compare', True), 'created_at': job['created_at'],
        'updated_at': job['updated_at'], 'stage': progress.get('stage'),
        'fetch_status': progress.get('fetch_status'), 'logs': progress.get('logs', []),
        'result': job.get('result'), 'error': job.get('error'),
    }


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return dict(job)


def _jsonable(value: Any) -> Any:
    if hasattr(value, 'model_dump'):
        return _jsonable(value.model_dump(mode='json'))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
