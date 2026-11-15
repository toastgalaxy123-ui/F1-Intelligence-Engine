"""Build the bounded S6-C validation evidence without acquiring new source data."""

from dataclasses import asdict, fields, replace
from json import loads
from pathlib import Path
from time import perf_counter
from zipfile import ZipFile

from f1ri.decision_board import (
    MODES,
    PINNED,
    PRESETS,
    VERSION,
    Provenance,
    Settings,
    Snapshot,
    canonical,
    digest,
    execute,
    export_run,
    load_assets,
    prepare,
    replay,
    scenarios,
)
from f1ri.simulator.strategy_optimization import optimize
from f1ri.simulator.strategy_robust import ScenarioFamily, evaluate_family
from f1ri.validation.commitment_study import COMPARATORS


def live_readiness():
    sources = {
        "lap timing": ("laps", "Frozen Singapore cutoff features; sector and lap reconstruction for historical studies",
                       "Observation dates require event-time filtering and arrival reconciliation."),
        "tyres and stints": ("stints / compound allocations", "Current archived stint, fitted compound and age",
                            "Per-driver unused set identity and prior wear are unavailable."),
        "race control": ("race_control", "Historical episode reconstruction and deterministic scripted transitions",
                         "Announcement, effect and receipt clocks require independent ordering evidence."),
        "positions": ("position / location / intervals", "Historical position and proximity research",
                      "Competitive classification cannot establish track-local predecessor or reliable route."),
        "pit": ("pit / pit telemetry", "Historical pit reconciliation and declared route simulation",
                "Service, occupancy and irreversible physical entry lack a live proof contract."),
        "weather": ("weather", "Verified archived weather measurement at or before the model cutoff",
                    "No observed publication lag or guaranteed cadence."),
        "grid and qualifying": ("starting_grid / laps / session_result", "Archived cutoff features",
                                "Corrections and missing records need versioned handling."),
        "pit lane geometry": ("reference archive / telemetry", "Declared route model and historical pilot studies",
                              "Actual session-specific branches, speed controls and coordinates require validation."),
        "source transport": ("OpenF1Client / raw_cache", "HTTP snapshots with retries, throttling, raw hashes and refresh",
                             "No receipt event ledger, revision ordering, freshness watermark or outage recovery proof."),
    }
    return {"version": VERSION, "release": "LIVE_UNSUPPORTED", "scope": "Checked-in adapters and frozen historical data",
            "fields": {field: {"source": spec[0], "current_capability": spec[1], "observability_gap": spec[2],
                               "cutoff_validity": "Verified only when pinned and cutoff-filtered",
                               "freshness": "UNVERIFIED", "historical_verification": "See current capability"}
                       for field, spec in sources.items()},
            "deployment_gate": ["Event and receipt clocks per field", "Delay distributions and freshness thresholds",
                                "Ordering and revision replay", "Outage recovery", "Validated physical snapshot coverage"]}


def _sha(value):
    return digest(canonical(value))


def _check(name, condition):
    if not condition:
        raise ValueError(f"S6-C verification failed: {name}")
    return True


def seal(out):
    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (out / "SHA256SUMS").write_text(
        "".join(f"{digest(p.read_bytes())}  {p.relative_to(out).as_posix()}\n" for p in files),
        encoding="utf-8", newline="\n",
    )


