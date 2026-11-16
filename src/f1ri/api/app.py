"""FastAPI entry point for localhost research. The engine data root is operator-configured."""

import os
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi import Path as PathParameter
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from f1ri.api.contracts import (
    AnalysisRequest,
    Context,
    Hash,
    Job,
    Preset,
    PresetId,
    ReplayRequest,
    SnapshotResponse,
)
from f1ri.api.jobs import MAX_PENDING, RSS_BYTES, WALL_SECONDS, Jobs
from f1ri.api.research import (
    CATALOG,
    PINS,
    PRESETS,
    ResearchError,
    assets,
    evidence,
    policy_catalog,
    prepare_analysis,
    snapshot_bytes,
    verify_workspace,
)
from f1ri.api.workspace import (
    AnalysisDetails,
    PolicyCatalog,
    PolicyCatalogRequest,
    WorkspaceFile,
    WorkspaceVerification,
)
from f1ri.decision_board import canonical

DEFAULT_ORIGINS = ["http://127.0.0.1:3000", "http://localhost:3000"]
MAX_BODY = 16 * 1024


def error(code, message, status):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class LocalRequests:
    def __init__(self, app, origins):
        self.app = app
        self.origins = origins

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "DELETE", "PUT"):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        if (origin := headers.get("origin")) and origin not in self.origins:
            return await error("ORIGIN_FORBIDDEN", "Use the local research workspace.", 403)(scope, receive, send)
        if scope["method"] == "DELETE":
            return await self.app(scope, receive, send)
        if headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            return await error("JSON_REQUIRED", "Submit an application/json request.", 415)(scope, receive, send)
        body = bytearray()
        while True:
            chunk = await receive()
            if chunk["type"] == "http.disconnect":
                return
            packet = chunk.get("body", b"")
            if len(body) + len(packet) > MAX_BODY:
                return await error("BODY_LIMIT", "The request body exceeds 16 KiB.", 413)(scope, receive, send)
            body.extend(packet)
            if not chunk.get("more_body"):
                break

        async def read():
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, read, send)


