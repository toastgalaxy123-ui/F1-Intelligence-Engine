"""Deterministic shadow gates and prospective race-control receipts."""

from dataclasses import asdict, replace
from datetime import timedelta
from json import loads

from f1ri.features.shadow_snapshot import ShadowConfiguration, make_snapshot, seal_decision
from f1ri.features.temporal_maturity import filter_receipt_snapshot
from f1ri.ingestion.source_receipt import (
    CLOCK_UNKNOWN,
    EVENT_ONLY,
    EXPERIMENT,
    OBSERVED,
    canonical,
    digest,
)
from f1ri.ingestion.source_replay import ReplayIndex
from f1ri.validation.source_availability_study import strategy_delay
from f1ri.validation.source_faults import (
    BASE,
    ENDPOINTS,
    ROSTER,
    SUITE_ID,
    _position,
    _raw_locations,
    _sc,
    response,
)


def _at(offset):
    return BASE+timedelta(seconds=offset)


def _view(rows, cutoff, policy, *, mode=EXPERIMENT, event=None):
    index = ReplayIndex(SUITE_ID, 9987, ENDPOINTS, rows, mode=mode, roster=ROSTER)
    at = _at(cutoff)
    return loads(index.replay(at, event_cutoff=_at(event) if event is not None else at, receipt_cutoff=at,
                              policy=policy))


def _fields(snapshot, endpoint):
    return [r["payload"] for r in filter_receipt_snapshot(snapshot)["observations"] if r["endpoint"] == endpoint]


def control_experiments(policy):
    controls = strategy_delay()
    from f1ri.simulator.strategy import run_strategy
    from f1ri.validation.strategy_fixtures import strategy_families

    scene = strategy_families()["sc"]
    policy_source = next(p for p in scene.policies if p.strategy_id == "react-sc")
    entry = controls["runs"][0]["pit_entries"][0]["entry_us"]
    physical = controls["runs"][0]["physical_effect_us"]
    before, after = ((entry-physical+d)/1e6 for d in (-1, 1))
    rows = []
    for label, arrival in (("just_before_entry", before), ("just_after_entry", after)):
        packet = response(1, "race_control", 0., arrival, [_sc(0)])
        before_receipt = _fields(_view([packet], arrival-.000001, policy), "race_control")
        at_receipt = _fields(_view([packet], arrival, policy), "race_control")
        review = physical+round(arrival*1e6)
        approved = replace(policy_source, stops=tuple(replace(s, review_us=review) for s in policy_source.stops))
        branch = run_strategy(replace(scene, policies=(approved,)), approved)
        decisions = [d for d in branch["decisions"] if d["outcome"] == "REQUESTED"]
        if before_receipt or not at_receipt or any(d["at_us"] < review for d in decisions):
            raise ValueError("Race-control message leaked ahead of receipt.")
        rows.append({"case": label, "physical_entry_reference_us": entry, "policy_receipt_us": review,
                     "requested_laps": [d["target_lap"] for d in decisions],
                     "committed_laps": [d["target_lap"] for d in branch["pit"]]})
    green = _sc(50) | {"message": "TRACK CLEAR", "category": "Flag"}
    arrivals = [response(1, "race_control", 0, .1, [_sc(0)]),
                response(2, "race_control", 51, 55, [green])]
    old, new = (_fields(_view(arrivals, t, policy), "race_control") for t in (54, 55))
    corrected = arrivals + [response(3, "race_control", 56, 57, [green | {"flag": "GREEN"}])]
    cut_before = _view(arrivals, 56.5, policy)
    cut_with_future = _view(corrected, 56.5, policy)
    current = _fields(_view(corrected, 58, policy), "race_control")
    if any(k["message"] == "TRACK CLEAR" for k in old) or not any(k["message"] == "TRACK CLEAR" for k in new):
        raise ValueError("GREEN availability disagrees with its receipt.")
    if cut_before != cut_with_future or not any(k.get("flag") == "GREEN" for k in current):
        raise ValueError("A corrected control version escaped its receipt gate.")
    pairs = {}
    for name, position_event, control_event in (
        ("fresh_position_stale_control", 0, -31), ("stale_position_fresh_control", -16, 0),
    ):
        packets = [response(1, "position", .1, .2, [_position(1, position_event), _position(4, position_event, 2)]),
                   response(2, "race_control", .2, .3, [_sc(control_event)])]
        status = _view(packets, .4, policy)["endpoints"]
        pairs[name] = {ep: status[ep]["field_input_allowed"] for ep in ("position", "race_control")}
    if pairs != {"fresh_position_stale_control": {"position": True, "race_control": False},
                 "stale_position_fresh_control": {"position": False, "race_control": True}}:
        raise ValueError("Race-control and position freshness gates are not independent.")
    return controls | {"physical_controls_unmodified": True, "deadline_controls": rows,
                       "independent_freshness": pairs,
                       "green_delivery": {"source_event_s": 50, "usable_s": 55,
                                          "visible_before": False, "visible_after": True},
                       "correction": {"usable_s": 57, "prefix_stable": True, "corrected_flag_after": "GREEN"}}


