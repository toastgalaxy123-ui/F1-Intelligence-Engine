"""Cutoff-aware assembly and reproducible execution of the S6-C research board."""

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from io import BytesIO
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from f1ri.simulator.pace_adapter import forecast_new_stint, forecast_pace
from f1ri.simulator.pit_execution import PitStopRequest
from f1ri.simulator.pit_pace import replacement_pace_request
from f1ri.simulator.strategy import fingerprint
from f1ri.simulator.strategy_forecast import read_forecast_inputs
from f1ri.simulator.strategy_optimization import (
    REFERENCE,
    PaceSensitivity,
    _enumerate,
    _known_scenario,
    _MeanAdapter,
    _score,
    _validate,
    optimize,
)
from f1ri.simulator.strategy_robust import BEST_REALIZED, ScenarioFamily, evaluate_family
from f1ri.validation.commitment_study import COMPARATORS, verified_baseline
from f1ri.validation.optimization_scenarios import fixture_request, scenario_requests
from f1ri.validation.robust_scenarios import control_script, variant

VERSION = "s6-c-v1"
PRESETS = {
    "archive": "Singapore 2025 · archived pace cutoff",
    "two-car": "Declared two-car encounter",
    "neutralization": "Declared GREEN / VSC / SC",
    "timing": "Frozen SC deployment at +58 s",
    "shared-box": "Declared shared pit box",
    "unsupported": "Unsupported full-field reconstruction",
}
MODES = ("HISTORICAL_CUTOFF_REPLAY", "DECLARED_COUNTERFACTUAL", "HINDSIGHT_ANALYSIS", "LIVE")
SETS = ("hard", "medium", "soft", "used-soft")
FUTURES = ("GREEN", "VSC", "SC", "SC_LATE")
OBJECTIVES = ("MINIMAX_REGRET", "WORST_CASE_RELATIVE_LOSS", "DECLARED_WEIGHTED_DELTA", "PARETO")
PINNED = {
    "artifacts/reports/stage6_s6_b1_metrics.json":
        "47398442d5cc240f8a5b7191135ac5b47db36f40fd90d3c3c1d58f51358afd53",
    "artifacts/reports/stage6_s6_b1_timing_risk.json":
        "851448e5d4c683e0d166f565c124b97fc005c23c720d3ee7cf3b21cf56bd4ecc",
    "artifacts/reports/stage6_s6_b1_ledgers.zip":
        "9bc7b0106dd9dc949f68c80b655642af4ca82631c8a4cd3bd90d2511fe96d06f",
}


class Provenance(StrEnum):
    OBSERVED = "OBSERVED"
    DERIVED = "DERIVED_FROM_OBSERVED"
    DECLARED = "DECLARED"
    FORECAST = "MODEL_FORECAST"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class Field:
    value: Any
    provenance: Provenance
    source: str
    at_utc: datetime | None = None


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    cutoff_utc: Field
    session: Field
    circuit: Field
    regulation_era: Field
    race_clock_us: Field
    layout: Field
    rule_profile: Field
    drivers: tuple[dict[str, Field], ...]
    observed_track_state: Field
    simulated_track_state: Field
    weather: Field
    pit_evidence: Field
    box_occupancy: Field
    data_freshness: Field
    model: Field
    support: Field


