"""Freeze traffic window selection and acquire immutable historical telemetry."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from f1ri.ingestion.openf1 import OpenF1Client
from f1ri.ingestion.telemetry import TelemetryWindow, load_telemetry_window


@dataclass(frozen=True, slots=True)
class TrafficWindow:
    kind: str
    telemetry: TelemetryWindow
    seed_driver: int | None = None
    seed_lap: int | None = None

    @property
    def name(self) -> str:
        return f"{self.kind}_{self.telemetry.start_utc:%H%M%S}"


def race_inputs(warehouse: Path, key: int) -> dict[str, pd.DataFrame]:
    queries = {
        "laps": """SELECT * FROM gold.lap_context WHERE session_key = ?
                   AND lap_start_utc IS NOT NULL ORDER BY lap_start_utc, driver_number""",
        "positions": """SELECT driver_number, date, position FROM silver.position
                        WHERE session_key = ? AND date IS NOT NULL ORDER BY date, driver_number, position""",
        "pits": """SELECT driver_number, date, lane_duration FROM silver.pit
                   WHERE session_key = ? AND date IS NOT NULL ORDER BY date""",
        "track": """SELECT start_utc, end_utc, status, confidence FROM gold.track_status_intervals
                    WHERE session_key = ? ORDER BY start_utc""",
        "drivers": """SELECT DISTINCT driver_number FROM silver.drivers WHERE session_key = ?
                      ORDER BY driver_number""",
        "passes": """SELECT driver_number, lap_number, date_start FROM silver.laps WHERE session_key = ?
                     AND date_start IS NOT NULL ORDER BY date_start, driver_number""",
    }
    with duckdb.connect(str(warehouse), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        return {name: con.execute(sql, [key]).df() for name, sql in queries.items()}


def geometry_window(race: dict, laps: pd.DataFrame) -> dict:
    driver, lap = race["geometry_driver"], race["geometry_lap"]
    own = laps[laps.driver_number == driver].set_index("lap_number")
    if not own.loc[lap, "is_clean_green_pace_lap"]:
        raise ValueError(f"Reference lap is not screened clean green for session {race['session_key']}")
    return {"driver": driver, "lap": lap,
            "start": (own.loc[lap, "lap_start_utc"] - timedelta(seconds=2)).isoformat(),
            "end": (own.loc[lap + 1, "lap_start_utc"] + timedelta(seconds=2)).isoformat()}


def select_traffic_windows(
    key: int, laps: pd.DataFrame, track: pd.DataFrame, geometry_available: pd.Timestamp,
    sampling: dict, seed: int,
) -> tuple[TrafficWindow, ...]:
    """Seed two strata from Stage 1 and sample remaining green starts uniformly by second."""
    width = timedelta(seconds=sampling["window_seconds"])
    pad = timedelta(seconds=sampling["pad_before_lap_seconds"])
    intervals = [(max(row.start_utc, geometry_available).ceil("s"), row.end_utc.floor("s"))
                 for row in track.itertuples() if row.status == "GREEN" and row.confidence == "HIGH"]
    green = {start for begin, end in intervals for start in pd.date_range(begin, end - width, freq="s")}
    rng = np.random.default_rng(np.random.SeedSequence([seed, key]))
    seeded = {"close_seed": {}, "clean_seed": {}}
    for row in laps.itertuples():
        start = row.lap_start_utc.floor("s") - pad
        gap = row.track_gap_ahead_at_start_s
        if (start not in green or pd.isna(gap) or pd.isna(row.observed_lap_time_s)
                or row.lap_start_utc + timedelta(seconds=row.observed_lap_time_s) >= start + width
                or row.is_pit_lap or row.track_status != "GREEN" or row.rainfall != 0):
            continue
        seed_lap = (int(row.driver_number), int(row.lap_number))
        if 0 < gap <= sampling["screen_close_gap_s"]:
            seeded["close_seed"].setdefault(start, seed_lap)
        if gap >= sampling["screen_clean_gap_s"] and row.is_clean_green_pace_lap:
            seeded["clean_seed"].setdefault(start, seed_lap)
    selected = []
    for kind, count in sampling["windows_per_race"].items():
        candidates = sorted(green if kind == "random_green" else seeded[kind])
        accepted = 0
        for index in rng.permutation(len(candidates)):
            start = candidates[index]
            end = start + width
            if any(start < other.telemetry.end_utc and end > other.telemetry.start_utc
                   for other in selected):
                continue
            driver, lap = seeded.get(kind, {}).get(start, (None, None))
            selected.append(TrafficWindow(kind, TelemetryWindow(key, None, start, end), driver, lap))
            accepted += 1
            if accepted == count:
                break
    return tuple(sorted(selected, key=lambda window: window.telemetry.start_utc))


def acquire_window(
    root: Path, window: TelemetryWindow, endpoints: list[str], drivers: tuple[int, ...],
    client: OpenF1Client | None,
) -> tuple[dict[str, pd.DataFrame], list[dict]]:
    frames, summaries = {}, []
    for endpoint in endpoints:
        frame, info = load_telemetry_window(root, window, endpoint, client=client)
        unexpected = set(frame.driver_number) - set(drivers)
        if unexpected:
            raise ValueError(f"Unexpected drivers in {endpoint} for {window.session_key}: {sorted(unexpected)}")
        rows = []
        for driver in drivers:
            dates = frame.loc[frame.driver_number == driver, "date"]
            boundaries = pd.concat((pd.Series([window.start_utc]), dates,
                                    pd.Series([window.end_utc])), ignore_index=True)
            rows.append({"driver_number": driver, "samples": len(dates),
                         "first_utc": dates.iloc[0].isoformat() if len(dates) else None,
                         "last_utc": dates.iloc[-1].isoformat() if len(dates) else None,
                         "longest_gap_s": float(boundaries.diff().dt.total_seconds().max())})
        frames[endpoint] = frame
        summaries.append(info | {"drivers": rows})
    return frames, summaries
