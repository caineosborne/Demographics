"""FastAPI boundary for the demographics application."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

import read_services
import admin_services
import research_services


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings needed to construct the application boundary."""

    app_name: str = "demographics-agent"
    api_version: str = "v1"
    environment: str = "development"

    @classmethod
    def from_environment(cls) -> "Settings":
        defaults = cls()
        return cls(
            app_name=os.getenv("DEMOGRAPHICS_APP_NAME", defaults.app_name),
            api_version=os.getenv("DEMOGRAPHICS_API_VERSION", defaults.api_version),
            environment=os.getenv("DEMOGRAPHICS_ENVIRONMENT", defaults.environment),
        )


@lru_cache
def get_settings() -> Settings:
    """Return process settings, cached so dependencies share one instance."""

    return Settings.from_environment()


class AuthContext(BaseModel):
    """Authentication hook result reserved for the private admin boundary."""

    model_config = ConfigDict(frozen=True)

    subject: str = "local"


AuthDependency = Callable[[], AuthContext]


def get_auth_context() -> AuthContext:
    """Local-development auth hook.

    This deliberately permits local access. Production authentication can be
    injected through ``create_app(auth_dependency=...)`` without changing
    route handlers.
    """

    return AuthContext()


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str


class CountryChoice(BaseModel):
    name: str
    iso3: str


class CountriesResponse(BaseModel):
    items: list[CountryChoice]


class JsonObjectResponse(BaseModel):
    """Typed boundary object that permits additive service fields.

    Internal SQLite columns are filtered by the service before this model is
    built; ``extra=allow`` keeps the API forward-compatible without returning
    a bare ``dict`` response model.
    """

    model_config = ConfigDict(extra="allow")


class FindingsResponse(BaseModel):
    items: list[JsonObjectResponse]


class GraphSeriesResponse(BaseModel):
    country: str
    iso3: str | None
    metrics: list[str]
    historic: list[JsonObjectResponse]
    forecast: list[JsonObjectResponse]
    alternate_releases: dict[str, list[JsonObjectResponse]]
    findings: list[JsonObjectResponse]


class ResearchHistoryResponse(BaseModel):
    items: list[JsonObjectResponse]


class MutationResponse(JsonObjectResponse):
    status: str | None = None
    finding_id: int | str | None = None
    run_id: str | None = None
    canonical_url: str | None = None
    metric: str | None = None


class ResearchSettingsPayload(BaseModel):
    categories: list[JsonObjectResponse] = Field(default_factory=list)
    reddit_enabled: bool | None = None
    reddit_limit: int | None = None
    max_candidates: int | None = None
    max_per_domain: int | None = None
    domain_limit_scope: str | None = None
    review_criteria: str | None = None
    model_config = ConfigDict(extra="allow")


class SettingsResponse(BaseModel):
    settings: ResearchSettingsPayload


class CountryGapPreviewResponse(JsonObjectResponse):
    prefix: str
    days: int
    total_matches: int
    total_gaps: int
    start_at: int
    country_count: int
    selected: list[JsonObjectResponse] = Field(default_factory=list)
    excluded: list[JsonObjectResponse] = Field(default_factory=list)
    remaining: int


class JobStartResponse(JsonObjectResponse):
    id: str | None = None
    job_id: str | None = None
    run_id: str | None = None
    status: str | None = None
    kind: str | None = None
    country_iso3: str | None = None
    country_iso3s: list[str] | None = None
    country: str | None = None
    compare: bool | None = None
    created_at: str | None = None
    updated_at: str | None = None
    stage: str | None = None
    result: JsonObjectResponse | None = None
    error: str | None = None


class RunDetailResponse(JsonObjectResponse):
    id: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    settings: ResearchSettingsPayload = Field(default_factory=ResearchSettingsPayload)
    events: list[JsonObjectResponse] = Field(default_factory=list)
    candidates: list[JsonObjectResponse] = Field(default_factory=list)


class CandidateDetailResponse(JsonObjectResponse):
    id: int
    run_id: str
    url: str | None = None
    source: str
    category: str
    status: str
    updated_at: str
    details: JsonObjectResponse = Field(default_factory=JsonObjectResponse)


class WorkerJobResponse(BaseModel):
    id: str
    kind: str
    status: str
    result: dict[str, Any] | None = None
    progress: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: str
    updated_at: str
    attempts: int


class FindingMutationRequest(BaseModel):
    finding: dict[str, Any]


class MetricDeleteRequest(BaseModel):
    metric: str


class SourceUnblockRequest(BaseModel):
    url: str


class SourceRuleRequest(BaseModel):
    match_type: str
    match_value: str
    action: str
    classification: str | None = None
    enabled: bool = True
    note: str | None = None