@dataclass(frozen=True)
class Settings:
    preset: str = "archive"
    mode: str = MODES[0]
    driver: int = 63
    horizon: int = 5
    cutoff_utc: str = ""
    sets: tuple[str, ...] = SETS
    stop_options: int = 2
    max_stops: int = 1
    budget: int = 64
    robust_budget: int = 220
    objective: str = OBJECTIVES[0]
    team_objective: str = "TEAM_COMBINED"
    futures: tuple[str, ...] = FUTURES
    weights: tuple[int, ...] = ()
    conditional_future: str = "GREEN"
    gap_m: float = 24.635
    pace_offset_s: float = 0.0
    archived_opponent_pace: bool = False
    vsc_at_s: int = 5
    vsc_duration_s: int = 75
    sc_at_s: int = 5
    sc_duration_s: int = 92
    service_s: float = 3.0
    extra_pit_travel_s: float = 0.0
    manual: tuple[str, ...] | None = None
    cache: bool = True

    def __post_init__(self):
        for key in ("sets", "futures", "weights"):
            object.__setattr__(self, key, tuple(getattr(self, key)))
        if self.manual is not None:
            object.__setattr__(self, "manual", tuple(self.manual))
        if (self.preset not in PRESETS or self.mode not in MODES or self.horizon not in (3, 5, 10)
                or self.driver not in (63, 4) or self.objective not in OBJECTIVES
                or self.team_objective not in ("TEAM_COMBINED", "LEAD_DRIVER", "TEAM_WEIGHTED")
                or not set(self.sets) <= set(SETS) or len(set(self.sets)) != len(self.sets)
                or self.conditional_future not in FUTURES
                or not self.futures or not set(self.futures) <= set(FUTURES)
                or len(set(self.futures)) != len(self.futures)
                or not 1 <= self.stop_options <= 3 or self.max_stops not in (1, 2)
                or not 2 <= self.budget <= 128 or not 4 <= self.robust_budget <= 400
                or not 0 < self.gap_m <= 690 or not -1 <= self.pace_offset_s <= 1
                or not 1 <= self.vsc_at_s <= 240 or not 1 <= self.sc_at_s <= 240
                or not 20 <= self.vsc_duration_s <= 240 or not 20 <= self.sc_duration_s <= 240
                or not 1 <= self.service_s <= 10 or not 0 <= self.extra_pit_travel_s <= 10
                or self.manual is not None and (len(set(self.manual)) != len(self.manual) or len(self.manual) > 6)):
            raise ValueError("Settings are outside the supported research bounds.")
        if self.objective == "DECLARED_WEIGHTED_DELTA" and (
            len(self.weights) != len(self.futures) or any(not 1 <= w <= 10 for w in self.weights)
        ):
            raise ValueError("Enter one explicit positive weight per declared future.")


