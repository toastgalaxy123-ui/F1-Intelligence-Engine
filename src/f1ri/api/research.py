"""Adapters for the frozen engine and its evidence catalog."""

from dataclasses import asdict, replace
from datetime import UTC
from functools import lru_cache
from json import loads

from f1ri import decision_board as board
from f1ri.api.contracts import MODEL_SHA, Context, Preset, Result, SnapshotResponse
from f1ri.api.workspace import record_branch
from f1ri.simulator.strategy_optimization import (
    REFERENCE,
    _enumerate,
    _known_scenario,
    _MeanAdapter,
    _score,
    _validate,
    optimize,
)
from f1ri.validation.historical_field_sources import checked_members
from f1ri.validation.pit_branch_sources import PINNED as FIELD_PINS

REPORTS = "artifacts/reports/"
PINS = {
    "stage7_s7_c0_evidence.zip": "b2b68f2d2791ea154cc8e89b31bb2915e9f4adbc3ad0d00774f7262ac873c46b",
    "stage7_s7_c0_rehearsal.zip": "d5d58a7991b5ba5444caf8a68304263361e2b92513b14e5e4ed169c2e943a44c",
    "stage7_s7_c0_metrics.json": "bc9c84883f6632425129cf083bc6e114073141eff76c298aaaf655449fafd3e8",
    "stage7_s7_c0_dashboard.json": "b44e5a01b548f021058890a2e408e3e1fc5748632facdb41396d7f053ad79b63",
    "stage7_s7_c0_clock.json": "74b62b4dfc9c981a6a875c6415b3bb3ca9bced90bf7492b142cf39266717d576",
}
OPERATIONS = ("forecast", "manual", "conditional", "robust")
PRESETS = tuple(Preset(id=key, title=title, summary=summary, mode=mode, operations=ops)
                for key, title, summary, mode, ops in (
    ("archive", "Singapore 2025", "Archived race cutoff · model-backed continuation pace", board.MODES[0], ("forecast",)),
    ("two-car", "Declared two-car field", "Compare legal stops under following and pit constraints", board.MODES[1], OPERATIONS),
    ("neutralization", "GREEN / VSC / SC", "Test policy sensitivity across declared race-control futures", board.MODES[1], OPERATIONS),
    ("timing", "Safety Car timing failure", "Inspect and reproduce the frozen sc-deploy-58 case", board.MODES[2], ("timing",)),
    ("shared-box", "Shared pit box", "Compare two-car pit coordination with a declared shared box", board.MODES[1], OPERATIONS),
    ("unsupported", "Historical full field", "Singapore timing context with unavailable physical reconstruction", board.MODES[0], ()),
    ("shadow", "Source and shadow evidence", "Read-only S7-C0 receipt, maturity and sealed-checkpoint diagnostics",
     "SHADOW_OBSERVATION", ()),
))
CATALOG = {p.id: p for p in PRESETS}


class ResearchError(ValueError):
    def __init__(self, code, message, status=422):
        super().__init__(message)
        self.code, self.status = code, status


@lru_cache(maxsize=2)
def assets(root):
    return board.load_assets(root)


@lru_cache(maxsize=2)
def evidence(root):
    for name, checksum in PINS.items():
        if board.digest((root / (REPORTS + name)).read_bytes()) != checksum:
            raise ValueError("Published shadow evidence changed.")
    for name, count in (("stage7_s7_c0_evidence.zip", 64), ("stage7_s7_c0_rehearsal.zip", 43)):
        if len(checked_members(root / (REPORTS + name), PINS[name])) != count:
            raise ValueError("Published shadow evidence member count differs.")
    dashboard = loads((root / (REPORTS + "stage7_s7_c0_dashboard.json")).read_bytes())
    metrics = loads((root / (REPORTS + "stage7_s7_c0_metrics.json")).read_bytes())
    return dashboard, metrics