def create_app(root=None, *, queue_factory=Jobs, allowed_hosts=None, private_config=None):
    root = Path(root or os.environ.get("F1RI_REPO_ROOT") or Path(__file__).resolve().parents[3]).resolve()
    private = private_config
    store = None
    if private:
        from f1ri.private.auth import install, owner
        from f1ri.private.jobs import OwnedJobs
        from f1ri.private.routes import workspace_routes
        from f1ri.private.storage import Store, lease

        store = Store(private.database)

    @asynccontextmanager
    async def lifespan(app):
        with lease(private.database) if private else nullcontext():
            assets(root)
            evidence(root)
            if private:
                from logging import getLogger

                getLogger("f1ri.private").setLevel(private.log_level)
                store.migrate()
            app.state.jobs = OwnedJobs(root, store, private) if private else queue_factory(root)
            try:
                yield
            finally:
                app.state.jobs.close()

    app = FastAPI(title="F1 Race Intelligence Research API", version="1.0.0", lifespan=lifespan,
                  openapi_url=None if private else "/openapi.json")
    app.add_middleware(LocalRequests, origins=[private.origin] if private else DEFAULT_ORIGINS)
    if not private:
        app.add_middleware(CORSMiddleware, allow_origins=DEFAULT_ORIGINS,
                           allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type"])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=(
        [private.host] if private else allowed_hosts or ["127.0.0.1", "localhost", "[::1]"]))
    if private:
        install(app, private, store)
    api = APIRouter(prefix="/api/v1")

    @app.exception_handler(ResearchError)
    async def research_error(_, problem):
        return error(problem.code, str(problem), problem.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, problem):
        response = {"code": "INVALID_REQUEST", "message": "Correct the invalid research inputs.",
                    "fields": [".".join(map(str, issue["loc"])) for issue in problem.errors()]}
        return JSONResponse({"error": response}, status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(_, problem):
        return error("REQUEST_FAILED", "The requested API operation is unavailable.", problem.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(_, problem):
        return error("SERVICE_ERROR", "The local research service could not complete the request.", 500)

    def resolve(context):
        return SnapshotResponse.model_validate_json(snapshot_bytes(root, context.model_dump_json()))

    @api.get("/health")
    def health():
        return {"status": "ready", "api": "v1", "deployment": "AUTHENTICATED_RESEARCH" if private else "LOCAL_RESEARCH_ONLY",
                "live": "LIVE_UNSUPPORTED", "budgets": {"wall_s": WALL_SECONDS,
                    "rss_mib": RSS_BYTES // 1024**2, "pending": MAX_PENDING, "active": 1}}

    @api.get("/presets", response_model=list[Preset])
    def presets():
        return PRESETS

    @api.get("/presets/{preset}", response_model=Preset)
    def preset_details(preset: PresetId):
        return CATALOG[preset]

    @api.get("/presets/{preset}/snapshot", response_model=SnapshotResponse)
    def default_snapshot(preset: PresetId, driver: Annotated[int, Query(ge=1, le=99)] = 63,
                         horizon: Annotated[int, Query(ge=3, le=10)] = 5):
        if driver not in (63, 4) or horizon not in (3, 5, 10):
            raise ResearchError("INVALID_CONTEXT", "Select a catalog driver and a 3, 5 or 10 lap horizon.")
        return resolve(Context(preset=preset, driver=driver, horizon=horizon))

    @api.post("/snapshots", response_model=SnapshotResponse)
    def selected_snapshot(context: Context):
        return resolve(context)

    @api.post("/policy-catalog", response_model=PolicyCatalog)
    def available_policies(request: PolicyCatalogRequest):
        return policy_catalog(root, request)

    @api.post("/workspaces/verify", response_model=WorkspaceVerification)
    def verify_saved_workspace(workspace: WorkspaceFile):
        return verify_workspace(root, workspace)

    @api.get("/evidence")
    def validation():
        data, metrics = evidence(root)
        bundle, hashes, _ = assets(root)
        return {"model": {"version": bundle.model.model_version, "sha256": bundle.artifact_sha256,
                           "era": bundle.regulation_era, "release": "MEAN_ONLY_VALIDATED", "g3": "FAILED",
                           "transfer_2026": "MODEL_ERA_UNSUPPORTED"},
                "physical": {"baseline": "T1_DETERMINISTIC_NO_PASS", "historical_predecessors": None,
                             "historical_exact_pit_merge": None, "historical_tyre_inventory": None,
                             "empirical_sc_vsc_calibration": "LIMITED"},
                "live": "LIVE_UNSUPPORTED", "shadow_metrics": metrics,
                "source_hashes": hashes | PINS, "readiness": data["readiness"],
                "modes": ["HISTORICAL_CUTOFF_REPLAY", "DECLARED_COUNTERFACTUAL", "HINDSIGHT_ANALYSIS",
                          "SHADOW_OBSERVATION", "LIVE_UNSUPPORTED"]}

    @api.get("/evidence/shadow")
    def shadow_summary():
        data, _ = evidence(root)
        return {key: data[key] for key in ("mode", "capture_id", "session", "release", "feasibility", "clock",
                                           "baseline", "capture", "readiness", "later_evaluation")} | {
            "checkpoints": [{key: checkpoint[key] for key in ("clock", "source_event_cutoff", "earned_versions",
                                                              "seal_sha256", "withheld")}
                            for checkpoint in data["checkpoints"]]}

    @api.get("/evidence/shadow/{checkpoint}")
    def shadow_checkpoint(checkpoint: Annotated[int, PathParameter(ge=0, le=30)]):
        data, _ = evidence(root)
        if checkpoint >= len(data["checkpoints"]):
            raise ResearchError("CHECKPOINT_NOT_FOUND", "Select a sealed archive checkpoint.", 404)
        return data["checkpoints"][checkpoint]

    def submit(http, request, expected=None):
        try:
            normalized, _, _, identity = prepare_analysis(root, request)
        except ResearchError as problem:
            if problem.status != 409 or problem.code == "SNAPSHOT_MISMATCH":
                raise
            from f1ri.decision_board import digest

            return Job(id=digest(canonical(request.model_dump(mode="json"))), state="UNSUPPORTED",
                       code=problem.code, message=str(problem))
        service = http.app.state.jobs
        return service.submit_for(owner(http), identity, normalized, expected) if private else service.submit(
            identity, normalized, expected)

    @api.post("/analyses", response_model=Job, status_code=202)
    def analysis(request: AnalysisRequest, http: Request):
        return submit(http, request)

    @api.post("/replays", response_model=Job, status_code=202)
    def replay(request: ReplayRequest, http: Request):
        return submit(http, request.request, request.expected_sha256)

    @api.get("/analyses/{identity}", response_model=Job)
    def job(identity: Hash, http: Request):
        service = http.app.state.jobs
        return service.get_for(owner(http), identity) if private else service.get(identity)

    @api.get("/analyses/{identity}/details", response_model=AnalysisDetails)
    def analysis_details(identity: Hash, http: Request):
        service = http.app.state.jobs
        return service.details_for(owner(http), identity) if private else service.details(identity)

    @api.delete("/analyses/{identity}", response_model=Job)
    def cancel(identity: Hash, http: Request):
        service = http.app.state.jobs
        return service.cancel_for(owner(http), identity) if private else service.cancel(identity)

    @api.get("/analyses/{identity}/export")
    def export(identity: Hash, http: Request):
        record = job(identity, http)
        if private:
            store.limit(owner(http), "export", private.rate_exports)
        if not record.result:
            raise ResearchError("EXPORT_UNAVAILABLE", "A completed analysis is required for export.", 409)
        return Response(canonical(record.result.model_dump(mode="json")), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="f1ri-{identity[:12]}.json"'})

    app.include_router(api)
    if private:
        workspace_routes(app, root, store, private)
    return app


from f1ri.private.config import configured

app = create_app(private_config=configured())
