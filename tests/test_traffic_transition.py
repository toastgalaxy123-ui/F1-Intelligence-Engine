from datetime import UTC, datetime, timedelta

import pytest

from f1ri.validation.proximity_pilot import _parquet
from f1ri.validation.traffic_transition import (
    _prior_interval,
    _visible_physical,
    anchor_conversion,
    approach_signal,
)

START = datetime(2025, 6, 29, tzinfo=UTC)


@pytest.mark.parametrize(("current", "previous", "expected"), [
    (1.5, 2.4, "COARSE_INTERVAL_APPROACH"),
    (1.1, 1.5, "NO_USABLE_APPROACH"),
    (None, 2.4, "NO_USABLE_APPROACH"),
])
def test_timing_interval_only_prioritizes_a_closing_candidate(current, previous, expected):
    assert approach_signal(None, None, None, None, current, previous) == expected


def test_physical_approach_requires_same_leader_and_distinguishes_existing_following():
    now = {"gap_upper_m": 110, "ahead": 16}
    earlier = {"gap_upper_m": 160, "ahead": 16}
    assert approach_signal(now, earlier, 110, 160, None, None) == "PHYSICAL_APPROACH"
    assert approach_signal(now, earlier | {"ahead": 5}, 110, 160, 1., 2.5) == "ALREADY_PHYSICALLY_CLOSE"


def test_cutoff_ignores_future_samples_and_partial_fields():
    clocks = [START, START + timedelta(seconds=1), START + timedelta(seconds=2)]
    rows = [{"coverage": "COMPLETE", "gap_upper_m": 100},
            {"coverage": "SUBSET", "gap_upper_m": 40},
            {"coverage": "COMPLETE", "gap_upper_m": 20}]
    assert _visible_physical(rows, clocks, START + timedelta(seconds=.9)) is rows[0]
    assert _visible_physical(rows, clocks, START + timedelta(seconds=1.9)) is None
    assert _prior_interval(clocks, [3., 2., 1.], START + timedelta(seconds=13)) == (clocks[1], 2.)
    assert _prior_interval(clocks, [3., 2., 1.], START + timedelta(seconds=40)) == (None, None)


def test_anchor_with_full_lap_coverage_still_requires_cutoff_support(tmp_path):
    folder = tmp_path / "9955"
    folder.mkdir()
    identity = "9955/18/40"
    _parquet(folder / "lap_cutoff_cohort.parquet", [{
        "opportunity_id": identity, "cutoff_exposure": "UNSUPPORTED", "opponent_number": 16,
    }])
    _parquet(folder / "lap_eligibility_ledger.parquet", [{
        "opportunity_id": identity, "full_lap": True, "physical_coverage": .96,
        "screen": "PHYSICAL_SUPPORT_BELOW_MINIMUM",
    }])
    locked = {"races": {"9955": {"windows": [{"arm": "transition", "anchor": {
        "opportunity_id": identity, "cutoff_utc": START.isoformat(), "approach": "PHYSICAL_APPROACH",
        "history_status": "QUALIFIED", "d8_status": "SUPPORTED", "physical_neighbor": 16,
    }}]}}}
    row, = anchor_conversion([{"session_key": 9955}], locked, tmp_path,
                             {"label_minimum_coverage": .9, "minimum_matched_controls": 5})
    assert row["stages"]["full_target_lap"]
    assert not row["stages"]["physical_support"]
    assert row["matched_control_count"] is None
    assert row["first_decisive_outcome"] == "PHYSICAL_SUPPORT_BELOW_MINIMUM"