@lru_cache(maxsize=2)
def historical_field(root):
    relative = next(path for path in FIELD_PINS if path.name == "stage7_s7_a_dashboard.json")
    raw = (root / relative).read_bytes()
    if board.digest(raw) != FIELD_PINS[relative]:
        raise ValueError("Published historical field evidence changed.")
    field = loads(raw)["snapshots"]["9896"]["Archived model cutoff"]["EVENT_TIME_CAUSAL"]
    keys = ("driver_number", "classification", "completed_laps", "progress_m", "route", "quality",
            "location_utc", "age_s", "reason", "provenance")
    return {key: field[key] for key in ("cutoff", "source_sha256", "supported_positions",
                                      "supported_predecessors", "source_availability", "strategy_support")} | {
        "drivers": [{key: row.get(key) for key in keys} for row in field["drivers"]],
        "artifact_sha256": FIELD_PINS[relative],
    }


def _settings(ctx):
    return board.Settings(preset="archive" if ctx.preset == "shadow" else ctx.preset,
                          mode=board.MODES[0] if ctx.mode == "SHADOW_OBSERVATION" else ctx.mode,
                          driver=ctx.driver, horizon=ctx.horizon,
                          cutoff_utc=ctx.cutoff.isoformat() if ctx.cutoff else "",
                          **ctx.assumptions.model_dump())


def _policies(request):
    candidates, _, _ = _enumerate(request)
    return [{"id": p.strategy_id, "kind": p.kind, "stops": [asdict(s) for s in p.stops]} for p in candidates]


def _driver_options(fixtures):
    return [{"id": number, "code": fixtures[1][number]["request"]["driver_id"],
             "team": fixtures[1][number]["request"]["team_id"]} for number in (63, 4)]


def _default_mode(context, preset):
    return context.model_copy(update={"mode": context.mode or preset.mode})


@lru_cache(maxsize=64)
def snapshot_bytes(root, context_json):
    return board.canonical(snapshot(root, Context.model_validate_json(context_json)).model_dump(mode="json"))


def snapshot(root, context):
    preset = CATALOG[context.preset]
    ctx = _default_mode(context, preset)
    bundle, hashes, fixtures = assets(root)
    if bundle.artifact_sha256 != MODEL_SHA:
        raise ValueError("Model release checksum differs.")
    if ctx.preset == "shadow" and ctx.mode != preset.mode:
        raise ResearchError("MODE_UNSUPPORTED", "Shadow evidence uses shadow observation mode.")
    if ctx.mode == "SHADOW_OBSERVATION" and ctx.preset != "shadow":
        raise ResearchError("MODE_UNSUPPORTED", "Select the source and shadow preset for observation evidence.")
    settings = _settings(ctx)
    state, request, unsupported = board.prepare(root, settings, (bundle, hashes, fixtures))
    if ctx.session is not None and ctx.session != state.session.value:
        unsupported.append("SESSION_NOT_IN_VERIFIED_CATALOG")
    if ctx.model_sha256 != MODEL_SHA:
        unsupported.append("MODEL_HASH_MISMATCH")
    if ctx.model_era != "2023_2025":
        unsupported.append("MODEL_ERA_UNSUPPORTED")
    if ctx.mode not in (preset.mode, "LIVE") and not (
        preset.mode == "DECLARED_COUNTERFACTUAL" and ctx.mode == "HINDSIGHT_ANALYSIS"
    ):
        unsupported.append("MODE_UNSUPPORTED")
    available = () if unsupported else preset.operations
    fields = asdict(state)
    support = list(dict.fromkeys([*unsupported, *state.support.value]))
    if ctx.preset in ("archive", "unsupported"):
        support += ["HISTORICAL_STRATEGY_UNSUPPORTED", "HISTORICAL_INVENTORY_UNAVAILABLE"]
        if ctx.mode == board.MODES[0] and not set(unsupported) & {
            "CUTOFF_NOT_IN_VERIFIED_CATALOG", "SESSION_NOT_IN_VERIFIED_CATALOG"
        }:
            fields["historical_field"] = historical_field(root)
            hashes = hashes | {REPORTS + "stage7_s7_a_dashboard.json": fields["historical_field"]["artifact_sha256"]}
    if ctx.preset == "shadow":
        shadow, _ = evidence(root)
        fields = {"session": fields["session"], "model": fields["model"], "shadow": shadow["readiness"]}
        support = ["SHADOW_OBSERVATION_ONLY", "LIVE_UNSUPPORTED"]
        hashes = hashes | {REPORTS + name: checksum for name, checksum in PINS.items()}
    if ctx.preset == "timing" and (ctx.driver != 63 or ctx.horizon != 5):
        available = ()
        support.append("FROZEN_TIMING_CONTEXT_REQUIRED")
    if "CUTOFF_NOT_IN_VERIFIED_CATALOG" in support or "SESSION_NOT_IN_VERIFIED_CATALOG" in support:
        fields = {"model": fields["model"]}
    if "LIVE_UNSUPPORTED" in unsupported:
        fields = {"model": fields["model"]}
    cutoff = ctx.cutoff or state.cutoff_utc.value
    ctx = ctx.model_copy(update={"session": ctx.session or state.session.value,
                                 "cutoff": cutoff.astimezone(UTC) if cutoff else None})
    authoritative_id = board.digest(board.canonical(("v1", board.VERSION, ctx.model_dump(mode="json"), hashes)))
    fields = loads(board.canonical(fields))
    physical = any(op in available for op in ("manual", "conditional", "robust"))
    return SnapshotResponse(context=ctx, preset=preset, snapshot_id=authoritative_id,
                            driver_options=_driver_options(fixtures), fields=fields,
                            legal_policies=_policies(request) if physical else [],
                            operations=available, support=support, source_hashes=hashes,
                            evaluation_marker=list(map(list, request.scenario.targets)) if physical else None)


