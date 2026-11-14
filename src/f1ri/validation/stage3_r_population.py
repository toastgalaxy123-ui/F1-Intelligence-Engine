"""Rebuild the historical clean-air population in an isolated warehouse."""
import duckdb
import pandas as pd

from f1ri.features.degradation_features import build_degradation_dataset_audit
from f1ri.features.stint_examples import populate_stint_training_examples
from f1ri.validation.quality import assert_valid, check_session
from f1ri.validation.splits import build_split_manifest
from f1ri.validation.stage3_r_sources import census, stage_cached_input
from f1ri.warehouse.build import build_warehouse


def rebuild(root, cache, plan, cohort):
    sources = census(root, cache, cohort)
    rows = []
    bounds = {}
    warehouse = cache/"data/warehouse/f1ri.duckdb"
    for row in sources:
        session = row["session_key"]
        detail = {"session_key": session, "role": row["split"], "race": row["label"]}
        if row["split"] not in plan["acquisition"]["roles"]:
            detail["status"] = "TRANSFER_NOT_OPENED"
        elif absent := [ep for ep, data in row["endpoints"].items()
                        if data is None or (not data["records"] and ep != "pit")]:
            detail.update(status="MISSING_REQUIRED_INPUT", missing=absent)
        else:
            try:
                for endpoint in row["endpoints"]:
                    stage_cached_input(root, cache, row["year"], session, endpoint)
                build_warehouse(cache, row["year"], session)
                quality = check_session(cache, session)
                detail["quality"] = quality
                assert_valid(quality)
                if quality["clean_green_pace_laps"] == 0:
                    raise RuntimeError("No eligible clean-air laps")
                detail["status"] = "READY"
                if not row["endpoints"]["pit"]["records"]:
                    detail["pit_limitation"] = "EMPTY_SOURCE_USE_EXISTING_STINT_BOUNDARY_AND_OUT_LAP_MASK"
            except (ValueError, RuntimeError, FileNotFoundError, duckdb.Error) as error:
                detail.update(status="QUALITY_FAILED", reason=str(error))
        print(f"{session}: {detail['status']}", flush=True)
        rows.append(detail)
    ready = [r["session_key"] for r in rows if r["status"] == "READY"]
    if not ready:
        return sources, rows, build_split_manifest(cohort, bounds), pd.DataFrame(), pd.DataFrame(), {}
    with duckdb.connect(str(warehouse)) as db:
        db.execute("SET TimeZone='UTC'")
        mark = ", ".join("?" for _ in ready)
        data = db.execute(f"""SELECT session_key, date_start, date_end, year, circuit_short_name
                              FROM silver.sessions WHERE session_key IN ({mark})""", ready).fetchall()
        for session, start, end, year, circuit in data:
            if start is None or end is None or end <= start:
                raise ValueError(f"Invalid historical session clocks for {session}")
            enrolled = next(r for r in sources if r["session_key"] == session)
            if enrolled["year"] != year:
                raise ValueError(f"Year mismatch for {session}")
            bounds[session] = start, end
            next(row for row in rows if row["session_key"] == session).update(circuit=circuit, start=start, end=end)
        examples = []
        for session in ready:
            populate_stint_training_examples(db, session_key=session, horizons=tuple(plan["validation"]["horizons"]))
            examples.append(db.execute("""SELECT * FROM gold.stint_training_examples WHERE session_key=?
                ORDER BY driver_number, cutoff_lap_number, scenario_type, requested_horizon""", [session]).df())
        population = db.execute(f"""SELECT * FROM gold.stint_lap_observations WHERE session_key IN ({mark})
                                 ORDER BY session_key, driver_number, lap_number""", ready).df()
        audit = build_degradation_dataset_audit(db)
    return (sources, rows, build_split_manifest(cohort, bounds),
            pd.concat(examples, ignore_index=True), population, audit)


def ready_splits(plan, splits, rebuild_rows):
    ready = {r["session_key"] for r in rebuild_rows if r["status"] == "READY"}
    groups = {
        "train": [s for s in splits.train_keys if s in ready],
        "calibration": [s for s in splits.val_keys if s in ready],
        "test": [s for s in splits.test_keys if s in ready],
        "test_historical_uninspected": [s for s in splits.untouched_test_keys if s in ready],
    }
    spec = plan["validation"]
    minima = {"train": spec["minimum_train_races"], "calibration": spec["minimum_calibration_races"],
              "test": spec["minimum_test_races"], "test_historical_uninspected": spec["minimum_untouched_test_races"]}
    gaps = {k: {"available": len(groups[k]), "required": n} for k, n in minima.items() if len(groups[k]) < n}
    return groups, gaps


def validate_examples(frame):
    key = ["session_key", "driver_number", "cutoff_lap_number", "scenario_type", "requested_horizon"]
    if frame.duplicated(key).any():
        raise ValueError("Duplicate continuation or replacement forecast target")
    if not (frame.planned_start_lap > frame.cutoff_lap_number).all():
        raise ValueError("Forecast start leaks prior or target laps")
    if frame.forecast_as_of_utc.isna().any() or (frame.tyre_age_at_forecast_start < 0).any():
        raise ValueError("Invalid feature timestamp or tyre age")
    if frame.temperature_observation_age_s.dropna().lt(0).any():
        raise ValueError("Future weather found in modeling features")
    return True
