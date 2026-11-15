"""S7-A.1 negative controls and frozen-data regression tests."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from f1ri.features.historical_field import FieldMode, Series, reconstruct_field, with_sparsity
from f1ri.features.pit_branch import (
    LaneCurve,
    OrderWitness,
    Route,
    certificate,
    fit_graph,
    match_fix,
    matched_at,
    oriented_interval,
)
from f1ri.features.track_geometry import LocationFix
from f1ri.validation.historical_field_sources import stamp
from f1ri.validation.pit_branch_evidence import qualification, trajectory, transition
from f1ri.validation.pit_branch_sources import INPUTS, INPUTS_SHA256, checked_members, load
from f1ri.validation.pit_branch_study import _graphs

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pilot():
    sessions, _, visits, setup, meta = load(ROOT)
    graphs, geometry, held, audits = _graphs(sessions, visits, setup["policy"], meta)
    return sessions, graphs, visits, setup, geometry, held, audits


def test_raw_sources_and_separate_validation(pilot):
    sessions, graphs, visits, _, geometry, held, audits = pilot
    assert len(checked_members(ROOT/INPUTS, INPUTS_SHA256)) == 12
    assert graphs[9987].loop is sessions[9987].geometry
    assert all(row["visit"] != graphs[9987].fit_id for row in held)
    assert [r["passed"] for r in audits if r["session"] == 9987] == [True, True]
    assert geometry[9896]["fit_reason"] == "PIT_FIT_GAP_OR_SPEED"
    assert len({visit["id"] for visit in visits}) == 7


def test_no_main_or_pit_swaps_in_held_controls(pilot):
    _, graphs, _, setup, _, held, _ = pilot
    controls = qualification(held, setup["policy"]["held_gate"])
    for side in (Route.MAIN, Route.LANE):
        assert controls[side]["passed"]
        assert controls[side]["wrong"] == 0
    assert graphs[9987].lane.length_m > 300


def test_shifted_geometry_and_wrong_layout_are_refused(pilot):
    sessions, graphs, *_ = pilot
    field = sessions[9987]
    graph = graphs[9987]
    sample = field.fixes[1].last(stamp("2025-05-18T13:43:11Z")).point
    assert match_fix(graph, LocationFix(sample.at_utc, sample.x+5000, sample.y-6000))["route"] is Route.UNKNOWN
    with pytest.raises(ValueError, match="one layout"):
        replace(graph, lane=replace(graph.lane, layout="different-layout"))
    assert matched_at(graph, sessions[9896], 4, sessions[9896].start, FieldMode.RETROSPECTIVE)["route"] is Route.UNKNOWN
    mismatch = replace(field, geometry=replace(field.geometry, version="shifted"))
    assert not graph.compatible(mismatch)


def test_invalid_curve_and_wrong_direction_are_rejected(pilot):
    sessions, graphs, *_ = pilot
    graph = graphs[9987]
    with pytest.raises(ValueError, match="nonzero segments"):
        LaneCurve(graph.loop.version, ((1., 2.),)*4, 1.)
    series = sessions[9987].fixes[1]
    at = stamp("2025-05-18T13:43:22Z")
    visible = series.window(at-timedelta(seconds=1), at)
    a, b = visible[-2:]
    reversed_samples = Series([replace(a, point=replace(b.point, at_utc=a.point.at_utc)),
                               replace(b, point=replace(a.point, at_utc=b.point.at_utc))],
                              lambda r: r.point.at_utc)
    modified = replace(sessions[9987], fixes=sessions[9987].fixes | {1: reversed_samples})
    backward = matched_at(graph, modified, 1, b.point.at_utc, FieldMode.RETROSPECTIVE)
    assert backward["route"] is Route.UNKNOWN


def test_core_cutoff_causal_availability_and_receipt_gate(pilot):
    sessions, graphs, *_ = pilot
    at = stamp("2025-05-18T13:43:11Z")
    field, graph = sessions[9987], graphs[9987]
    assert matched_at(graph, field, 1, at, FieldMode.CAUSAL)["route"] is Route.UNKNOWN
    assert matched_at(graph, field, 1, at, FieldMode.RECEIPT)["route"] is Route.UNKNOWN
    assert matched_at(graph, field, 1, at, FieldMode.RETROSPECTIVE)["route"] is Route.LANE


def test_route_matching_withholds_sparse_and_conflicted_fix(pilot):
    sessions, graphs, *_ = pilot
    at = stamp("2025-05-18T13:43:11Z")
    session, graph = sessions[9987], graphs[9987]
    row = session.fixes[1].last(at)
    for points in ([row], [replace(row, conflict=True)]):
        sparse = replace(session, fixes=session.fixes | {1: Series(points, lambda f: f.point.at_utc)})
        result = matched_at(graph, sparse, 1, at, FieldMode.RETROSPECTIVE)
        assert result["route"] is Route.UNKNOWN


def test_observed_transition_is_distinct_from_exact_geometric_boundary(pilot):
    sessions, graphs, visits, *_ = pilot
    visit = next(x for x in visits if x["id"] == "imola-vsc-1")
    session, graph = sessions[9987], graphs[9987]
    rows = trajectory(session, visit, graph)
    for kind in ("entry", "merge"):
        bounds = transition(rows, visit, kind, graph)
        assert bounds["quality"] == "OBSERVED_ROUTE_CHANGE_WINDOW"
        assert bounds["earliest_utc"] <= bounds["timing_report_utc"] <= bounds["latest_utc"]
        assert bounds["point_estimate_utc"] is None
        assert bounds["independent_geometric_reference"] is None
    assert transition(rows, visit | {"exit": None}, "merge", graph)["earliest_utc"] is None
    assert transition([], visit, "entry", graph)["earliest_utc"] is None
    assert trajectory(session, visit, graph, FieldMode.CAUSAL, visit["exit"]) == []


def test_inconsistent_timing_is_rejected_before_fitting(pilot):
    sessions, _, visits, setup, *_ = pilot
    sample = next(v for v in visits if v["id"] == "imola-vsc-1")
    g, reason = fit_graph(sessions[9987], sample | {"exit": sample["entry"]-timedelta(seconds=3)}, setup["policy"])
    assert g is None
    assert reason == "LANE_TIMING_OR_FIXES_UNAVAILABLE"


def test_stationary_racing_fix_does_not_become_service(pilot):
    _, graphs, *_ = pilot
    graph = graphs[9987]
    sample = graph.loop.points[len(graph.loop.points)//2]
    point = LocationFix(graph.fit_through, *sample)
    assert match_fix(graph, point)["route"] is not Route.LANE


def test_wrap_and_lapped_car_are_resolved_by_circular_distance():
    assert oriented_interval((995, 1), (7, 2), 1000) == (9, 15)
    assert oriented_interval((995, 3), (997, 3), 1000) == (0, 1000)
    field = {1: (995., 1., 10995.), 4: (7., 2., 10007.), 5: (250., 3., 10250.)}
    result = certificate(1, field, 1000)
    assert result["bounded_nearest"] == 4
    assert result["status"] == "ORDER_PARTIALLY_CONSTRAINED"
    assert result["certified"] is None
    assert result["separation_interval_m"] == (9., 15.)


def test_missing_car_blocks_certificate_unless_movement_envelope_excludes_it():
    field = {1: (100., 2., 1100.), 4: (132., 2., 2132.), 5: None, 9: (600., 5., 1600.)}
    ambiguous = certificate(1, field, 1000, movement_bounds={5: (135., 18., "recent source")})
    assert ambiguous["possible_interveners"] == [5]
    assert ambiguous["certified"] is None
    result = certificate(1, field, 1000, movement_bounds={5: (500., 25., "recent source")})
    assert result["exclusions"][5]["reason"] == "MOVEMENT_ENVELOPE_CANNOT_INTERVENE"
    assert result["bounded_nearest"] == 4
    at = stamp("2025-05-18T14:10:00Z")
    witness = OrderWitness(9987, 1, 4, at, "fixture simultaneous order witness", "AHEAD")
    exact_fixture = certificate(1, field, 1000, off_route={5: "fixture route record"},
                                witness=witness, session=9987, cutoff=at)
    assert exact_fixture["certified"] == 4
    stale = certificate(1, field, 1000, off_route={5: "fixture route record"}, witness=witness,
                        session=9987, cutoff=at+timedelta(milliseconds=1))
    assert stale["certified"] is None


def test_close_queue_keeps_order_ambiguous_and_unconfirmed_retirement_blocks():
    field = {1: (600., 10., 10600.), 4: (614., 10., 10614.), 5: (620., 11., 10620.), 9: None}
    proof = certificate(1, field, 1000)
    assert proof["status"] == "ORDER_AMBIGUOUS"
    assert proof["certified"] is None
    assert 9 in proof["possible_interveners"]


def test_singapore_forecast_drivers_keep_strict_route_guard(pilot):
    sessions, graphs, *_ = pilot
    session = sessions[9896]
    cutoff = stamp("2025-10-05T12:52:29.272Z")
    frame = reconstruct_field(session, cutoff, drivers=(4, 63),
                              route_matcher=lambda s, driver, at, mode: matched_at(graphs[9896], s, driver, at, mode))
    assert all(r["progress_m"] is None for r in frame["drivers"])
    assert frame["strategy_support"] == "HISTORICAL_STRATEGY_UNSUPPORTED"
    assert frame["inventory"] is None


def test_thinning_does_not_turn_stationary_sc_traces_into_racing_positions(pilot):
    sessions, graphs, *_ = pilot
    session = sessions[9987]
    at = stamp("2025-05-18T14:21:42Z")
    def match(s, driver, clock, mode):
        return matched_at(graphs[s.key], s, driver, clock, mode)

    full = reconstruct_field(session, at, route_matcher=match)
    thinned = reconstruct_field(with_sparsity(session, 4), at, route_matcher=match)
    full_drivers = {row["driver_number"] for row in full["drivers"] if row["progress_m"] is not None}
    assert {row["driver_number"] for row in thinned["drivers"] if row["progress_m"] is not None} <= full_drivers


def test_dashboard_exposes_route_panel_with_historical_strategy_gate():
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT/"src/f1ri/dashboard/app.py"), default_timeout=40).run()
    next(iter(app.get("segmented_control"))).set_value("Race State").run()
    assert not app.exception
    assert any("Pit routes and predecessor evidence" in item.label for item in app.expander)
    assert any("Historical strategy execution is unavailable" in item.value for item in app.info)
    assert next(widget for widget in app.selectbox if widget.label == "Route evidence cutoff").value == "Archived model cutoff"
