"""Stable request, context and result envelopes for the local research API."""

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

VERSION = "v1"
MODEL_SHA = "f98e8444a5982dd482a29444ce2c06848a39829cc248cb6cfceb760b59eae16a"
PresetId = Literal["archive", "two-car", "neutralization", "timing", "shared-box", "unsupported", "shadow"]
Mode = Literal["HISTORICAL_CUTOFF_REPLAY", "DECLARED_COUNTERFACTUAL", "HINDSIGHT_ANALYSIS",
               "SHADOW_OBSERVATION", "LIVE"]
Operation = Literal["forecast", "manual", "conditional", "robust", "timing"]
Future = Literal["GREEN", "VSC", "SC", "SC_LATE"]
Objective = Literal["MINIMAX_REGRET", "WORST_CASE_RELATIVE_LOSS", "DECLARED_WEIGHTED_DELTA", "PARETO"]
RunState = Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELED", "UNSUPPORTED"]
TyreSet = Literal["hard", "medium", "soft", "used-soft"]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PolicyId = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class Assumptions(Contract):
    gap_m: float = Field(24.635, gt=0, le=690)
    pace_offset_s: float = Field(0.0, ge=-1, le=1)
    service_s: float = Field(3.0, ge=1, le=10)
    extra_pit_travel_s: float = Field(0.0, ge=0, le=10)
    vsc_at_s: int = Field(5, ge=1, le=240)
    vsc_duration_s: int = Field(75, ge=20, le=240)
    sc_at_s: int = Field(5, ge=1, le=240)
    sc_duration_s: int = Field(92, ge=20, le=240)


class Context(Contract):
    preset: PresetId = "archive"
    driver: Literal[63, 4] = 63
    horizon: Literal[3, 5, 10] = 5
    mode: Mode | None = None
    cutoff: AwareDatetime | None = None
    session: int | None = Field(None, gt=0, le=99999)
    model_sha256: Hash = MODEL_SHA
    model_era: Literal["2023_2025", "2026"] = "2023_2025"
    assumptions: Assumptions = Field(default_factory=Assumptions)


class AnalysisRequest(Contract):
    context: Context
    snapshot_id: Hash
    operation: Operation
    policies: tuple[PolicyId, ...] = Field((), max_length=5)
    tyre_sets: tuple[TyreSet, ...] = Field(("hard", "medium", "soft", "used-soft"), max_length=4)
    future: Future = "GREEN"
    futures: tuple[Future, ...] = Field(("GREEN", "VSC", "SC"), min_length=1, max_length=4)
    objective: Objective = "MINIMAX_REGRET"
    weights: tuple[Annotated[int, Field(ge=1, le=10)], ...] = Field((), max_length=4)
    budget: int = Field(32, ge=2, le=128)
    robust_budget: int = Field(100, ge=4, le=240)
    max_stops: Literal[1, 2] = 1
    team_objective: Literal["TEAM_COMBINED", "LEAD_DRIVER", "TEAM_WEIGHTED"] = "TEAM_COMBINED"

    @model_validator(mode="after")
    def check_options(self):
        if any(len(x) != len(set(x)) for x in (self.policies, self.futures, self.tyre_sets)):
            raise ValueError("Policies, futures and tire sets must be unique.")
        if self.operation != "manual" and self.policies:
            raise ValueError("Manual policy IDs apply only to a manual comparison.")
        if self.objective == "DECLARED_WEIGHTED_DELTA":
            if len(self.weights) != len(self.futures):
                raise ValueError("Enter one weight per declared future.")
        elif self.weights:
            raise ValueError("Weights require the declared weighted objective.")
        return self


class Preset(Contract):
    id: PresetId
    title: str
    summary: str
    mode: Mode
    operations: tuple[Operation, ...]


class PolicyOption(Contract):
    id: str
    kind: str
    stops: list[dict[str, Any]]


class SnapshotResponse(Contract):
    context: Context
    preset: Preset
    snapshot_id: Hash
    driver_options: list[dict[str, Any]]
    fields: dict[str, Any]
    legal_policies: list[PolicyOption]
    operations: tuple[Operation, ...]
    support: list[str]
    source_hashes: dict[str, str]
    evaluation_marker: list[list[int]] | None


class Recommendation(Contract):
    statuses: list[str]
    unique_action: str | None
    alternatives: list[str]
    near: list[str]


class Result(Contract):
    version: Literal["v1"] = VERSION
    analysis_id: Hash
    snapshot_id: Hash
    engine_snapshot_id: Hash
    request: AnalysisRequest
    model: dict[str, Any]
    scenario_family: str
    evidence_classification: str
    evaluation_marker: list[list[int]] | None
    executed_policies: list[str]
    completion: str
    warnings: list[str]
    recommendation: Recommendation
    source_hashes: dict[str, str]
    payload: dict[str, Any]
    result_sha256: Hash


class Job(Contract):
    id: Hash
    state: RunState
    cache_hit: bool = False
    code: str | None = None
    message: str | None = None
    measurements: dict[str, float] | None = None
    result: Result | None = None


class ReplayRequest(Contract):
    request: AnalysisRequest
    expected_sha256: Hash
