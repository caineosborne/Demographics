"""API services for manual analysis, discovery, and country hunts.

These services intentionally contain no FastAPI or Gradio imports. Jobs use the
existing durable research store and finding pipeline; the HTTP layer only
validates and serializes their plain-data results.
"""

from __future__ import annotations

import threading
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import agents
import admin_services
import research_store
import tools
from langchain_core.messages import HumanMessage
from research import BossAgent, CRITERIA, DEFAULT_SETTINGS, SearchSettings


HUNT_QUERY = ('"{country}" (population OR births OR deaths OR fertility OR migration) '
              '("official statistics" OR "statistical office" census OR release)')

# Search providers benefit from the common article name as well as the WPP
# canonical label. Keep this allow-list deliberately small and deterministic;
# it is not a free-form query expansion mechanism.
COUNTRY_HUNT_ALIASES = {
    'Russian Federation': ('Russia',),
    'Türkiye': ('Turkey',),
    'Republic of Korea': ('South Korea',),
    'United States of America': ('United States', 'USA'),
}


def _country_hunt_query_name(label: str) -> str:
    aliases = COUNTRY_HUNT_ALIASES.get(label, ())
    return '" OR "'.join((label, *aliases))

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.RLock()
_research_workers: dict[str, tuple[threading.Thread, threading.Event, str, str]] = {}
_research_lock = threading.RLock()


