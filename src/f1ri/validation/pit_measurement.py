"""Retrospective counterfactual measurements, distinct from pre-stop forecasts.

NormalLapReference is a Stage 4 nuisance estimator, NOT a Pace V2 upgrade. It
learns tire/progress effects from strictly earlier races and anchors the car's
pace on clean laps already observed before the stop. Poor references are retained
with uncertainty and support flags rather than disguised as exact timing.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from f1ri.features.pit_events import integer, number, utc

COMPOUNDS = ("HARD", "MEDIUM", "SOFT")


def _design(frame: pd.DataFrame) -> np.ndarray:
    age = pd.to_numeric(frame.tyre_age_at_lap_start, errors="coerce").fillna(0).to_numpy(float)
    lap = pd.to_numeric(frame.lap_number, errors="coerce").to_numpy(float)
    columns = [lap / 60]
    for compound in COMPOUNDS:
        mask = (frame.compound == compound).to_numpy(float)
        columns.extend((mask, mask*age))
    return np.column_stack(columns)


def clean_reference_laps(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    return frame[frame.is_clean_green_pace_lap.fillna(False)
                 & frame.compound.isin(COMPOUNDS)
                 & (frame.observed_lap_time_s > 0)].copy()


@dataclass
class NormalLapReference:
    version: str = "past-race-centered-ridge-reference-v1"
    model: Ridge = field(default_factory=lambda: Ridge(alpha=20, fit_intercept=False))
    training_sessions: tuple[int, ...] = ()
    trained_through_utc: datetime = field(default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC))
    reference_std_s: float = 1.0
    fitted: bool = False

    def fit(self, past_laps: pd.DataFrame, before_utc) -> NormalLapReference:
        self.__dict__.update(NormalLapReference().__dict__)
        clean = clean_reference_laps(past_laps)
        if clean.empty:
            return self
        end = pd.to_datetime(clean.lap_end_estimate_utc, utc=True)
        clean = clean[end < utc(before_utc)].copy()
        if len(clean) < 20:
            return self
        x = _design(clean)
        y = clean.observed_lap_time_s.to_numpy(float)
        keys = [clean.session_key, clean.driver_number]
        centered_x = x - pd.DataFrame(x, index=clean.index).groupby(keys).transform("mean").to_numpy()
        centered_y = y - clean.groupby(["session_key", "driver_number"]).observed_lap_time_s.transform("mean").to_numpy()
        self.model.fit(centered_x, centered_y)
        residual = centered_y-self.model.predict(centered_x)
        # This residual includes unexplained tire/pace drift, not just standard
        # error of a fitted mean; do not shrink uncertainty to zero with lap count.
        self.reference_std_s = max(float(np.quantile(np.abs(residual), .68)), .4)
        self.training_sessions = tuple(sorted(map(int, clean.session_key.unique())))
        self.trained_through_utc = pd.to_datetime(clean.lap_end_estimate_utc, utc=True).max().to_pydatetime()
        self.fitted = True
        return self

    def predict_pair(self, event: dict, current_laps: pd.DataFrame) -> dict:
        cutoff = utc(event.get("transition_start_utc"))
        if cutoff is None or not self.fitted:
            raise ValueError("No earlier-race reference fit")
        if int(event["session_key"]) in self.training_sessions or self.trained_through_utc >= cutoff:
            raise ValueError("Reference model contains the target race or future data")
        clean = clean_reference_laps(current_laps)
        clean = clean[(clean.driver_number == event["driver_number"])
                      & (pd.to_datetime(clean.lap_end_estimate_utc, utc=True) <= cutoff)
                      & (clean.lap_number < event["resolved_in_lap"])].sort_values("lap_number")
        recent = clean.tail(5)
        if len(recent) < 3:
            raise ValueError("Too few pre-stop clean laps for the car-specific reference")
        old_age = integer(event.get("old_tyre_age"))
        new_age = integer(event.get("new_tyre_initial_age"))
        if old_age is None or new_age is None:
            raise ValueError("Missing tire state")
        planned = pd.DataFrame([
            {"compound": event["old_compound"], "tyre_age_at_lap_start": old_age-1,
             "lap_number": event["resolved_in_lap"]},
            {"compound": event["new_compound"], "tyre_age_at_lap_start": new_age,
             "lap_number": event["resolved_out_lap"]},
        ])
        offset = np.median(recent.observed_lap_time_s.to_numpy(float)-self.model.predict(_design(recent)))
        estimates = offset + self.model.predict(_design(planned))
        # Positive correlation between the two reference laps is deliberate.
        # Label std is NOT divided by sqrt(number of historical laps).
        age_gap = max(int(event["resolved_in_lap"])-int(recent.lap_number.max()), 1)
        std = 2*self.reference_std_s*np.sqrt(1 + age_gap/10)
        return {"normal_in_reference_s": float(estimates[0]),
                "normal_out_reference_s": float(estimates[1]),
                "reference_error_std_s": float(std),
                "reference_support": "LIMITED_SUPPORT" if age_gap > 5 else "SUPPORTED",
                "reference_forecast_cutoff_utc": cutoff,
                "reference_training_cutoff_utc": self.trained_through_utc,
                "reference_training_sessions_json": json.dumps(self.training_sessions)}


def matched_control_estimate(event: dict, laps: pd.DataFrame) -> tuple[float | None, int]:
    """Difference-in-differences using uninterrupted simultaneous control cars.

    This supporting measurement intentionally does not call the ridge reference.
    Different tire choices still confound the counterfactual; preserve method
    disagreement instead of averaging away that limitation.
    """
    clean = clean_reference_laps(laps)
    start = utc(event.get("transition_start_utc"))
    if start is None:
        return None, 0
    target_past = clean[(clean.driver_number == event["driver_number"])
                        & (pd.to_datetime(clean.lap_end_estimate_utc, utc=True) <= start)].sort_values("lap_number").tail(5)
    if len(target_past) < 3:
        return None, 0
    references = []
    for driver, group in clean.groupby("driver_number"):
        if int(driver) == event["driver_number"]:
            continue
        group = group.sort_values("lap_number")
        pairs = group[group.lap_number.isin([event["resolved_in_lap"], event["resolved_out_lap"]])]
        if len(pairs) != 2:
            continue
        pair_start = utc(pairs.iloc[0].lap_start_utc)
        if pair_start is None or abs((pair_start-start).total_seconds()) > 45:
            continue
        prior = group[pd.to_datetime(group.lap_end_estimate_utc, utc=True) <= start].tail(5)
        if len(prior) < 3:
            continue
        adjustment = float(target_past.observed_lap_time_s.median()-prior.observed_lap_time_s.median())
        references.append(float(pairs.observed_lap_time_s.sum())+2*adjustment)
    if len(references) < 2:
        return None, len(references)
    observed = number(event.get("observed_in_lap_s")), number(event.get("observed_out_lap_s"))
    if None in observed:
        return None, len(references)
    return float(sum(observed)-np.median(references)), len(references)


def measure_pit_transitions(
    events: pd.DataFrame, current_laps: pd.DataFrame, reference: NormalLapReference,
) -> pd.DataFrame:
    rows = []
    for event in events.to_dict("records"):
        flags: list[str] = []
        base = {"pit_event_id": event["pit_event_id"], "session_key": event["session_key"],
                "measurement_method": "TWO_LAP_CROSS_FIT", "reference_model_version": reference.version,
                "reference_training_cutoff_utc": reference.trained_through_utc,
                "reference_forecast_cutoff_utc": utc(event.get("transition_start_utc")),
                "observation_available_utc": utc(event.get("transition_end_utc")),
                "reference_training_sessions_json": json.dumps(reference.training_sessions),
                "normal_in_reference_s": None, "normal_out_reference_s": None,
                "transition_loss_s": None, "reference_error_std_s": None, "label_std_s": None,
                "label_weight": 0.0, "matched_control_loss_s": None,
                "method_disagreement_s": None, "n_matched_controls": 0,
                "reference_support": "UNSUPPORTED", "eligible_primary_label": False}
        if event["transition_track_status"] != "GREEN":
            flags.append("GREEN_REFERENCE_INVALID_FOR_NON_GREEN_TRANSITION")
        elif event["timing_quality"] != "B":
            flags.append("AMBIGUOUS_EVENT_TIMING")
        else:
            try:
                pred = reference.predict_pair(event, current_laps)
                base.update(pred)
                observed = number(event["observed_in_lap_s"]), number(event["observed_out_lap_s"])
                if None in observed:
                    raise ValueError("Missing observed transition lap time")
                loss = float(sum(observed)-pred["normal_in_reference_s"]-pred["normal_out_reference_s"])
                std = float(np.hypot(pred["reference_error_std_s"], .5))
                base.update(transition_loss_s=loss, label_std_s=std, label_weight=1/(std**2),
                            eligible_primary_label=bool(event["eligible_primary_label"]))
                control, n = matched_control_estimate(event, current_laps)
                base.update(matched_control_loss_s=control, n_matched_controls=n,
                            method_disagreement_s=loss-control if control is not None else None)
                if control is not None and abs(loss-control) > 3*std:
                    flags.append("MEASUREMENT_METHOD_DISAGREEMENT")
                    # Preserve the target, increase its uncertainty; do not reject
                    # it solely because a competing estimator produced a big error.
                    base["label_std_s"] = float(np.hypot(std, (loss-control)/2))
                    base["label_weight"] = 1/base["label_std_s"]**2
            except ValueError as exc:
                flags.append(str(exc))
        base["input_sha256"] = hashlib.sha256(json.dumps({
            "event": event, "pre_stop_laps": current_laps[
                (current_laps.driver_number == event["driver_number"])
                & (current_laps.lap_number < (integer(event.get("resolved_in_lap")) or 0))
            ].to_dict("records"), "reference_sessions": reference.training_sessions,
        }, sort_keys=True, default=str).encode()).hexdigest()
        base["measurement_flags_json"] = json.dumps(flags)
        rows.append(base)
        if base["matched_control_loss_s"] is not None:
            rows.append({**base, "measurement_method": "MATCHED_NON_PITTING_CONTROLS",
                         "transition_loss_s": base["matched_control_loss_s"],
                         "reference_model_version": "pre-stop-pace-adjusted-controls-v1",
                         "eligible_primary_label": False})
    return pd.DataFrame(rows)


def measure_anchored_route(
    *, pit_route_elapsed_s: float, on_track_elapsed_s: float,
    boundary_std_s: float, anchors_verified: bool, source_sha256: str,
) -> dict:
    """Method C requires independently verified equal-distance entry/exit anchors."""
    if not anchors_verified or not source_sha256:
        raise ValueError("Approximate GPS alone does not establish comparable route anchors")
    if min(pit_route_elapsed_s, on_track_elapsed_s) <= 0 or boundary_std_s <= 0:
        raise ValueError("Invalid route timing or uncertainty")
    return {"measurement_method": "VERIFIED_ROUTE_ANCHORS",
            "transition_loss_s": pit_route_elapsed_s-on_track_elapsed_s,
            "label_std_s": float(2*boundary_std_s), "source_sha256": source_sha256,
            "includes_pit_out_warmup": False}


def audit_stationary_trace(car_data: pd.DataFrame, *, event_utc, lane_duration_s: float,
                           recorded_stationary_s: float | None) -> dict:
    """Resolution-limited cross-check, never a replacement FIA timing loop."""
    if car_data.empty:
        return {"status": "MISSING_CAR_DATA"}
    samples = car_data.sort_values("date").copy()
    dates = pd.to_datetime(samples.date, utc=True, format="ISO8601")
    origin = utc(event_utc)
    relative = (dates-origin).dt.total_seconds().to_numpy()
    speed = pd.to_numeric(samples.speed, errors="coerce").to_numpy(float)
    gaps = dates.diff().dt.total_seconds().dropna().to_numpy()
    resolution = float(np.median(gaps)) if len(gaps) else None
    if resolution is None or resolution <= 0:
        return {"status": "INSUFFICIENT_TIME_RESOLUTION"}
    # The source pit record timestamps a timing-feed report. A completed stop
    # can precede it. This backwards window is approximate, not a legal loop.
    low = (speed <= 1) & (relative >= -lane_duration_s) & (relative <= 0)
    runs = []
    start = None
    for idx in range(len(low)+1):
        on = idx < len(low) and low[idx]
        if on and start is None:
            start = idx
        if start is not None and not on:
            end = idx-1
            lower = relative[end]-relative[start]
            before_gap = relative[start]-relative[start-1] if start > 0 else resolution
            after_gap = relative[idx]-relative[end] if idx < len(relative) else resolution
            # Zero-speed samples separated by a dropped sample are not two
            # separate tire services. Preserve the span, expose internal gaps,
            # and bound its edges by adjacent nonstationary observations.
            runs.append((lower+(before_gap+after_gap)/2, lower,
                         lower+before_gap+after_gap, before_gap+after_gap,
                         float(np.max(np.diff(relative[start:end+1]))) if end > start else 0.0))
            start = None
    best = max(runs) if runs else None
    stationary = float(best[0]) if best else None
    return {"status": "APPROXIMATE_SENSOR_CHECK", "sample_spacing_s": resolution,
            "stationary_trace_estimate_s": stationary,
            "stationary_lower_bound_s": float(best[1]) if best else None,
            "stationary_upper_bound_s": float(best[2]) if best else None,
            "largest_internal_sample_gap_s": float(best[4]) if best else None,
            "boundary_resolution_s": float(best[3]) if best else None,
            "recorded_stationary_s": recorded_stationary_s,
            "difference_s": stationary-recorded_stationary_s
            if stationary is not None and recorded_stationary_s is not None else None,
            "notes": "Assumes one uninterrupted service between adjacent low-speed samples; quantized speed and feed timing, not legal route anchors"}
