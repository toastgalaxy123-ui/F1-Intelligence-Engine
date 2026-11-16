"""Additive S8-B workspace and inspection contracts."""

from typing import Annotated, Any, Literal

from pydantic import UUID4, AwareDatetime, Field, model_validator

from f1ri.api.contracts import (
    AnalysisRequest,
    Context,
    Contract,
    Hash,
    PolicyId,
    PolicyOption,
    TyreSet,
)


class WorkspaceReceipt(Contract):
    analysis_id: Hash
    result_sha256: Hash


class WorkspaceFile(Contract):
    version: Literal["s8b-workspace-v1"]
    id: UUID4
    title: Annotated[str, Field(min_length=1, max_length=80)]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    context: Context
    snapshot_id: Hash
    model_sha256: Hash
    source_hashes: dict[Annotated[str, Field(min_length=1, max_length=200)], Hash] = Field(max_length=48)
    support: list[Annotated[str, Field(max_length=120)]] = Field(max_length=40)
    analysis: AnalysisRequest | None = None
    receipt: WorkspaceReceipt | None = None
    reference: PolicyId = "reference"

    @model_validator(mode="after")
    def coherent_receipt(self):
        if self.updated_at < self.created_at:
            raise ValueError("Workspace update predates creation.")
        if self.receipt is not None and self.analysis is None:
            raise ValueError("A replay receipt requires its original inputs.")
        return self


class WorkspaceVerification(Contract):
    status: Literal["INPUTS_VERIFIED"]
    snapshot_id: Hash
    analysis_id: Hash | None
    replay_available: bool


class PolicyCatalogRequest(Contract):
    context: Context
    snapshot_id: Hash
    max_stops: Literal[1, 2] = 1
    tyre_sets: tuple[TyreSet, ...] = Field(("hard", "medium", "soft", "used-soft"), max_length=4)


class PolicyCatalog(Contract):
    snapshot_id: Hash
    policies: list[PolicyOption]


class Checkpoint(Contract):
    driver: int
    checkpoint: int
    clock_us: int
    elapsed_us: int
    event: str
    route: str
    progress_m: float | None


class PolicyTrace(Contract):
    initial_tyres: dict[int, dict[str, Any] | None]
    checkpoints: list[Checkpoint]
    policy: dict[str, Any]
    run_status: str
    mounted_tyres: dict[int, dict[str, dict[str, Any]]]


class AnalysisDetails(Contract):
    version: Literal["s8b-traces-v1"] = "s8b-traces-v1"
    analysis_id: Hash
    result_sha256: Hash
    race_start_us: int
    branches: dict[str, dict[str, PolicyTrace]]


def _trace(run, start, markers):
    points = []
    initial = {}
    mounted = {}
    for row in run["trajectory"]:
        driver = row["driver"]
        if driver not in markers:
            continue
        if row["clock_us"] == start and driver not in initial:
            initial[driver] = row.get("tyres")
        tyres = row.get("tyres")
        if tyres and tyres.get("set_id") and tyres["set_id"] != "archived-continuation":
            mounted.setdefault(driver, {}).setdefault(tyres["set_id"], tyres)
        if (row["event_driver"] == driver and row["event"] in {"CHECKPOINT_CROSSING", "LAP_COMPLETED"}
                and row.get("checkpoint") is not None and row["checkpoint"] <= markers[driver]):
            points.append({"driver": driver, "checkpoint": row["checkpoint"], "clock_us": row["clock_us"],
                           "elapsed_us": row["clock_us"]-start, "event": row["event"], "route": row["route"],
                           "progress_m": row.get("progress_m")})
    return {"initial_tyres": initial, "checkpoints": points, "policy": run["policy"], "run_status": run["status"],
            "mounted_tyres": mounted}


def record_branch(branches, name, score, scene):
    markers = dict(scene.targets)
    branches[name] = {key: _trace(run, scene.initial.race_clock_us, markers)
                      for key, run in score["paired"]["results"].items()}