def prepare_analysis(root, request):
    state = snapshot(root, request.context)
    request = request.model_copy(update={"context": state.context})
    if request.snapshot_id != state.snapshot_id:
        raise ResearchError("SNAPSHOT_MISMATCH", "Reload the selected research snapshot before running.", 409)
    identity = board.digest(board.canonical(("v1", request.model_dump(mode="json"), state.source_hashes)))
    if request.operation not in state.operations:
        raise ResearchError(next((code for code in state.support if code.endswith(
            ("UNSUPPORTED", "MISMATCH", "CATALOG", "REQUIRED"))), "OPERATION_UNSUPPORTED"),
            "This operation is unavailable with the selected evidence and model.", 409)
    settings = replace(_settings(state.context), sets=request.tyre_sets, conditional_future=request.future,
        futures=request.futures,
        objective=request.objective, weights=request.weights, budget=request.budget, robust_budget=request.robust_budget,
        max_stops=request.max_stops, manual=request.policies if request.operation == "manual" else (),
        team_objective=request.team_objective)
    if request.operation == "manual":
        _, model_request, _ = board.prepare(root, settings, assets(root))
        allowed = {entry["id"] for entry in _policies(model_request)}
        if set(request.policies) - allowed or REFERENCE in request.policies:
            raise ResearchError("ILLEGAL_POLICY", "Use the offered alternatives. The stay-out reference is included automatically.")
    return request, state, settings, identity


def policy_catalog(root, request):
    state = snapshot(root, request.context)
    if state.snapshot_id != request.snapshot_id:
        raise ResearchError("SNAPSHOT_MISMATCH", "Reload the selected snapshot.", 409)
    if "manual" not in state.operations:
        raise ResearchError("PHYSICAL_COMPARISON_UNAVAILABLE", "This preset cannot execute physical comparisons.", 409)
    settings = replace(_settings(state.context), sets=request.tyre_sets, max_stops=request.max_stops)
    _, model_request, _ = board.prepare(root, settings, assets(root))
    return {"snapshot_id": state.snapshot_id, "policies": _policies(model_request)}