def canonical(value):
    return dumps(value, default=str, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return sha256(data).hexdigest()


def load_assets(root):
    if any(digest((root / path).read_bytes()) != checksum for path, checksum in PINNED.items()):
        raise ValueError("Frozen S6-B.1 evidence checksum mismatch.")
    model, _, hashes, _ = verified_baseline(root)
    return model, hashes | PINNED, scenario_requests(root)


def _missing(reason):
    return Field(None, Provenance.UNAVAILABLE, reason)


def _declared(value, source="Research scenario"):
    return Field(value, Provenance.DECLARED, source)


def prepare(root, settings, assets):
    """Validate the cutoff before constructing an executable payload."""
    bundle, hashes, fixtures = assets
    _, archive = fixtures
    fixture = (("clean-air" if settings.driver == 63 else "original-opponent")
               if settings.preset in ("archive", "unsupported") else
               "shared-box" if settings.preset == "shared-box" else "two-car")
    historical = settings.mode == MODES[0]
    archived = settings.preset in ("archive", "unsupported")
    features = archive[settings.driver]
    observed = features["request"]
    cutoff = datetime.fromisoformat(observed["as_of_utc"])
    unsupported = []
    if settings.mode == "LIVE":
        unsupported.append("LIVE_UNSUPPORTED")
    if settings.preset == "unsupported":
        unsupported.append("FULL_FIELD_PHYSICAL_STATE_UNAVAILABLE")
    if historical and not archived:
        unsupported.append("DECLARED_SCENARIO_REQUIRES_COUNTERFACTUAL_OR_HINDSIGHT")
    if settings.cutoff_utc:
        try:
            matches = datetime.fromisoformat(settings.cutoff_utc) == cutoff
        except ValueError:
            matches = False
        if not matches:
            unsupported.append("CUTOFF_NOT_IN_VERIFIED_CATALOG")
    cutoff_view = (_missing("The requested cutoff is outside the verified catalog.")
                   if "CUTOFF_NOT_IN_VERIFIED_CATALOG" in unsupported else
                   Field(cutoff, Provenance.OBSERVED, features["source_sha256"], cutoff))
    use_archive_pace = historical and archived or settings.archived_opponent_pace
    request = fixture_request(
        root, fixture, horizon=settings.horizon, sets=settings.sets,
        stop_options=settings.stop_options, max_stops=settings.max_stops,
        budget=settings.budget, fixtures=fixtures, driver=settings.driver, cache=settings.cache,
        objective=settings.team_objective if fixture == "shared-box" else None,
    )
    source = request.scenario
    if not archived:
        positions = tuple(replace(p, progress_m=source.physical.cars[0].progress_m+settings.gap_m)
                          if p.driver_number == 4 else p for p in source.physical.cars)
        resources = []
        for item in source.resources:
            route = item.route
            service_delta = settings.service_s-route.service_s
            route = replace(route, service_s=settings.service_s, timing_line_s=route.timing_line_s+service_delta,
                            lane_exit_s=route.lane_exit_s+service_delta+settings.extra_pit_travel_s,
                            merge_s=route.merge_s+service_delta+settings.extra_pit_travel_s,
                            out_sector_2_s=route.out_sector_2_s+service_delta+settings.extra_pit_travel_s,
                            out_line_s=route.out_line_s+service_delta+settings.extra_pit_travel_s)
            resources.append(replace(item, route=route))
        source = replace(source, physical=replace(source.physical, cars=positions), resources=tuple(resources))
    if use_archive_pace:
        request = replace(request, features=read_forecast_inputs([archive[c.driver_number] for c in source.initial.cars]))
    request = replace(request, scenario=source, sensitivity=PaceSensitivity(
        level_s=0.0 if historical else settings.pace_offset_s))
    if any(stamp > cutoff for f in request.features for _, stamp in f.evidence_as_of):
        raise ValueError("Forecast input was observed after the cutoff.")
    archived_only = archived or settings.mode == "LIVE"
    if archived_only:
        drivers = (settings.driver,)
    else:
        drivers = tuple(c.driver_number for c in source.initial.cars)
    cars = {c.driver_number: c for c in source.initial.cars}
    positions = {p.driver_number: p for p in source.physical.cars}
    resources = {r.driver: r for r in source.resources}
    driver_fields = []
    for driver in drivers:
        original = archive[driver]["request"]
        from_archive = archive[driver]["source_sha256"]
        position = positions[driver]
        driver_fields.append({
            "identity": Field({"number": driver, "code": original["driver_id"], "team": original["team_id"]},
                              Provenance.OBSERVED, from_archive, cutoff),
            "lap": Field(original["cutoff_lap_number"], Provenance.DERIVED, from_archive, cutoff),
            "fitted_tyre": Field({key: original[key] for key in (
                "compound_relative", "compound_absolute", "tyre_age_at_start", "stint_number")},
                Provenance.OBSERVED, from_archive, cutoff),
            "inventory": (_missing("Archived set allocation unavailable.") if archived_only else
                          _declared([asdict(t) for t in resources[driver].tyres if t.set_id in settings.sets])),
            "progress": (_missing("No verified physical reconstruction at this cutoff.") if archived_only else
                         _declared(asdict(position))),
            "classification": (_missing("No verified classification in this snapshot.") if archived_only else
                               _declared(position.classification)),
            "physical_predecessor": (_missing("No verified track-local predecessor.") if archived_only else
                                     _declared(next(p.driver_number for p in positions.values()
                                                    if p.driver_number != driver))),
            "predecessor_gap_m": (_missing("No verified track-local gap.") if archived_only else _declared(
                (next(p.progress_m for p in positions.values() if p.driver_number != driver)
                 - position.progress_m) % source.physical.length_m)),
            "route": _missing("Historical pit route unavailable.") if archived_only else _declared(
                asdict(resources[driver].route)),
            "pace_history": (Field(cutoff.isoformat(), Provenance.OBSERVED, from_archive, cutoff)
                             if driver == 63 or use_archive_pace or archived else
                             _declared("Archived predecessor history shifted by +4 s")),
            "simulated_team": (_missing("No historical pit-box reconstruction.") if archived_only else
                               _declared(cars[driver].team)),
        })
    weather = Field({k: observed[k] for k in ("track_temperature_c", "air_temperature_c")}, Provenance.OBSERVED,
                    features["source_sha256"], datetime.fromisoformat(features["evidence_as_of"]["weather"]))
    state = Snapshot(
        fingerprint((VERSION, settings, hashes)),
        cutoff_view,
        Field(observed["session_key"], Provenance.OBSERVED, features["source_sha256"], cutoff),
        Field(observed["circuit_short_name"], Provenance.OBSERVED, features["source_sha256"], cutoff),
        Field({"season": observed["season"], "era": observed["regulation_era"]},
              Provenance.DERIVED, features["source_sha256"], cutoff),
        Field(source.initial.race_clock_us, Provenance.DERIVED, features["source_sha256"], cutoff),
        _missing("Exact cutoff layout state unavailable.") if archived_only else _declared(
            {"version": source.physical.layout_version, "length_m": source.physical.length_m}),
        _missing("No physical evaluation in archived forecast mode.") if archived_only else
        _declared(source.profile.identifier),
        tuple(driver_fields),
        _missing("Race-control history at the cutoff is unverified in this snapshot."),
        _missing("No physical run.") if archived_only else _declared(source.initial.track_state),
        weather,
        _missing("Cutoff-known service and pit-lane evidence unavailable."),
        _missing("Historical service-box occupancy unavailable.") if archived_only else _declared("Clear"),
        _missing("The archive provides observation clocks. Receipt clocks and live freshness are unavailable."),
        Field({"version": bundle.model.model_version, "sha256": bundle.artifact_sha256,
               "training_cutoff_utc": bundle.trained_through_utc,
               "calibration_cutoff_utc": bundle.calibrated_through_utc, "era": bundle.regulation_era,
               "release": "MEAN_ONLY_VALIDATED", "calibrated_intervals": False, "g3": "FAILED"},
              Provenance.FORECAST, "Stage 3-R released artifact"),
        _declared(unsupported or (["ARCHIVED_MEAN_ONLY"] if archived_only else
                                  [source.field_support, "DETERMINISTIC_PHYSICS"])),
    )
    return state, request, unsupported


def scenarios(request, settings):
    cases = {"GREEN": variant(request, "GREEN", "Continue under declared GREEN")}
    clocks = {"VSC": (settings.vsc_at_s, settings.vsc_duration_s),
              "SC": (settings.sc_at_s, settings.sc_duration_s),
              "SC_LATE": (settings.sc_at_s+53, settings.sc_duration_s)}
    for name, (start, duration) in clocks.items():
        flag = name.split("_")[0]
        cases[name] = variant(request, name, f"{flag} +{start} s, green +{start+duration} s",
                              controls=control_script(request, state=flag, deployment=start,
                                                      release=start+duration))
    return cases


def forecast_rows(request, settings, snapshot, bundle):
    feature = next(f for f in request.features if f.request.driver_number == settings.driver)
    base = replace(feature.request, horizon_laps=settings.horizon, n_trajectory_samples=2,
                   retain_trajectory_samples=False)
    result = []

    def add(label, model_request, batch):
        result.append({
            "plan": label, "request": asdict(model_request), "provenance": Provenance.FORECAST,
            "feature_source_sha256": feature.source_sha256, "feature_as_of": feature.evidence_as_of,
            "model_sha256": bundle.artifact_sha256,
            "mean": [{"lap": row.target_lap_number, "age": row.projected_tyre_age, "stint": model_request.stint_number,
                      "compound": model_request.compound_relative, "seconds": row.expected_lap_time_s,
                      "cumulative_seconds": row.expected_cumulative_time_s, "support": row.support_status}
                     for row in batch.forecast.lap_forecasts],
        })

    add("Continue fitted tyre", base, forecast_pace(bundle, base))
    if snapshot.drivers[0]["inventory"].value is not None:
        resource = next(r for r in request.scenario.resources if r.driver == settings.driver)
        for tyre in resource.tyres:
            if tyre.set_id not in settings.sets:
                continue
            for delay in (0, 1):
                stop = PitStopRequest(base.session_key, base.driver_number, base.as_of_utc,
                                      base.planned_start_lap+delay, tyre, VERSION)
                candidate = replacement_pace_request(base, stop, horizon=settings.horizon-delay-1,
                                                     seed=42, samples=2)
                add(f"{tyre.set_id} · pit lap {stop.pit_in_lap}", candidate, forecast_new_stint(bundle, candidate))
    return result


def legal_manual(request):
    policies, _, _ = _enumerate(request)
    return [p.strategy_id for p in policies if p.strategy_id != REFERENCE]


def _support(conditional, robust, settings):
    active = robust["selected"]
    near = robust["near"]
    status = []
    case_winners = {}
    for name, case in robust["cases"].items():
        rows = {k: r["objective_us"] for k, r in case["rows"].items() if k in robust["policies"]
                and r["objective_us"] is not None}
        case_winners[name] = sorted(k for k in rows if rows[k] == min(rows.values()))
    if active:
        status.append("ROBUST_WITHIN_DECLARED_FAMILY")
    if len({tuple(winners) for winners in case_winners.values()}) > 1:
        status.append("FAMILY_SENSITIVE")
    if near:
        status.append("NEAR_EQUAL_ALTERNATIVES")
    if any(len(winners) > 1 and len({case["rows"][k]["latent_racing_us"] for k in winners}) > 1
           and any(case["rows"][k]["following_us"] > 0 for k in winners)
           for name, winners in case_winners.items() for case in (robust["cases"][name],)):
        status.append("CONSTRAINT_DOMINATED_IDENTIFIABILITY")
    incomplete = (conditional["completion"] != "EXHAUSTED_LEGAL_SPACE" or
                  robust["status"] != "ROBUST_SEARCH_COMPLETE")
    unique = active[0] if len(active) == 1 and not near and not incomplete and settings.mode != MODES[2] else None
    if unique is None:
        status.append("NO_UNIQUE_RECOMMENDATION")
    return {"statuses": status, "unique_action": unique, "selected": active, "near": near,
            "case_winners": case_winners, "incomplete": incomplete,
            "futures_evaluated": list(robust["cases"]),
            "preset_futures_not_evaluated": sorted(set(FUTURES)-set(robust["cases"])),
            "comparator": robust["comparator"],
            "policy_library_sha256": fingerprint(robust["policies"]),
            "binding_constraints": {name: {key: {term: row[term] for term in (
                "following_us", "neutralization_us", "restart_us", "service_wait_us")}
                for key in active if (row := case["rows"].get(key)) and row["objective_us"] is not None}
                for name, case in robust["cases"].items()}}


def execute(root, settings, assets=None, *, prepared=None):
    assets = assets or load_assets(root)
    bundle, hashes, _ = assets
    snapshot, request, unsupported = prepared or prepare(root, settings, assets)
    out = {"version": VERSION, "settings": asdict(settings), "snapshot": asdict(snapshot),
           "source_hashes": hashes, "unsupported": unsupported, "forecasts": [], "strategy": None,
           "recommendation": {"statuses": ["NO_UNIQUE_RECOMMENDATION"], "unique_action": None}}
    if unsupported:
        return out
    if settings.preset == "timing":
        out["timing"] = frozen_timing(root)
        out["recommendation"] = out["timing"]["support"]
        return out
    out["forecasts"] = forecast_rows(request, settings, snapshot, bundle)
    if settings.preset == "archive":
        return out
    cases = scenarios(request, settings)
    comparison = cases[settings.conditional_future].request
    if settings.mode == MODES[2]:
        comparison = replace(comparison, information_mode="PERFECT_FORESIGHT")
    conditional = optimize(comparison, bundle)
    base = _known_scenario(comparison.scenario)
    legal, _, _ = _enumerate(comparison)
    offered = {p.strategy_id for p in legal}
    alternatives = legal_manual(request)[:3] if settings.manual is None else settings.manual
    if set(alternatives)-offered:
        raise ValueError("A manual policy is outside the current legal candidate space.")
    selected = (REFERENCE, *alternatives)
    manual_policies = [p for p in legal if p.strategy_id in selected]
    manual_adapter = _MeanAdapter(comparison, bundle)
    manual_weights = _validate(comparison, bundle)
    manual = _score(base, manual_policies, manual_adapter, manual_weights)
    manual_future = (_score(comparison.scenario, manual_policies, manual_adapter, manual_weights)
                     if base != comparison.scenario and settings.mode != MODES[2] else manual)
    family = ScenarioFamily(
        "board-declared-futures", request, tuple(cases[name] for name in settings.futures),
        total_budget=settings.robust_budget, comparator=BEST_REALIZED,
        objective=settings.objective, weights=tuple(zip(settings.futures, settings.weights))
        if settings.objective == "DECLARED_WEIGHTED_DELTA" else (),
    )
    robust = evaluate_family(family, bundle)
    trace = {"scene": asdict(comparison.scenario), "features": [asdict(f) for f in comparison.features],
             "scenario_scripts": {name: [asdict(event) for event in case.request.scenario.controls]
                                  for name, case in cases.items()},
             "manual": manual, "manual_future": manual_future, "conditional": conditional, "robust": robust}
    return out | {"strategy": trace, "recommendation": _support(conditional, robust, settings)}


def frozen_timing(root):
    import duckdb

    data = loads((root / next(path for path in PINNED if path.endswith("timing_risk.json"))).read_bytes())
    policies = ("63-L31-soft", "adaptive-63-medium-or-soft", "sc-63-soft", "p2-green-stay-out",
                "p2-green-replan", "p5-scheduled-conditional")
    with ZipFile(root / next(path for path in PINNED if path.endswith("ledgers.zip"))) as archive:
        baseline = loads(archive.read("frozen_baseline.json"))
        with TemporaryDirectory() as scratch, duckdb.connect() as connection:
            ledgers = {}
            for label, member in (("commitments", "pit_commitment_ledger.parquet"),
                                  ("race_control", "race_control_timeline.parquet")):
                file = Path(scratch) / member
                file.write_bytes(archive.read(member))
                rows = connection.read_parquet(str(file)).filter(
                    "family = 'B-stress' and scenario = 'sc-deploy-58'").to_arrow_table().to_pylist()
                ledgers[label] = [row for row in rows if row["policy"] in policies]
    return {"versions": COMPARATORS, "boundaries": data["boundary_transitions"],
            "support": data["recommendation_support"], "contract": data["commitment_contract"],
            "case": {p: data["families"]["B-stress"]["outcomes"]["sc-deploy-58"][p] for p in policies},
            "baseline": baseline, **ledgers}


def export_run(result):
    content = {
        "manifest.json": canonical({key: result[key] for key in ("version", "settings", "source_hashes")} |
                                   {"snapshot_id": result["snapshot"]["snapshot_id"]}),
        "analysis.json": canonical(result),
    }
    content["SHA256SUMS"] = "".join(f"{digest(raw)}  {name}\n" for name, raw in sorted(content.items())).encode()
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, raw in sorted(content.items()):
            info = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            archive.writestr(info, raw, compress_type=ZIP_DEFLATED)
    return output.getvalue()


def replay(root: Path, exported: bytes, assets=None):
    with ZipFile(BytesIO(exported)) as archive:
        members = {"manifest.json", "analysis.json", "SHA256SUMS"}
        if set(archive.namelist()) != members or len(archive.infolist()) != len(members) or any(
            i.file_size > 100_000_000 for i in archive.infolist()
        ):
            raise ValueError("Invalid decision-board export members or size.")
        manifest = loads(archive.read("manifest.json"))
        analysis = archive.read("analysis.json")
        sums = "".join(f"{digest(archive.read(name))}  {name}\n" for name in (
            "analysis.json", "manifest.json"))
        if archive.read("SHA256SUMS").decode() != sums or manifest["version"] != VERSION:
            raise ValueError("Invalid decision-board export checksum or version.")
    assets = assets or load_assets(root)
    if manifest["source_hashes"] != assets[1]:
        raise ValueError("Run requires a different frozen source revision.")
    result = execute(root, Settings(**manifest["settings"]), assets)
    if result["snapshot"]["snapshot_id"] != manifest["snapshot_id"] or canonical(result) != analysis:
        raise ValueError("Recomputed analysis differs from the export.")
    return result
