"""Race-aware direct-component and uncertain-label metrics on common events."""
from __future__ import annotations

import numpy as np
import pandas as pd


def pit_metrics(frame: pd.DataFrame, target: str = "target_transition_loss_s",
                prediction: str = "expected_loss_s") -> dict:
    data = frame.dropna(subset=[target, prediction]) if not frame.empty else frame
    if data.empty:
        return {"n_events": 0, "n_races": 0, "status": "NO_EVALUABLE_EVENTS"}
    if data.pit_event_id.duplicated().any():
        raise ValueError("Metrics require one prediction per pit event")
    residual = data[target].to_numpy(float)-data[prediction].to_numpy(float)
    result = {"n_events": int(data.pit_event_id.nunique()), "n_races": int(data.session_key.nunique()),
              "mae_s": float(np.mean(np.abs(residual))), "rmse_s": float(np.sqrt(np.mean(residual**2))),
              "bias_s": float(np.mean(residual)),
              "race_weighted_mae_s": float(pd.Series(np.abs(residual), index=data.index).groupby(data.session_key).mean().mean())}
    if target == "target_transition_loss_s":
        y = data[target]
        for q0, q1, level in ((25, 75, 50), (5, 95, 90)):
            result[f"coverage_{level}"] = float(((y >= data[f"p{q0:02d}_loss_s"])
                                                 & (y <= data[f"p{q1:02d}_loss_s"])).mean())
            result[f"mean_width_{level}_s"] = float((data[f"p{q1:02d}_loss_s"]-data[f"p{q0:02d}_loss_s"]).mean())
        if "crps_s" in data:
            result["crps_s"] = float(data.crps_s.mean())
        if "label_std_s" in data:
            result["median_label_std_s"] = float(data.label_std_s.median())
    return result


def evaluate_pit_predictions(frame: pd.DataFrame) -> dict:
    out = {}
    for (model, split), group in frame.groupby(["model_id", "split"]):
        eligible = group[group.eligible_primary_label.fillna(False)]
        stationary = group.dropna(subset=["target_stop_duration_s", "expected_stationary_duration_s"])
        tail = {}
        if not stationary.empty:
            labels = (stationary.target_stop_duration_s > 4.5).to_numpy(float)
            probs = stationary.delay_probability.to_numpy(float)
            tail = {"n_events": len(labels), "n_races": int(stationary.session_key.nunique()),
                    "observed_slow_rate": float(np.mean(labels)),
                    "predicted_slow_rate": float(np.mean(probs)),
                    "brier_score": float(np.mean((labels-probs)**2))}
        slices = {}
        for column in ("circuit_short_name", "season", "team_id", "pit_event_type", "track_status_at_cutoff",
                       "timing_quality", "support_status", "pit_sequence", "service_category",
                       "pit_lap_bucket", "pit_speed_limit_kph", "historical_support_bucket"):
            if column in eligible:
                slices[column] = {str(k): pit_metrics(v) for k, v in eligible.groupby(column, dropna=False)}
        out.setdefault(str(model), {})[str(split)] = {
            "net_loss_proxy": pit_metrics(eligible),
            "direct_lane": pit_metrics(group[group.eligible_direct_lane.fillna(False)], "target_lane_duration_s", "expected_lane_duration_s"),
            "direct_stationary": pit_metrics(stationary, "target_stop_duration_s", "expected_stationary_duration_s"),
            "slow_stop_calibration": tail, "slices": slices,
            "unsupported_events": int((group.support_status == "UNSUPPORTED").sum()),
        }
    return out


def paired_pit_comparison(frame: pd.DataFrame, candidate: str, baseline: str,
                          *, split: str = "test", n_bootstrap: int = 2000, seed: int = 42) -> dict:
    selected = frame[(frame.split == split) & frame.eligible_primary_label.fillna(False)]
    cols = ["pit_event_id", "session_key", "target_transition_loss_s", "expected_loss_s"]
    a = selected[selected.model_id == candidate][cols]
    b = selected[selected.model_id == baseline][cols]
    joined = a.merge(b, on=["pit_event_id", "session_key"], suffixes=("_a", "_b"), validate="one_to_one")
    if joined.empty:
        return {"n_events": 0, "n_races": 0, "status": "NO_COMMON_EVENTS"}
    if not np.allclose(joined.target_transition_loss_s_a, joined.target_transition_loss_s_b, equal_nan=True):
        raise ValueError("Paired models do not share identical targets")
    delta = np.abs(joined.target_transition_loss_s_a-joined.expected_loss_s_a)-np.abs(joined.target_transition_loss_s_b-joined.expected_loss_s_b)
    races = pd.Series(delta.to_numpy(), index=joined.index).groupby(joined.session_key).mean().to_numpy()
    rng = np.random.default_rng(seed)
    draws = rng.choice(races, (n_bootstrap, len(races)), replace=True).mean(axis=1)
    return {"n_events": len(joined), "n_races": len(races),
            "race_mean_mae_delta_s": float(races.mean()),
            "p05_delta_s": float(np.quantile(draws, .05)), "p95_delta_s": float(np.quantile(draws, .95)),
            "races_improved": int((races < 0).sum()), "resampling_unit": "race"}
