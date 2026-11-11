"""Predictive uncertainty estimation and empirical interval calibration."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from f1ri.models.contracts import Z_50, Z_90, PaceDistribution, PaceRequest


def _bucket_for_stint_laps(clean_stint_laps: int) -> str:
    if clean_stint_laps <= 0:
        return "0_COLD"
    if clean_stint_laps <= 2:
        return "1_2_EARLY"
    return "3_PLUS_WARM"


@dataclass
class ResidualIntervalCalibrator:
    """Calibrates predictive dispersion and 50%/90% intervals from out-of-sample residuals."""

    min_std_seconds: float = 0.18
    global_scale_: float = 0.35
    bucket_scales_: dict[str, float] = field(default_factory=dict)
    q05_mult_: float = -Z_90
    q25_mult_: float = -Z_50
    q50_mult_: float = 0.0
    q75_mult_: float = Z_50
    q95_mult_: float = Z_90
    is_fitted_: bool = False

    def fit(self, calib_df: pd.DataFrame, predictions: np.ndarray) -> ResidualIntervalCalibrator:
        """Fit scale and standardized quantiles on eligible clean-green validation/OOF rows."""
        mask = (
            calib_df["target_is_clean_green"].fillna(False).astype(bool)
            & calib_df["observed_target_lap_time_s"].notna()
        ).to_numpy(dtype=bool)
        if mask.sum() < 10:
            self.global_scale_ = max(0.35, self.min_std_seconds)
            self.bucket_scales_ = {
                "0_COLD": 0.70,
                "1_2_EARLY": 0.40,
                "3_PLUS_WARM": 0.30,
            }
            self.is_fitted_ = True
            return self

        sub = calib_df.loc[mask].copy()
        y_true = sub["observed_target_lap_time_s"].to_numpy(dtype=float)
        y_pred = np.asarray(predictions, dtype=float)[mask]
        residuals = y_true - y_pred

        med_r = float(np.median(residuals))
        mad_r = float(np.median(np.abs(residuals - med_r)))
        self.global_scale_ = max(float(1.4826 * mad_r), self.min_std_seconds)

        sub["bucket"] = [
            _bucket_for_stint_laps(int(n or 0))
            for n in sub["clean_laps_observed_current_stint"]
        ]
        self.bucket_scales_ = {}
        for b_name in ("0_COLD", "1_2_EARLY", "3_PLUS_WARM"):
            b_res = residuals[sub["bucket"].to_numpy() == b_name]
            if len(b_res) >= 8:
                b_mad = float(np.median(np.abs(b_res - np.median(b_res))))
                raw_scale = max(float(1.4826 * b_mad), self.min_std_seconds)
                w = len(b_res) / (len(b_res) + 15.0)
                self.bucket_scales_[b_name] = float(
                    w * raw_scale + (1.0 - w) * self.global_scale_
                )
            else:
                mult = 1.65 if b_name == "0_COLD" else (1.12 if b_name == "1_2_EARLY" else 0.92)
                self.bucket_scales_[b_name] = max(self.global_scale_ * mult, self.min_std_seconds)

        scales_per_row = np.array(
            [self.bucket_scales_[b] for b in sub["bucket"]], dtype=float
        )
        z = residuals / np.maximum(scales_per_row, 1e-6)
        q05, q25, q50, q75, q95 = np.quantile(z, [0.05, 0.25, 0.50, 0.75, 0.95])
        # Shrink empirical standardized quantiles slightly toward Gaussian to avoid tail overfitting
        n_eff = len(z)
        w_q = n_eff / (n_eff + 40.0)
        self.q05_mult_ = float(min(w_q * q05 + (1.0 - w_q) * (-Z_90), -0.80))
        self.q25_mult_ = float(min(w_q * q25 + (1.0 - w_q) * (-Z_50), -0.25))
        self.q50_mult_ = float(np.clip(w_q * q50, -0.10, 0.10))
        self.q75_mult_ = float(max(w_q * q75 + (1.0 - w_q) * Z_50, 0.25))
        self.q95_mult_ = float(max(w_q * q95 + (1.0 - w_q) * Z_90, 0.80))
        self.is_fitted_ = True
        return self

    def make_distribution(
        self,
        expected_seconds: float,
        request: PaceRequest,
        model_version: str,
        trained_through_utc: datetime,
        extra_scale_factor: float = 1.0,
    ) -> PaceDistribution:
        bucket = _bucket_for_stint_laps(request.clean_laps_observed_current_stint)
        base_scale = self.bucket_scales_.get(bucket, self.global_scale_)

        # Inflate dispersion modestly for very old tires or multi-step horizons
        age_excess = max(0, int(request.tyre_age_at_lap_start) - 22)
        age_factor = 1.0 + 0.012 * min(age_excess, 20)
        horizon_factor = 1.0 + 0.06 * max(0, int(request.horizon) - 1)
        std = max(
            float(base_scale * age_factor * horizon_factor * extra_scale_factor),
            self.min_std_seconds,
        )

        med = float(expected_seconds + self.q50_mult_ * std)
        p25 = float(expected_seconds + min(self.q25_mult_, self.q50_mult_ - 0.15) * std)
        p75 = float(expected_seconds + max(self.q75_mult_, self.q50_mult_ + 0.15) * std)
        p05 = float(min(expected_seconds + self.q05_mult_ * std, p25 - 0.10 * std))
        p95 = float(max(expected_seconds + self.q95_mult_ * std, p75 + 0.10 * std))

        if request.clean_laps_observed_race == 0:
            conf = "COLD_START"
        elif request.clean_laps_observed_current_stint < 2:
            conf = "LOW"
        elif request.clean_laps_observed_current_stint >= 3 and age_excess <= 5:
            conf = "HIGH"
        else:
            conf = "NORMAL"

        return PaceDistribution(
            expected_seconds=float(expected_seconds),
            std_seconds=std,
            model_version=model_version,
            trained_through_utc=trained_through_utc,
            median_seconds=med,
            p25_seconds=p25,
            p75_seconds=p75,
            p05_seconds=p05,
            p95_seconds=p95,
            confidence_level=conf,
            forecast_as_of_utc=request.as_of_utc,
        )