def start_manual_analysis(url: str, *, country_iso3: str | None = None,
                          compare: bool = True, idempotency_key: str | None = None,
                          allow_rerun: bool = False, review_before_store: bool = False) -> dict[str, Any]:
    url = str(url or '').strip()
    canonical_url = tools.canonicalise_source_url(url)
    if any(character.isspace() for character in url) or url.count('://') != 1:
        raise ValueError('url must contain exactly one absolute HTTP or HTTPS URL.')
    context = _country_context(country_iso3) if country_iso3 else None
    job_id = str(uuid4())
    job = {
        'id': job_id, 'kind': 'manual_analysis', 'status': 'queued',
        'url': url, 'country_iso3': context['iso3'] if context else None,
        'country': context['label'] if context else None,
        'compare': bool(compare), 'allow_rerun': bool(allow_rerun),
        'review_before_store': bool(review_before_store), 'logs': [], 'fetch_status': None,
        'extraction_prompt_version': agents.EXTRACTION_PROMPT_VERSION,
        'extraction_rule_version': agents.EXTRACTION_RULE_VERSION,
        'created_at': _now(), 'updated_at': _now(),
    }
    payload = {
        'url': url, 'canonical_url': canonical_url, 'country_iso3': job['country_iso3'],
        'compare': bool(compare), 'allow_rerun': bool(allow_rerun),
        'review_before_store': bool(review_before_store),
        'extraction_prompt_version': agents.EXTRACTION_PROMPT_VERSION,
        'extraction_rule_version': agents.EXTRACTION_RULE_VERSION,
    }
    if idempotency_key:
        winning_job_id, inserted = research_store.reserve_analysis_job(idempotency_key, job_id, payload)
        if not inserted:
            winner = research_store.get_job(winning_job_id)
            if winner is None:
                raise RuntimeError('The idempotent analysis job could not be recovered.')
            return _durable_manual_public_job(winner)
    else:
        research_store.create_job('manual_analysis', payload, job_id=job_id)
    with _jobs_lock:
        _jobs[job_id] = job
    worker_owner = research_store.owner_id()
    research_store.claim_job(job_id, worker_owner)
    job['owner_id'] = worker_owner
    stop_event = threading.Event()
    worker = threading.Thread(target=_run_manual,
                              args=(job_id, url, context, bool(compare), worker_owner, stop_event,
                                    bool(allow_rerun), True, bool(review_before_store)),
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


def get_analysis_draft(draft_id: str) -> dict[str, Any]:
    draft = research_store.get_analysis_draft(draft_id)
    if draft is None:
        raise ValueError('Analysis draft not found.')
    return _public_draft(draft)


def get_analysis_draft_actions(draft_id: str) -> list[dict[str, Any]]:
    if research_store.get_analysis_draft(draft_id) is None:
        raise ValueError('Analysis draft not found.')
    return research_store.list_analysis_draft_actions(draft_id)


def _validate_manual_finding(finding: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the same schema and deterministic Step 3.4 guards as extraction."""
    try:
        model = agents.RelevantResult.model_validate(finding)
    except Exception as exc:
        return finding, {
            'status': 'needs_review',
            'issues': [{'level': 'needs_review', 'reason': f'Finding schema validation failed: {exc}'}],
        }
    validation = agents.validate_extracted_result(model)
    if validation.get('status') == 'validated' and not any(
            isinstance(metric, agents.Statistic) and metric.value is not None
            for metric in model.statistics.__dict__.values()):
        validation = {
            **validation,
            'status': 'rejected',
            'issues': [*(validation.get('issues') or []), {
                'level': 'reject',
                'reason': 'No numeric demographic metric is available for approval.',
            }],
        }
    return model.model_dump(mode='json'), validation


def edit_analysis_draft(draft_id: str, finding: dict[str, Any], *, expected_revision: int | None = None) -> dict[str, Any]:
    draft = research_store.get_analysis_draft(draft_id)
    if draft is None:
        raise ValueError('Analysis draft not found.')
    if draft['status'] != 'pending_review':
        raise ValueError('Only a pending analysis draft can be edited.')
    if not isinstance(finding, dict) or not str(finding.get('url') or '').strip():
        raise ValueError('Draft finding must be an object with a non-empty url.')
    tools.canonicalise_source_url(finding['url'])
    normalized, validation = _validate_manual_finding(finding)
    return _public_draft(research_store.update_analysis_draft(
        draft_id, finding=normalized, validation=validation,
        expected_revision=expected_revision, action='edited',
        note=f"validation={validation.get('status', 'needs_review')}",
    ))


def approve_analysis_draft(draft_id: str) -> dict[str, Any]:
    draft = research_store.get_analysis_draft(draft_id)
    if draft is None:
        raise ValueError('Analysis draft not found.')
    if draft['status'] == 'approved':
        return _public_draft(draft)
    if draft['status'] != 'pending_review':
        raise ValueError('Only a pending analysis draft can be approved.')
    finding = draft.get('finding') or {}
    if not isinstance(finding, dict) or not str(finding.get('url') or '').strip():
        raise ValueError('The draft has no valid finding to approve.')
    normalized, validation = _validate_manual_finding(finding)
    if validation.get('status') != 'validated':
        # Keep the latest guard result durable so a reviewer can correct the
        # draft and retry.  Approval is never inferred from an old validation
        # payload supplied by a client or a previous extraction run.
        research_store.update_analysis_draft(
            draft_id, finding=normalized, validation=validation,
            action='validation_failed',
            note=f"validation={validation.get('status', 'needs_review')}",
        )
        raise ValueError(
            'The draft cannot be approved until deterministic validation passes: '
            + '; '.join(item.get('reason', 'review required') for item in validation.get('issues', []))
        )
    finding = normalized
    if not any(isinstance(metric, dict) and metric.get('value') is not None
               for metric in (finding.get('statistics') or {}).values()):
        raise ValueError('The draft has no numeric demographic metric to approve.')
    stored = tools.store_webpage_finding(finding, provenance={
        'submission_type': 'manual', 'discovery_source': 'api',
        'analysis_draft_id': draft_id,
        'extraction_prompt_version': finding.get('extraction_prompt_version'),
        'extraction_rule_version': finding.get('extraction_rule_version'),
    })
    if stored.get('status') != 'stored':
        outcome = str(stored.get('status') or 'not_stored')
        reason = str(stored.get('reason') or '').strip()
        research_store.update_analysis_draft(
            draft_id, action='storage_failed', note=outcome,
        )
        raise ValueError(
            f'Finding was not stored ({outcome})'
            + (f': {reason}' if reason else '')
            + '; the draft remains pending review.'
        )
    finding_id = stored.get('id') or stored.get('existing_id')
    updated = research_store.update_analysis_draft(
        draft_id, status='approved', finding_id=finding_id, action='approved',
        note=str(stored.get('status') or 'stored'),
    )
    return _public_draft(updated)


def reject_analysis_draft(draft_id: str, note: str | None = None) -> dict[str, Any]:
    draft = research_store.get_analysis_draft(draft_id)
    if draft is None:
        raise ValueError('Analysis draft not found.')
    if draft['status'] in {'rejected', 'removed', 'suppressed', 'existing_record', 'suppressed_source'}:
        return _public_draft(draft)
    if draft['status'] != 'pending_review':
        raise ValueError('Only a pending analysis draft can be rejected.')
    return _public_draft(research_store.update_analysis_draft(
        draft_id, status='rejected', action='rejected', note=note
    ))


def rerun_analysis_draft(draft_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
    draft = research_store.get_analysis_draft(draft_id)
    if draft is None:
        raise ValueError('Analysis draft not found.')
    finding = draft.get('finding') or {}
    url = finding.get('url')
    if not url:
        job = research_store.get_job(draft['job_id']) or {}
        url = (job.get('payload') or {}).get('url')
    if not url:
        raise ValueError('The analysis draft has no source URL to rerun.')
    return start_manual_analysis(url, country_iso3=finding.get('geography_iso3'),
                                 compare=True, idempotency_key=idempotency_key,
                                 allow_rerun=True)


def remove_analysis_draft(draft_id: str, *, suppress: bool = False) -> dict[str, Any]:
    draft = research_store.get_analysis_draft(draft_id)
    if draft is None:
        raise ValueError('Analysis draft not found.')
    terminal_status = 'suppressed' if suppress else 'removed'
    if draft['status'] == terminal_status:
        return {**_public_draft(draft), 'action': {'status': terminal_status}}
    if draft['status'] in {'existing_record', 'suppressed_source'}:
        raise ValueError('This source reference is not an approvable finding; rerun it explicitly instead.')
    if draft['status'] in {'rejected', 'removed', 'suppressed'}:
        return _public_draft(draft)
    if draft.get('finding_id'):
        result = (admin_services.delete_and_block(int(draft['finding_id'])) if suppress
                  else admin_services.delete_finding(int(draft['finding_id'])))
    else:
        finding = draft.get('finding') or {}
        if suppress and finding.get('url'):
            result = {'canonical_url': tools.block_source_url(finding['url']), 'status': 'suppressed'}
        else:
            result = {'status': 'removed'}
    updated = research_store.update_analysis_draft(
        draft_id, status=terminal_status,
        action='suppressed' if suppress else 'removed', note=result.get('status'),
    )
    return {**_public_draft(updated), 'action': result}


def start_research(settings: dict[str, Any]) -> dict[str, Any]:
    validated = SearchSettings.model_validate(settings)
    _validate_country_scopes(validated)
    if not any(category.enabled for category in validated.categories) and not validated.reddit_enabled:
        raise ValueError('Enable a search category or Reddit before starting research.')
    return _start_research(validated.model_dump())


def get_research_settings() -> dict[str, Any]:
    return research_store.load_settings(DEFAULT_SETTINGS)


def get_country_hunt_queue() -> list[dict[str, Any]]:
    return research_store.list_country_hunt_queue()


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
    research_store.upsert_country_hunt_queue([context])
    result = _start_research(
        settings.model_dump(), queue_iso3s=[context['iso3']], persist_settings=False,
    )
    result.update({'country_iso3': context['iso3'], 'country': context['label']})
    return result


def country_hunt_settings(context: dict[str, str], max_results: int = 12) -> SearchSettings:
    if not 1 <= int(max_results) <= 20:
        raise ValueError('max_results must be between 1 and 20.')
    return SearchSettings(
        categories=[{
            'name': f'Country hunt: {context["label"]}',
            'query': HUNT_QUERY.format(country=_country_hunt_query_name(context['label'])),
            'topic': 'news', 'max_results': int(max_results),
            'time_range': 'year', 'search_depth': 'advanced',
            'country_iso3': context['iso3'],
        }],
        reddit_enabled=False, max_candidates=int(max_results), max_per_domain=5,
        domain_limit_scope='category', review_criteria=CRITERIA,
        country_hunt_mode='direct', country_hunt_iso3s=[context['iso3']],
    )


def start_bulk_country_hunt(country_iso3s: list[str], *, max_results: int = 5) -> dict[str, Any]:
    if not country_iso3s or len(country_iso3s) > 100:
        raise ValueError('country_iso3s must contain between 1 and 100 countries.')
    contexts = [_country_context(value) for value in country_iso3s]
    if not 1 <= int(max_results) <= 20:
        raise ValueError('max_results must be between 1 and 20.')
    categories = [{
        'name': f'Country hunt: {context["label"]}',
        'query': HUNT_QUERY.format(country=_country_hunt_query_name(context['label'])),
        'topic': 'news', 'max_results': int(max_results),
        'time_range': 'year', 'search_depth': 'advanced',
        'country_iso3': context['iso3'],
    } for context in contexts]
    settings = SearchSettings(
        categories=categories, reddit_enabled=False,
        max_candidates=int(max_results) * len(contexts), max_per_domain=5,
        domain_limit_scope='category', review_criteria=CRITERIA,
        country_hunt_mode='bulk', country_hunt_iso3s=[context['iso3'] for context in contexts],
    )
    research_store.upsert_country_hunt_queue(contexts)
    result = _start_research(
        settings.model_dump(), queue_iso3s=[context['iso3'] for context in contexts],
        persist_settings=False,
    )
    result.update({'country_iso3s': [context['iso3'] for context in contexts]})
    return result


def get_research_run(run_id: str) -> dict[str, Any]:
    run = research_store.get_run(str(run_id))
    if not run:
        raise ValueError('Research run not found.')
    try:
        settings = json.loads(run.get('settings_json') or '{}')
    except (TypeError, ValueError):
        settings = {}
    try:
        events = json.loads(run.get('events_json') or '[]')
    except (TypeError, ValueError):
        events = []
    return {
        'id': run['id'], 'started_at': run['started_at'],
        'finished_at': run.get('finished_at'), 'status': run['status'],
        'settings': settings, 'events': events,
        'candidates': research_store.list_candidates(str(run_id)),
    }


def stop_research(run_id: str) -> dict[str, Any]:
    run_id = str(run_id)
    with _research_lock:
        task = _research_workers.get(run_id)
    run = research_store.get_run(run_id)
    if not run:
        raise ValueError('Research run not found.')
    status = run['status']
    if status in {'running', 'stopping'}:
        status = research_store.request_run_stop(run_id)
        if task is not None and task[0].is_alive():
            task[1].set()
    # A completed/failed/interrupted run is terminal and must not be reported
    # as newly stopped when the same request is retried.
    return {'run_id': run_id, 'status': status}


def _start_research(
    settings: dict[str, Any], queue_iso3s: list[str] | None = None, *,
    persist_settings: bool = True,
) -> dict[str, Any]:
    ready = threading.Event()
    holder: dict[str, Any] = {}
    stop_event = threading.Event()
    worker_job = research_store.create_job('news_search', {'settings': settings})
    if queue_iso3s:
        research_store.attach_country_hunt_job(queue_iso3s, worker_job['id'])
    lock_id = worker_job['id']
    worker_owner = research_store.owner_id()
    try:
        research_store.acquire_worker_lock('discovery', lock_id, worker_owner)
        research_store.claim_job(lock_id, worker_owner)
    except Exception:
        if queue_iso3s:
            research_store.finish_country_hunt_queue_for_job(
                lock_id, outcome='error', next_eligible_at=(_now_datetime() + timedelta(days=31)).isoformat()
            )
        raise

    def worker() -> None:
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=_heartbeat_loop,
            args=(lock_id, worker_owner, heartbeat_stop),
            name=f'discovery-heartbeat-{lock_id}', daemon=True,
        )
        heartbeat.start()
        try:
            iterator = BossAgent().run(
                settings, stop_event=stop_event, owner_id=worker_owner,
                persist_settings=persist_settings,
            )
            first = next(iterator)
            holder['run_id'] = first[0]
            if queue_iso3s:
                research_store.mark_country_hunt_run(first[0], queue_iso3s)
            research_store.update_job(lock_id, progress={'run_id': first[0], 'message': first[1]})
            ready.set()
            for _run_id, message in iterator:
                research_store.log_event(_run_id, {'event': 'progress', 'message': message})
                research_store.update_job(lock_id, progress={'run_id': _run_id, 'message': message})
            completed_run = research_store.get_run(holder['run_id']) or {}
            job_status = completed_run.get('status') if completed_run.get('status') in {
                'complete', 'completed_with_errors', 'interrupted'
            } else 'complete'
            if queue_iso3s:
                candidates = research_store.list_candidates(holder['run_id'])
                successes = {
                    str(candidate.get('extracted_iso3') or '').upper()
                    for candidate in candidates
                    if candidate.get('status') == 'complete' and candidate.get('extracted_iso3')
                }
                outcome_by_iso3 = {}
                for iso3 in queue_iso3s:
                    scoped = [candidate for candidate in candidates
                              if str(candidate.get('country_iso3') or '').upper() == iso3]
                    if iso3 in successes:
                        outcome_by_iso3[iso3] = 'finding_ready'
                    elif any(candidate.get('status') == 'error' for candidate in scoped):
                        outcome_by_iso3[iso3] = 'error'
                    elif scoped:
                        outcome_by_iso3[iso3] = scoped[0].get('status') or job_status
                    else:
                        outcome_by_iso3[iso3] = 'no_candidate'
                research_store.finish_country_hunt_queue(
                    holder['run_id'], outcomes_by_iso3=outcome_by_iso3,
                    successful_iso3s=successes,
                    next_eligible_at=(_now_datetime() + timedelta(days=31)).isoformat(),
                )
            research_store.update_job(lock_id, status=job_status, result={'run_id': holder['run_id']})
        except StopIteration:
            ready.set()
            if queue_iso3s:
                research_store.finish_country_hunt_queue_for_job(
                    lock_id, outcome='error', next_eligible_at=(_now_datetime() + timedelta(days=31)).isoformat()
                )
            research_store.update_job(lock_id, status='complete', result={})
        except Exception as exc:
            holder['error'] = str(exc)
            ready.set()
            if queue_iso3s:
                if holder.get('run_id'):
                    research_store.finish_country_hunt_queue(
                        holder['run_id'], outcome='interrupted' if 'stop' in str(exc).lower() else 'error',
                        next_eligible_at=(_now_datetime() + timedelta(days=31)).isoformat(),
                    )
                else:
                    research_store.finish_country_hunt_queue_for_job(
                        lock_id, outcome='error', next_eligible_at=(_now_datetime() + timedelta(days=31)).isoformat()
                    )
            research_store.update_job(lock_id, status='failed', error=str(exc))
        finally:
            heartbeat_stop.set()
            research_store.release_worker_lock('discovery', lock_id, worker_owner)
            research_store.update_job(lock_id, owner_id=None)
            run_id = holder.get('run_id')
            if run_id:
                with _research_lock:
                    _research_workers.pop(run_id, None)

    thread = threading.Thread(target=worker, name='demographics-research-api', daemon=True)
    thread.start()
    if not ready.wait(timeout=10):
        # The thread may be about to acquire its run ID.  Releasing the lock
        # here would allow a second discovery run to overlap it.
        stop_event.set()
        raise RuntimeError(f'Research job {worker_job["id"]} did not start within ten seconds.')
    if holder.get('error'):
        raise ValueError(holder['error'])
    run_id = holder.get('run_id')
    if not run_id:
        raise RuntimeError('Research job did not return a run identifier.')
    with _research_lock:
        _research_workers[run_id] = (thread, stop_event, worker_owner, lock_id)
    return {'run_id': run_id, 'job_id': worker_job['id'], 'status': 'running'}


def _run_manual(job_id: str, url: str, context: dict[str, str] | None, compare: bool,
                worker_owner: str | None = None, stop_event: threading.Event | None = None,
                allow_rerun: bool = False, direct_service: bool = False,
                review_before_store: bool = False) -> None:
    if not direct_service:
        return _run_manual_legacy(job_id, url, context, compare, worker_owner, stop_event)
    worker_owner = worker_owner or research_store.owner_id()
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(job_id, worker_owner, heartbeat_stop),
        name=f'analysis-heartbeat-{job_id}', daemon=True,
    )
    heartbeat.start()
    _set_job(job_id, status='running', stage='fetching')
    progress_token = tools.set_progress_callback(
        lambda event: _record_job_progress(job_id, event)
    )
    try:
        provenance = {
            'submission_type': 'manual', 'discovery_source': 'api',
            **({'country_iso3': context['iso3']} if context else {}),
        }
        canonical_url = tools.canonicalise_source_url(url)
        draft_status = 'pending_review' if review_before_store else 'approved'
        reference_finding_id = None
        if not allow_rerun and canonical_url in tools.blocked_source_urls():
            draft_status = 'suppressed_source'
            extracted = {'result': {
                'title': 'Suppressed source', 'url': url, 'source': '', 'site_seen': '',
                'statistics': {}, 'comments': 'This source is suppressed.',
            }, 'comparison': None, 'un_data': [],
                'storage': {'status': 'excluded_blocked_source', 'canonical_url': canonical_url},
                'validation': {}}
        elif not allow_rerun:
            existing = tools.find_webpage_finding_by_url(url)
            if existing:
                draft_status = 'existing_record'
                reference_finding_id = existing['id']
                extracted = {'result': existing.get('finding') or {
                    'title': f"Existing finding #{existing['id']}", 'url': url,
                    'source': '', 'site_seen': '', 'statistics': {},
                }, 'comparison': None, 'un_data': [],
                    'storage': {'status': 'excluded_duplicate_url', 'existing_id': existing['id'],
                                'canonical_url': canonical_url}, 'validation': {}}
            else:
                _set_job(job_id, stage='fetching')
                page_text = (tools.get_page_text.invoke({'url': url})
                             if hasattr(tools.get_page_text, 'invoke')
                             else tools.get_page_text(url))
                _set_job(job_id, stage='extracting')
                extracted = agents.extract_from_page_text(
                    page_text, url, provenance=provenance, country_context=context
                )
        else:
            _set_job(job_id, stage='fetching')
            page_text = (tools.get_page_text.invoke({'url': url})
                         if hasattr(tools.get_page_text, 'invoke')
                         else tools.get_page_text(url))
            _set_job(job_id, stage='extracting')
            extracted = agents.extract_from_page_text(
                page_text, url, provenance=provenance, country_context=context
            )
        result = dict(extracted)
        extracted_finding = result.get('result')
        if isinstance(extracted_finding, dict):
            geography_value = (extracted_finding.get('geography_iso3')
                               or extracted_finding.get('geography'))
        else:
            geography_value = (getattr(extracted_finding, 'geography_iso3', None)
                               or getattr(extracted_finding, 'geography', None))
        derived_iso3 = (tools.resolve_country_iso3(str(geography_value).strip())
                        if geography_value else None)
        if derived_iso3:
            derived_iso3 = derived_iso3.strip().upper()
            derived_country = tools.normalise_country_name(derived_iso3)
            _set_job(job_id, country_iso3=derived_iso3, country=derived_country)
        if compare and hasattr(extracted.get('result'), 'model_dump_json') and extracted.get('storage', {}).get('status') not in {
                'excluded_duplicate_url', 'excluded_blocked_source', 'excluded_no_data',
                'excluded_country_mismatch'}:
            _set_job(job_id, stage='comparing')
            compared = agents.compare_to_un({'result': extracted['result'],
                                             'storage': extracted.get('storage') or {}})
            result.update(compared)
        else:
            result.update({'comparison': None, 'un_data': []})
        finding_id = None
        if not review_before_store and isinstance(result.get('result'), dict):
            stored = tools.store_webpage_finding(result['result'], provenance={
                'submission_type': 'manual', 'discovery_source': 'api',
            })
            result['storage'] = stored
            finding_id = stored.get('id') or stored.get('existing_id')
            if stored.get('status') != 'stored':
                draft_status = 'pending_review'
        draft = research_store.create_analysis_draft(
            job_id, finding=_jsonable(result.get('result') or {}),
            comparison=_jsonable(result.get('comparison')),
            un_data=_jsonable(result.get('un_data') or []),
            validation=_jsonable(result.get('validation') or {}),
            status=draft_status, reference_finding_id=reference_finding_id,
        )
        if finding_id:
            draft = research_store.update_analysis_draft(
                draft['id'], finding_id=finding_id, action='stored_by_default',
                note='Manual analysis stored automatically.'
            )
        payload = {
            'draft_id': draft['id'], 'draft_status': draft['status'],
            'finding': draft['finding'], 'comparison': draft['comparison'],
            'un_data': draft['un_data'], 'validation': draft['validation'],
            'storage': result.get('storage') or {},
        }
        _set_job(job_id, status='complete', stage='complete', result=_jsonable(payload))
    except Exception as exc:
        _set_job(job_id, status='failed', stage='failed', error=str(exc))
    finally:
        tools.reset_progress_callback(progress_token)
        heartbeat_stop.set()
        # Clearing ownership is best-effort; terminal state remains durable
        # even if the process exits during cleanup.
        research_store.update_job(job_id, owner_id=None)


def _run_manual_legacy(job_id: str, url: str, context: dict[str, str] | None, compare: bool,
                       worker_owner: str | None = None, stop_event: threading.Event | None = None) -> None:
    """Compatibility adapter for callers of the pre-3.5 private helper.

    The supported HTTP and worker entry points pass ``direct_service=True``;
    this branch keeps older local scripts and parity tests operational while
    the public workflow moves to drafts.
    """
    worker_owner = worker_owner or research_store.owner_id()
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat_loop,
                                 args=(job_id, worker_owner, heartbeat_stop), daemon=True)
    heartbeat.start()
    _set_job(job_id, status='running', stage='fetching')
    progress_token = tools.set_progress_callback(lambda event: _record_job_progress(job_id, event))
    try:
        state = {
            'messages': [HumanMessage(content=f'Analyse the article at {url}')],
            'article_url': url, 'page_text': '',
            'provenance': {'submission_type': 'manual', 'discovery_source': 'api',
                           **({'country_iso3': context['iso3']} if context else {})},
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
            result['comparison'] = None; result['un_data'] = []
        payload = {key: result.get(key) for key in ('result', 'comparison', 'un_data', 'storage')}
        _set_job(job_id, status='complete', stage='complete', result=_jsonable(payload))
    except Exception as exc:
        _set_job(job_id, status='failed', stage='failed', error=str(exc))
    finally:
        tools.reset_progress_callback(progress_token)
        heartbeat_stop.set()
        research_store.update_job(job_id, owner_id=None)


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


def _heartbeat_loop(job_id: str, worker_owner: str, stop_event: threading.Event) -> None:
    """Renew ownership while provider/model work is in progress."""
    while not stop_event.wait(research_store.WORKER_HEARTBEAT_SECONDS):
        if not research_store.heartbeat_worker(job_id, worker_owner):
            return


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
        job['progress'] = progress
    research_store.update_job(job_id, progress=progress)


def _durable_manual_public_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get('payload') or {}
    progress = job.get('progress') or {}
    result = job.get('result') or {}
    finding = (result.get('finding') or {}) if isinstance(result, dict) else {}
    geography_value = (payload.get('country_iso3') or finding.get('geography_iso3')
                       or finding.get('geography'))
    derived_iso3 = (tools.resolve_country_iso3(str(geography_value).strip())
                    if geography_value else None)
    draft = research_store.get_analysis_draft_for_job(job['id'])
    return {
        'id': job['id'], 'kind': job['kind'], 'status': job['status'],
        'url': payload.get('url'), 'country_iso3': derived_iso3,
        'country': tools.normalise_country_name(derived_iso3) if derived_iso3 else None,
        'compare': payload.get('compare', True), 'created_at': job['created_at'],
        'updated_at': job['updated_at'], 'stage': progress.get('stage'),
        'fetch_status': progress.get('fetch_status'), 'logs': progress.get('logs', []),
        'extraction_prompt_version': payload.get('extraction_prompt_version'),
        'extraction_rule_version': payload.get('extraction_rule_version'),
        'result': job.get('result'), 'error': job.get('error'),
        'draft': _public_draft(draft) if draft else None,
    }


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in job.items() if key not in {'owner_id'}}
    draft = research_store.get_analysis_draft_for_job(job['id'])
    if draft:
        result['draft'] = _public_draft(draft)
    return result


def _public_draft(draft: dict[str, Any] | None) -> dict[str, Any]:
    if not draft:
        return {}
    result = {
        'id': draft['id'], 'job_id': draft['job_id'], 'status': draft['status'],
        'finding': draft.get('finding') or {}, 'comparison': draft.get('comparison'),
        'un_data': draft.get('un_data') or [], 'validation': draft.get('validation') or {},
        'revision': draft['revision'], 'finding_id': draft.get('finding_id'),
        'reference_finding_id': draft.get('reference_finding_id'),
        'created_at': draft['created_at'], 'updated_at': draft['updated_at'],
    }
    return result


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


def _now_datetime() -> datetime:
    return datetime.now(timezone.utc)
