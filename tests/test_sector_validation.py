from datetime import UTC, datetime, timedelta

from f1ri.validation.sector_validation_audit import calibration_census, control_census

START = datetime(2025, 6, 29, tzinfo=UTC)


def sector(driver, serial=0, tier="A"):
    return {
        "opportunity_id": f"9955/{driver}/{serial}", "session_key": 9955, "driver_number": driver,
        "stint_number": 1, "checkpoint": 0, "as_of_utc": START, "target_end_utc": START + timedelta(seconds=20),
        "available_by_utc": START + timedelta(seconds=30), "tyre_age": 12, "compound": "HARD",
        "race_progress": .35, "track_temperature_c": 35., "timing_pass": True, "following_label": False, "tier": tier,
    }


def residual(row, **fields):
    return row | {
        "model_id": "cal", "test_fold": 0, "latest_training_availability_utc": START - timedelta(seconds=5),
        "signed_residual_s": .12, "raw_scale_s": .20,
    } | fields


def test_calibration_rejects_overlap_and_future_and_counts_independent_stints():
    target = sector(1) | {"as_of_utc": START + timedelta(seconds=90)}
    candidates = [sector(driver) for driver in range(2, 11)] + [sector(2, 2), sector(11), sector(12)]
    rows = [residual(r) for r in candidates]
    rows[-2]["available_by_utc"] = target["as_of_utc"]
    rows[-1]["test_fold"] = 1
    snapshots = {"test": {"training_ids": [candidates[0]["opportunity_id"]]}, "cal": {"training_ids": []}}
    evidence = {r["opportunity_id"]: r for r in candidates}
    forecast = {"model_id": "test", "raw_scale_s": .2}
    _, result = calibration_census(target, forecast, rows, snapshots, evidence, {1: 0}, {"minimum_independent_stints": 8})
    assert result["independent_prior_stints"] == 9
    assert result["reasons"]["TRAIN_CALIBRATION_OVERLAP"] == 1
    assert result["reasons"]["FUTURE_RESIDUAL_UNAVAILABLE"] == 1
    assert result["reasons"]["DIFFERENT_CALIBRATION_FOLD"] == 1
    assert set(result["intervals"]) == {"U0", "U1", "U2"}
    _, unsupported = calibration_census(target, forecast, rows[:8], snapshots, evidence, {1: 0},
                                        {"minimum_independent_stints": 8})
    assert unsupported["intervals"] == {}
    assert unsupported["status"] == "INSUFFICIENT_PRIOR_CALIBRATION_STINTS"


def test_h1_control_census_keeps_three_distinct_stints_and_separates_recoverable_tier_b():
    target = sector(1) | {"as_of_utc": START + timedelta(seconds=90)}
    approved = [sector(2), sector(2, 2), sector(3)]
    other = [sector(4, tier="B"), sector(5) | {"checkpoint": 1},
             sector(6) | {"available_by_utc": target["as_of_utc"]},
             sector(7) | {"session_key": 9928}, target]
    folds = {1: 0, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1, 7: 1}
    settings = {"folds": 4, "minimum_matched_controls": 3, "maximum_tyre_age_delta": 5,
                "maximum_progress_delta": .20, "maximum_track_temperature_delta_c": 5}
    summary, audit = control_census(target, approved + other, [residual(r) for r in approved], settings, 0, folds)
    assert summary["counts"]["independent_matched_stints"] == 2
    assert summary["recoverable_stint_upper_bound"] == 3
    assert summary["potential_additional_stints"] == 1
    assert summary["first_decisive_failure"] == "independent_matched_stints"
    reasons = {r["first_exclusion"] for r in audit}
    assert {"repeated_control_stint", "evidence_before_cutoff", "approved_race", "same_sector", "distinct_stint"} <= reasons
