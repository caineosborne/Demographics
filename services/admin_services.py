"""Framework-independent finding administration services.

The service layer owns the mutation contract used by the Phase 2 API. It
delegates persistence and audit semantics to ``tools`` so the existing Gradio
client and the API have one source of truth.
"""

from __future__ import annotations

import json
from typing import Any

from data import tools


def get_finding(finding_id: int) -> dict[str, Any]:
    return tools.get_webpage_finding(_validated_id(finding_id))


def update_finding(finding_id: int, finding: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(finding, dict):
        raise ValueError("Finding must be a JSON object.")
    _validate_finding_geography(finding)
    tools.update_webpage_finding(_validated_id(finding_id), json.dumps(finding, ensure_ascii=False))
    return get_finding(finding_id)


def delete_finding(finding_id: int) -> dict[str, Any]:
    validated = _validated_id(finding_id)
    tools.delete_webpage_finding(validated)
    return {"finding_id": validated, "status": "deleted"}


def rerun_finding(finding_id: int) -> dict[str, Any]:
    """Remove a finding and explicitly make its canonical source eligible again."""
    validated = _validated_id(finding_id)
    tools.delete_webpage_finding(validated)
    recheck = next((item for item in tools.list_automatic_rechecks()
                    if item.get('requested_finding_id') == validated), None)
    return {"finding_id": validated, "status": "rerun_requested",
            "canonical_url": recheck.get('canonical_url') if recheck else None}


def delete_metric(finding_id: int, metric: str) -> dict[str, Any]:
    validated = _validated_id(finding_id)
    tools.delete_finding_metric(validated, metric)
    return {"finding_id": validated, "metric": metric, "status": "deleted"}


def delete_and_block(finding_id: int) -> dict[str, Any]:
    validated = _validated_id(finding_id)
    canonical_url = tools.delete_and_block_webpage_finding(validated)
    return {"finding_id": validated, "canonical_url": canonical_url, "status": "deleted_and_blocked"}


def unblock_source(url: str) -> dict[str, Any]:
    if not str(url or '').strip():
        raise ValueError("A source URL is required.")
    canonical_url = tools.unblock_source_url(url)
    return {"canonical_url": canonical_url, "status": "unblocked"}


def list_blocked_sources() -> list[dict[str, Any]]:
    return tools.list_blocked_sources()


def list_automatic_rechecks() -> list[dict[str, Any]]:
    return tools.list_automatic_rechecks()


def list_finding_actions(finding_id: int | None = None) -> list[dict[str, Any]]:
    if finding_id is not None:
        finding_id = _validated_id(finding_id)
    return tools.list_finding_actions(finding_id)


def list_source_rules() -> list[dict[str, Any]]:
    return tools.list_source_rules()


def list_source_rule_actions(rule_id: int | None = None) -> list[dict[str, Any]]:
    return tools.list_source_rule_actions(rule_id)


def upsert_source_rule(*, match_type: str, match_value: str, action: str,
                       classification: str | None = None, enabled: bool = True,
                       note: str | None = None) -> dict[str, Any]:
    rule_id = tools.add_source_rule(match_type, match_value, action, classification, enabled, note)
    result = next((rule for rule in tools.list_source_rules() if rule["id"] == rule_id), None)
    if result is None:
        raise ValueError(f"No source rule exists with ID {rule_id}.")
    return result


def disable_source_rule(rule_id: int) -> dict[str, Any]:
    return tools.disable_source_rule(_validated_id(rule_id))


def undo_source_rule(rule_id: int) -> dict[str, Any]:
    return tools.undo_source_rule(_validated_id(rule_id))


def list_fallback_providers() -> list[dict[str, Any]]:
    return tools.list_fallback_providers()


def update_fallback_provider(domain: str, **values: Any) -> dict[str, Any]:
    tools.update_fallback_provider(domain, **values)
    normalized = domain.strip().lower().removeprefix('www.')
    result = next((provider for provider in tools.list_fallback_providers()
                   if provider["domain"] == normalized), None)
    if result is None:
        raise ValueError(f"No fallback provider is configured for {normalized}.")
    return result


def _validated_id(finding_id: int) -> int:
    try:
        value = int(finding_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("finding_id must be a positive integer.") from exc
    if value <= 0:
        raise ValueError("finding_id must be a positive integer.")
    return value


def _validate_finding_geography(finding: dict[str, Any]) -> None:
    value = str(finding.get("geography_iso3") or "").strip().upper()
    if len(value) != 3 or not value.isalpha():
        raise ValueError("finding.geography_iso3 must be a three-letter ISO3 code.")
    resolved = tools.resolve_country_iso3(value)
    if not resolved or resolved.upper() != value:
        raise ValueError(f"Unknown finding geography ISO3: {value}.")
    geography = finding.get("geography")
    if geography:
        geography_iso3 = tools.resolve_country_iso3(str(geography))
        if not geography_iso3 or geography_iso3.upper() != value:
            raise ValueError("finding.geography must match finding.geography_iso3.")
    finding["geography_iso3"] = value
