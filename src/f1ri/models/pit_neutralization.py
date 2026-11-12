"""Conditional neutralization estimates; never simulate Safety Car positions."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from f1ri.models.contracts import PitLossRequest


@dataclass
class PitNeutralizationModel:
    minimum_races: int = 2
    minimum_controls: int = 5
    corrections_: dict[tuple[str, str], tuple[float, float, int, int]] = field(default_factory=dict)

    def fit(self, controls: pd.DataFrame) -> PitNeutralizationModel:
        """Fit ONLY independently reconstructed route-equivalent on-track extras.

        A whole-lap SC slowdown cannot be substituted for the pit-bypassed segment.
        Inputs require comparable verified boundaries and observation availability.
        When unavailable, neutralized predictions are explicitly unsupported.
        """
        self.corrections_ = {}
        if controls.empty:
            return self
        required = {"circuit_short_name", "track_status", "on_track_extra_s", "session_key",
                    "anchors_verified", "observation_available_utc"}
        if not required.issubset(controls):
            raise ValueError("Neutralization controls need verified route-equivalent observations")
        valid = controls[controls.anchors_verified.fillna(False)]
        for (circuit, state), group in valid.groupby(["circuit_short_name", "track_status"]):
            values = group.on_track_extra_s.dropna().to_numpy(float)
            if len(values):
                self.corrections_[(str(circuit), str(state))] = (
                    float(np.median(values)), max(float(np.std(values)), .5),
                    len(values), int(group.session_key.nunique()),
                )
        return self

    def adjustment(self, request: PitLossRequest) -> tuple[float, float, str, tuple[str, ...]]:
        if request.track_status == "GREEN" and not request.future_status_scenario:
            return 0.0, 0.0, "SUPPORTED", ()
        if request.track_status in {"RED", "UNKNOWN"}:
            return 0.0, 10.0, "UNSUPPORTED", ("Unknown/red-flag routing requires a legality check",)
        segments = request.future_status_scenario
        if request.track_status == "MIXED" and not segments:
            return 0.0, 10.0, "UNSUPPORTED", ("Mixed state requires an explicit future timeline",)
        if segments:
            # Segment weights must cover a declared comparable route window.
            # These are scenario assumptions, never the historical future timeline.
            duration = sum(s.end_s-s.start_s for s in segments)
            weights = {state: sum(s.end_s-s.start_s for s in segments if s.status == state)/duration
                       for state in ("GREEN", "VSC", "SC")}
        else:
            weights = {request.track_status: 1.0}
        mean, variance, support, assumptions = 0.0, 0.0, "SUPPORTED", []
        for state, weight in weights.items():
            if state == "GREEN" or not weight:
                continue
            fit = self.corrections_.get((request.circuit_short_name, state))
            if fit is None or fit[2] < self.minimum_controls or fit[3] < self.minimum_races:
                support = "UNSUPPORTED"
                variance += (10*weight)**2
                assumptions.append(f"{state}: insufficient route-matched controls; green-centered wide fallback")
            else:
                mean -= weight*fit[0]
                variance += (weight*fit[1])**2
            if state == "SC":
                support = "UNSUPPORTED"
                assumptions.append("SC elapsed-time scenario only; queue and relative field loss unmodeled")
        if request.scenario_mode == "INFORMATION_LIMITED":
            assumptions.append("Assumes current track state persists; future neutralization timing is not predicted")
        return mean, float(np.sqrt(variance)), support, tuple(assumptions)
