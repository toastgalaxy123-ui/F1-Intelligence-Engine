"""S7-A source, geometry and historical-cutoff controls."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from f1ri.features.historical_field import (
    FieldMode,
    Series,
    Session,
    TimedValue,
    indexed,
    reconcile_fixes,
    reconstruct_field,
    with_sparsity,
)
from f1ri.features.track_geometry import LocationFix, TrackGeometry, map_fixes
from f1ri.validation.historical_field_sources import (
    FROZEN,
    INPUT_ARCHIVE,
    checked_members,
    load_pilots,
    stamp,
)

ROOT = Path(__file__).resolve().parents[1]
CUT = stamp("2025-05-18T13:45:00Z")


@pytest.fixture(scope="module")
def pilots():
    return load_pilots(ROOT)[0]


def xy(distance):
    distance %= 4000
    if distance < 1000:
        return distance, 0
    if distance < 2000:
        return 1000, distance-1000
    if distance < 3000:
        return 3000-distance, 1000
    return 0, 4000-distance


def series(*coords):
    fixes = [(LocationFix(CUT+timedelta(seconds=offset), *xy(distance)), "test", "fixture")
             for offset, distance in coords]
    return reconcile_fixes(fixes)[0]


def fixture_field():
    layout = TrackGeometry("test-square", 4000., CUT-timedelta(minutes=4),
                           ((0., 0.), (1000., 0.), (1000., 1000.), (0., 1000.), (0., 0.)), 0.)
    anchor = CUT-timedelta(seconds=68)
    active = {1: series((-.5, 1072), (-.25, 1081), (0, 1090), (.25, 1099)),
              4: series((-.5, 1310), (-.25, 1320), (0, 1330), (.25, 1340)),
              63: series((-.5, 1550), (-.25, 1562), (0, 1574), (.25, 1586))}
    return Session(9987, "Fixture", (1, 4, 63), CUT-timedelta(minutes=1), CUT+timedelta(minutes=1),
                   layout, "test", active, indexed({1: [TimedValue(anchor, 20, "timing")],
                                                    4: [TimedValue(anchor, 19, "timing")],
                                                    63: [TimedValue(anchor, 20, "timing")]}),
                   indexed({1: [TimedValue(anchor, 1, "classification")],
                            4: [TimedValue(anchor, 3, "classification")],
                            63: [TimedValue(anchor, 2, "classification")]}),
                   {}, Series([], lambda row: row.at), "test")


def test_sources_are_sealed_and_clocks_normalize():
    assert len(checked_members(ROOT / INPUT_ARCHIVE, FROZEN[INPUT_ARCHIVE])) == 74
    assert stamp("2025-10-05T20:52:29.272+08:00") == stamp("2025-10-05T12:52:29.272Z")
    with pytest.raises(ValueError, match="explicit UTC offset"):
        stamp("2025-10-05T12:52:29.272")


def test_duplicate_and_conflicting_revision():
    a, b = LocationFix(CUT, 3, 4), LocationFix(CUT, 3, 5)
    state, report = reconcile_fixes([(a, "first", "a"), (a, "first", "b"), (b, "earlier", "c")])
    assert report == {"duplicates": 2, "conflicts": 1}
    assert state.last(CUT).conflict
    assert state.last(CUT).sources == ("a", "b", "c")


def test_full_field_lapped_order_is_not_classification():
    result = reconstruct_field(fixture_field(), CUT)
    car = result["drivers"][0]
    assert result["support"] == "FIELD_ORDER_SUPPORTED"
    assert car["predecessor"] == 4
    assert car["classification"] == 1
    assert car["unwrapped_m"] == pytest.approx(81_090)
    assert car["subset_ahead"]["gap_m"] == pytest.approx(240)
    assert car["subset_ahead"]["encounter"] == "LAPPING"


def test_unknown_car_prevents_full_predecessor_claim():
    fixture = fixture_field()
    result = reconstruct_field(replace(fixture, fixes={d: v for d, v in fixture.fixes.items() if d != 63}), CUT)
    assert result["support"] == "PARTIAL_FIELD_SUPPORTED"
    assert result["drivers"][0]["predecessor"] is None
    assert result["drivers"][0]["subset_ahead"]["driver_number"] == 4


def test_overlap_keeps_predecessor_unknown():
    fixture = fixture_field()
    fixes = fixture.fixes | {4: series((-.5, 1073), (-.25, 1082), (0, 1091))}
    row = reconstruct_field(replace(fixture, fixes=fixes), CUT)["drivers"][0]
    assert row["predecessor"] is None
    assert row["ahead_status"] == "CIRCULAR_ORDER_OVERLAP"


def test_pit_corridor_and_archived_lane_clocks_are_separate():
    fixture = fixture_field()
    fixes = fixture.fixes | {1: series((-.5, 3990), (-.25, 3995), (0, 0), (.25, 10))}
    first = reconstruct_field(replace(fixture, fixes=fixes), CUT)["drivers"][0]
    assert first["mapped_candidate_m"] == 0
    assert first["reason"] == "PIT_BRANCH_UNRESOLVED_NEAR_CONTROL_LINE"
    pit = indexed({1: [TimedValue(CUT-timedelta(seconds=1), "LANE_ENTRY_TIMING", "fixture"),
                       TimedValue(CUT+timedelta(seconds=2), "LANE_EXIT_TIMING", "fixture")]})
    second = reconstruct_field(replace(fixture, pit=pit), CUT)["drivers"][0]
    assert second["reason"] == "PIT_ROUTE_PARTIAL_LANE_TIMING"
    assert second["route"] == "UNKNOWN"


def test_wrong_way_and_off_track_projection():
    geo = fixture_field().geometry
    reverse = map_fixes(geo, [LocationFix(CUT, *xy(1100)), LocationFix(CUT+timedelta(seconds=.25), *xy(1080))])
    assert reverse[-1].reason == "NO_PLAUSIBLE_SEGMENT"
    assert map_fixes(geo, [LocationFix(CUT, 500, 500)])[0].progress_m is None


def test_future_changes_do_not_change_causal_state():
    fixture = fixture_field()
    at = CUT+timedelta(seconds=.1)
    future = fixture.fixes | {4: series((-.5, 1310), (-.25, 1320), (0, 1330), (.25, 1348))}
    other = replace(fixture, fixes=future,
                    control=Series([TimedValue(CUT+timedelta(seconds=.2), "SC_REPORTED", "future")],
                                   lambda row: row.at))
    assert reconstruct_field(fixture, at) == reconstruct_field(other, at)
    a = reconstruct_field(fixture, at, FieldMode.RETROSPECTIVE)
    b = reconstruct_field(other, at, FieldMode.RETROSPECTIVE)
    assert a["drivers"][1]["progress_m"] != b["drivers"][1]["progress_m"]
    assert max(r["maximum_fix_used_utc"] for r in a["drivers"]) > at


def test_driver_selection_preserves_neighbor_context():
    fixture = fixture_field()
    a = reconstruct_field(fixture, CUT, drivers=[4, 1])
    b = reconstruct_field(fixture, CUT, drivers=[1, 4])
    assert a == b
    assert a["drivers"] == reconstruct_field(fixture, CUT)["drivers"][:2]


def test_stationary_samples_do_not_claim_retirement_or_racing():
    fixture = fixture_field()
    static = series(*((i/4, 1110) for i in range(-12, 1)))
    row = reconstruct_field(replace(fixture, fixes=fixture.fixes | {1: static}), CUT)["drivers"][0]
    assert row["reason"] == "STATIONARY_ROUTE_UNVERIFIED"
    assert row["route"] == "UNKNOWN"


def test_real_singapore_pilot_and_negative_control(pilots):
    sgp = pilots[9896]
    cutoff = stamp("2025-10-05T12:52:29.272Z")
    snapshot = reconstruct_field(sgp, cutoff)
    assert snapshot["supported_positions"] == 17
    assert snapshot["supported_predecessors"] == 0
    assert snapshot["inventory"] is None
    assert snapshot["strategy_support"] == "HISTORICAL_STRATEGY_UNSUPPORTED"
    assert reconstruct_field(sgp, cutoff, FieldMode.RECEIPT)["support"] == "RECEIPT_ORDER_UNAVAILABLE"
    assert reconstruct_field(sgp, stamp("2025-10-05T13:05:00Z"))["supported_positions"] == 0
    assert reconstruct_field(with_sparsity(sgp, 8), cutoff)["supported_positions"] < snapshot["supported_positions"]


def test_imola_reported_control_changes_at_cutoff(pilots):
    imola = pilots[9987]
    effect = stamp("2025-05-18T14:06:27Z")
    before = reconstruct_field(imola, effect-timedelta(milliseconds=1))
    deployed = reconstruct_field(imola, effect)
    released = reconstruct_field(imola, stamp("2025-05-18T14:21:42Z"))
    assert before["track_status"]["value"] != "SC_REPORTED"
    assert deployed["track_status"]["value"] == "SC_REPORTED"
    assert released["track_status"]["value"] == "TRACK_CLEAR_REPORTED"
    assert any(r["reason"] == "STATIONARY_ROUTE_UNVERIFIED" for r in released["drivers"])


def test_dashboard_historical_controls_and_receipt_gate():
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "src/f1ri/dashboard/app.py"), default_timeout=30).run()
    next(select for select in app.get("segmented_control")).set_value("Race State").run()
    assert not app.exception
    assert any("17/20 track-local" in row.value for row in app.markdown)
    next(select for select in app.selectbox if select.label == "Historical race").select("9987").run()
    assert not app.exception
    next(select for select in app.selectbox if select.label == "Reconstruction mode").select(FieldMode.RECEIPT).run()
    assert not app.exception
    assert any("no receipt clocks" in row.value for row in app.warning)