def verify_workspace(root, workspace):
    if workspace.model_sha256 != MODEL_SHA or workspace.context.model_sha256 != MODEL_SHA:
        raise ResearchError("WORKSPACE_MODEL_MISMATCH", "The saved model differs from the pinned model.", 409)
    state = snapshot(root, workspace.context)
    if workspace.source_hashes != state.source_hashes:
        raise ResearchError("WORKSPACE_SOURCE_MISMATCH", "The saved source hashes differ from the pinned inputs.", 409)
    if workspace.snapshot_id != state.snapshot_id:
        raise ResearchError("WORKSPACE_SNAPSHOT_MISMATCH", "The saved snapshot cannot be reproduced.", 409)
    identity = None
    if workspace.analysis:
        if workspace.analysis.snapshot_id != workspace.snapshot_id:
            raise ResearchError("WORKSPACE_SNAPSHOT_MISMATCH", "The saved analysis uses a different snapshot.", 409)
        _, _, _, identity = prepare_analysis(root, workspace.analysis)
        if workspace.receipt and workspace.receipt.analysis_id != identity:
            raise ResearchError("WORKSPACE_ANALYSIS_MISMATCH", "The saved request does not match its result identity.", 409)
        if workspace.reference not in (REFERENCE, *workspace.analysis.policies):
            raise ResearchError("WORKSPACE_REFERENCE_MISMATCH", "The saved reference is outside the selected policies.", 409)
    return {"status": "INPUTS_VERIFIED", "analysis_id": identity, "snapshot_id": state.snapshot_id,
            "replay_available": workspace.receipt is not None}


def _score_summary(score):
    paired = score["paired"]
    pit = {name: {key: run[key] for key in ("pit", "pit_events", "decisions", "race_control")}
           for name, run in paired["results"].items()}
    return {key: score[key] for key in ("rows", "ranked", "best", "near")} | {"timelines": pit}


def _conditional(result):
    return {k: v for k, v in result.items() if k not in ("decision", "conditional", "candidates", "rejected")} | {
        key: _score_summary(result[key]) for key in ("decision", "conditional")}


def _robust(result):
    return {k: result[k] for k in ("family", "objective", "comparator", "status", "selected", "near",
                                   "analysis", "oracles", "policies", "search")} | {
        "cases": {key: {"rows": value["rows"]} for key, value in result["cases"].items()}}


def _comparison(settings, scenario):
    request = scenario.request
    return replace(request, information_mode="PERFECT_FORESIGHT") if settings.mode == board.MODES[2] else request


def _manual(comparison, request, bundle, branches):
    selected = (REFERENCE, *request.policies)
    policies = [p for p in _enumerate(comparison)[0] if p.strategy_id in selected]
    base = _known_scenario(comparison.scenario)
    adapter = _MeanAdapter(comparison, bundle)
    weights = _validate(comparison, bundle)
    known = _score(base, policies, adapter, weights)
    future = (_score(comparison.scenario, policies, adapter, weights)
              if base != comparison.scenario and request.context.mode != board.MODES[2] else known)
    if branches is not None:
        record_branch(branches, "decision", known, base)
        record_branch(branches, "future", future, comparison.scenario)
    return {"decision": _score_summary(known), "declared": _score_summary(future)}


@lru_cache(maxsize=2)
def timing(root):
    from f1ri.validation.commitment_study import _study, reproduce, verified_baseline

    frozen = board.frozen_timing(root)
    bundle, rankings, _, _ = verified_baseline(root)
    _, reproduced = reproduce(tuple(_study(root)), bundle, rankings)
    if reproduced["regret_us"] != frozen["case"]["sc-63-soft"]["regret_us"]:
        raise ValueError("The frozen SC timing comparator failed parity.")
    return frozen | {"reproduced_regret_us": reproduced["regret_us"], "marker_gap": reproduced["marker_gap"]}


