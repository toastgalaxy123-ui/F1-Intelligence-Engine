"""Read the warehouse's independent timing and race-context evidence for S4-B."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import timedelta
from itertools import pairwise

import duckdb
import pandas as pd

from f1ri.validation.proximity_pilot import _asof, _driver_rows
from f1ri.validation.traffic_acquisition import race_inputs
from f1ri.validation.traffic_short_horizon import sector_boundaries


class RaceHistory:
    def __init__(self, warehouse, key, off_track):
        inputs = race_inputs(warehouse, key)
        self.inputs = inputs
        self.track = tuple(inputs["track"].itertuples())
        self.track_starts = tuple(r.start_utc for r in self.track)
        self.positions = _driver_rows(inputs["positions"], "date")
        self.laps = _driver_rows(inputs["laps"], "lap_start_utc")
        self.pits = defaultdict(list)
        for r in inputs["pits"].itertuples():
            duration = float(r.lane_duration) if pd.notna(r.lane_duration) and r.lane_duration > 0 else 0
            self.pits[int(r.driver_number)].append(
                (r.date - timedelta(seconds=duration + 2), r.date + timedelta(seconds=2), r.date))
        self.retirements = off_track
        with duckdb.connect(str(warehouse), read_only=True) as db:
            db.execute("SET TimeZone = 'UTC'")
            tables = ("sessions", "intervals", "race_control", "stints", "weather")
            data = {name: db.execute(f"SELECT * FROM silver.{name} WHERE session_key = ?", [key]).df()
                    for name in tables}
        self.metadata = data["sessions"].iloc[0].to_dict()
        self.availability = {k: len(v) for k, v in (inputs | data).items()}
        self.intervals = _driver_rows(data["intervals"].sort_values("date"), "date")
        self.weather = tuple(data["weather"].sort_values("date").itertuples())
        self.weather_times = tuple(r.date for r in self.weather)
        self.control = tuple(data["race_control"].sort_values("date").itertuples())
        self.markers = defaultdict(list)
        self.clean_history = defaultdict(list)
        for d, (_, laps) in self.laps.items():
            for lap in laps:
                boundaries = sector_boundaries(lap.lap_start_utc, (lap.sector_1_s, lap.sector_2_s))
                self.markers[d].extend((at, i, int(lap.lap_number)) for i, at in enumerate(boundaries))
            self.markers[d].sort()
            for lap, following in pairwise(laps):
                if following.lap_number != lap.lap_number + 1 or not (
                    pd.notna(lap.observed_lap_time_s) and lap.observed_lap_time_s > 0
                ):
                    continue
                start = lap.lap_start_utc
                end = max(start + timedelta(seconds=lap.observed_lap_time_s), following.lap_start_utc)
                # A prior lap screen must not depend on a later rival lap's
                # pit or traffic tags. Use timestamped interval/rank checks.
                if (self.interruptions((d,), start, end, pit_receipt_cutoff=end)
                        or not all(self._clear_interval(d, at) for at in (start, end))):
                    continue
                self.clean_history[d].append((end, float(lap.observed_lap_time_s)))

    def _clear_interval(self, d, at):
        rank = _asof(self.positions, d, at)
        interval = _asof(self.intervals, d, at)
        if rank and rank.position == 1:
            return True
        if not interval or (at - interval.date).total_seconds() > 10:
            return False
        try:
            return float(interval.interval_raw) >= 2.5
        except (ValueError, TypeError):
            return False

    def green_interval(self, at):
        i = bisect_right(self.track_starts, at) - 1
        if i >= 0:
            row = self.track[i]
            if at < row.end_utc and row.status == "GREEN" and row.confidence == "HIGH":
                return row
        return None

    def green_at(self, at):
        return self.green_interval(at) is not None

    def interruptions(self, drivers, start, end, *, pit_receipt_cutoff=None):
        items = []
        track = self.green_interval(start)
        if track is None or track.end_utc <= end:
            items.append((track.end_utc if track else start, "CENSORED_TRACK_STATE"))
        for d in drivers:
            for lo, hi, observed in self.pits[d]:
                if lo <= end and hi >= start and (pit_receipt_cutoff is None or observed <= pit_receipt_cutoff):
                    items.append((max(start, lo), "CENSORED_PIT"))
            if d in self.retirements and self.retirements[d] <= end:
                items.append((max(start, self.retirements[d]), "CENSORED_RETIREMENT"))
        return sorted(set(items))

    def active_at(self, drivers, at):
        return all(d not in self.retirements or self.retirements[d] > at for d in drivers)

    def checkpoint(self, d, at, segment, limit):
        wanted = segment["sector"] % 3
        bound = at + timedelta(seconds=limit)
        timings = self.markers[d]
        start = bisect_right(timings, (at, 3))
        end = bisect_right(timings, (bound, 3))
        return next((stamp for stamp, marker, _ in timings[start:end] if marker == wanted), None)

    def cutoff_context(self, d, target, at, lap_counts):
        def tyre(driver, completed_laps):
            lap = _asof(self.laps, driver, at)
            if not lap or lap.lap_number != completed_laps + 1 or pd.isna(lap.compound):
                return None
            return {"compound": lap.compound, "source_utc": lap.lap_start_utc,
                    "stint": int(lap.stint_number) if pd.notna(lap.stint_number) else None,
                    "age_laps": int(lap.tyre_age_at_lap_start) if pd.notna(lap.tyre_age_at_lap_start) else None,
                    "availability": "RETROSPECTIVE_STINT_METADATA_RECEIPT_UNKNOWN"}

        def recent(driver):
            rows = [(date, pace) for date, pace in self.clean_history[driver] if date <= at][-3:]
            return {"lap_times_s": [p for _, p in rows], "latest_lap_completion_utc": rows[-1][0]} if rows else None

        weather_index = bisect_right(self.weather_times, at) - 1
        weather = self.weather[weather_index] if weather_index >= 0 else None
        return {
            "recent_clean_laps": [recent(d), recent(target)],
            "weather": {"source_utc": weather.date, "rainfall": int(weather.rainfall)
                        if pd.notna(weather.rainfall) else None, "track_temperature_c": float(weather.track_temperature)
                        if pd.notna(weather.track_temperature) else None} if weather else None,
            "retrospective_tyre_context": [tyre(d, lap_counts[0]), tyre(target, lap_counts[1])],
        }

    def event_evidence(self, event):
        lo, hi = event["event_time_lower_utc"], event["event_time_upper_utc"]
        drivers = event["attacker"], event["target"]
        evidence = {}
        for d in drivers:
            positions = _asof(self.positions, d, lo), self._next_position(d, hi)
            timings = self.markers[d]
            i = bisect_left(timings, (lo,))
            j = bisect_right(timings, (hi, 3))
            if j < len(timings):
                j += 1
            interval = _asof(self.intervals, d, lo)
            evidence[d] = {
                "position_before": {"at": positions[0].date, "rank": int(positions[0].position)}
                if positions[0] and pd.notna(positions[0].position) else None,
                "position_after": {"at": positions[1].date, "rank": int(positions[1].position)}
                if positions[1] and pd.notna(positions[1].position) else None,
                "timing_markers": [{"at": at, "marker": marker, "lap": lap} for at, marker, lap in
                                   timings[max(0, i-1):j]],
                "interval_at_start": {"at": interval.date, "interval": interval.interval_raw,
                                      "gap_to_leader": interval.gap_to_leader_raw} if interval else None,
                "pit_observations": [observed for start, end, observed in self.pits[d] if start <= hi and end >= lo],
            }
        a, b = (evidence[d] for d in drivers)
        ranks = [a["position_before"], b["position_before"], a["position_after"], b["position_after"]]
        fresh = all(r and min(abs((r["at"] - t).total_seconds()) for t in (lo, hi)) <= 30 for r in ranks)
        flip = fresh and ranks[0]["rank"] > ranks[1]["rank"] and ranks[2]["rank"] < ranks[3]["rank"]
        timing_orders = []
        for aa in a["timing_markers"]:
            for bb in b["timing_markers"]:
                if aa["marker"] == bb["marker"] and aa["lap"] == bb["lap"]:
                    timing_orders.append({"marker": aa["marker"], "lap": aa["lap"],
                                          "attacker_utc": aa["at"], "target_utc": bb["at"],
                                          "attacker_first": aa["at"] < bb["at"]})
        same_lap = event["lap_advantage"] == 0
        compatible_after = any(r["attacker_first"] and min(r["attacker_utc"], r["target_utc"]) >= hi
                               for r in timing_orders)
        disagree_after = any(not r["attacker_first"] and min(r["attacker_utc"], r["target_utc"]) >= hi
                             for r in timing_orders)
        ahead_index = 0 if event["lap_advantage"] > 0 else 1
        ranks_consistent = fresh and all(ranks[i + ahead_index]["rank"] < ranks[i + 1 - ahead_index]["rank"]
                                        for i in (0, 2))
        retained_lap_relation = event["lap_advantage"] != 0 and ranks_consistent
        control = [{"at": r.date, "flag": r.flag, "scope": r.scope, "message": r.message}
                   for r in self.control if lo - timedelta(seconds=5) <= r.date <= hi + timedelta(seconds=5)]
        return {"drivers": evidence, "timing_pair_order": timing_orders, "race_control_nearby": control,
                "position_agreement": "SAME_LAP_RANK_REVERSAL" if same_lap and flip else
                "LAPPED_RANK_ORDER_CONSISTENT" if retained_lap_relation else
                "NO_CLEAR_INDEPENDENT_RANK_REVERSAL" if fresh else "INDEPENDENT_RANK_EVIDENCE_SPARSE",
                "nearest_shared_marker_support": "SUPPORTS_ATTACKER_FIRST" if compatible_after else
                "TARGET_FIRST" if disagree_after else "UNAVAILABLE",
                "corroborated": bool(same_lap and flip and compatible_after and not disagree_after)}

    def _next_position(self, driver, at):
        times, rows = self.positions.get(driver, ((), ()))
        i = bisect_right(times, at)
        return rows[i] if i < len(rows) else None