def shadow_cases(session, policy):
    payload = _raw_locations(session)
    timing = {"session_key": 9987, "date": str(_at(-.02)), "driver_number": 4, "lap_number": 45}
    active = [
        response(1, "location", -.1, .1, payload),
        response(2, "race_control", .1, .15, [_sc(0)]),
        response(3, "pit", .15, .2, [timing]),
        response(4, "position", .2, .25, [_position(1, 0), _position(4, 0, 2)]),
    ]
    valid = _view(active, .4, policy)
    declared = ShadowConfiguration(2025, search_complete=True, unique_recommendation=True)
    variants = {}

    def add(name, raw, cfg=declared, geometry=session):
        snapshot = make_snapshot(raw, cfg, observed_geometry=geometry)
        variants[name] = seal_decision(snapshot)
        if snapshot.action is not None:
            raise ValueError("Shadow preflight enabled an action.")

    add("complete_receipt_fixture", valid)
    add("predecessor_uncertified", valid)
    add("missing_tyre_inventory", valid)
    add("model_2026_incompatible", valid, replace(declared, year=2026))
    add("pit_geometry_missing", valid, geometry=None)
    add("stale_location", _view(active, 4, policy))
    stale = [active[0], response(2, "race_control", .1, .15, [_sc(-31)]), *active[2:]]
    add("stale_race_control", _view(stale, .4, policy))
    clash = next(row for row in payload if row["driver_number"] == 1)
    conflict = (*active, response(5, "location", .3, .35, [clash, clash | {"x": clash["x"]+30}]))
    add("source_revision_conflict", _view(conflict, .4, policy))
    archive = valid | {"mode": EVENT_ONLY, "observations": [], "receipt_cutoff": None, "endpoints": {},
                       "capture_id": "archive-imola", "prefix_sha256": digest(b"archive-imola")}
    add("historical_no_original_receipts", archive)
    current = [(replace(env, mode=OBSERVED, clock_quality=CLOCK_UNKNOWN), raw) for env, raw in active]
    add("contemporary_receipt_schema_fixture", _view(current, .4, policy, mode=OBSERVED))
    add("search_budget_exhausted", valid, replace(declared, search_complete=False))
    add("non_unique_candidate", valid, replace(declared, unique_recommendation=False))
    amended = (*active, response(5, "position", 5, 6, [_position(1, 0, 3)]))
    earlier = _view(amended, .4, policy)
    add("receipt_future_mutation", earlier)
    add("wrong_model_hash", valid, replace(declared, expected_model_sha256="0"*64))
    add("snapshot_unavailable", _view([], .4, policy))
    outage = (*active, response(5, "location", .3, .35, status=503, error="HTTP_503"))
    recovered = (*outage, response(6, "location", .4, .5, payload))
    add("outage_then_recovered", _view(recovered, .6, policy))
    before = make_snapshot(_view(outage, .4, policy), declared, observed_geometry=session)
    if "SOURCE_OUTAGE" not in before.withholding_reasons:
        raise ValueError("Failure prefix was not preserved.")
    gates = {key: v["snapshot"]["withholding_reasons"] for key, v in variants.items()}
    expectations = {
        "predecessor_uncertified": "PHYSICAL_PREDECESSOR_UNCERTIFIED",
        "missing_tyre_inventory": "TYRE_INVENTORY_UNAVAILABLE", "model_2026_incompatible": "MODEL_ERA_UNSUPPORTED",
        "pit_geometry_missing": "PIT_GEOMETRY_UNSUPPORTED", "stale_location": "LOCATION_INPUT_UNSUPPORTED",
        "stale_race_control": "RACE_CONTROL_INPUT_UNSUPPORTED", "source_revision_conflict": "SOURCE_CONFLICT",
        "historical_no_original_receipts": "ORIGINAL_RECEIPT_UNAVAILABLE",
        "search_budget_exhausted": "SEARCH_NOT_COMPLETE", "non_unique_candidate": "NO_UNIQUE_RECOMMENDATION",
        "wrong_model_hash": "MODEL_HASH_MISMATCH", "snapshot_unavailable": "LOCATION_INPUT_UNSUPPORTED",
    }
    if not all(reason in gates[name] for name, reason in expectations.items()) or not (
        canonical(variants["complete_receipt_fixture"]) == canonical(variants["receipt_future_mutation"]) and
        "LOCATION_INPUT_UNSUPPORTED" not in gates["outage_then_recovered"] and
        "SHADOW_SNAPSHOT_SUPPORTED" in variants["complete_receipt_fixture"]["snapshot"]["support"]
    ):
        raise ValueError("Controlled shadow eligibility failed.")
    correction = [response(1, "laps", 1, 1.1, [{"session_key": 9987, "driver_number": 1, "lap_number": 45,
                                                 "date_start": str(_at(-99)), "lap_duration": 94.}]),
                  response(2, "laps", 2, 2.2, [{"session_key": 9987, "driver_number": 1, "lap_number": 46,
                                                 "date_start": str(_at(0)), "lap_duration": 95.}]),
                  response(3, "laps", 4, 4.1, [{"session_key": 9987, "driver_number": 1, "lap_number": 45,
                                                 "date_start": str(_at(-99)), "lap_duration": 95.}])]
    corrected_duration = [next(v["lap_duration"] for v in _fields(_view(correction, t, policy), "laps")
                               if v["lap_number"] == 45) for t in (3, 5)]
    backfill = [response(1, "position", 1, 2, [_position(1, -5)])]
    tests = {"T05": corrected_duration == [94., 95.],
             "T08": control_experiments(policy)["physical_controls_unmodified"],
             "T09": not _fields(_view(backfill, 1.5, policy), "position") and
             len(_fields(_view(backfill, 2.1, policy), "position")) == 1}
    if not all(tests.values()):
        raise ValueError("Receipt maturity scenarios failed.")
    return variants, {
        "temporal_receipt_controls": tests, "corrected_lap_durations": corrected_duration,
        "later_evaluation": {"sealed_sha256": variants["complete_receipt_fixture"]["sha256"],
                             "future_versions_observed": len(_view(amended, 7, policy)["observations"]),
                             "sealed_record_rewritten": False},
        "before_recovery": asdict(before), "controlled_checks": len(variants),
    }
