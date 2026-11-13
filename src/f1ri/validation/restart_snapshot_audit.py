"""Audit cutoff-visible Imola localization and compare declared-field replays."""
from datetime import timedelta
from statistics import median

import duckdb

from f1ri.simulator.replay import replay_session
from f1ri.validation.neutralization_study import _compare_archive
from f1ri.validation.overtake_historical_study import _load_json
from f1ri.validation.traffic_history_study import _sha


def snapshot_audit(root, warehouse, folder, evidence):
    session = evidence["session"]
    replay = replay_session(warehouse, session, 63, annotations_root=root) if warehouse else None
    race_start = replay.run.final_state.race_start_utc if replay else None
    interval = evidence["interval"]
    clear = interval["end_utc"]
    announcement = next(r["date"] for r in evidence["observed_messages"] if r["message"] == "SAFETY CAR IN THIS LAP")
    targets = {"deployment": interval["start_utc"], "announcement": announcement,
               "withdrawal_proxy": clear, "global_clear": clear}
    for driver in sorted({r["driver"] for r in evidence["observed_lines"]}):
        line = next((r["utc"] for r in evidence["observed_lines"] if r["driver"] == driver and r["utc"] >= clear), None)
        if line:
            targets[f"observed_next_line_{driver}"] = line
    positions = folder / "physical_positions.parquet"
    manifest = {"localization_sha256": _sha(positions), "geometry_sha256": _sha(folder / "geometry.json"),
                "acquisition_manifest_sha256": _sha(folder / "acquisition.json")}
    geometry = _load_json(folder / "geometry.json")
    rows = []
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'")
        for label, cutoff in targets.items():
            frames = [f for f in replay.run.trace if race_start + timedelta(microseconds=f.event.at_us) <= cutoff
                      ] if replay else []
            last = frames[-1].state if frames else None
            result = db.execute(
                """SELECT * FROM read_parquet(?) WHERE mode='POINT_IN_TIME' AND as_of_utc <= ?
                   AND as_of_utc >= ? QUALIFY row_number() OVER (
                   PARTITION BY driver_number ORDER BY as_of_utc DESC, "window") = 1""",
                [str(positions), cutoff, cutoff-timedelta(seconds=.9)],
            )
            names = [c[0] for c in result.description]
            observed = {r["driver_number"]: r for raw in result.fetchall() if (r := dict(zip(names, raw, strict=True)))}
            roster = {r["driver"] for r in evidence["observed_lines"]}
            for d in last.timing_order if last else sorted(roster):
                car = next(c for c in last.cars if c.driver_number == d) if last else None
                if car is not None and car.status.value != "ACTIVE":
                    continue
                fix = observed.get(d, {})
                stamp = fix.get("observation_utc") or fix.get("observed_at_utc")
                progress = fix.get("progress_m")
                age = (cutoff-stamp).total_seconds() if stamp else None
                supported = ((car is None or car.route.value == "ON_TRACK") and fix.get("route") == "RACING"
                             and progress is not None and fix.get("lap_aligned") and age is not None and 0 <= age <= .9)
                next_line = next((r["utc"] for r in evidence["observed_lines"]
                                  if r["driver"] == d and r["utc"] >= cutoff), None)
                rows.append({"target": label, "cutoff_utc": cutoff, "driver": d,
                             "route": car.route if car else fix.get("route"),
                             "roster_source": "TIMING_REPLAY" if car else "FROZEN_WINDOW_DRIVER_SET",
                             "completed_laps": car.completed_laps if car else fix.get("completed_laps"),
                             "laps_to_timing_leader": last.leader_completed_laps-car.completed_laps if car else None,
                             "last_committed_timing_us": car.last_crossing_us if car else None,
                             "next_recorded_line_utc": next_line, "supported": bool(supported),
                             "unwrapped_progress_m": fix["completed_laps"]*geometry["length_m"]+progress
                             if supported else None,
                             "sample_utc": stamp, "age_s": age, "uncertainty_m": fix.get("uncertainty_m"),
                             "reason": "CUTOFF_POSITION_SUPPORTED" if supported else
                             fix.get("reason") or "NO_ELIGIBLE_CUTOFF_PHYSICAL_POSITION"})
    summaries = {label: {"active": sum(r["target"] == label for r in rows),
                         "supported": sum(r["target"] == label and r["supported"] for r in rows)}
                 for label in targets}
    for info in summaries.values():
        info["completeness"] = info["supported"]/info["active"] if info["active"] else None
    return rows, summaries, manifest


def historical_comparison(replays, evidence, frozen):
    out = {}
    for name, result in replays.items():
        old = frozen[name]
        new = _compare_archive(result, evidence[name])
        old_lines = {(r["driver"], r["completed_laps"]): r for r in old["compared_lines"]}
        matched = [r for r in new["compared_lines"] if (r["driver"], r["completed_laps"]) in old_lines]
        out[name] = {
            "frozen": old, "stabilized": new,
            "matched_line_count": len(matched),
            "matched_median_abs_before_s": median(
                abs(old_lines[r["driver"], r["completed_laps"]]["error_s"]) for r in matched
            ) if matched else None,
            "matched_median_abs_after_s": median(abs(r["error_s"]) for r in matched) if matched else None,
            "held_fixed": ["declared initial physical spacing", "archived lap counts", "clean pace",
                           "SC/VSC pace limits", "collection policy before clear", "route and pit evidence policy"],
        }
    return out