class FallbackProviderRequest(BaseModel):
    enabled: bool
    max_age_days: int
    only_when_country_blank_days: int
    allow_undated_seed: bool
    note: str | None = None


class ManualAnalysisRequest(BaseModel):
    url: str
    country_iso3: str | None = None
    compare: bool = True


class ResearchStartRequest(BaseModel):
    settings: dict[str, Any]


class CountryHuntRequest(BaseModel):
    country_iso3: str
    max_results: int = 12


class BulkCountryHuntRequest(BaseModel):
    country_iso3s: list[str]
    max_results: int = 5


class CountryGapPreviewRequest(BaseModel):
    prefix: str
    days: int = 31
    country_count: int = 5
    start_at: int = 1
    scope_iso3s: list[str] | None = None


def create_app(
    settings: Settings | None = None,
    *,
    auth_dependency: AuthDependency = get_auth_context,
) -> FastAPI:
    """Construct the FastAPI application.

    Keeping construction explicit makes tests independent of process
    environment and gives deployment code one place to wire dependencies.
    """

    runtime_settings = settings or get_settings()
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Recover local worker state left by an earlier terminated process."""
        research_services.research_store.recover_orphaned_worker_jobs()
        research_services.research_store.recover_orphaned_runs()
        yield

    app = FastAPI(
        title=runtime_settings.app_name,
        version=runtime_settings.api_version,
        lifespan=lifespan,
    )
    app.state.settings = runtime_settings

    frontend_dir = Path(__file__).parent / "frontend"
    admin_assets_dir = frontend_dir / "admin-assets"
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

    @app.get("/", include_in_schema=False)
    def frontend_index():
        """Serve the small local API testing surface."""
        return FileResponse(frontend_dir / "index.html")

    app.mount("/assets", StaticFiles(directory=frontend_dir), name="frontend-assets")
    app.mount("/admin-assets", StaticFiles(directory=admin_assets_dir), name="admin-assets")
    app.mount("/fixtures", StaticFiles(directory=Path(__file__).parent / "fixtures"), name="fixtures")

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_shell(request: Request):
        """Serve the API-backed admin shell.

        The template contains no application data. All data shown after the
        shell loads comes from versioned JSON routes (or local fixtures when
        the browser is opened with ``?fixtures=1``).
        """
        return templates.TemplateResponse(
            request=request,
            name="admin.html",
            context={
                "app_name": runtime_settings.app_name,
                "api_version": runtime_settings.api_version,
                "environment": runtime_settings.environment,
            },
        )

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health(
        current_auth: AuthContext = Depends(auth_dependency),
    ) -> HealthResponse:
        del current_auth  # The hook is intentionally enforced at the boundary.
        return HealthResponse(
            status="ok",
            service=runtime_settings.app_name,
            version=runtime_settings.api_version,
            environment=runtime_settings.environment,
        )

    @app.get("/api/v1/countries", response_model=CountriesResponse, tags=["read"])
    def countries(
        _auth: AuthContext = Depends(auth_dependency),
    ) -> CountriesResponse:
        return CountriesResponse(items=read_services.list_country_choices())

    @app.get("/api/v1/findings", response_model=FindingsResponse, tags=["read"])
    def findings(
        iso3: str | None = None,
        _auth: AuthContext = Depends(auth_dependency),
    ) -> FindingsResponse:
        return FindingsResponse(items=_read_call(read_services.list_findings, iso3))

    @app.get("/api/v1/graph-series/{iso3}", response_model=GraphSeriesResponse, tags=["read"])
    def graph_series(
        iso3: str,
        metrics: list[str] | None = Query(default=None),
        revisions: list[int] | None = Query(default=None),
        _auth: AuthContext = Depends(auth_dependency),
    ) -> GraphSeriesResponse:
        return GraphSeriesResponse(**_read_call(read_services.graph_series, iso3, metrics, revisions))

    @app.get("/api/v1/research/runs", response_model=ResearchHistoryResponse, tags=["read"])
    def research_runs(
        _auth: AuthContext = Depends(auth_dependency),
    ) -> ResearchHistoryResponse:
        return ResearchHistoryResponse(items=read_services.list_run_history())

    @app.get("/api/v1/research/candidates", response_model=ResearchHistoryResponse, tags=["read"])
    def research_candidates(
        run_id: str | None = None,
        _auth: AuthContext = Depends(auth_dependency),
    ) -> ResearchHistoryResponse:
        return ResearchHistoryResponse(items=read_services.list_candidate_history(run_id))

    @app.get("/api/v1/research/candidates/{candidate_id}", response_model=CandidateDetailResponse, tags=["read"])
    def research_candidate(candidate_id: int,
                           _auth: AuthContext = Depends(auth_dependency)) -> CandidateDetailResponse:
        return CandidateDetailResponse(**_admin_call(read_services.get_candidate, candidate_id))

    @app.get("/api/v1/admin/findings/{finding_id}", response_model_exclude_none=True, tags=["administration"])
    def admin_finding(finding_id: int, _auth: AuthContext = Depends(auth_dependency)) -> MutationResponse:
        return MutationResponse(**_admin_call(admin_services.get_finding, finding_id))

    @app.put("/api/v1/admin/findings/{finding_id}", response_model_exclude_none=True, tags=["administration"])
    def edit_finding(finding_id: int, request: FindingMutationRequest,
                     _auth: AuthContext = Depends(auth_dependency)) -> MutationResponse:
        return MutationResponse(**_admin_call(admin_services.update_finding, finding_id, request.finding))

    @app.delete("/api/v1/admin/findings/{finding_id}", response_model_exclude_none=True, tags=["administration"])
    def remove_finding(finding_id: int, _auth: AuthContext = Depends(auth_dependency)) -> MutationResponse:
        return MutationResponse(**_admin_call(admin_services.delete_finding, finding_id))

    @app.post("/api/v1/admin/findings/{finding_id}/delete-metric", response_model_exclude_none=True, tags=["administration"])
    def remove_metric(finding_id: int, request: MetricDeleteRequest,
                      _auth: AuthContext = Depends(auth_dependency)) -> MutationResponse:
        return MutationResponse(**_admin_call(admin_services.delete_metric, finding_id, request.metric))

    @app.post("/api/v1/admin/findings/{finding_id}/delete-and-block", response_model_exclude_none=True, tags=["administration"])
    def remove_and_block(finding_id: int, _auth: AuthContext = Depends(auth_dependency)) -> MutationResponse:
        return MutationResponse(**_admin_call(admin_services.delete_and_block, finding_id))

    @app.get("/api/v1/admin/blocked-sources", tags=["administration"])
    def blocked_sources(_auth: AuthContext = Depends(auth_dependency)) -> ResearchHistoryResponse:
        return ResearchHistoryResponse(items=admin_services.list_blocked_sources())

    @app.post("/api/v1/admin/blocked-sources/unblock", response_model_exclude_none=True, tags=["administration"])
    def unblock_source(request: SourceUnblockRequest,
                       _auth: AuthContext = Depends(auth_dependency)) -> MutationResponse:
        return MutationResponse(**_admin_call(admin_services.unblock_source, request.url))

    @app.get("/api/v1/admin/rechecks", tags=["administration"])
    def rechecks(_auth: AuthContext = Depends(auth_dependency)) -> ResearchHistoryResponse:
        return ResearchHistoryResponse(items=admin_services.list_automatic_rechecks())

    @app.get("/api/v1/admin/finding-actions", tags=["administration"])
    def finding_actions(
        finding_id: int | None = None,
        _auth: AuthContext = Depends(auth_dependency),
    ) -> ResearchHistoryResponse:
        return ResearchHistoryResponse(items=_admin_call(admin_services.list_finding_actions, finding_id))

    @app.get("/api/v1/admin/source-rules", tags=["administration"])
    def source_rules(_auth: AuthContext = Depends(auth_dependency)) -> ResearchHistoryResponse:
        return ResearchHistoryResponse(items=admin_services.list_source_rules())

    @app.post("/api/v1/admin/source-rules", response_model_exclude_none=True, tags=["administration"])
    def save_source_rule(request: SourceRuleRequest,
                         _auth: AuthContext = Depends(auth_dependency)) -> MutationResponse:
        return MutationResponse(**_admin_call(admin_services.upsert_source_rule, **request.model_dump()))

    @app.get("/api/v1/admin/fallback-providers", tags=["administration"])
    def fallback_providers(_auth: AuthContext = Depends(auth_dependency)) -> ResearchHistoryResponse:
        return ResearchHistoryResponse(items=admin_services.list_fallback_providers())

    @app.put("/api/v1/admin/fallback-providers/{domain}", response_model_exclude_none=True, tags=["administration"])
    def save_fallback_provider(domain: str, request: FallbackProviderRequest,
                               _auth: AuthContext = Depends(auth_dependency)) -> MutationResponse:
        return MutationResponse(**_admin_call(admin_services.update_fallback_provider, domain, **request.model_dump()))

    @app.post("/api/v1/analysis/jobs", status_code=202, response_model_exclude_none=True, tags=["analysis"])
    def start_analysis(request: ManualAnalysisRequest,
                       _auth: AuthContext = Depends(auth_dependency)) -> JobStartResponse:
        return JobStartResponse(**_admin_call(
            research_services.start_manual_analysis,
            request.url,
            country_iso3=request.country_iso3,
            compare=request.compare,
        ))

    @app.get("/api/v1/analysis/jobs/{job_id}", response_model_exclude_none=True, tags=["analysis"])
    def analysis_status(job_id: str, _auth: AuthContext = Depends(auth_dependency)) -> JobStartResponse:
        return JobStartResponse(**_admin_call(research_services.get_manual_analysis, job_id))

    @app.post("/api/v1/research/jobs", status_code=202, response_model_exclude_none=True, tags=["research"])
    def start_research(request: ResearchStartRequest,
                       _auth: AuthContext = Depends(auth_dependency)) -> JobStartResponse:
        return JobStartResponse(**_admin_call(research_services.start_research, request.settings))

    @app.get("/api/v1/research/settings", tags=["research"])
    def research_settings(_auth: AuthContext = Depends(auth_dependency)) -> SettingsResponse:
        return SettingsResponse(settings=research_services.get_research_settings())

    @app.get("/api/v1/worker/jobs/{job_id}", response_model=WorkerJobResponse, tags=["worker"])
    def worker_job(job_id: str, _auth: AuthContext = Depends(auth_dependency)) -> WorkerJobResponse:
        return WorkerJobResponse(**_admin_call(research_services.get_worker_job, job_id))

    @app.put("/api/v1/research/settings", response_model_exclude_none=True, tags=["research"])
    def save_research_settings(request: ResearchStartRequest,
                               _auth: AuthContext = Depends(auth_dependency)) -> SettingsResponse:
        return SettingsResponse(settings=_admin_call(research_services.save_research_settings, request.settings))

    @app.get("/api/v1/research/jobs/{run_id}", response_model_exclude_none=True, tags=["research"])
    def research_status(run_id: str, _auth: AuthContext = Depends(auth_dependency)) -> RunDetailResponse:
        return RunDetailResponse(**_admin_call(research_services.get_research_run, run_id))

    @app.post("/api/v1/research/jobs/{run_id}/stop", response_model_exclude_none=True, tags=["research"])
    def stop_research(run_id: str, _auth: AuthContext = Depends(auth_dependency)) -> MutationResponse:
        return MutationResponse(**_admin_call(research_services.stop_research, run_id))

    @app.post("/api/v1/research/country-hunts", status_code=202, response_model_exclude_none=True, tags=["research"])
    def country_hunt(request: CountryHuntRequest,
                    _auth: AuthContext = Depends(auth_dependency)) -> JobStartResponse:
        return JobStartResponse(**_admin_call(
            research_services.start_country_hunt,
            request.country_iso3,
            max_results=request.max_results,
        ))

    @app.post("/api/v1/research/bulk-country-hunts", status_code=202, response_model_exclude_none=True, tags=["research"])
    def bulk_country_hunt(request: BulkCountryHuntRequest,
                          _auth: AuthContext = Depends(auth_dependency)) -> JobStartResponse:
        return JobStartResponse(**_admin_call(
            research_services.start_bulk_country_hunt,
            request.country_iso3s,
            max_results=request.max_results,
        ))

    @app.post("/api/v1/research/country-gap-preview", response_model=CountryGapPreviewResponse, tags=["research"])
    def country_gap_preview(request: CountryGapPreviewRequest,
                            _auth: AuthContext = Depends(auth_dependency)) -> CountryGapPreviewResponse:
        return CountryGapPreviewResponse(**_read_call(
            read_services.country_gap_preview,
            request.prefix, request.days, request.country_count,
            request.start_at, request.scope_iso3s,
        ))

    @app.get("/api/v1/research/country-gap-preview", response_model=CountryGapPreviewResponse, tags=["research"])
    def country_gap_preview_get(
        prefix: str, days: int = 31, country_count: int = 5, start_at: int = 1,
        scope_iso3s: list[str] | None = Query(default=None),
        _auth: AuthContext = Depends(auth_dependency),
    ) -> CountryGapPreviewResponse:
        return CountryGapPreviewResponse(**_read_call(
            read_services.country_gap_preview,
            prefix, days, country_count, start_at, scope_iso3s,
        ))

    return app


def _admin_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except RuntimeError as exc:
        # Lock/lease conflicts are a valid operational response, not an
        # internal server error. The caller can safely retry after polling the
        # active job.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        message = str(exc)
        status = 404 if (
            message.startswith("No stored finding")
            or message.startswith("No fallback provider")
            or message in {
                "Analysis job not found.", "Research run not found.",
                "Worker job not found.", "Candidate not found.",
            }
        ) else 400
        raise HTTPException(status_code=status, detail=message) from exc


def _read_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


app = create_app()
