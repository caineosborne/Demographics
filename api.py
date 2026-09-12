"""FastAPI boundary for the demographics application."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
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


class FindingsResponse(BaseModel):
    items: list[dict[str, Any]]


class GraphSeriesResponse(BaseModel):
    country: str
    iso3: str | None
    metrics: list[str]
    historic: list[dict[str, Any]]
    forecast: list[dict[str, Any]]
    alternate_releases: dict[str, list[dict[str, Any]]]
    findings: list[dict[str, Any]]


class ResearchHistoryResponse(BaseModel):
    items: list[dict[str, Any]]


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

    @app.get("/", include_in_schema=False)
    def frontend_index():
        """Serve the small local API testing surface."""
        return FileResponse(frontend_dir / "index.html")

    app.mount("/assets", StaticFiles(directory=frontend_dir), name="frontend-assets")

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

    @app.get("/api/v1/admin/findings/{finding_id}", tags=["administration"])
    def admin_finding(finding_id: int, _auth: AuthContext = Depends(auth_dependency)):
        return _admin_call(admin_services.get_finding, finding_id)

    @app.put("/api/v1/admin/findings/{finding_id}", tags=["administration"])
    def edit_finding(finding_id: int, request: FindingMutationRequest,
                     _auth: AuthContext = Depends(auth_dependency)):
        return _admin_call(admin_services.update_finding, finding_id, request.finding)

    @app.delete("/api/v1/admin/findings/{finding_id}", tags=["administration"])
    def remove_finding(finding_id: int, _auth: AuthContext = Depends(auth_dependency)):
        return _admin_call(admin_services.delete_finding, finding_id)

    @app.post("/api/v1/admin/findings/{finding_id}/delete-metric", tags=["administration"])
    def remove_metric(finding_id: int, request: MetricDeleteRequest,
                      _auth: AuthContext = Depends(auth_dependency)):
        return _admin_call(admin_services.delete_metric, finding_id, request.metric)

    @app.post("/api/v1/admin/findings/{finding_id}/delete-and-block", tags=["administration"])
    def remove_and_block(finding_id: int, _auth: AuthContext = Depends(auth_dependency)):
        return _admin_call(admin_services.delete_and_block, finding_id)

    @app.get("/api/v1/admin/blocked-sources", tags=["administration"])
    def blocked_sources(_auth: AuthContext = Depends(auth_dependency)):
        return {"items": admin_services.list_blocked_sources()}

    @app.post("/api/v1/admin/blocked-sources/unblock", tags=["administration"])
    def unblock_source(request: SourceUnblockRequest,
                       _auth: AuthContext = Depends(auth_dependency)):
        return _admin_call(admin_services.unblock_source, request.url)

    @app.get("/api/v1/admin/rechecks", tags=["administration"])
    def rechecks(_auth: AuthContext = Depends(auth_dependency)):
        return {"items": admin_services.list_automatic_rechecks()}

    @app.get("/api/v1/admin/finding-actions", tags=["administration"])
    def finding_actions(
        finding_id: int | None = None,
        _auth: AuthContext = Depends(auth_dependency),
    ):
        return _admin_call(admin_services.list_finding_actions, finding_id)

    @app.get("/api/v1/admin/source-rules", tags=["administration"])
    def source_rules(_auth: AuthContext = Depends(auth_dependency)):
        return {"items": admin_services.list_source_rules()}

    @app.post("/api/v1/admin/source-rules", tags=["administration"])
    def save_source_rule(request: SourceRuleRequest,
                         _auth: AuthContext = Depends(auth_dependency)):
        return _admin_call(admin_services.upsert_source_rule, **request.model_dump())

    @app.get("/api/v1/admin/fallback-providers", tags=["administration"])
    def fallback_providers(_auth: AuthContext = Depends(auth_dependency)):
        return {"items": admin_services.list_fallback_providers()}

    @app.put("/api/v1/admin/fallback-providers/{domain}", tags=["administration"])
    def save_fallback_provider(domain: str, request: FallbackProviderRequest,
                               _auth: AuthContext = Depends(auth_dependency)):
        return _admin_call(admin_services.update_fallback_provider, domain, **request.model_dump())

    @app.post("/api/v1/analysis/jobs", status_code=202, tags=["analysis"])
    def start_analysis(request: ManualAnalysisRequest,
                       _auth: AuthContext = Depends(auth_dependency)):
        return _admin_call(
            research_services.start_manual_analysis,
            request.url,
            country_iso3=request.country_iso3,
            compare=request.compare,
        )

    @app.get("/api/v1/analysis/jobs/{job_id}", tags=["analysis"])
    def analysis_status(job_id: str, _auth: AuthContext = Depends(auth_dependency)):
        return _admin_call(research_services.get_manual_analysis, job_id)

    @app.post("/api/v1/research/jobs", status_code=202, tags=["research"])
    def start_research(request: ResearchStartRequest,
                       _auth: AuthContext = Depends(auth_dependency)):
        return _admin_call(research_services.start_research, request.settings)

    @app.get("/api/v1/research/settings", tags=["research"])
    def research_settings(_auth: AuthContext = Depends(auth_dependency)):
        return {"settings": research_services.get_research_settings()}

    @app.get("/api/v1/worker/jobs/{job_id}", response_model=WorkerJobResponse, tags=["worker"])
    def worker_job(job_id: str, _auth: AuthContext = Depends(auth_dependency)) -> WorkerJobResponse:
        return WorkerJobResponse(**_admin_call(research_services.get_worker_job, job_id))

    @app.put("/api/v1/research/settings", tags=["research"])
    def save_research_settings(request: ResearchStartRequest,
                               _auth: AuthContext = Depends(auth_dependency)):
        return {"settings": _admin_call(research_services.save_research_settings, request.settings)}

    @app.get("/api/v1/research/jobs/{run_id}", tags=["research"])
    def research_status(run_id: str, _auth: AuthContext = Depends(auth_dependency)):
        return _admin_call(research_services.get_research_run, run_id)

    @app.post("/api/v1/research/jobs/{run_id}/stop", tags=["research"])
    def stop_research(run_id: str, _auth: AuthContext = Depends(auth_dependency)):
        return _admin_call(research_services.stop_research, run_id)

    @app.post("/api/v1/research/country-hunts", status_code=202, tags=["research"])
    def country_hunt(request: CountryHuntRequest,
                    _auth: AuthContext = Depends(auth_dependency)):
        return _admin_call(
            research_services.start_country_hunt,
            request.country_iso3,
            max_results=request.max_results,
        )

    @app.post("/api/v1/research/bulk-country-hunts", status_code=202, tags=["research"])
    def bulk_country_hunt(request: BulkCountryHuntRequest,
                          _auth: AuthContext = Depends(auth_dependency)):
        return _admin_call(
            research_services.start_bulk_country_hunt,
            request.country_iso3s,
            max_results=request.max_results,
        )

    return app


def _admin_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ValueError as exc:
        message = str(exc)
        status = 404 if (
            message.startswith("No stored finding")
            or message.startswith("No fallback provider")
            or message in {"Analysis job not found.", "Research run not found."}
        ) else 400
        raise HTTPException(status_code=status, detail=message) from exc


def _read_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


app = create_app()