def run_analysis(root, request, *, diagnostics=None):
    request, snapshot, settings, identity = prepare_analysis(root, request)
    inputs = assets(root)
    prepared = board.prepare(root, settings, inputs)
    branches = None
    if diagnostics is not None and request.operation in ("manual", "conditional"):
        diagnostics.update(race_start_us=prepared[1].scenario.initial.race_clock_us, branches={})
        branches = diagnostics["branches"]
    operation = request.operation
    warnings = list(snapshot.support)
    statuses, unique, alternatives, near = ["NO_UNIQUE_RECOMMENDATION"], None, [], []
    executed = []
    if operation == "forecast":
        engine_state, engine_request, _ = prepared
        payload = {"forecasts": board.forecast_rows(engine_request, settings, engine_state, inputs[0])}
        completion = "MEAN_ONLY_VALIDATED"
        warnings.append("G3_CALIBRATION_FAILED")
    elif operation == "timing":
        payload = timing(root)
        completion = "FROZEN_TIMING_REPRODUCED"
        executed = list(payload["case"])
        statuses = payload["support"]["statuses"]
    else:
        cases = board.scenarios(prepared[1], settings)
        comparison = _comparison(settings, cases[request.future])
        if operation == "manual":
            payload = _manual(comparison, request, inputs[0], branches)
            active = payload["decision"]
            executed = [row["candidate"] for row in active["rows"]]
            alternatives, near = active["best"], active["near"]
            completion = "MANUAL_POLICY_SET_EVALUATED"
            if len(alternatives) > 1:
                statuses.append("EXACT_SIMULATION_TIE")
                tied = [row for row in active["rows"] if row["candidate"] in alternatives]
                if len({row["latent_racing_us"] for row in tied}) > 1 and any(row["following_us"] for row in tied):
                    statuses.append("CONSTRAINT_DOMINATED_IDENTIFIABILITY")
            if near:
                statuses.append("NEAR_EQUAL_ALTERNATIVES")
        elif operation == "conditional":
            optimized = optimize(comparison, inputs[0])
            if branches is not None:
                record_branch(branches, "decision", optimized["decision"], comparison.scenario)
                record_branch(branches, "future", optimized["conditional"], comparison.scenario)
            payload = _conditional(optimized)
            executed = [row["candidate"] for row in payload["decision"]["rows"]]
            alternatives, near = payload["decision"]["best"], payload["decision"]["near"]
            completion = payload["completion"]
            if completion != "EXHAUSTED_LEGAL_SPACE":
                statuses.append("BOUNDED_SEARCH_INCOMPLETE")
            if len(alternatives) > 1:
                statuses.append("EXACT_SIMULATION_TIE")
            if near:
                statuses.append("NEAR_EQUAL_ALTERNATIVES")
        else:
            data = board.execute(root, settings, inputs, prepared=prepared)
            payload = _robust(data["strategy"]["robust"])
            completion = payload["status"]
            executed = list(dict.fromkeys(key for case in payload["cases"].values() for key in case["rows"]
                                          if key in payload["policies"]))
            rec = data["recommendation"]
            statuses, unique, alternatives, near = rec["statuses"], rec["unique_action"], rec["selected"], rec["near"]
            payload["sensitivity"] = rec["case_winners"]
        payload["scenario_scripts"] = {
            name: [asdict(event) for event in cases[name].request.scenario.controls]
            for name in (request.futures if operation == "robust" else (request.future,))
        }
    document = loads(board.canonical({
        "version": "v1", "analysis_id": identity, "snapshot_id": snapshot.snapshot_id,
        "engine_snapshot_id": prepared[0].snapshot_id, "request": request.model_dump(mode="json"),
        "model": snapshot.fields["model"]["value"], "scenario_family": ("sc-deploy-58" if operation == "timing"
            else "board-declared-futures" if operation == "robust" else request.context.preset),
        "evaluation_marker": list(map(list, prepared[1].scenario.targets))
            if operation in ("manual", "conditional", "robust") else None,
        "evidence_classification": request.context.mode, "executed_policies": executed, "completion": completion,
        "warnings": warnings, "recommendation": {"statuses": statuses, "unique_action": unique,
                                                "alternatives": alternatives, "near": near},
        "source_hashes": snapshot.source_hashes, "payload": payload,
    }))
    return Result(**document, result_sha256=board.digest(board.canonical(document)))
