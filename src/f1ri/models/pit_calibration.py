"""Validation-only physical-variation calibration with frozen reference ownership."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

import numpy as np

from f1ri.models.contracts import PitLossDistribution, PitLossRequest, PitSharedScenario
from f1ri.models.pit_loss import PitLossModelV1
from f1ri.models.pit_uncertainty import summarize_samples


def calibrated_pit_distribution(
    forecast: PitLossDistribution, scale: float, *, retain_samples: bool,
    calibrated_through_utc: datetime | None = None,
) -> PitLossDistribution:
    """Shrink or widen stop variability, preserving the -reference draw exactly.

    This is a statistical residual adjustment, not a physical route measurement.
    Service scenarios still shift total loss one-for-one; service measurements
    and the shared reference sign convention are not rescaled or double counted.
    """
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("Pit calibration scale must be positive and finite")
    if forecast.sampled_losses_s is None or forecast.sampled_reference_errors_s is None:
        raise ValueError("Pit calibration requires retained base samples")
    errors = np.asarray(forecast.sampled_reference_errors_s)
    original = np.asarray(forecast.sampled_losses_s)
    physical = original+errors
    calibrated = physical.mean()+scale*(physical-physical.mean())-errors
    delta = calibrated-original
    route = np.asarray(forecast.sampled_route_losses_s)+delta
    trained = max(forecast.trained_through_utc, calibrated_through_utc or forecast.trained_through_utc)
    return replace(
        forecast, **summarize_samples(calibrated),
        model_version=f"{forecast.model_version}:physical_scale={scale:g}",
        trained_through_utc=trained,
        expected_route_loss_s=float(np.mean(route)),
        assumptions=forecast.assumptions+(f"Validation-only physical variation scale {scale:g}; shared normal-reference error unchanged",),
        sampled_losses_s=tuple(calibrated) if retain_samples else None,
        sampled_route_losses_s=tuple(route) if retain_samples else None,
        sampled_lane_durations_s=forecast.sampled_lane_durations_s if retain_samples else None,
        sampled_stationary_durations_s=forecast.sampled_stationary_durations_s if retain_samples else None,
        sampled_transition_losses_s=forecast.sampled_transition_losses_s if retain_samples else None,
        sampled_reference_errors_s=forecast.sampled_reference_errors_s if retain_samples else None,
    )


@dataclass
class CalibratedPitLossModel:
    base_model: PitLossModelV1
    scale: float = 1.0
    calibrated_through_utc: datetime | None = None

    def predict_pit_loss(
        self, request: PitLossRequest, shared_scenario: PitSharedScenario | None = None,
    ) -> PitLossDistribution:
        if self.calibrated_through_utc is not None and self.calibrated_through_utc > request.as_of_utc:
            raise ValueError("Pit calibration uses outcomes after this forecast cutoff")
        forecast = self.base_model.predict_pit_loss(replace(request, retain_samples=True), shared_scenario)
        return calibrated_pit_distribution(
            forecast, self.scale, retain_samples=request.retain_samples,
            calibrated_through_utc=self.calibrated_through_utc,
        )