def run_study(root: Path, out: Path):
    assets = load_assets(root)
    model, hashes, _ = assets
    out.mkdir(parents=True, exist_ok=False)
    (out / "diagnostics").mkdir()
    (out / "screenshots").mkdir()
    settings = {key: Settings(preset=key, mode=MODES[0 if key in ("archive", "unsupported") else 1],
                              conditional_future="SC" if key == "neutralization" else "GREEN",
                              stop_options=1, sets=("medium", "soft") if key == "shared-box" else Settings().sets)
                for key in PRESETS}

    def write(name, obj):
        (out / f"{name}.json").write_bytes(canonical(obj)+b"\n")

    observations = {}
    runs = {}
    for name, cfg in settings.items():
        start = perf_counter()
        runs[name] = execute(root, cfg, assets)
        observations[name] = round(perf_counter()-start, 3)
    result = runs["neutralization"]
    snapshot, request, _ = prepare(root, settings["neutralization"], assets)
    cases = scenarios(request, settings["neutralization"])
    control = optimize(cases["SC"].request, model)
    conditional = result["strategy"]["conditional"]
    reference_robust = evaluate_family(ScenarioFamily(
        "parity", request, tuple(cases.values()), total_budget=settings["neutralization"].robust_budget,
    ), model)
    robust = result["strategy"]["robust"]
    timing = runs["timing"]["timing"]
    cached = execute(root, replace(settings["neutralization"], cache=False), assets)
    c = cached["strategy"]["conditional"]
    with ZipFile(root / next(path for path in PINNED if path.endswith("ledgers.zip"))) as archive:
        baseline = loads(archive.read("frozen_baseline.json"))
        prior = loads(archive.read("metrics.json"))
    parity = _check("conditional score and candidate metadata", canonical(conditional) == canonical(control))
    robust_parity = _check("full robust matrix and traces", canonical(robust | {"family": "parity"}) ==
                           canonical(reference_robust))
    timing_parity = _check("original and expanded timing evidence", canonical(timing["baseline"]) ==
                            canonical(baseline) and timing["case"]["sc-63-soft"]["regret_us"] == 46_754_185)
    cache_parity = _check("cached vs uncached conditional branches and robust matrix",
                          (conditional["decision"], conditional["conditional"], robust["analysis"]) ==
                          (c["decision"], c["conditional"], cached["strategy"]["robust"]["analysis"]))
    exported = export_run(result)
    reproduced = replay(root, exported, assets)
    export_parity = _check("bytewise export and replay", export_run(reproduced) == exported)
    (out / "diagnostics" / "neutralization_replay.zip").write_bytes(exported)
    changed = {
        "driver": {"driver": 4}, "horizon": {"horizon": 3}, "inventory": {"sets": ("soft",)},
        "scenario": {"preset": "two-car"}, "sc_clock": {"sc_at_s": 28}, "budget": {"budget": 2},
        "objective": {"objective": "PARETO"}, "family": {"futures": ("GREEN", "SC")},
        "cutoff": {"cutoff_utc": "2025-10-05T12:55:00Z"},
    }
    identities = {key: prepare(root, replace(settings["neutralization"], **kwargs), assets)[0].snapshot_id
                  for key, kwargs in changed.items()}
    incomplete = execute(root, replace(settings["neutralization"], budget=2, robust_budget=4), assets)
    checks = {
        "conditional_parity": parity, "robust_parity": robust_parity, "timing_risk_parity": timing_parity,
        "cached_vs_uncached": cache_parity, "export_reproduction": export_parity,
        "all_edits_invalidate": _check("input identities invalidate", len(set(identities.values()) |
                                                                           {snapshot.snapshot_id}) == 10),
        "prospective_isolated": _check("future control stripped before prospective scoring",
                                       conditional["decision"] ==
                                       optimize(replace(cases["SC"].request, scenario=replace(
                                           cases["SC"].request.scenario, controls=())), model)["decision"]),
        "incomplete_withholds_action": _check("budget exhaustion",
                                               incomplete["recommendation"]["incomplete"] and
                                               incomplete["recommendation"]["unique_action"] is None),
        "archive_limits": _check("archived scope", len(runs["archive"]["forecasts"]) == 1 and
                                 runs["archive"]["strategy"] is None and runs["unsupported"]["unsupported"]),
        "replacement_age": _check("used set request", {
            row["request"]["tyre_age_at_start"] for row in result["forecasts"] if row["plan"].startswith("used")
        } == {3}),
    }
    write("protocol", {"version": VERSION, "frozen_sha256": hashes, "without_acquisition": True,
                       "clock_unit": "integer microseconds", "cuts": [snapshot.cutoff_utc.value]})
    write("dashboard_manifest", {"version": VERSION, "entry": "src/f1ri/dashboard/app.py", "modes": MODES,
                                 "presets": PRESETS, "model_sha256": model.artifact_sha256,
                                 "run_key": "settings and frozen source hashes"})
    write("snapshot_schema", {"version": VERSION, "fields": {f.name: str(f.type) for f in fields(Snapshot)},
                              "provenance": list(Provenance), "missing_value": None})
    write("snapshot_catalog", {key: row["snapshot"] for key, row in runs.items()})
    write("source_provenance", {"frozen_sha256": hashes, "forecast_feature_sources": {
        p["plan"]: p["feature_source_sha256"] for p in result["forecasts"]}})
    write("model_support", result["snapshot"]["model"])
    write("physical_support", {key: {"layout": row["snapshot"]["layout"], "profile": row["snapshot"]["rule_profile"],
                                    "support": row["snapshot"]["support"]} for key, row in runs.items()})
    write("scenario_presets", {key: asdict(value) for key, value in settings.items()})
    write("decision_contract", {"point_in_time": "KNOWN_AT_CUTOFF", "declared": "DECLARED_FUTURE",
                                "retrospective": "REALIZED_HINDSIGHT", "comparators": COMPARATORS,
                                "incomplete_action": None, "probabilities_estimated": False})
    write("interaction_replay", {"original": snapshot.snapshot_id, "edited": identities})
    write("conditional_parity", {"passed": parity, "sha256": _sha(conditional), "search": conditional["search"]})
    write("robust_parity", {"passed": robust_parity, "sha256": _sha(robust), "search": robust["search"]})
    write("timing_risk_parity", {"passed": timing_parity, "versions": timing["versions"], "case": timing["case"],
                                "commitments": timing["commitments"]})
    write("recommendation_support", {key: row["recommendation"] for key, row in runs.items()})
    write("performance", {"observed_wall_seconds": observations, "deterministic": False,
                          "scope": "Warm dependency cache, cold decision run; UI navigation reuses analysis"})
    write("live_readiness", live_readiness())
    write("export_reproduction", {"passed": export_parity, "export_sha256": digest(exported)})
    write("regression_parity", {"frozen_sha256": hashes, "s6_b1_original_metrics_sha256": _sha(prior),
                                "cached_vs_uncached": cache_parity})
    write("invariant_checks", checks)
    metrics = {"release_candidate": "RESEARCH_DECISION_BOARD", "presets": len(runs), "checks": checks,
               "conditional_evaluations": conditional["search"]["simulator_evaluations"],
               "robust_evaluations": robust["search"]["simulator_evaluations"],
               "frozen_sc_regret_us": timing["case"]["sc-63-soft"]["regret_us"], "live": "UNSUPPORTED"}
    write("metrics", metrics)
    seal(out)
    return metrics
