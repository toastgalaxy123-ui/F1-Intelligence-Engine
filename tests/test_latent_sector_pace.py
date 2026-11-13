from datetime import UTC, datetime, timedelta

import pytest

from f1ri.features.clean_sector_evidence import classify_sector
from f1ri.models.latent_sector_pace import fit_sector, interval_from_prefix, sector_prediction
from f1ri.validation.sector_reference_backtest import assign_folds, replay_sector_references

START = datetime(2025, 6, 29, tzinfo=UTC)
MODEL = {"minimum_fit_rows": 3, "minimum_fit_stints": 3, "context_penalty": 4, "driver_penalty": 8,
         "adaptation_prior_count": 4, "minimum_noise_s": .04, "maximum_age_extrapolation": 5,
         "maximum_progress_extrapolation": .2, "maximum_temperature_extrapolation_c": 5}


def row(i, driver, elapsed=20., tier="A", stint=1):
    return {"opportunity_id": str(i), "session_key": 9955, "driver_number": driver, "stint_number": stint,
            "tier": tier, "checkpoint": 0, "tyre_age": 10, "race_progress": .3, "compound": "HARD",
            "track_temperature_c": 35., "sector_elapsed_s": elapsed,
            "as_of_utc": START + timedelta(seconds=i), "available_by_utc": START + timedelta(seconds=i+1)}


@pytest.mark.parametrize(("failure", "clean", "physical", "tier"), [
    ("PIT_OR_STINT_TRANSITION", True, {"clean_control": True, "timing_pass": True}, "EXCLUDED"),
    ("NON_GREEN_OR_WET", True, {}, "EXCLUDED"),
    ("OFFICIAL_TIMING_UNRECONCILED", True, {}, "EXCLUDED"),
    (None, True, {"cutoff_group": "CLOSE_SUPPORTED"}, "C"),
    (None, True, {"clean_control": True, "timing_pass": True, "mid_interval_close": True}, "C"),
    (None, False, {"cutoff_group": "PHYSICAL_STATE_UNRESOLVED"}, "C"),
    (None, True, {}, "B"),
    (None, False, {"clean_control": True, "timing_pass": True}, "A"),
])
def test_clean_sector_tiers(failure, clean, physical, tier):
    assert classify_sector(failure, clean, physical)[0] == tier


def test_latent_adaptation_uses_only_trusted_earlier_sectors_and_shrinks():
    pool = [row(i, i) for i in range(3)]
    model = fit_sector(pool, MODEL)
    assert model == fit_sector(pool, MODEL)
    target = row(20, 63)
    cold, _ = sector_prediction(model, target, [], MODEL)
    earlier = row(10, 63, 21.)
    warm, _ = sector_prediction(model, target, [earlier, row(11, 63, 80, "C"),
                                              row(12, 63, 80, "B"), row(30, 63, 80), target], MODEL)
    assert warm["prior_count"] == 1
    assert warm["adaptation_s"] == pytest.approx(.2)
    assert warm["adapted_scale_s"] < cold["adapted_scale_s"]
    assert cold["adapted_s"] == pytest.approx(20.)
    assert sector_prediction(model, target | {"tyre_age": 30}, [], MODEL)[0] is None
    with pytest.raises(ValueError, match="earlier training"):
        sector_prediction(model, pool[0], [], MODEL)


def test_calibration_is_chronological_and_counts_distinct_stints():
    cutoff = row(20, 63)["as_of_utc"]
    calibration = [row(i, d) | {"signed_residual_s": e, "raw_scale_s": .2}
                   for i, d, e in ((1, 1, .1), (2, 1, .2), (3, 2, .3), (25, 3, 99))]
    assert interval_from_prefix(calibration, cutoff, .2, 3) == ([None, None], 2)
    widths, n = interval_from_prefix(calibration, cutoff, .2, 2)
    assert n == 2 and widths == pytest.approx([.3, .3])


def test_fold_assignment_is_stable_and_cross_race_pool_is_rejected():
    assert assign_folds([2, 1, 3], 4, 4) == assign_folds([3, 2, 1], 4, 4)
    with pytest.raises(ValueError, match="single permitted race"):
        replay_sector_references([row(1, 1), row(2, 2) | {"session_key": 9928}], {}, {})
