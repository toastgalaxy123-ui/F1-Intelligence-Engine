"""Group contiguous quality-screened physical exposures for pilot inspection."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from itertools import groupby


def candidate_episodes(
    observations: Iterable[dict], *, maximum_gap_upper_m: float, minimum_duration_s: float,
) -> Iterator[dict]:
    """Return descriptive episodes on the half-second sampling grid."""
    if maximum_gap_upper_m <= 0 or minimum_duration_s <= 0:
        raise ValueError("Exposure thresholds must be positive")

    def stream_key(row):
        return row["window"], row["mode"], row["driver_number"]

    def summarize(rows):
        if not rows:
            return None
        start, end = rows[0], rows[-1]
        duration = (end["as_of_utc"] - start["as_of_utc"]).total_seconds()
        if duration < minimum_duration_s:
            return None
        return {key: start[key] for key in (
            "window", "mode", "driver_number", "ahead", "encounter", "layout_version", "session_key",
        )} | {
            "start_utc": start["as_of_utc"], "end_utc": end["as_of_utc"],
            "duration_s": duration, "samples": len(rows),
            "minimum_gap_lower_m": min(row["gap_lower_m"] for row in rows),
            "maximum_gap_upper_m": max(row["gap_upper_m"] for row in rows),
            "label_status": "DESCRIPTIVE_PILOT_ONLY",
        }

    ordered = sorted(observations, key=lambda row: (*stream_key(row), row["as_of_utc"]))
    for _, stream in groupby(ordered, key=stream_key):
        active = []
        for row in stream:
            eligible = (
                row["coverage"] == "COMPLETE" and row["track_state"] == "GREEN"
                and row["encounter"] in {"SAME_LAP", "LAPPING", "BEING_LAPPED"}
                and row["gap_upper_m"] <= maximum_gap_upper_m
            )
            if active and (not eligible or row["ahead"] != active[-1]["ahead"]
                           or row["encounter"] != active[-1]["encounter"]
                           or (row["as_of_utc"] - active[-1]["as_of_utc"]).total_seconds() != 0.5):
                if episode := summarize(active):
                    yield episode
                active = []
            if eligible:
                active.append(row)
        if episode := summarize(active):
            yield episode
