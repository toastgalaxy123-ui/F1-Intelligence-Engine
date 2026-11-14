from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from f1ri.features.track_geometry import LocationFix, TrackGeometry, map_fixes
from f1ri.validation.historical_neutralization_compare import (
    compare_lines,
    error_stats,
    relative_errors,
)
from f1ri.validation.historical_neutralization_evidence import (
    _timing_bound,
    chronology,
    overlap,
    position_at,
    route_events,
    timing_observations,
)
from f1ri.validation.historical_neutralization_sources import (
    AuditSources,
    clock,
    read_release,
    verify_releases,
)
from f1ri.validation.historical_neutralization_study import PLAN

ROOT = Path(__file__).resolve().parents[1]
ZERO = datetime(2025, 5, 18, 14, tzinfo=UTC)


def at(seconds):
    return ZERO + timedelta(seconds=seconds)


@pytest.fixture(scope="module")
def archived():
    return read_release(ROOT, "s5_a")["source_manifest.json"]["historical"]


def test_frozen_provenance_and_chronology(archived):
    plan = yaml.safe_load((ROOT / PLAN).read_bytes())
    parity = verify_releases(ROOT, plan["frozen"])
    assert parity["s5_a1"]["status"] == "METRICS_BYTES_VERIFIED"
    episodes, messages, intervals = chronology(archived)
    assert len(episodes) == len(intervals) == 6
    sc = next(r for r in episodes if r["status"] == "SC")
    assert sc["announcement_utc"] == at(20*60+9)
    assert sc["archived_release_utc"] == at(21*60+42)
    assert sc["actual_withdrawal_utc"] is None and sc["withdrawal_proxy_utc"] == sc["archived_release_utc"]
    assert not [r for r in episodes if r["race"] == "Singapore" and r["scope"] == "TRACK"]
    assert all(r["receipt_utc"] is None and r["driver_response_utc"] is None for r in messages)


@pytest.mark.parametrize(("stamp", "geometry", "lap", "route", "reason"), [
    (None, True, True, "RACING", "NO_LOCATION_BEFORE_CUTOFF"),
    (at(0), True, True, "RACING", "STALE_LOCATION_NO_CACHE_WINDOW"),
    (at(1), None, True, "RACING", "GEOMETRY_UNAVAILABLE"),
    (at(1), True, False, "RACING", "NO_LAP_ALIGNMENT_INPUT"),
    (at(1), True, True, "PIT_OR_OFF_TRACK", "PIT_OR_ROUTE_AMBIGUITY"),
])
def test_location_exclusion_order(stamp, geometry, lap, route, reason):
    assert position_at((), geometry, at(1), .9, lap, route, stamp) == (None, reason)


def test_location_rejects_future_fix_and_keeps_uncertainty():
    layout = TrackGeometry("test", 400, at(0), ((0, 0), (100, 0), (100, 100), (0, 100), (0, 0)), 0.)
    fixes = (LocationFix(at(10), 24., 0.), LocationFix(at(10.2), 32., 0.),
             LocationFix(at(10.4), 40., 0.))
    trace = map_fixes(layout, fixes)
    estimate, reason = position_at(trace[:2], layout, at(10.3), .9, {"start": at(8)}, "RACING", at(10.2))
    assert reason is None and estimate.progress_m == pytest.approx(36.)
    assert estimate.latest_observation_utc == at(10.2) and estimate.uncertainty_m > 0
    with pytest.raises(ValueError, match="visible-prefix"):
        position_at(trace, layout, at(10.3), .9, {"start": at(8)}, "RACING", at(10.2))


def test_partial_lap_overlap_and_missing_period():
    intervals = [(at(1), at(5), "VSC"), (at(5), at(10), "SC")]
    assert overlap(at(0), at(7), intervals) == {"UNKNOWN": 1., "VSC": 4., "SC": 2.}
    with pytest.raises(ValueError, match="disjoint"):
        overlap(at(1), at(5), intervals + [(at(2), at(4), "GREEN")])


def test_timing_bound_requires_markers_and_lap_bracket():
    rows = {("episode", 4, 10): {"start": at(0), "lap": 10, "end": at(90), "pieces": (25, 30, 35)}}
    assert _timing_bound(rows, "episode", 4, at(43), (0., 1200., 3000.), 5000.)[1] == (1200., 3000.)
    assert _timing_bound(rows, "episode", 4, at(43), None, 5000.)[1] is None
    assert _timing_bound(rows, "episode", 4, at(-1), (0., 1200., 3000.), 5000.) == (None, None)


def test_pit_in_and_incomplete_laps_have_distinct_timing_populations(archived):
    episodes, _, _ = chronology(archived)
    _, visits = route_events(archived, episodes)
    lap = {"driver_number": 1, "lap_number": 99, "date_start": "2025-05-18T13:42:45+00:00",
           "lap_duration": 90., "duration_sector_1": 25., "duration_sector_2": 30.,
           "duration_sector_3": 35., "is_pit_out_lap": False}
    sources = AuditSources([], {}, {}, {}, {"vsc_timing": [
        lap, lap | {"lap_number": 100, "lap_duration": None, "duration_sector_3": None},
    ]})
    records, _, _ = timing_observations(sources, episodes, archived, visits, .12)
    assert {r["population"] for r in records if r["lap"] == 99} == {"PIT_AFFECTED"}
    assert {r["population"] for r in records if r["lap"] == 100} == {"INCOMPLETE_OR_UNRESOLVED"}
    assert all(not r["receipt_time_known"] for r in records)


def test_fixed_population_and_successive_laps():
    observed = [{"driver": 1, "completed_laps": lap, "utc": at(lap*10)} for lap in (1, 2, 4)]
    a = {(1, 1): at(13), (1, 2): at(25), (1, 4): at(49)}
    b = {(1, 1): at(11), (1, 2): at(21)}
    rows, stats = compare_lines(observed, {"A": a, "B": b})
    assert stats["full"]["A"]["n"] == 3 and stats["common"]["A"]["n"] == stats["common"]["B"]["n"] == 2
    assert stats["common"]["A"]["median_abs_s"] == 4
    assert next(r for r in rows if r["variant"] == "B" and r["lap"] == 4)["simulated_utc"] is None
    laps, _ = relative_errors({(1, r["completed_laps"]): r["utc"] for r in observed}, a)
    assert [r["error_s"] for r in laps] == [2.]
    assert error_stats([])["mae_s"] is None
    assert clock(None) is None
